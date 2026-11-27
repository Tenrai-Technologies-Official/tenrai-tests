"""Copies the browser prover's package to the web wallet and the SDK and writes the SDK's pins from its release.

    py -3.14 vendor_prover.py
"""

import json
import shutil
from pathlib import Path

from node_checkout import node_checkout

GITHUB = Path(__file__).resolve().parents[2]
REPO = node_checkout(GITHUB)
PROVER = REPO / "crates/tsp2/prover-wasm"
if not (PROVER / "Cargo.toml").is_file():
    PROVER = REPO / "crates/tsp2-prover-wasm"
PACKAGE = PROVER / "package"

release = json.loads((PACKAGE / "release.json").read_text(encoding="utf-8-sig"))
names = {path.relative_to(PACKAGE).as_posix() for path in PACKAGE.rglob("*") if path.is_file()}
for needed in release["files"]:
    if needed not in names:
        raise SystemExit(f"the package misses {needed}")

for target in (GITHUB / "tenrai-webwallet" / "vendor" / "tenrai-prover", GITHUB / "tenrai-sdk" / "prover"):
    if target.exists():
        for old in [path for path in target.rglob("*") if path.is_file()]:
            if old.relative_to(target).as_posix() not in names:
                old.unlink()
    for name in sorted(names):
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PACKAGE / name, destination)

lines = "".join(f"  {json.dumps(name)}: {json.dumps(release['files'][name])},\n" for name in sorted(release["files"]))
pins = (
    "/** SHA-256 of every file of the `prover` folder, the reviewed build in `prover/release.json`. */\n"
    "export const PROVER_FILES: Readonly<Record<string, string>> = {\n" + lines + "};\n"
)
(GITHUB / "tenrai-sdk" / "src" / "wallet" / "prover-pins.ts").write_text(pins, encoding="utf-8", newline="\n")
print(json.dumps({key: value for key, value in release.items() if key != "files"}))
print("vendored", len(names), "files")
