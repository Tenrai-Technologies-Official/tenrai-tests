"""The process-kill test uses a log-inferred window after certified availability and a finality
proposal. Logging is not a durable-head oracle: reopened actual API heads are checked against
their stored canonical cuts, and either whole predecessor or whole successor may survive.

That is the moment the restart stall needed: the certified blocks then lived only in memory. The
test passes only if every validator that voted restores all blocks the certified outcome names,
all four agree on cut IDs and state roots, vote again, finalize five more heights on their own and
wait for no block data at the end. Accepted and finalized payments are counted apart.
"""

import json
import re
import threading
import time
import urllib.request

from collections import Counter
from pathlib import Path

from bench_agree import final_checks
from bench_determinism import gate_start
from bench_crash_money import baseline, recovered_heads, replay_finalized
from bench_private import PrivateLoad
from bench_submit import load, statuses, submit, wait_final

INSTALLED = re.compile(r"cut installed height=(\d+)")
NAMED = re.compile(r"availability proposal accepted view=\d+ proposer=\d+ blocks=(\d+)")
PROPOSAL = re.compile(r"finality proposal (?:signed|accepted) height=(\d+)")
RESTORED = re.compile(r"work kept for availability votes is back in the pool height=(\d+) blocks=(\d+)")
SIGNER = re.compile(r"finality vote received signer=(\d+) phase=\w+ height=(\d+)")
CERTIFIED = re.compile(r"finality certificate installed height=(\d+)")
QUORUM = 3
WAITING = ("waits for local data", "asking peers for work")


def unresolved_waits(lines):
    """A wait for block data counts as resolved once the node installed two more cuts after it."""
    last = max((index for index, line in enumerate(lines) if any(text in line for text in WAITING)), default=None)
    if last is None:
        return False
    return sum(1 for line in lines[last + 1:] if INSTALLED.search(line)) < 2


class LogWatch:
    """Which height each validator installed last, which one it voted available for since, and
    which heights have a finality proposal, read from the logs as they grow."""

    def __init__(self, paths):
        self.paths = paths
        self.offsets = dict.fromkeys(paths, 0)
        self.installed = dict.fromkeys(paths, 0)
        self.voted = dict.fromkeys(paths)
        self.named = dict.fromkeys(paths, 0)
        self.certified = {}
        self.proposed = set()

    def poll(self):
        for name, path in self.paths.items():
            with open(path, "rb") as file:
                file.seek(self.offsets[name])
                chunk = file.read()
            end = chunk.rfind(b"\n") + 1
            self.offsets[name] += end
            for line in chunk[:end].decode(errors="replace").splitlines():
                if match := INSTALLED.search(line):
                    self.installed[name] = int(match.group(1))
                elif match := NAMED.search(line):
                    self.named[name] = int(match.group(1))
                elif "availability certified" in line:
                    self.certified[self.installed[name] + 1] = self.named[name]
                elif "availability vote signed" in line:
                    self.voted[name] = self.installed[name] + 1
                elif match := PROPOSAL.search(line):
                    self.proposed.add(int(match.group(1)))

    def window(self):
        """The open height when a crash now hits it between its certified availability votes and
        its install."""
        height = max(self.installed.values()) + 1
        voters = sorted(name for name, voted in self.voted.items() if voted == height)
        if (len(voters) >= QUORUM and self.certified.get(height, 0) > 0
                and height in self.proposed):
            return height, voters
        return None


def get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as reply:
            return json.loads(reply.read())
    except Exception:
        return None


def after(path, offset):
    with open(path, "rb") as file:
        file.seek(offset)
        return file.read().decode(errors="replace").splitlines()


def crash_test(bench, port, log):
    """Runs one targeted crash under payments and returns the result."""
    scenario = bench.scenario
    nodes = [f"v{index}" for index in bench.nodes]
    paths = {f"v{index}": bench.data(index) / "node.log" for index in bench.nodes}
    payments = load(bench.snapshot() / "tx" / "pay.txt")[:scenario["rate"] * scenario["seconds"]]
    transparent = not bench.private_chain()
    wallets = bench.bench_wallets() if transparent else None
    if transparent:
        manifest = json.loads(Path(bench.manifest).read_text())
        reward_wallets = {value[key] for value in manifest["validators"]
                          for key in ("owner_address", "reward_address")}
        if set(wallets) & reward_wallets:
            raise SystemExit("monetary oracle requires payment wallets separate from validator/miner rewards")
    initial = baseline(bench.ports(), wallets) if transparent else None
    bench.gate_start = gate_start(bench.ports())
    records = []
    payer = threading.Thread(target=lambda: records.extend(
        submit(bench.ports(), payments, scenario["rate"], "payments", log)), daemon=True)
    payer.start()
    statements = None
    if scenario.get("private"):
        frames = sorted((bench.snapshot() / "private" / "frames").glob("*.hex"))
        statements = PrivateLoad(frames, bench.ports(), scenario["private"], port("rpc", bench.steady_validator()), log)
        statements.start()
    time.sleep(scenario["crash_after"])

    watch = LogWatch(paths)
    deadline = time.time() + 60
    while (found := watch.window()) is None:
        if time.time() > deadline:
            raise SystemExit(f"no crash window: installed {watch.installed}, voted {watch.voted}")
        time.sleep(0.02)
        watch.poll()
    height, voters = found
    certified_blocks = watch.certified[height]
    for index in bench.nodes:
        bench.processes[f"v{index}"].kill()
    for index in bench.nodes:
        bench.processes[f"v{index}"].wait()
    crashed = time.time()
    watch.poll()
    log(f"crashed every validator at height {height}: voted by {voters}, installed {watch.installed}")
    if any(installed >= height for installed in watch.installed.values()):
        raise SystemExit(f"a validator installed height {height} before the crash; run again")
    restart_offsets = {name: path.stat().st_size for name, path in paths.items()}

    bench.start_nodes()
    api_s = time.time() - crashed
    first = None
    reopened = {}
    while True:
        heights = {name: get(port("rpc", int(name[1:])), "/v1/chain/head") for name in nodes}
        for name, value in heights.items():
            if value is not None:
                reopened.setdefault(name, value)
        reached = min((value or {}).get("height", 0) for value in heights.values())
        if first is None and reached >= height:
            first = time.time() - crashed
        if reached >= height + 5:
            break
        if time.time() - crashed > 240:
            raise SystemExit(f"stalled after the restart at height {height}: {heights}")
        time.sleep(0.5)
    five_s = time.time() - crashed
    time.sleep(10)
    quiet_since = time.time() - 10

    cuts = {}
    for at in range(height - 1, height + 6):
        seen = {}
        for name in nodes:
            cut = (get(port("rpc", int(name[1:])), f"/v1/cuts/{at}") or {}).get("cut") or {}
            seen[name] = (cut.get("cut_id"), cut.get("state_root"))
        cuts[at] = {"same": len(set(seen.values())) == 1 and (None, None) not in seen.values(),
                    "cut_id": seen[nodes[0]][0], "state_root": seen[nodes[0]][1]}

    per_node = {}
    for name, path in paths.items():
        lines = after(path, restart_offsets[name])
        restored = [(int(m.group(1)), int(m.group(2))) for line in lines if (m := RESTORED.search(line))]
        signers = {int(m.group(1)) for line in lines if (m := SIGNER.search(line)) and int(m.group(2)) >= height}
        certified = sorted({int(m.group(1)) for line in lines if (m := CERTIFIED.search(line))})
        late = [line for line in lines if stamp(line) >= quiet_since]
        per_node[name] = {
            "restored": restored,
            "voted_before_crash": name in voters,
            "vote_signers_seen_after": sorted(signers),
            "certificates_installed_after": certified[:8],
            "errors": [line[:300] for line in lines if " ERROR " in line],
            "warnings": [line[:300] for line in lines if " WARN " in line],
            "asks_for_work_last_10s": sum("asking peers for work" in line for line in late),
            "waits_for_data_last_10s": sum("waits for local data" in line for line in late),
            "timeouts_last_10s": sum("finality phase timed out" in line for line in late),
            "waits_after_restart": sum(any(text in line for text in WAITING) for line in lines),
            "unresolved_wait": unresolved_waits(lines),
        }

    payer.join()
    accepted = [record for record in records if record["status"] == 200]
    before = [record for record in accepted if record["sent"] < crashed]
    if transparent:
        initial["accepted_before_kill"] = sorted({record["txid"] for record in accepted
                                                   if record["done"] < crashed})
    final, pending = wait_final(port("rpc", bench.nodes[0]), [record["txid"] for record in accepted], 90,
                                "payments", log)
    lifecycle = {txid: str(status.get("lifecycle", "")).lower() for txid, status in final.items()}
    open_ids = [record["txid"] for record in accepted if lifecycle.get(record["txid"]) != "finalized"]
    open_by_node = {}
    for name in nodes:
        found = statuses(port("rpc", int(name[1:])), open_ids) if open_ids else {}
        open_by_node[name] = dict(Counter(
            str(found[txid].get("lifecycle", "")).lower() if txid in found else "unknown" for txid in open_ids))

    def tally(group):
        ids = [record["txid"] for record in group]
        return {"accepted": len(ids),
                "finalized": sum(lifecycle.get(txid) == "finalized" for txid in ids),
                "failed": sum(lifecycle.get(txid) == "failed" for txid in ids),
                "not_final": sum(txid in pending for txid in ids)}

    durable_reopened = recovered_heads(dict(bench.ports()), reopened)
    checks = [
        ("reopened actual API heads match stored canonical cuts",
         len(durable_reopened) == len(nodes), durable_reopened),
        ("log-inferred window after certified availability, before install logs",
         all(installed < height for installed in watch.installed.values()) and len(voters) >= QUORUM,
         f"voted by {voters}, installed {watch.installed}, certified outcome names {certified_blocks} blocks"),
        ("every voter restored all certified blocks",
         all(sum(blocks for at, blocks in per_node[name]["restored"] if at == height) >= certified_blocks
             for name in voters),
         {name: per_node[name]["restored"] for name in nodes}),
        ("same cut IDs and state roots on all four",
         all(entry["same"] for entry in cuts.values()), f"heights {height - 1}-{height + 5}"),
        ("every validator hears the votes of the other three again",
         all(len(set(per_node[name]["vote_signers_seen_after"])) >= len(nodes) - 1 for name in nodes),
         {name: per_node[name]["vote_signers_seen_after"] for name in nodes}),
        ("five more heights without help", True, f"{round(five_s, 1)} s after the crash"),
        ("every wait for block data resolved",
         not any(node["unresolved_wait"] for node in per_node.values()),
         {name: node["waits_after_restart"] for name, node in per_node.items()}),
        ("no errors after the restart", all(not node["errors"] for node in per_node.values()),
         {name: len(node["errors"]) for name, node in per_node.items()}),
    ]
    final_state = None
    if bench.private_chain():
        # Active private chains keep their full genuine private-state oracle.
        if statements:
            statements.finish()
        checked = dict(bench.ports())
        bench.rest_seconds = bench.come_to_rest(checked)
        if bench.rest_seconds is None:
            raise SystemExit("private crash traffic did not settle for monetary checks")
        if statements:
            bench.private_result = statements.summary(checked)
        executed = set(bench.private_result["executed_digests"] or []) if statements else set()
        final_state = final_checks(checked, bench.prover_tool(), Path(bench.manifest), bench.bench_wallets(),
                                   bench.expected_private(executed), log)
        same = all(final_state[key] for key in ("anchor_equal", "supply_equal", "cut_fields_equal", "value_accounted",
                                                 "transparent_balances_equal", "private_balances_equal"))
        checks.append(("the same final state and the expected balances on all four",
                       same and final_state["balances_as_expected"],
                       {"executed_statements": len(executed),
                        "private_scans_failed": final_state.get("private_scans_failed")}))
    else:
        checked = dict(bench.ports())
        replay = replay_finalized(checked, payments, initial, log)
        # Real miners remain running while the duplicate signed bytes could be included.
        target = max((get(value, "/v1/chain/head") or {}).get("height", 0)
                     for value in checked.values()) + 5
        replay["minimum_height"] = target
        deadline = time.monotonic() + 240
        while min((get(value, "/v1/chain/head") or {}).get("height", 0)
                  for value in checked.values()) < target:
            if time.monotonic() >= deadline:
                raise SystemExit("validators did not progress after replaying finalized payments")
            time.sleep(0.5)
        bench.rest_seconds = bench.come_to_rest(checked)
        if bench.rest_seconds is None:
            raise SystemExit("transparent crash traffic did not settle for monetary checks")
        final_state = final_checks(checked, None, Path(bench.manifest), wallets, None, log,
                                   transparent={"initial": initial, "payments": payments, "replay": replay})
        required = ("anchor_equal", "supply_equal", "cut_fields_equal", "value_accounted",
                    "transparent_balances_equal", "private_balances_equal", "balances_as_expected",
                    "one_time_effects", "replay_receipts_unchanged")
        checks.append(("transparent money, exact one-time effects and late replay on every validator",
                       all(final_state[key] for key in required) and final_state["applied_payments"] > 0,
                       {"applied_payments": final_state["applied_payments"],
                        "applied_pre_kill_payments": final_state["applied_pre_kill_payments"],
                        "fee_atoms": final_state["fee_atoms"],
                        "replayed_payments": final_state["replayed_payments"]}))
    gate = bench.gate(dict(bench.ports()), final)
    checks.append(("the determinism gate: one chain and one ledger on every validator", gate["passed"],
                   {name: value for name, value in gate["parts"].items() if not value} or "every part held"))
    return {
        "scenario": bench.name,
        "passed": all(passed for _, passed, _ in checks),
        "determinism": gate,
        "checks": [{"check": check, "passed": passed, "detail": detail} for check, passed, detail in checks],
        "crash_height": height,
        "certified_blocks": certified_blocks,
        "voters": voters,
        "installed_at_crash": watch.installed,
        "crash_window_evidence": "log-inferred; reopened API heads are authoritative; process kill is not power loss",
        "reopened_api_heads": reopened,
        "recovered_canonical_heads": durable_reopened,
        "api_s": round(api_s, 1),
        "crash_height_final_s": round(first, 1),
        "five_more_heights_s": round(five_s, 1),
        "same_cuts": cuts,
        "final_state": final_state,
        "nodes": per_node,
        "payments": {"offered": len(records), "before_crash": tally(before),
                     "after_crash": tally([record for record in accepted if record["sent"] >= crashed]),
                     "not_finalized_by_node": open_by_node},
    }


def stamp(line):
    """Seconds since the epoch from a log line's local time stamp, or 0."""
    try:
        return time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S")) + float("0" + line[19:23])
    except ValueError:
        return 0


def print_crash(outcome):
    height = outcome["crash_height"]
    print(("PASS" if outcome["passed"] else "FAIL") + f": targeted crash at height {height}")
    for check in outcome["checks"]:
        print(f"  [{'ok' if check['passed'] else 'FAILED'}] {check['check']}: {check['detail']}")
    print(f"crash at height {height}, voted available by {', '.join(outcome['voters'])}, "
          f"installed then {outcome['installed_at_crash']}")
    print(f"  API back after {outcome['api_s']} s, height {height} final on all after "
          f"{outcome['crash_height_final_s']} s, five more heights after {outcome['five_more_heights_s']} s")
    same = all(entry["same"] for entry in outcome["same_cuts"].values())
    print(f"  same cut IDs on every validator for heights {min(outcome['same_cuts'])}-{max(outcome['same_cuts'])}: "
          f"{'yes' if same else 'NO'}")
    for name, node in outcome["nodes"].items():
        print(f"  {name}: restored {node['restored']}, voted before the crash {node['voted_before_crash']}, "
              f"vote signers seen {node['vote_signers_seen_after']}, errors {len(node['errors'])}, "
              f"warnings {len(node['warnings'])}; last 10 s: {node['asks_for_work_last_10s']} work requests, "
              f"{node['waits_for_data_last_10s']} waits for data, {node['timeouts_last_10s']} timeouts")
    for phase, label in (("before_crash", "accepted before the crash"), ("after_crash", "accepted after it")):
        tally = outcome["payments"][phase]
        print(f"  payments {label}: {tally['accepted']} accepted, {tally['finalized']} finalized, "
              f"{tally['failed']} failed, {tally['not_final']} not final")
    for name, counts in outcome["payments"]["not_finalized_by_node"].items():
        print(f"  accepted but not finalized, as {name} sees them: {counts}")
