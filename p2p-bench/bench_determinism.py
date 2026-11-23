"""The determinism gate of a bench run, once its chain came to rest: one finalized chain and one ledger on every node.

Every node must hold every height from its floor (the first height for a validator, the height it started from for
a node that joined) to the common top, with the same cut ID, state root and validator set as every other node, and
must serve for each of them a finality proof that bench-private verifies from the genesis as a wallet does. Every
payment of the run must have one outcome on every node, at the cut the chain holds for its height and the cut the
run saw it final at; the payers must hold what the run's own corpus says (bench_ledger), and the economics of every
node must agree and add up. A node that does not answer, a height it lacks or a step that did not run fails the
gate: only a complete comparison passes."""

import hashlib
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bench_agree import head, read_or_status
from bench_ledger import (command, compare_holdings, corpus_effects, economics, economics_checks, expected_burn,
                          expected_holdings, payers, pinned_issuance)
from bench_submit import statuses

CUT_PAGE = 100
# A failure that ran at a cut is an outcome every node shares; a node's other failures are its own.
EXECUTED_FAILURES = ("conflict", "no_effect")


def patient_read(port, path, tries=40):
    """A node's answer to `path`; a busy node is asked again, as a wallet asks it."""
    answer = read_or_status(port, path, 60)
    for _ in range(tries):
        if answer.get("status") not in (429, 503):
            return answer
        time.sleep(0.5)
        answer = read_or_status(port, path, 60)
    return answer


def ranges(heights):
    """Heights as [first, last] runs, short enough for a report."""
    found = []
    for height in sorted(heights):
        if found and height == found[-1][1] + 1:
            found[-1][1] = height
        else:
            found.append([height, height])
    return found


def cut_table(port, top):
    """Each cut the node lists up to `top`, by height: (cut ID, state root, validator set) and the rewards it reports."""
    table, rewards, before = {}, {}, top + 1
    while before > 1:
        page = patient_read(port, f"/v1/cuts?before={before}&limit={CUT_PAGE}")
        if "cuts" not in page:
            raise RuntimeError(f"the cut list before {before} failed: {page}")
        for summary in page["cuts"]:
            cut = summary["cut"]
            table[int(cut["height"])] = (cut["cut_id"], cut["state_root"], cut["validator_set_id"])
            rewards[int(cut["height"])] = (summary.get("rewards"), summary.get("stake_rewards"))
        before -= CUT_PAGE
    return table, rewards


def compare_chains(tables, floors, top):
    """What each node lacks between its floor and `top`, the heights at which two nodes hold different cuts, and the
    chain they hold: per height the cut of the first node that holds it."""
    nodes = {}
    for name, table in tables.items():
        lacking = [height for height in range(floors[name], top + 1) if height not in table]
        nodes[name] = {"floor": floors[name], "lowest": min(table, default=None), "held": len(table),
                       "missing": ranges(lacking)}
    conflicts, chain = [], {}
    for height in sorted(set().union(*tables.values())):
        seen = {name: table[height] for name, table in tables.items() if height in table}
        if len(set(seen.values())) > 1:
            conflicts.append({"height": height, "views": {name: list(value) for name, value in seen.items()}})
        chain[height] = next(iter(seen.values()))
    return nodes, conflicts, chain


def chain_digest(chain, first, top):
    """One hash over the cut ID, state root and validator set of every height from `first` to `top`."""
    digest = hashlib.sha256()
    for height in range(first, top + 1):
        digest.update(f"{height} {' '.join(chain.get(height, ('-', '-', '-')))}\n".encode())
    return digest.hexdigest()


def reward_differences(rewards):
    """Heights at which two nodes that both report the rewards of a cut report different ones."""
    found = []
    for height in sorted(set().union(*rewards.values())):
        seen = {name: value[height] for name, value in rewards.items()
                if height in value and None not in value[height]}
        if len(set(seen.values())) > 1:
            found.append({"height": height, "views": {name: list(value) for name, value in seen.items()}})
    return found


def verified_cuts(tool, manifest, port, bridge_port, first, last):
    """The cuts the node's finality proofs from `first` to `last` show, each checked by bench-private from the
    genesis; the bridges come from `bridge_port`, since a bridge proves itself whoever serves it."""
    said = []
    with subprocess.Popen([*command(tool), "verify-cuts", str(manifest), str(first), str(last)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    said.append(line.strip())
                    continue
                if "get" in message:
                    path = message["get"]
                    answer = patient_read(bridge_port if path.startswith("/v1/finality/bridge") else port, path)
                    process.stdin.write(json.dumps(answer) + "\n")
                    process.stdin.flush()
                    continue
                return message
        finally:
            process.kill()
    return {"error": " ".join(said)[-500:] or "the check ended without an answer"}


def compare_proofs(found, chain, floors, top):
    """Per node how many heights its proofs verified and where a proof failed or showed another cut than the chain."""
    report = {}
    for name, message in found.items():
        wrong = [height for height, cut_id, state_root, validators in message.get("cuts", [])
                 if chain.get(height) != (cut_id, state_root, validators)]
        verified = len(message.get("cuts", []))
        report[name] = {"verified": verified, "expected": top - floors[name] + 1, "different": wrong[:10],
                        "error": message.get("error"), "error_height": message.get("height")}
    return report


def outcome(status):
    """A payment's outcome as every node must share it: final or failed at a cut, else open."""
    if status is None:
        return ("open",)
    lifecycle = str(status.get("lifecycle", "")).lower()
    cut = status.get("finalized_cut")
    if lifecycle == "finalized" and cut:
        return ("final", int(cut["height"]), cut["cut_id"])
    failure = str(status.get("failure") or "").lower()
    cut = cut or status.get("registered_cut")
    if lifecycle == "failed" and failure in EXECUTED_FAILURES and cut:
        return ("failed " + failure, int(cut["height"]), cut["cut_id"])
    return ("open",)


def compare_payments(found, floors, chain, recorded):
    """`found` holds per node the outcome of every payment, `recorded` the outcomes the run itself saw, `floors` the
    first height whose transactions each node ran itself. Every node that ran a payment's height must give its one
    outcome, at the cut the chain holds there; what the run saw final stays final at that cut. A node that started
    after a payment's first outcome lacks its history: it knows at most a later copy of it (a conflict), so it is not
    compared for that payment; the state roots of the heights it holds show it ran the same."""
    conflicts, lacking, off_chain, moved, agreed = [], [], [], [], {}
    names = list(found)
    for txid in sorted(set().union(*found.values()) | set(recorded)):
        values = {name: found[name].get(txid, ("open",)) for name in names}
        first = min((value[1] for value in values.values() if value[0] != "open"), default=None)
        values = {name: value for name, value in values.items() if first is None or floors[name] <= first}
        decided = {value for value in values.values() if value[0] != "open"}
        if len(decided) > 1:
            conflicts.append({"transaction": txid, "outcomes": {name: list(value) for name, value in values.items()}})
            continue
        value = decided.pop() if decided else ("open",)
        agreed[txid] = value
        if value[0] != "open":
            without = [name for name in values if floors[name] <= value[1] and values[name] != value]
            if without:
                lacking.append({"transaction": txid, "nodes": without, "outcome": list(value)})
            if chain.get(value[1], (None,))[0] != value[2]:
                off_chain.append({"transaction": txid, "outcome": list(value)})
        seen = recorded.get(txid)
        if seen is not None and seen[0] != "open" and seen != value:
            moved.append({"transaction": txid, "seen": list(seen), "now": list(value)})
    return agreed, {"conflicts": conflicts, "lacking": lacking, "off_chain": off_chain, "moved": moved}


def parallel(ports, work):
    """`work(name, port)` for every node at once; an exception is that node's answer."""
    def guarded(item):
        try:
            return work(*item)
        except Exception as error:
            return error
    with ThreadPoolExecutor(max_workers=len(ports)) as pool:
        return dict(zip(ports, pool.map(guarded, ports.items())))


def gate_start(ports):
    """What the gate compares the end of a run with, read before the run sends anything: the economics of every node,
    one node's minted atoms and carried work subsidy at one height, and how many heights below a cut the anchor lies
    whose work subsidy it settles."""
    start = economics(ports)
    first = next(iter(ports.values()))
    start["issuance"] = pinned_issuance(first)
    start["window"] = int(patient_read(first, "/v1/chain/daa")["registration_window_heights"])
    return start


def bench_plan(bench, checked, final, bridge_port, deposit):
    """What the gate needs to know of a bench run: its corpus, what it saw final, its start, its rest and what ran
    in it; `deposit` is the coin every payer and depositor starts with."""
    folder = bench.snapshot() / "tx"
    plan = {"tool": bench.prover_tool(), "manifest": Path(bench.manifest), "recorded": final,
            "files": [folder / name for name in ("round1.txt", "round2.txt", "round3.txt", "pay.txt")
                      if (folder / name).exists()],
            "start": bench.gate_start, "deposit": deposit, "bridge_port": bridge_port,
            "rested": bench.rest_seconds is not None,
            "unbonding": (0, bench.bond_of(3)) if bench.scenario.get("stake_changes") else (0,),
            "stake_moves": bool(bench.scenario.get("stake_changes"))}
    if "v5" in checked:
        plan["joined"] = {"v5": bench.joined["validators_head"]}
    if getattr(bench, "private_result", None):
        plan["entries"] = json.loads((bench.snapshot() / "private" / "frames" / "corpus.json").read_text())
        plan["executed"] = set(bench.private_result["executed_digests"])
        plan["private_agree"] = bench.private_result["all_validators_agree"]
    return plan


def run_gate(ports, plan, log):
    """The gate over `ports` (name to API port, every node of the run). `plan` names the bench-private tool, the
    manifest, the corpus files, the run's own outcomes (`recorded`), the economics read before the run (`start`),
    the private statements that ran and what may unbond. A node in `joined` is compared from the lowest height it
    holds, which must not lie above the head the validators held when it started (the height `joined` names); every
    other node from the first height. Returns the parts, the report and the errors."""
    began = time.time()
    parts, report, errors = {}, {"nodes": sorted(ports)}, []

    def failed(step, error):
        errors.append(f"{step}: {error}")
        log(f"determinism gate: {step} failed: {error}")

    heads = {name: head(port) for name, port in ports.items()}
    parts["nodes_reachable"] = None not in heads.values()
    top = min((height for height in heads.values() if height is not None), default=0)
    report["top"] = top
    joined = plan.get("joined", {})
    start_height = int(next(iter(plan["start"]["supply"].values()))["finalized_height"])

    tables = parallel(ports, lambda name, port: cut_table(port, top))
    broken = {name: str(value) for name, value in tables.items() if isinstance(value, Exception)}
    for name, error in broken.items():
        failed(f"cut list of {name}", error)
    held = {name: value[0] for name, value in tables.items() if name not in broken}
    floors = {name: min(held.get(name) or [top + 1]) if name in joined else 1 for name in ports}
    parts["joiners_hold_since_joining"] = all(floors[name] <= ceiling for name, ceiling in joined.items())
    nodes, conflicts, chain = compare_chains(held, floors, top) if held else ({}, [], {})
    parts["every_height_held"] = not broken and top > 0 and all(not node["missing"] for node in nodes.values())
    parts["one_cut_per_height"] = not broken and not conflicts
    parts["rewards_equal"] = not broken and not reward_differences({name: tables[name][1] for name in held})
    first = min(floors.values(), default=1)
    report["chain"] = {"nodes": nodes, "conflicts": conflicts[:10], "first": first,
                       "digest": chain_digest(chain, first, top),
                       "first_cut": list(chain.get(first, ())), "top_cut": list(chain.get(top, ()))}
    log(f"determinism gate: heights {first}-{top} compared on {len(held)} nodes, {len(conflicts)} conflicts")

    bridge = plan.get("bridge_port") or next(iter(ports.values()))
    proofs = parallel(ports, lambda name, port: verified_cuts(plan["tool"], plan["manifest"], port, bridge,
                                                              floors[name], top))
    for name, value in proofs.items():
        if isinstance(value, Exception):
            failed(f"proofs of {name}", value)
            proofs[name] = {"error": str(value)}
    report["proofs"] = compare_proofs(proofs, chain, floors, top)
    parts["proofs_verified"] = all(value["verified"] == value["expected"] and not value["different"]
                                   and not value["error"] for value in report["proofs"].values())
    log("determinism gate: finality proofs", json.dumps({name: value["verified"]
                                                         for name, value in report["proofs"].items()}))

    try:
        genesis, corpus = corpus_effects(plan["tool"], plan["manifest"], plan["files"])
    except Exception as error:
        failed("corpus", error)
        genesis, corpus = {}, {}
    txids = sorted(corpus)
    found = parallel(ports, lambda name, port: {txid: outcome(status)
                                                for txid, status in statuses(port, txids).items()})
    for name, value in found.items():
        if isinstance(value, Exception):
            failed(f"payment statuses of {name}", value)
            found[name] = {}
    recorded = {txid: outcome(status) for txid, status in plan["recorded"].items()}
    # A node that started from a package holds the package's cut but ran none of its transactions: their statuses
    # start at the cut after it.
    ran_from = {name: floor + 1 if name in joined and floor > 1 else floor for name, floor in floors.items()}
    agreed, differences = compare_payments(found, ran_from, chain, recorded)
    final_at = {txid: value[1] for txid, value in agreed.items() if value[0] == "final" and txid in corpus}
    parts["payments_agree"] = bool(corpus) and not differences["conflicts"] and not differences["lacking"]
    parts["payments_on_the_chain"] = bool(corpus) and not differences["off_chain"]
    parts["payments_not_moved"] = bool(corpus) and not differences["moved"]
    report["payments"] = {"corpus": len(corpus), "final": len(final_at),
                          "final_in_the_run": sum(height > start_height for height in final_at.values()),
                          **{key: {"count": len(value), "first": value[:5]} for key, value in differences.items()}}

    tracked = payers(corpus)
    holdings, unspendable = expected_holdings(genesis, corpus, final_at, tracked)
    parts["payments_spend_once"] = bool(corpus) and not unspendable
    try:
        compared = compare_holdings(ports, holdings)
        parts["payer_balances_as_expected"] = bool(tracked) and not compared["differences"] and (
            compared["height"] >= max(final_at.values(), default=0))
    except (Exception, SystemExit) as error:
        failed("payer balances", error)
        compared = {"height": None, "differences": {}}
        parts["payer_balances_as_expected"] = False
    report["payers"] = {"tracked": len(tracked), "unspendable": unspendable[:5], "balances_height": compared["height"],
                        "expected_atoms": str(sum(value[0] for value in holdings.values())),
                        "expected_coins": sum(value[1] for value in holdings.values()),
                        "differences": compared["differences"]}

    try:
        end = economics(ports)
        end["issuance"] = pinned_issuance(next(iter(ports.values())))
        burn = expected_burn(plan.get("entries", []), plan.get("executed", set()), plan["deposit"])
        economy, told = economics_checks(plan["start"], end, burn, plan.get("unbonding", (0,)),
                                         plan.get("stake_moves", False))
    except (Exception, SystemExit) as error:
        failed("economics", error)
        economy = {key: False for key in ("supply_equal", "emission_equal", "stake_equal", "value_conserved",
                                          "burn_as_expected", "minting_as_scheduled", "bonds_listed")}
        told = {}
    parts.update(economy)
    report["economics"] = told
    parts["chain_came_to_rest"] = bool(plan["rested"])
    if "private_agree" in plan:
        parts["private_statements_agree"] = bool(plan["private_agree"])
    report["seconds"] = round(time.time() - began, 1)
    passed = all(parts.values()) and not errors
    failing = sorted(name for name, value in parts.items() if not value)
    log(f"determinism gate {'PASS' if passed else 'FAIL'} in {report['seconds']} s:",
        ", ".join(failing) or "every part held")
    return {"passed": passed, "parts": parts, "report": report, "errors": errors}
