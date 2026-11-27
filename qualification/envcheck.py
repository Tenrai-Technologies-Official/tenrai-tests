"""Checks the machine before a test series that is not a bench run or a test binary filter, such as the browser
rounds or a build: py -3.14 envcheck.py [options]

  --frozen FOLDER     a folder frozen by p2p-bench/freeze.py or build-variant.py; every file must match
  --file PATH=SHA256  a single binary that must have this hash (a prefix of 16 or more hex digits will do)
  --files-from LIST   a file of PATH=SHA256 lines, each checked as --file
  --commit-gb N       free commit needed (default 8)
  --room PATH=GB      free room needed on the disk of PATH, any number of times
  --allow NAME        a process image that may run beside the series, any number of times
  --holder NAME       the series a hold of the machine was placed for

Exit code 0 when every check passes, 3 with each reason printed when one fails. No build, bench, node, prover,
test binary or test browser may run, except the images --allow names.

While the hold file exists, only its holder can start a series until the stated expiry.
Set TENRAI_TEST_HOLD to use a different hold file.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p2p-bench"))
import preflight  # noqa: E402


HOLD = Path(os.environ.get("TENRAI_TEST_HOLD", Path(__file__).resolve().with_name("run") / "hold.json"))


def held(holder):
    """Why a hold keeps this series from starting, if one does."""
    try:
        hold = json.loads(HOLD.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return [f"{HOLD} cannot be read, so it holds the machine until it is fixed or removed"]
    until = str(hold.get("until", ""))
    if until and time.strftime("%Y-%m-%dT%H:%M:%S") >= until:
        return []
    if holder is not None and holder == hold.get("holder"):
        return []
    return [f"the machine is held for {hold.get('for', 'a serial test sequence')} until "
            f"{until or 'the hold is removed'}: start no build, test or bench beside it ({HOLD})"]


def main():
    arguments = sys.argv[1:]
    frozen, files, rooms, allowed, commit, holder = [], [], [], set(), 8.0, None
    for key, value in zip(arguments[::2], arguments[1::2]):
        if key == "--frozen":
            frozen.append(value)
        elif key == "--file":
            path, digest = value.rsplit("=", 1)
            files.append((path, digest.lower()))
        elif key == "--files-from":
            for line in Path(value).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    path, digest = line.rsplit("=", 1)
                    files.append((path, digest.lower()))
        elif key == "--commit-gb":
            commit = float(value)
        elif key == "--room":
            path, gb = value.rsplit("=", 1)
            rooms.append((path, float(gb)))
        elif key == "--allow":
            allowed.add(value.lower())
        elif key == "--holder":
            holder = value
        else:
            raise SystemExit(__doc__)
    reasons = held(holder)
    for folder in frozen:
        reasons += preflight.frozen(folder)[1]
    for path, digest in files:
        if not Path(path).exists():
            reasons.append(f"{path} is missing")
        elif len(digest) < 16 or not preflight.sha256(path).startswith(digest):
            reasons.append(f"{path} does not have the hash {digest}")
    memory = preflight.memory()
    if memory["free_commit_gb"] < commit:
        reasons.append(f"{memory['free_commit_gb']} GB of commit free, {commit} GB needed")
    for path, gb in rooms:
        free = shutil.disk_usage(path).free / 2**30
        if free < gb:
            reasons.append(f"{free:.1f} GB free on {path}, {gb} GB needed")
    others = [found for found in preflight.competing([]) if found.split(" ")[0].lower() not in allowed]
    if others:
        reasons.append("other processes would share the machine: " + ", ".join(others))
    for reason in reasons:
        print("preflight refused:", reason)
    state = f"commit free {memory['free_commit_gb']} GB, RAM free {memory['free_physical_gb']} GB"
    print(("preflight passed: " if not reasons else "preflight state: ") + state)
    sys.exit(3 if reasons else 0)


if __name__ == "__main__":
    main()
