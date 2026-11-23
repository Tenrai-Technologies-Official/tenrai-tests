"""The verdicts of stored runs under the 10 % rule, whatever rule they ran under:
py -3.14 verdicts.py [RESULT_DIR ...] (default: every folder in results/ of a run with a verdict).

Rates are compared with the scenario's targets again; every other part stays as the run measured it.
"""

import json
import sys
from pathlib import Path

from bench import SCENARIOS
from bench_analyze import WAN_TOLERANCE

RESULTS = Path(__file__).resolve().parent / "results"


def row(path):
    result = json.loads(path.read_text(encoding="utf-8"))
    verdict = result.get("verdict")
    scenario = SCENARIOS.get(result.get("scenario"))
    if not verdict or "measured" not in verdict or not scenario:
        return None
    low, high = 1 - WAN_TOLERANCE, 1 + WAN_TOLERANCE
    parts = dict(verdict["parts"])
    measured = verdict["measured"]
    applied = (measured.get("applied_tps") or measured.get("finalized_tps") or {}).get("value")
    if applied is not None:
        parts.pop("finalized_tps", None)
        parts["applied_tps"] = applied >= low * scenario["rate"]
    cuts = measured["cuts_per_min"]["value"]
    parts["cuts_per_min"] = cuts >= low * 60 / scenario["target"]
    blocks = (measured.get("blocks_per_s") or {}).get("value")
    if blocks is not None:
        parts["blocks_per_s"] = low * scenario["bps"] <= blocks <= high * scenario["bps"]
    private = (measured.get("private_per_s") or {}).get("value")
    if private is not None:
        parts["private_per_s"] = private >= low * scenario["private"]
    failed = sorted(name for name, passed in parts.items() if not passed)
    return {
        "run": path.parent.name,
        "stored": verdict["passed"],
        "failed": failed,
        "rate": scenario["rate"],
        "applied": applied,
        "cuts_per_min": cuts,
        "blocks_per_s": blocks,
        "private_per_s": private,
        "waiting": measured.get("waiting_trend") or measured.get("mempool_trend"),
    }


def main():
    folders = [Path(arg) / "result.json" for arg in sys.argv[1:]] or sorted(RESULTS.glob("*/result.json"))
    print("run | stored | 10 % | offered | applied/s | cuts/min | blocks/s | private/s | waiting | failed parts")
    for path in folders:
        entry = row(path)
        if not entry:
            continue
        waiting = entry["waiting"]
        waiting = "->".join(f"{value:.0f}" for value in waiting) if waiting and None not in waiting else "-"
        print(
            f"{entry['run']} | {'PASS' if entry['stored'] else 'FAIL'} | {'FAIL' if entry['failed'] else 'PASS'} | "
            f"{entry['rate']} | {entry['applied']} | {entry['cuts_per_min']} | {entry['blocks_per_s']} | "
            f"{entry['private_per_s']} | {waiting} | {', '.join(entry['failed'])}"
        )


if __name__ == "__main__":
    main()
