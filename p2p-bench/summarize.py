"""Result folders in short: py -3.14 summarize.py [RESULT_DIR ...] (default: every folder in results/)."""

import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def spread(summary, scale=1.0, digits=1):
    if not summary:
        return "-"
    return "/".join(f"{summary[key] * scale:.{digits}f}" for key in ("median", "p90", "p99", "max"))


def percentiles(summary, digits=2):
    """p50/p95/p99 of a summary; results from before p95 was recorded show it as -."""
    if not summary:
        return "-"
    return "/".join("-" if summary.get(key) is None else f"{summary[key]:.{digits}f}"
                    for key in ("median", "p95", "p99"))


def nodes(result, key):
    values = [node[key] for node in result["nodes"].values() if node.get(key)]
    if not values:
        return "-"
    return "/".join(f"{max(value[part] for value in values):.0f}" for part in ("median", "max"))


def finalized_tps(result):
    """Payments sent in the window that became final, each counted once; older results have no such key."""
    return result.get("finalized_unique_tps", result["finalized_payments"] / result["window_s"])


def withheld(result):
    held = result.get("withheld") or {}
    return "".join(f", {count} {reason}" for reason, count in sorted(held.items()))


def show(path):
    result = json.loads(path.read_text())
    cores = {name: values["cpu_cores_mean"] for name, values in result["resources"].items()
             if values["cpu_cores_mean"] is not None}
    validators = [value for name, value in cores.items() if name.startswith("v")]
    per_tx = [values.get("cpu_ms_per_tx") for name, values in result["resources"].items()
              if name.startswith("v") and values.get("cpu_ms_per_tx")]
    refused = sum(result["refused"].values())
    print(f"{result['scenario']} ({path.parent.name[-15:]}), window {result['window_s']:.0f} s")
    outcome = result.get("verdict")
    if outcome:
        missed = [name for name, passed in outcome["parts"].items() if not passed]
        print("  VERDICT " + ("PASS" if outcome["passed"] else "FAIL: " + ", ".join(missed)) + f"; {outcome['measured']}")
    print(f"  cuts/min {result['cuts_per_min']:.1f}, cut s median/p90/p99/max {spread(result['cut_interval_s'])}, "
          f"late {result['late_cuts']}, views {result['views']}")
    whole = result['over_whole_cuts']
    print(f"  over whole cuts: applied/s {whole['applied_per_s']:.1f}, entries/s {whole['entries_per_s']:.1f}, blocks/s {whole['blocks_per_s']:.1f}; waiting {result.get('waiting_trend')}")
    print(f"  blocks/s {result['bps_registered']:.1f}; payments/s offered {result['offered_tps']:.0f}, "
          f"accepted {result['accepted_tps']:.0f}, finalized {finalized_tps(result):.0f} "
          f"(each once, by its status), {refused} refused" + withheld(result))
    content = result.get("block_content") or {}
    if content.get("entries"):
        blocks = max(content["executed_blocks"], 1)
        print(f"  executed blocks {content['executed_blocks']}, {100 * content['empty_blocks'] / blocks:.0f} % empty; "
              f"per block {content['entries'] / blocks:.2f} transactions, {content['applied'] / blocks:.2f} applied, "
              f"{content['duplicates'] / blocks:.2f} copies within a cut, {content['spent'] / blocks:.2f} spent "
              f"before; {100 * content['applied'] / (10 * blocks):.0f} % of the room for 10 used")
    if result.get("accepted_by_node"):
        print("  accepted by node: " + ", ".join(f"{node} {count}" for node, count in
                                                 sorted(result["accepted_by_node"].items())))
    print(f"  finality s median/p90/p99/max {spread(result['finality_latency_s'])}, "
          f"not final {result['not_final']}")
    print(f"  latency p50/p95/p99: finality s {percentiles(result['finality_latency_s'])}, "
          f"submission ms {percentiles(result.get('submit_ms'), 0)}")
    print(f"  per validator ms median/max: build {nodes(result, 'build_ms')}, check {nodes(result, 'check_ms')}, "
          f"install {nodes(result, 'install_ms')}; mempool median/max {nodes(result, 'mempool')}")
    cpu = [node.get("cpu_ms") or {} for node in result["nodes"].values()]
    if any((part.get("install") or {}).get("count") for part in cpu):
        def median(step):
            values = [(part.get(step) or {}).get("median") for part in cpu if (part.get(step) or {}).get("median") is not None]
            return f"{max(values):.0f}" if values else "-"
        print(f"  of that on the thread's CPU, median: build {median('build')}, check {median('check')}, install "
              f"{median('install')}; prefix ahead {nodes(result, 'prefix_ms')} ms, CPU {median('prefix')}")
    print(f"  CPU: machine {spread(result['machine_cpu'], 100, 0)} %, validators "
          f"{'/'.join(f'{value:.2f}' for value in validators)} cores"
          + (f", {max(per_tx):.1f} ms CPU per finalized tx" if per_tx else "")
          + f", miners {sum(value for name, value in cores.items() if name.startswith('miner')):.1f}")
    runs = sum(node.get("builds", 0) + node.get("checks", 0) for node in result["nodes"].values())
    reused = sum(node.get("reused_cuts", 0) for node in result["nodes"].values())
    if runs:
        print(f"  cut runs on all validators {runs}, proposals served by a cut that had already run {reused}")
    for name, values in sorted(result["resources"].items()):
        if values.get("threads"):
            print(f"  {name} threads: " + ", ".join(
                f"{thread} {use['cores']:.2f} (busiest {use['busiest_thread_cores']:.2f})"
                for thread, use in list(values["threads"].items())[:5]))
    for name, node in sorted(result["nodes"].items()):
        if node.get("mempool_carried"):
            print(f"  {name} mempool at a cut, median: {node['mempool_carried']['median']:.0f} already in blocks "
                  f"({node['copies_per_carried'] or 0:.2f} blocks each), "
                  f"{node['mempool_waiting']['median']:.0f} waiting for one")
        templates = node.get("templates") or {}
        if templates.get("templates"):
            pool = node.get("template_pool") or {}
            pooled = (pool.get("pooled") or {}).get("median", 0)
            print(f"  {name} templates {templates['templates']}: {templates['filled']} with transactions "
                  f"({templates['full']} full), empty {templates['empty_pool']} for an empty pool, "
                  f"{templates['lease_cap']} for the lease cap, {templates['none_chosen']} with nothing chosen, "
                  f"{templates['asked_empty']} asked empty; {templates['chosen']} chosen "
                  f"({templates['chosen_young']} young relayed); passed over {templates['skipped_leased']} leased, "
                  f"{templates['skipped_held']} in held blocks, {templates['refused_limits']} over a limit; "
                  f"pool median {pooled:.0f}")
        relay = node.get("relay")
        if relay:
            backlog = node.get("relay_backlog") or {}
            waiting = (backlog.get("inventory_waiting") or {}).get("max", 0)
            print(f"  {name} relay: announcements deferred {relay['inventory_deferred']}, lost to a full queue "
                  f"{relay['inventory_overflowed']}, expired {relay['inventory_expired']}, at most {waiting:.0f} "
                  f"waiting; evicted unannounced {relay['announce_evicted']}, unfetched {relay['fetch_evicted']}")
        if relay and relay.get("scans"):
            print(f"  {name} fetch scans {relay['scans']}: found {relay['scan_found']}, no provider "
                  f"{relay['scan_no_provider']}, deferred {relay['scan_deferred']}, unavailable {relay['scan_unavailable']}")
        for peer, counts in sorted((node.get("relay_by_peer") or {}).items()):
            if any(counts.values()):
                print(f"    {name} -> {peer}: " + ", ".join(f"{key} {value}" for key, value in counts.items()))
        for peer, slots in sorted((node.get("fetch_slots") or {}).items()):
            print(f"    {name} fetch slots of {peer}: " + ", ".join(f"{key} {value}" for key, value in slots.items()))
        for peer, counts in sorted((node.get("sync_shares") or {}).items()):
            print(f"    {name} sync shares of {peer}: " + ", ".join(f"{key} {value}" for key, value in counts.items()))
        sites = list((node.get("engine_lock") or {}).items())[:5]
        if sites:
            print(f"  {name} engine lock, most waited: " + "; ".join(
                f"{at.split(chr(92))[-1]} {site['count']}x waited {site['waited_ms']} ms (longest {site['longest_wait_ms']}) "
                f"held {site['held_ms']} ms" for at, site in sites))
    waits = sum(node["waits_for_data"] for node in result["nodes"].values())
    nil = sum(node["nil_votes"] for node in result["nodes"].values())
    slow = max(node["slow_write_max_ms"] for node in result["nodes"].values())
    print(f"  nil votes {nil}, proposals waiting for data {waits}, slowest fsync {slow} ms, "
          f"proxy late {result['proxy']['late_max_ms']:.0f} ms, packets lost {result['proxy']['dropped']}")
    environment = result.get("environment")
    if environment:
        print("  environment: " + ("valid" if environment["valid"] else "INVALID, " + "; ".join(environment["reasons"])))


def main():
    folders = [Path(arg) for arg in sys.argv[1:]] or sorted(RESULTS.glob("*/result.json"))
    for folder in folders:
        path = folder if folder.name == "result.json" else folder / "result.json"
        if path.exists():
            show(path)


main()
