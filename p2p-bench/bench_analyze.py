"""Scenario numbers from node logs, submission records, transaction statuses and process samples."""

import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+(\w+)\s+(\S+?):\s+(.*)$")
FIELD = re.compile(r"(\w+)=(\S+)")
EVENTS = [
    ("cut installed", "install"),
    ("finality certificate installed", "certificate"),
    ("finality proposal signed", "built"),
    ("finality proposal accepted", "checked"),
    ("finality vote signed", "vote"),
    ("finality phase timed out", "timeout"),
    ("finality moved to a later view", "moved"),
    ("durable write was slow", "slow"),
    ("availability proposal waits for local data", "waits"),
    ("asking peers for work", "ask"),
    ("work templates filled", "templates"),
    ("public swarm transaction relay", "relay"),
    ("public swarm transactions by peer", "relay_peer"),
    ("public swarm sync shares by peer", "shares"),
    ("engine lock use", "lock"),
    ("public swarm transaction fetch slots", "slots"),
    ("cut prefix executed ahead", "prefix"),
    ("public swarm asks for private items", "private_ask"),
    ("public swarm received private items", "private_received"),
    ("public swarm checked private items", "private_checked"),
]
TEMPLATE_COUNTS = ("templates", "filled", "full", "chosen", "chosen_other", "chosen_young", "asked_empty",
                   "empty_pool", "lease_cap", "none_chosen", "skipped_leased", "skipped_held", "skipped_anchor",
                   "refused_limits")
TEMPLATE_GAUGES = ("pooled", "held", "leases", "leased")
RELAY_COUNTS = ("announce_evicted", "fetch_evicted", "inventory_deferred", "inventory_overflowed",
                "inventory_expired", "scans", "scan_found", "scan_no_provider", "scan_deferred",
                "scan_unavailable")
RELAY_GAUGES = ("to_announce", "to_fetch", "waiting_admission", "inventory_waiting")
PEER_COUNTS = ("announced", "held_back", "over_budget", "offered", "asked", "received", "admitted", "refused",
               "batches", "admission_ms")
BLOCK_CONTENT = ("entries", "applied", "duplicates", "spent", "no_effect", "empty_blocks")



def ran_cut(fields, own):
    """Whether admitting this proposal ran its cut; older nodes did not say and always ran it."""
    if "ran" in fields:
        return fields["ran"] == "true"
    if "reused" in fields:
        return fields["reused"] != "true" and (fields.get("height"), fields.get("view")) not in own
    return True

def parse(path):
    events, warnings = [], []
    with open(path, errors="replace") as file:
        for line in file:
            match = LINE.match(line)
            if not match:
                continue
            stamp, level, target, text = match.groups()
            when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f").timestamp()
            if level in ("WARN", "ERROR"):
                warnings.append((when, level, target, text.strip()))
            for needle, name in EVENTS:
                if text.startswith(needle):
                    events.append((when, name, dict(FIELD.findall(text))))
                    break
    return events, warnings


def growth(first, last, keys):
    """What cumulative counters added between two reports; a counter that started over counts from zero."""
    return {key: (int(last.get(key, 0)) - int(first.get(key, 0))
                  if int(last.get(key, 0)) >= int(first.get(key, 0)) else int(last.get(key, 0)))
            for key in keys}


def private_relay(in_window):
    """What the private relay of one node fetched within the window: asks, how many of their items it had
    asked for before or a cut waited for, what arrived, the round trip of a fetch and the check of its items."""
    asks = [fields for _, name, fields in in_window if name == "private_ask"]
    received = [fields for _, name, fields in in_window if name == "private_received"]
    checked = [fields for _, name, fields in in_window if name == "private_checked"]
    if not (asks or received or checked):
        return None
    return {
        "asks": len(asks),
        "ids_asked": sum(int(fields.get("ids", 0)) for fields in asks),
        "asked_again": sum(int(fields.get("again", 0)) for fields in asks),
        "asked_for_a_cut": sum(int(fields.get("for_cut", 0)) for fields in asks),
        "items_received": sum(int(fields.get("items", 0)) for fields in received),
        "empty_answers": sum(1 for fields in received if fields.get("items") == "0"),
        "fetch_ms": summary([int(fields.get("millis", 0)) for fields in received]),
        "items_admitted": sum(int(fields.get("admitted", 0)) for fields in checked),
        "check_ms": summary([int(fields.get("millis", 0)) for fields in checked]),
        "invalid": sum(1 for fields in checked if fields.get("invalid") == "true"),
    }


def relay_by_peer(in_window):
    """Per peer, what the transaction relay counters added within the window."""
    reports = defaultdict(list)
    for _, name, fields in in_window:
        if name == "relay_peer":
            reports[fields["peer"]].append(fields)
    return {peer[-8:]: growth(rows[0], rows[-1], PEER_COUNTS) for peer, rows in reports.items()}


def fetch_slots(in_window):
    """Per peer, the most transaction fetch slots in use and batches waiting, and how often it was silent."""
    peers = defaultdict(lambda: {"pending_max": 0, "waiting_max": 0, "admitting_max": 0, "silent_reports": 0,
                                 "reports": 0})
    for _, name, fields in in_window:
        if name != "slots":
            continue
        peer = peers[fields["peer"][-8:]]
        peer["reports"] += 1
        peer["pending_max"] = max(peer["pending_max"], int(fields.get("pending", 0)))
        peer["waiting_max"] = max(peer["waiting_max"], int(fields.get("waiting", 0)))
        peer["admitting_max"] = max(peer["admitting_max"], int(fields.get("admitting", 0)))
        peer["silent_reports"] += fields.get("silent") == "true"
    return dict(peers)


def lock_sites(in_window):
    """Per call site of the engine lock: uses, milliseconds waited and held, the longest wait."""
    sites = defaultdict(lambda: {"count": 0, "waited_ms": 0, "held_ms": 0, "longest_wait_ms": 0})
    for _, name, fields in in_window:
        if name != "lock":
            continue
        site = sites[fields["at"]]
        for key in ("count", "waited_ms", "held_ms"):
            site[key] += int(fields.get(key, 0))
        site["longest_wait_ms"] = max(site["longest_wait_ms"], int(fields.get("longest_wait_ms", 0)))
    return dict(sorted(sites.items(), key=lambda item: -item[1]["waited_ms"]))


def message_of(text):
    return re.sub(r"\s+\w+=\S+", "", text).strip()


def summary(values):
    if not values:
        return None
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean": statistics.fmean(ordered),
        "median": ordered[len(ordered) // 2],
        "p90": ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))],
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "p99": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))],
        "max": ordered[-1],
    }


def thirds(samples, start, end):
    """The median of (when, value) samples in the middle and in the last third of the window: what still
    grows at the end of a run shows here, the ramp at its start does not."""
    third = (end - start) / 3
    middle = [value for when, value in samples if start + third <= when < start + 2 * third]
    last = [value for when, value in samples if start + 2 * third <= when <= end]
    return [statistics.median(middle) if middle else None, statistics.median(last) if last else None]


def caught_up(samples, name, since, end):
    """How a node that returned or joined at `since` fared against the moving head while the load ran:
    the seconds until it stood at most a cut behind the others, and how far it fell behind afterwards."""
    reached, behind = None, 0
    for sample in samples:
        own = sample["heads"].get(name)
        top = max((height for height in sample["heads"].values() if height is not None), default=None)
        if not since <= sample["at"] <= end or top is None:
            continue
        if reached is None:
            if own is not None and top - own <= 1:
                reached = sample["at"] - since
        else:
            behind = max(behind, top - own if own is not None else top)
    return {"seconds": reached, "most_behind_after": behind}


# Allow a 10% deviation from target rates over WAN links.
WAN_TOLERANCE = 0.10


def verdict(result, scenario, private_rate, final_state, continuous, agreement):
    """The bar of the combined run, each part with what was measured: the offered payments and private
    statements finalized at their rates, the cut rate of the profile, no backlog that still grows, and
    one chain on every validator. A rate passes within `WAN_TOLERANCE` of its target; the run passes
    only as a whole."""
    low, high = 1 - WAN_TOLERANCE, 1 + WAN_TOLERANCE
    rate, target_cuts = scenario["rate"], 60 / scenario["target"]
    interval = (result["cut_interval_s"] or {}).get("mean")
    cuts = 60 / interval if interval else 0.0
    # What the chain applied inside the window: payments that only become final in the wait after the
    # run were a backlog during it. Over whole cuts: one cut more or less inside 60 s moves it by 4 %.
    applied = result["over_whole_cuts"]["applied_per_s"]
    checks = {
        "applied_tps": {"value": round(applied, 1), "required": low * rate},
        "cuts_per_min": {"value": round(cuts, 2), "required": low * target_cuts},
    }
    passed = {name: check["value"] >= check["required"] for name, check in checks.items()}
    # The block rate of the profile: above it the run shows room the network will not have.
    blocks = result["over_whole_cuts"]["blocks_per_s"]
    checks["blocks_per_s"] = {"value": round(blocks, 1), "required": [low * scenario["bps"], high * scenario["bps"]]}
    passed["blocks_per_s"] = low * scenario["bps"] <= blocks <= high * scenario["bps"]
    passed["no_payment_refused"] = not result["refused"]
    passed["every_payment_final"] = result["not_final"] == 0
    waiting, finality = result["waiting_trend"], result["finality_trend_s"]
    checks["waiting_trend"] = waiting
    checks["finality_trend_s"] = finality
    passed["waiting_not_growing"] = None not in waiting and waiting[1] <= waiting[0] * 1.1 + rate
    passed["finality_not_growing"] = None not in finality and finality[1] <= finality[0] * 1.25 + scenario["target"]
    if private_rate:
        ran_at = result["private"]["ran_at"]
        start, end = result["window"]
        # A run may go on past its last statement, or begin them late.
        sending_starts = start - scenario["warm"] + scenario.get("private_after", 0)
        sending_ends = sending_starts + result["private"]["sent"] / private_rate
        counted_from = max(start, sending_starts)
        counted_until = min(end, sending_ends)
        per_second = sum(1 for when in ran_at if counted_from <= when <= counted_until) / (counted_until - counted_from)
        checks["private_per_s"] = {"value": round(per_second, 2), "required": low * private_rate}
        passed["private_per_s"] = per_second >= checks["private_per_s"]["required"]
        passed["no_statement_refused"] = result["private"]["accepted"] == result["private"]["sent"]
        pools = [(sample["at"], max((count for count in sample["statements"].values() if count is not None), default=0))
                 for sample in continuous["pool_samples"]]
        pool = thirds(pools, counted_from, counted_until)
        checks["private_pool_trend"] = pool
        passed["private_pool_not_growing"] = None not in pool and pool[1] <= pool[0] + 4
    passed["heights_match"] = not continuous["mismatches"] and not (agreement or {}).get("mismatches")
    returned = {f"v{outage['validator']}": outage["up"] for outage in result.get("outages") or []}
    if result.get("joiner"):
        returned["v5"] = result["joiner"]["joined"]
    if returned:
        # A node that starts from a package joins once one is written, which can be after the load; it is
        # judged until the run's last sample.
        last = max((sample["at"] for sample in continuous["pool_samples"]), default=result["window"][1])
        end = {name: last if name == "v5" and since >= result["window"][1] else result["window"][1]
               for name, since in returned.items()}
        back = {name: caught_up(continuous["pool_samples"], name, since, end[name])
                for name, since in returned.items()}
        checks["caught_up"] = back
        passed["caught_up_and_stayed"] = all(value["seconds"] is not None and value["most_behind_after"] <= 2
                                             for value in back.values())
    if final_state is not None:
        passed["final_state_equal"] = all(final_state[key] for key in (
            "anchor_equal", "supply_equal", "cut_fields_equal", "transparent_balances_equal",
            "private_balances_equal"))
        passed["balances_as_expected"] = final_state["balances_as_expected"]
        passed["value_accounted"] = final_state["value_accounted"]
    # One finalized chain and one ledger on every node (bench_determinism): every part is judged, in every run, and
    # a gate that did not run or broke off fails.
    gate = result.get("determinism") or {}
    passed["determinism_gate_ran"] = bool(gate.get("parts")) and not gate.get("errors")
    passed.update({f"determinism_{name}": value for name, value in (gate.get("parts") or {}).items()})
    if scenario.get("fault_run"):
        # The rates stop with the chain by design here, so they are told, not judged; what counts is
        # that the fault was safe, that the chain went on and that it is one chain.
        told = {"applied_tps", "cuts_per_min", "blocks_per_s", "no_payment_refused", "every_payment_final",
                "waiting_not_growing", "finality_not_growing", "private_per_s", "no_statement_refused",
                "private_pool_not_growing"}
        checks["told_not_judged"] = {name: passed.pop(name) for name in told if name in passed}
        fault = result["fault"]
        passed["fault_was_safe"] = bool(fault and fault["safe"])
        passed["chain_went_on"] = bool(fault and fault["resumed_s"] is not None)
        resent = result.get("resent") or {}
        passed["open_payments_went_final"] = not resent.get("still_open")
        checks["resent"] = {key: value for key, value in resent.items() if key != "still_open"}
        checks["resent"]["still_open"] = len(resent.get("still_open") or [])
        checks["fault"] = fault
    if scenario.get("stake_changes"):
        moves = result.get("stake_moves") or {}
        passed["committee_changed_as_planned"] = bool(moves) and all(moves["checks"].values())
        checks["stake_moves"] = moves
    return {"passed": all(passed.values()), "parts": passed, "measured": checks}


def analyze(logs, window, target_interval, records, finals, samples, proxy_lines):
    """logs: node -> path; window: (start, end) epoch seconds."""
    start, end = window
    span = end - start
    parsed = {node: parse(path) for node, path in logs.items()}

    installs = defaultdict(dict)
    install_fields = {}
    views = {}
    for node, (events, _) in parsed.items():
        for when, name, fields in events:
            if name == "install":
                height = int(fields["height"])
                installs[height][node] = when
                install_fields.setdefault(height, fields)
            elif name == "certificate":
                height = int(fields["height"])
                views[height] = max(views.get(height, 0), int(fields["view"]))
    first = {height: min(times.values()) for height, times in installs.items()}
    inside = sorted(height for height, when in first.items() if start <= when <= end)

    intervals = [first[h] - first[h - 1] for h in inside if h - 1 in first]
    late = [value for value in intervals if value > 1.5 * target_interval]
    spread = [max(installs[h].values()) - min(installs[h].values()) for h in inside if len(installs[h]) == len(logs)]

    registered = sum(int(install_fields[h].get("registered", 0)) for h in inside)
    executed = sum(int(install_fields[h].get("executed", 0)) for h in inside)
    transactions = sum(int(install_fields[h].get("transactions", 0)) for h in inside)
    block_content = {key: sum(int(install_fields[h].get(key, 0)) for h in inside) for key in BLOCK_CONTENT}
    block_content["executed_blocks"] = executed

    per_node = {}
    for node, (events, warnings) in parsed.items():
        in_window = [(when, name, fields) for when, name, fields in events if start <= when <= end]
        slow = [int(fields.get("millis", 0)) for _, name, fields in in_window if name == "slow"]
        install_cost = [int(fields.get("millis", 0)) for _, name, fields in in_window if name == "install"]
        install_cpu = [int(fields["cpu"]) for _, name, fields in in_window if name == "install" and "cpu" in fields]
        build_cost = [int(fields.get("build_millis", 0)) for _, name, fields in in_window if name == "built"]
        build_cpu = [int(fields["build_cpu"]) for _, name, fields in in_window if name == "built" and "build_cpu" in fields]
        prefixes = [fields for _, name, fields in in_window if name == "prefix"]
        # A node that reuses a cut, or admits the one it just built, logs the proposal without running it.
        own = {(fields.get("height"), fields.get("view")) for _, name, fields in in_window if name == "built"}
        checks = [fields for _, name, fields in in_window if name == "checked"]
        ran = [fields for fields in checks if ran_cut(fields, own)]
        reused = [fields for fields in checks if not ran_cut(fields, own)
                  and (fields.get("height"), fields.get("view")) not in own]
        check_cost = [int(fields.get("millis", 0)) for fields in ran]
        check_cpu = [int(fields["cpu"]) for fields in ran if "cpu" in fields]
        installs_here = [fields for _, name, fields in in_window if name == "install" and "carried" in fields]
        carried = [int(fields["carried"]) for fields in installs_here]
        waiting = [int(fields["mempool"]) - int(fields["carried"]) for fields in installs_here]
        copies = sum(int(fields["copies"]) for fields in installs_here)
        mempool = [int(fields.get("mempool", 0)) for _, name, fields in in_window if name == "install"]
        store_cost = [int(fields.get("store", 0)) for _, name, fields in in_window if name == "install"]
        lag = [installs[h][node] - first[h] for h in inside if node in installs[h]]
        templates = [fields for _, name, fields in in_window if name == "templates"]
        relay = [fields for _, name, fields in in_window if name == "relay"]
        shares = {}
        for _, name, fields in in_window:
            if name == "shares":
                shares[fields["peer"][-8:]] = {key: int(value) for key, value in fields.items() if key != "peer"}
        per_node[node] = {
            "nil_votes": sum(1 for _, name, fields in in_window if name == "vote" and fields.get("nil") == "true"),
            "timeouts": sum(1 for _, name, _ in in_window if name == "timeout"),
            "moved_views": sum(1 for _, name, _ in in_window if name == "moved"),
            "waits_for_data": sum(1 for _, name, _ in in_window if name == "waits"),
            "work_requests": sum(1 for _, name, _ in in_window if name == "ask"),
            "slow_writes": len(slow),
            "slow_write_max_ms": max(slow, default=0),
            "slow_write_total_ms": sum(slow),
            "build_ms": summary(build_cost),
            "builds": len(build_cost),
            "checks": len(check_cost),
            "reused_cuts": len(reused),
            "mempool_carried": summary(carried),
            "mempool_waiting": summary(waiting),
            "copies_per_carried": round(copies / sum(carried), 3) if sum(carried) else None,
            "check_ms": summary(check_cost),
            "mempool": summary(mempool),
            "install_ms": summary(install_cost),
            "store_ms": summary(store_cost),
            "behind_first_install_s": summary(lag),
            "templates": {key: sum(int(fields.get(key, 0)) for fields in templates) for key in TEMPLATE_COUNTS},
            "template_pool": {key: summary([int(fields[key]) for fields in templates if key in fields])
                              for key in TEMPLATE_GAUGES},
            "relay": growth(relay[0], relay[-1], RELAY_COUNTS) if len(relay) > 1 else None,
            "relay_backlog": {key: summary([int(fields[key]) for fields in relay if key in fields])
                              for key in RELAY_GAUGES},
            "relay_by_peer": relay_by_peer(in_window),
            "private_relay": private_relay(in_window),
            "sync_shares": shares,
            "engine_lock": lock_sites(in_window),
            "fetch_slots": fetch_slots(in_window),
            "cpu_ms": {"build": summary(build_cpu), "check": summary(check_cpu), "install": summary(install_cpu),
                       "prefix": summary([int(fields["cpu"]) for fields in prefixes if "cpu" in fields])},
            "prefix_ms": summary([int(fields.get("millis", 0)) for fields in prefixes]),
            "warnings": Counter(f"{level} {message_of(text)}" for _, level, _, text in warnings),
            "warnings_in_window": sum(1 for when, *_ in warnings if start <= when <= end),
        }

    payments = [record for record in records if record and start <= record["sent"] <= end]
    # -1 and -2 the bench held back itself: too far behind, or a parent that was not final.
    offered = [record for record in payments if record["status"] not in (-1, -2)]
    withheld = Counter(record["error"] for record in payments if record["status"] in (-1, -2))
    accepted = [record for record in payments if record["status"] == 200]
    refused = Counter((record["error"] or "")[:120] for record in offered if record["status"] != 200)
    latencies, latency_by_sent, finalized = [], [], 0
    for record in accepted:
        status = finals.get(record["txid"])
        if not status or not status.get("finalized_cut"):
            continue
        height = status["finalized_cut"]["height"]
        when = installs.get(height, {}).get(record["node"], first.get(height))
        if when is None:
            continue
        finalized += 1
        latencies.append(when - record["sent"])
        latency_by_sent.append((record["sent"], when - record["sent"]))
    submit_ms = [(record["done"] - record["sent"]) * 1000 for record in accepted]
    busy = [record.get("busy", 0) for record in payments]
    behind = [record["sent"] - record["due"] for record in payments]

    processes = defaultdict(lambda: {"cpu": [], "rss": [], "write": [], "read": [], "write_ops": [], "read_ops": []})
    machine = []
    threads = defaultdict(lambda: defaultdict(lambda: {"cores": [], "busiest": []}))
    for row in samples:
        if not start <= row["t"] <= end:
            continue
        machine.append(row["machine_cpu"])
        for process, names in row.get("threads", {}).items():
            for name, use in names.items():
                threads[process][name]["cores"].append(use["cores"])
                threads[process][name]["busiest"].append(use["busiest"])
        for name, values in row["processes"].items():
            processes[name]["cpu"].append(values["cpu"])
            processes[name]["rss"].append(values["rss"])
            processes[name]["write"].append(values["write_bytes"])
            processes[name]["read"].append(values["read_bytes"])
            processes[name]["write_ops"].append(values["writes"])
            processes[name]["read_ops"].append(values["reads"])
    finalized_tps = transactions / span
    resources = {
        name: {
            "cpu_cores_mean": statistics.fmean(values["cpu"]) if values["cpu"] else None,
            "cpu_ms_per_tx": (statistics.fmean(values["cpu"]) * 1000 / finalized_tps
                              if values["cpu"] and finalized_tps and name.startswith("v") else None),
            "cpu_cores_max": max(values["cpu"], default=None),
            "rss_mb_max": max(values["rss"], default=0) / 2**20,
            "write_mb_s": statistics.fmean(values["write"]) / 2**20 if values["write"] else None,
            "write_mb_s_max": max(values["write"], default=0) / 2**20,
            "read_mb_s": statistics.fmean(values["read"]) / 2**20 if values["read"] else None,
            "write_ops_s": statistics.fmean(values["write_ops"]) if values["write_ops"] else None,
            "read_ops_s": statistics.fmean(values["read_ops"]) if values["read_ops"] else None,
            "threads": {thread: {"cores": round(statistics.fmean(use["cores"]), 3),
                                 "busiest_thread_cores": round(statistics.fmean(use["busiest"]), 3)}
                        for thread, use in sorted(threads[name].items(),
                                                  key=lambda item: -statistics.fmean(item[1]["cores"]))},
        }
        for name, values in sorted(processes.items())
    }

    proxy = {"late_max_ms": 0.0, "dropped": 0, "packets": 0}
    # Every link crosses one proxy, which reports each direction every 10 s: what a validator sent
    # and received over the wide area links, as a mean over the window.
    sent, received = Counter(), Counter()
    day_start, day_end = (datetime.fromtimestamp(edge, timezone.utc).strftime("%H:%M:%S") for edge in (start, end + 10))
    for line in proxy_lines:
        fields = re.search(r"(\w+)->(\w+) packets (\d+) bytes (\d+) dropped (\d+) .* late max ([\d.]+) ms", line)
        if fields and day_start <= line[:8] <= day_end:
            proxy["packets"] += int(fields.group(3))
            proxy["dropped"] += int(fields.group(5))
            proxy["late_max_ms"] = max(proxy["late_max_ms"], float(fields.group(6)))
            sent[fields.group(1)] += int(fields.group(4))
            received[fields.group(2)] += int(fields.group(4))
    proxy["mbit_s"] = {node: {"sent": round(sent[node] * 8 / span / 1e6, 2),
                              "received": round(received[node] * 8 / span / 1e6, 2)}
                       for node in sorted(set(sent) | set(received))}

    # From the first cut of the window to the last, what the cuts after the first brought.
    whole = inside[1:]
    whole_span = (first[inside[-1]] - first[inside[0]]) if len(inside) > 1 else 0.0
    over_whole_cuts = {
        name: sum(int(install_fields[h].get(field, 0)) for h in whole) / whole_span if whole_span else 0.0
        for name, field in (("applied_per_s", "applied"), ("entries_per_s", "entries"), ("blocks_per_s", "registered"))
    }

    return {
        "window_s": span,
        "cuts": len(inside),
        "over_whole_cuts": over_whole_cuts,
        "cuts_per_min": len(inside) * 60 / span,
        "cut_interval_s": summary(intervals),
        "late_cuts": len(late),
        "install_spread_s": summary(spread),
        "views_above_zero": sum(1 for h in inside if views.get(h, 0) > 0),
        "highest_view": max((views.get(h, 0) for h in inside), default=0),
        "bps_registered": registered / span,
        "bps_executed": executed / span,
        "tps_finalized": transactions / span,
        "tx_per_executed_block": transactions / executed if executed else 0.0,
        "mempool_first_last": [int(install_fields[h].get("mempool", 0)) for h in inside[:1] + inside[-1:]],
        "views": dict(sorted(Counter(views.get(h, 0) for h in inside).items())),
        "block_content": block_content,
        "scheduled_tps": len(payments) / span,
        "offered_tps": len(offered) / span,
        "accepted_tps": len(accepted) / span,
        "finalized_unique_tps": finalized / span,
        "withheld": dict(withheld),
        "accepted_by_node": dict(Counter(record["node"] for record in accepted)),
        "refused": dict(refused),
        "finalized_payments": finalized,
        "finality_latency_s": summary(latencies),
        "finality_trend_s": thirds(latency_by_sent, start, end),
        "mempool_trend": thirds([(first[h], int(install_fields[h].get("mempool", 0))) for h in inside], start, end),
        # What waits for a block, on each node: the mempool without what sits in blocks on the way to a cut.
        "waiting_trend": thirds([(when, int(fields["mempool"]) - int(fields["carried"]))
                                 for events, _ in parsed.values() for when, name, fields in events
                                 if name == "install" and "carried" in fields], start, end),
        "submit_ms": summary(submit_ms),
        "submits_answered_busy": sum(1 for value in busy if value),
        "payments_sent_away": sum(1 for record in payments if record.get("sent_away")),
        "busy_answers": sum(busy),
        "submitter_behind_s": summary(behind),
        "nodes": per_node,
        "machine_cpu": summary(machine),
        "resources": resources,
        "proxy": proxy,
    }
