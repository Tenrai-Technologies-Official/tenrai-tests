"""Freezes a build for the benchmarks: py -3.14 freeze.py VARIANT [SOURCE] [--replace]

Copies tenraid, tenrai-release, tenrai-wallet and tenrai-miner from SOURCE (default: the node repository's
target/release) into bin-VARIANT/, read-only, with manifest.json holding each file's SHA-256. bench.py runs
only binaries of such a folder and checks every file against the manifest before it copies them, so a later
build in the repository cannot change what a run measures. An existing folder is replaced only with
--replace, since earlier results name its hashes.
"""

import os
import sys
from pathlib import Path

from preflight import BINARIES, freeze

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS.parent))
from release.node_checkout import node_checkout

REPO = node_checkout(TOOLS.parents[1])


def main():
    arguments = [argument for argument in sys.argv[1:] if not argument.startswith("--")]
    if not arguments:
        raise SystemExit(__doc__)
    variant = arguments[0]
    source = Path(arguments[1]) if len(arguments) > 1 else REPO / "target" / "release"
    folder = TOOLS / f"bin-{variant}"
    if folder.exists() and "--replace" not in sys.argv:
        raise SystemExit(f"{folder} exists; earlier results name its hashes, so replace it only with --replace")
    manifest = freeze({name: source / name for name in BINARIES}, folder)
    for name, digest in manifest["files"].items():
        print(f"{digest[:16]}  {name}")
    print("frozen", folder)


if __name__ == "__main__":
    main()
