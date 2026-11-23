"""Puts the proofs of a browser run before the verifier a node runs: each must be accepted by exactly one pinned
circuit for its statement, and neither a changed proof nor another statement's digest by any.

    py -3 verify-results.py RESULTS_JSON DIGESTS_LOG

DIGESTS_LOG is what `bench-private prove` printed; its lines name the digests in the order of the inputs.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "p2p-bench" / "bench-private" / "target" / "release" / "bench-private.exe"


def accepted(digest, proof_hex):
    with tempfile.NamedTemporaryFile("w", suffix=".hex", delete=False) as file:
        file.write(proof_hex)
    try:
        answer = subprocess.run([str(TOOL), "verify-proof", digest, file.name], capture_output=True, text=True, check=True)
        return json.loads(answer.stdout)["accepted_by"]
    finally:
        Path(file.name).unlink()


def changed(proof_hex, position):
    digit = "0" if proof_hex[position] != "0" else "1"
    return proof_hex[:position] + digit + proof_hex[position + 1:]


report = json.loads(Path(sys.argv[1]).read_text())
digests = re.findall(r"^\d+ \w+: ([0-9a-f]{96}) in ", Path(sys.argv[2]).read_text(encoding="utf-8-sig", errors="replace"), re.MULTILINE)
if len(digests) != len(report["results"]):
    raise SystemExit(f"{len(digests)} digests for {len(report['results'])} inputs")
results = report["results"]
failed = 0
for index, entry in enumerate(results):
    digest = digests[index]
    if not entry["ok"]:
        print(entry["name"], "no proof:", entry.get("error"))
        failed += 1
        continue
    proof = entry["proof_hex"]
    good = accepted(digest, proof)
    tampered = [accepted(digest, changed(proof, position)) for position in (len(proof) // 3, len(proof) // 2, len(proof) - 2)]
    other = accepted(digests[(index + 1) % len(digests)], proof)
    passed = len(good) == 1 and not any(tampered) and not other
    failed += not passed
    print(f"{entry['name']}: accepted by {good}; {len(tampered)} changed copies accepted by {sum(map(bool, tampered))}; "
          f"with another statement's digest by {other}: {'PASS' if passed else 'FAIL'}")
sys.exit(1 if failed else 0)
