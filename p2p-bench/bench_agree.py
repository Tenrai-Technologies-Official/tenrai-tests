"""Whether the validators hold the same chain: every finalized height compared while the run goes on,
and at its end the cut fields, the private roots, the supply and the balances of the bench's own
wallets, each read from every validator. A validator that lags is no divergence; once it holds a
height, that height must match. On a mismatch the answers of every validator are saved.

The final reads compare one state: a run may end with work still on its way, so the chain first comes
to rest (no miners, an empty private pool, balances that stay put), and every comparison then holds
at one height."""

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request


def read(port, path, timeout=15):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as reply:
        return json.loads(reply.read())


def read_or_status(port, path, timeout=30):
    try:
        return read(port, path, timeout)
    except urllib.error.HTTPError as error:
        return {"status": error.code}


def post(port, path, body, timeout=30):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return json.loads(reply.read())


def head(port):
    try:
        return read(port, "/v1/chain/head", 3).get("height")
    except Exception:
        return None


def cut_view(port, height):
    try:
        cut = read(port, f"/v1/cuts/{height}", 10)["cut"]
        return cut["cut_id"], cut["state_root"]
    except Exception:
        return None


def final_sample(port, top, cuts):
    """The transactions of the last `cuts` cuts up to `top` with the height each is final at."""
    held = {}
    for height in range(max(1, top - cuts + 1), top + 1):
        answer = read_or_status(port, f"/v1/cuts/{height}/transactions?limit=64")
        for transaction in answer.get("transactions", []) if isinstance(answer, dict) else []:
            held[transaction["transaction_id"]] = height
    return held


def status_answer(port, transaction):
    """A node's answer on a transaction; a refusal keeps its code and the message the node gave."""
    try:
        return read(port, f"/v1/transactions/{transaction}/status", 30)
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read())
        except ValueError:
            body = None
        return {"status": error.code, "body": body}


def final_answer(port, transaction, tries=20):
    """The height a transaction is final at on this node (or None), the answer that said so, and the
    refusals before it. A busy node is asked again: only an answer counts, not a refusal to give one."""
    refusals = []
    answer = None
    for _ in range(tries):
        answer = status_answer(port, transaction)
        if answer.get("status") in (429, 503):
            refusals.append(answer)
            time.sleep(0.5)
            continue
        cut = answer.get("finalized_cut")
        return (cut["height"] if cut else None), answer, refusals
    return None, answer, refusals


def final_height(port, transaction, tries=20):
    """The height a transaction is final at on this node, or None."""
    return final_answer(port, transaction, tries)[0]


def pooled_statements(port):
    """The private statements a validator's pool holds; None while it does not answer."""
    try:
        return read(port, "/v1/private/status", 3)["pool"]["statements"]
    except Exception:
        return None


class Agreement(threading.Thread):
    """Checks each validator's new heights against the first answer for that height, and samples the
    private pools, whose course over the run shows whether statements pile up."""

    def __init__(self, ports, folder, log):
        super().__init__(daemon=True)
        self.ports = ports
        self.folder = folder
        self.log = log
        self.checked = {name: 0 for name in ports}
        self.reference = {}
        self.mismatches = []
        self.pool_samples = []
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.wait(3):
            self.poll()

    def add(self, name, port):
        """A node that joins during the run; it is compared from the first height on."""
        self.checked[name] = 0
        self.ports[name] = port

    def poll(self):
        pools, heads = {}, {}
        for name, port in list(self.ports.items()):
            pools[name] = pooled_statements(port)
            top = heads[name] = head(port)
            while top is not None and self.checked[name] < top and not self.stopped.is_set():
                height = self.checked[name] + 1
                view = cut_view(port, height)
                if view is None:
                    break
                first, seen = self.reference.setdefault(height, (name, view))
                if view != seen:
                    self.diverged(height, name, first)
                self.checked[name] = height
        self.pool_samples.append({"at": time.time(), "statements": pools, "heads": heads})

    def diverged(self, height, name, first):
        self.log(f"DIVERGENCE at height {height}: {name} differs from {first}")
        target = self.folder / f"height-{height}"
        target.mkdir(parents=True, exist_ok=True)
        views = {}
        for other, port in list(self.ports.items()):
            views[other] = {"cut": read_or_status(port, f"/v1/cuts/{height}"),
                            "proof": read_or_status(port, f"/v1/finality/cuts/{height}")}
        (target / "views.json").write_text(json.dumps(views, indent=2))
        self.mismatches.append({"height": height, "validator": name, "reference": first})

    def finish(self):
        self.stopped.set()
        self.join(timeout=10)
        self.poll()
        return {"heights_compared": max(self.reference, default=0), "checked": self.checked,
                "mismatches": self.mismatches, "pool_samples": self.pool_samples}


def same_height(ports, path, settle_s=90):
    """Each validator's answer to `path` from one moment at which all of them stood at one height."""
    deadline = time.time() + settle_s
    while time.time() < deadline:
        answers = {name: read_or_status(port, path) for name, port in ports.items()}
        heights = {answer.get("height", answer.get("finalized_height")) for answer in answers.values()}
        if len(heights) == 1 and None not in heights:
            return answers
        time.sleep(0.3)
    raise SystemExit(f"{path}: the validators never stood at one height together")


def balances_of(port, wallets):
    """A validator's rows for `wallets` with the one height they were all read at, or None if the height
    moved between two pages."""
    rows, heights = [], set()
    for start in range(0, len(wallets), 64):
        answer = post(port, "/v1/address/balances", {"api_version": 1, "addresses": wallets[start:start + 64]})
        heights.add(answer["indexed_height"])
        rows += [(row["address"], row["balance"], row["utxos"]) for row in answer["balances"]]
    return (heights.pop(), rows) if len(heights) == 1 else None


def balances_at_one_height(ports, wallets, settle_s=90):
    """Every validator's rows for `wallets`, all read at one indexed height."""
    deadline = time.time() + settle_s
    while time.time() < deadline:
        read_rows = {name: balances_of(port, wallets) for name, port in ports.items()}
        heights = {found[0] if found else None for found in read_rows.values()}
        if len(heights) == 1 and None not in heights:
            return heights.pop(), {name: found[1] for name, found in read_rows.items()}
        time.sleep(0.3)
    raise SystemExit("the validators' address indexes never stood at one height together")


def wait_for_rest(ports, wallets, log, budget_s=240, quiet_cuts=3, private=True):
    """Waits until no pool holds a statement and the bench wallets' balances stayed put over `quiet_cuts`
    cuts, with the miners stopped and the private pool checked only where active: what was on its way
    when the run ended has landed. Returns the seconds
    that took, or None if the chain did not come to rest within the budget."""
    started = time.time()
    steady = next(iter(ports.values()))
    last, last_height, quiet = None, None, 0
    while time.time() < started + budget_s:
        pools = [pooled_statements(port) for port in ports.values()] if private else []
        found = balances_of(steady, wallets)
        if found is not None and all(pool == 0 for pool in pools):
            height, rows = found
            if rows != last:
                last, quiet = rows, 0
            elif height != last_height:
                quiet += 1
            last_height = height
            if quiet >= quiet_cuts:
                took = time.time() - started
                log(f"the chain came to rest after {took:.0f} s at height {height}")
                return took
        else:
            quiet = 0
        time.sleep(0.5)
    log(f"the chain did not come to rest within {budget_s} s")
    return None


def scan(tool, manifest, account, port):
    """The account's private notes as the wallet's own check reads them from this validator. A busy node
    is asked again, as a wallet does; what the tool says on failing becomes the error."""
    process = subprocess.Popen([str(tool), "scan", str(manifest), account], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    said = []
    try:
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                said.append(line.strip())
                continue
            if "get" in message:
                answer = read_or_status(port, message["get"], 60)
                for _ in range(40):
                    if answer.get("status") not in (429, 503):
                        break
                    time.sleep(0.5)
                    answer = read_or_status(port, message["get"], 60)
                process.stdin.write(json.dumps(answer) + "\n")
                process.stdin.flush()
                continue
            return message
    finally:
        process.kill()
        process.wait()
    return {"error": " ".join(said) or "the scan ended without an answer"}


def final_checks(ports, tool, manifest, wallets, expected, log, restored=(), unbonding=(0,), transparent=None):
    """What every validator holds once the chain came to rest; `expected` is what the executed statements
    must have moved, `wallets` the bench's transparent addresses. A node in `restored` started from a
    snapshot: it holds no private rows from before it, so its scan has to refuse or agree, never to differ."""
    if transparent is not None:
        from bench_crash_money import final_transparent_checks
        return final_transparent_checks(ports, wallets, transparent["initial"], transparent["payments"],
                                        transparent["replay"], log)
    result = {}
    anchors = same_height(ports, "/v1/private/anchor")
    first = next(iter(anchors.values()))
    result["anchor_height"] = first.get("height")
    result["anchor_equal"] = all(answer == first for answer in anchors.values())
    supplies = same_height(ports, "/v1/economics/supply")
    supply = next(iter(supplies.values()))
    result["supply_equal"] = all(answer == supply for answer in supplies.values())
    result["supply"] = supply
    # Nothing appeared or vanished: what was minted and not burned sits in outputs, stake, private notes
    # and reserves. Stake leaves only where a run names how much may be unbonding, none in most of them.
    leaving = int(supply["unbonding"])
    result["value_accounted"] = leaving in unbonding and int(supply["minted"]) - int(supply["burned"]) == leaving + sum(
        int(supply[key]) for key in ("circulating_supply", "bonded", "private_supply", "work_reserve", "stake_reserve"))
    height = result["anchor_height"]
    fields = {}
    for name, port in ports.items():
        # A proof is too long for a command line, so it goes through a file.
        proof = manifest.parent / f"cut-proof-{name}.hex"
        proof.write_text(read(port, f"/v1/finality/cuts/{height}")["proof"])
        fields[name] = json.loads(subprocess.run([str(tool), "cut-fields", str(proof)], check=True,
                                                 capture_output=True, text=True).stdout)
    result["cut_fields_equal"] = all(value == fields["v1"] for value in fields.values())
    result["cut_fields"] = fields["v1"]
    anchor_file = manifest.parent / "final-anchor.json"
    anchor_file.write_text(json.dumps(first))
    result["private_roots"] = json.loads(subprocess.run([str(tool), "anchor-fields", str(manifest), str(anchor_file)],
                                                        check=True, capture_output=True, text=True).stdout)

    balances_height, balances = balances_at_one_height(ports, wallets)
    result["balances_height"] = balances_height
    result["transparent_balances_equal"] = all(rows == balances["v1"] for rows in balances.values())
    result["transparent_wallets"] = len(wallets)

    scans = {name: {account: scan(tool, manifest, account, port) for account in ("sender", "receiver")}
             for name, port in ports.items() if name not in restored}
    result["private_scans"] = scans
    restored_scans = {name: {account: scan(tool, manifest, account, ports[name]) for account in ("sender", "receiver")}
                      for name in restored}
    result["restored_scans"] = restored_scans
    result["restored_scans_honest"] = all(
        "error" in value or (value.get("balance_atoms"), value.get("notes"))
        == (scans["v1"][account].get("balance_atoms"), scans["v1"][account].get("notes"))
        for found in restored_scans.values() for account, value in found.items())
    reference = scans["v1"]
    result["private_scans_failed"] = sorted(f"{name} {account}" for name, found in scans.items()
                                            for account, value in found.items() if "error" in value)
    result["private_balances_equal"] = not result["private_scans_failed"] and all(
        {account: (value.get("balance_atoms"), value.get("notes")) for account, value in found.items()}
        == {account: (value.get("balance_atoms"), value.get("notes")) for account, value in reference.items()}
        for found in scans.values())
    held = {account: int(reference[account].get("balance_atoms", -1)) for account in ("sender", "receiver")}
    exit_balance = next((int(balance) for address, balance, _ in balances["v1"] if address == wallets[-1]), 0)
    result["expected"] = {key: str(value) for key, value in expected.items()}
    result["held"] = {"sender": str(held["sender"]), "receiver": str(held["receiver"]),
                      "exit_address": str(exit_balance), "private": first.get("confidential_atoms")}
    result["balances_as_expected"] = (held["sender"] == expected["sender"]
                                      and held["receiver"] == expected["receiver"]
                                      and exit_balance == expected["exit_address"]
                                      and int(first.get("confidential_atoms", -1)) == expected["private"]
                                      and held["sender"] + held["receiver"] == expected["private"])
    log("final state:", json.dumps({key: value for key, value in result.items()
                                    if key not in ("private_scans", "restored_scans", "supply", "cut_fields")}))
    return result
