"""The whole gate against stand-in nodes on local ports and a stand-in for bench-private: a sound run passes, and each
kind of divergence, gap or silence fails the part that names it."""
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bench_determinism import run_gate

TOP = 250
TOOL = r'''
import json, sys
command = sys.argv[1]
if command == "corpus-effects":
    data = json.loads(open(sys.argv[2]).read())
    print(json.dumps({"genesis": data["genesis"]}))
    for entry in data["corpus"]:
        print(json.dumps(entry))
elif command == "verify-cuts":
    cuts = []
    for height in range(int(sys.argv[3]), int(sys.argv[4]) + 1):
        print(json.dumps({"get": f"/v1/finality/cuts/{height}"}), flush=True)
        answer = json.loads(sys.stdin.readline())
        if "proof" not in answer:
            print(json.dumps({"error": "no proof", "height": height, "cuts": cuts}))
            sys.exit(0)
        cuts.append(json.loads(bytes.fromhex(answer["proof"]).decode()))
    print(json.dumps({"cuts": cuts}))
'''
CORPUS = {
    "genesis": [["g", 0, "alice", "1000"], ["g", 1, "bob", "1000"]],
    "corpus": [{"txid": "split", "inputs": [["g", 0]], "outputs": [["alice", "450"], ["alice", "450"]]},
               {"txid": "pay", "inputs": [["split", 0]], "outputs": [["bob", "100"], ["alice", "250"]]}],
}


def supply(height, minted):
    return {"finalized_height": height, "max_supply": "1000000000", "minted": str(minted), "burned": "0",
            "total_supply": str(minted), "circulating_supply": str(minted - 400), "bonded": "300", "unbonding": "0",
            "private_supply": "40", "work_reserve": "30", "stake_reserve": "30", "unscheduled_emission": "0"}


def emission(height):
    return {"finalized_height": height, "cut_subsidy": "10", "cut_work_subsidy": "6", "cut_stake_subsidy": "4",
            "cuts_until_reduction": str(10 ** 6 - height), "reduction_interval_cuts": str(10 ** 6),
            "next_interval_subsidy": "9970000"}


# From height 1 to the top every cut mints 4 and, with a window of 4, the anchors 0 to TOP - 4 close with 6 each.
MINTED = 1000 + (TOP - 1) * 4 + (TOP - 4 + 1) * 6


class Node:
    """A node's answers: its chain, the outcomes of the payments and the balances of the payers."""

    def __init__(self):
        self.chain = {height: [height, f"cut{height}", f"root{height}", "set"] for height in range(1, TOP + 1)}
        self.statuses = {"split": (3, "cut3"), "pay": (4, "cut4")}
        self.balances = {"alice": ("700", 2), "bob": ("1100", 2)}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, path, body):
        url = urllib.parse.urlparse(path)
        query = dict(urllib.parse.parse_qsl(url.query))
        top = max(self.chain)
        if url.path == "/v1/chain/head":
            return {"height": top}
        if url.path == "/v1/cuts":
            before, limit = int(query["before"]), int(query["limit"])
            heights = [height for height in range(before - 1, max(0, before - limit - 1), -1) if height in self.chain]
            return {"cuts": [{"cut": dict(zip(("height", "cut_id", "state_root", "validator_set_id"),
                                              self.chain[height])), "rewards": "7", "stake_rewards": "3"}
                             for height in heights]}
        if url.path.startswith("/v1/finality/cuts/"):
            height = int(url.path.rsplit("/", 1)[1])
            if height not in self.chain:
                return None
            return {"height": height, "proof": json.dumps(self.chain[height]).encode().hex()}
        if url.path == "/v1/transactions/status":
            known = [txid for txid in body["transaction_ids"] if txid in self.statuses]
            return {"statuses": [{"transaction_id": txid, "lifecycle": "finalized", "failure": None,
                                  "finalized_cut": {"height": self.statuses[txid][0],
                                                    "cut_id": self.statuses[txid][1]}} for txid in known],
                    "unknown": [txid for txid in body["transaction_ids"] if txid not in self.statuses]}
        if url.path == "/v1/address/balances":
            return {"indexed_height": top, "balances": [{"address": address, "balance": self.balances[address][0],
                                                         "utxos": self.balances[address][1]}
                                                        for address in body["addresses"]]}
        if url.path == "/v1/economics/supply":
            return supply(top, MINTED)
        if url.path == "/v1/chain/issuance":
            return {"minted_atoms": str(MINTED), "burned_atoms": "0", "unminted_work_carry_atoms": "0"}
        if url.path == "/v1/economics/emission":
            return emission(top)
        if url.path == "/v1/staking/validators":
            return {"finalized_height": top, "total_bonded": "300"}
        return None

    def handler(self):
        node = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, body):
                answer = node.answer(self.path, body)
                data = json.dumps(answer if answer is not None else {"error": "not held"}).encode()
                self.send_response(200 if answer is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply(None)

            def do_POST(self):
                self.reply(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

            def log_message(self, *_):
                pass

        return Handler


class GateTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        (self.folder / "tool.py").write_text(TOOL)
        (self.folder / "manifest.json").write_text(json.dumps(CORPUS))
        self.nodes = {"v1": Node(), "v2": Node()}

    def tearDown(self):
        for node in self.nodes.values():
            node.server.shutdown()
            node.server.server_close()

    def gate(self, **changes):
        start = {"supply": {name: supply(1, 1000) for name in self.nodes},
                 "emission": {name: emission(1) for name in self.nodes},
                 "staking": {name: {"finalized_height": 1, "total_bonded": "300"} for name in self.nodes},
                 "issuance": {"height": 1, "minted": 1000, "carried": 0}, "window": 4}
        plan = {"tool": [sys.executable, str(self.folder / "tool.py")], "manifest": self.folder / "manifest.json",
                "files": [], "recorded": {"pay": {"lifecycle": "finalized", "finalized_cut": {"height": 4,
                                                                                            "cut_id": "cut4"}}},
                "start": start, "deposit": 1000, "rested": True, **changes}
        return run_gate({name: node.port for name, node in self.nodes.items()}, plan, lambda *_: None)

    def failing(self, gate):
        return sorted(name for name, value in gate["parts"].items() if not value)

    def test_a_sound_run_passes_every_part(self):
        gate = self.gate()
        self.assertEqual(self.failing(gate), [])
        self.assertTrue(gate["passed"], gate["errors"])
        self.assertEqual(gate["report"]["top"], TOP)
        self.assertEqual(gate["report"]["proofs"]["v2"]["verified"], TOP)
        self.assertEqual(gate["report"]["payments"]["final"], 2)

    def test_another_state_root_at_one_height_fails_the_chain_and_its_proof(self):
        self.nodes["v2"].chain[120][2] = "forked"
        gate = self.gate()
        self.assertEqual(self.failing(gate), ["one_cut_per_height", "proofs_verified"])
        self.assertFalse(gate["passed"])

    def test_a_height_a_validator_lacks_fails(self):
        del self.nodes["v2"].chain[50]
        self.assertIn("every_height_held", self.failing(self.gate()))

    def test_a_joiner_is_compared_from_where_it_holds_but_not_above_where_it_joined(self):
        for height in range(1, 100):
            del self.nodes["v2"].chain[height]
        self.assertEqual(self.failing(self.gate(joined={"v2": 120})), [])
        self.assertEqual(self.failing(self.gate(joined={"v2": 80})), ["joiners_hold_since_joining"])

    def test_a_payment_final_elsewhere_and_a_wrong_balance_fail(self):
        self.nodes["v2"].statuses["pay"] = (5, "cut5")
        self.nodes["v1"].balances["bob"] = ("1099", 2)
        failing = self.failing(self.gate())
        self.assertIn("payments_agree", failing)
        self.assertIn("payer_balances_as_expected", failing)

    def test_a_payment_the_run_saw_final_that_no_node_holds_fails(self):
        for node in self.nodes.values():
            del node.statuses["pay"]
        failing = self.failing(self.gate())
        self.assertIn("payments_not_moved", failing)
        self.assertIn("payer_balances_as_expected", failing)

    def test_a_silent_node_fails_the_gate(self):
        self.nodes["v2"].server.shutdown()
        self.nodes["v2"].server.server_close()
        gate = self.gate()
        self.assertFalse(gate["passed"])
        self.assertIn("nodes_reachable", self.failing(gate))

    def test_a_chain_that_did_not_come_to_rest_fails(self):
        self.assertEqual(self.failing(self.gate(rested=False)), ["chain_came_to_rest"])


if __name__ == "__main__":
    unittest.main()
