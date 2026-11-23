"""Whether bench runs passed, from their result.json alone and never from an exit code:
    py -3.14 verdict_check.py RESULT_DIR_OR_RUN_OUTPUT ...
A run output (the .out of a bench.py run) leads to the folder it names. A load run passes only on verdict.passed, a
crash run only on its passed, and a run that broke off or wrote no verdict fails. One line per run; exits 0 only if
every run passed."""

import json
import re
import sys
from pathlib import Path

NAMED = re.compile(r"(?:results in|logs in) (\S.*?)\s*$")


def folder_of(path):
    path = Path(path)
    if path.is_dir():
        return path
    found = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = NAMED.search(line)
        if match:
            found = Path(match.group(1))
    return found


def judge(folder):
    """(passed, what to say) for one result folder."""
    if folder is None:
        return False, "NO RESULT: the run named no result folder"
    path = folder / "result.json"
    if not path.exists():
        return False, f"NO RESULT: {path} is missing"
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("aborted"):
        return False, f"ABORTED: {result['aborted'][:200]}"
    verdict = result.get("verdict")
    if isinstance(verdict, dict) and isinstance(verdict.get("passed"), bool):
        if verdict["passed"]:
            return True, "PASS"
        failed = [name for name, passed in (verdict.get("parts") or {}).items() if not passed]
        return False, "FAIL: " + (", ".join(failed) or "verdict not passed")
    if isinstance(result.get("passed"), bool):
        return result["passed"], "PASS" if result["passed"] else "FAIL"
    return False, "NO VERDICT in result.json"


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    every = True
    for argument in sys.argv[1:]:
        folder = folder_of(argument)
        passed, said = judge(folder)
        every &= passed
        print(f"{said:<60} {folder.name if folder else argument}")
    sys.exit(0 if every else 1)


if __name__ == "__main__":
    main()
