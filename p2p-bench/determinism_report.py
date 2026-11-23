"""The determinism report of a qualification, one section per bench run: its nodes, the heights compared, the cuts at
both ends of the chain with the digest over all of them, the finality proofs verified, the payments, the payers'
holdings, the supply and every part of the gate with PASS or FAIL.
    py -3.14 determinism_report.py REPORT.md RUN_OUTPUT_OR_RESULT_DIR ...
Writes REPORT.md and REPORT.json beside it. Exits 0 only if every run holds a determinism gate that passed: a run
without one, or with a part that failed, fails the report."""

import json
import sys
from pathlib import Path

from verdict_check import folder_of


def cell(value):
    return "-" if value in (None, "", []) else str(value)


def section(name, gate):
    passed = bool(gate) and gate.get("passed") is True
    lines = [f"## {name}: {'PASS' if passed else 'FAIL'}", ""]
    if not gate:
        return lines + ["No determinism gate ran in this run, so it fails.", ""]
    report = gate.get("report") or {}
    chain = report.get("chain") or {}
    first, top = chain.get("first_cut") or [None, None, None], chain.get("top_cut") or [None, None, None]
    lines += [f"Nodes {', '.join(report.get('nodes') or [])}, heights {cell(chain.get('first'))} to "
              f"{cell(report.get('top'))}, digest over every height `{cell(chain.get('digest'))}`.", "",
              f"- first cut `{cell(first[0])}`, state root `{cell(first[1])}`",
              f"- top cut `{cell(top[0])}`, state root `{cell(top[1])}`", "",
              "| node | floor | heights held | missing | proofs verified | proof error |",
              "|---|---|---|---|---|---|"]
    proofs = report.get("proofs") or {}
    for node, held in sorted((chain.get("nodes") or {}).items()):
        proof = proofs.get(node) or {}
        lines.append(f"| {node} | {held['floor']} | {held['held']} | {cell(held['missing'])} | "
                     f"{cell(proof.get('verified'))} of {cell(proof.get('expected'))} | {cell(proof.get('error'))} |")
    payments = report.get("payments") or {}
    counts = {key: (payments.get(key) or {}).get("count") for key in ("conflicts", "lacking", "off_chain", "moved")}
    lines += ["", f"Payments: {cell(payments.get('corpus'))} in the corpus, {cell(payments.get('final'))} final, "
                  f"{cell(payments.get('final_in_the_run'))} of them in the run; conflicts {cell(counts['conflicts'])}, "
                  f"lacking on a node {cell(counts['lacking'])}, off the chain {cell(counts['off_chain'])}, "
                  f"moved {cell(counts['moved'])}."]
    payers = report.get("payers") or {}
    lines += [f"Payers: {cell(payers.get('tracked'))} addresses expected to hold {cell(payers.get('expected_atoms'))} "
              f"atoms in {cell(payers.get('expected_coins'))} coins, read at height {cell(payers.get('balances_height'))}"
              f"; nodes that differ: {cell(sorted(payers.get('differences') or {}))}."]
    economics = report.get("economics") or {}
    supply = economics.get("supply") or {}
    lines += [f"Economics from height {cell(economics.get('start_height'))} to {cell(economics.get('end_height'))}: "
              f"minted {cell(economics.get('minted'))} (bounds {cell(economics.get('minting_bounds'))}), burned "
              f"{cell(economics.get('burned'))} (expected {cell(economics.get('expected_burn'))}), bonded "
              f"{cell(economics.get('bonded'))}, total supply {cell(supply.get('total_supply'))}, private "
              f"{cell(supply.get('private_supply'))}.", "", "| part | result |", "|---|---|"]
    lines += [f"| {part} | {'PASS' if value else 'FAIL'} |" for part, value in (gate.get("parts") or {}).items()]
    if gate.get("errors"):
        lines += ["", "Errors:", *[f"- {error}" for error in gate["errors"]]]
    return lines + ["", f"The gate took {cell(report.get('seconds'))} s.", ""]


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    out = Path(sys.argv[1])
    lines, runs, every = ["# Determinism and money of the bench runs", ""], {}, True
    for argument in sys.argv[2:]:
        folder = folder_of(argument)
        result_file = folder / "result.json" if folder else None
        result = json.loads(result_file.read_text(encoding="utf-8")) if result_file and result_file.exists() else {}
        name = result.get("scenario") or (folder.name if folder else Path(argument).stem)
        gate = result.get("determinism")
        every &= bool(gate) and gate.get("passed") is True
        runs[name] = {"folder": str(folder) if folder else None, "determinism": gate}
        lines += section(name, gate)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(runs, indent=2) + "\n", encoding="utf-8")
    print(f"{'PASS' if every else 'FAIL'}: {len(runs)} runs in {out}")
    sys.exit(0 if every else 1)


if __name__ == "__main__":
    main()
