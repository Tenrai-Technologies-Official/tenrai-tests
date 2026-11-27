"""Copies the built wallet WASM package to the web wallet and the SDK and writes the SDK's pin from its release.

    py -3.14 vendor_wasm.py
"""

import json
import re
import shutil
from pathlib import Path

from node_checkout import node_checkout

GITHUB = Path(__file__).resolve().parents[2]
REPO = node_checkout(GITHUB)
PACKAGE = REPO / "crates" / "wasm" / "pkg"
FILES = ("README.md", "browser-source-inputs.json", "package.json", "release.json", "tenrai_wasm.d.ts",
         "tenrai_wasm.js", "tenrai_wasm_bg.wasm", "tenrai_wasm_bg.wasm.d.ts")

release = json.loads((PACKAGE / "release.json").read_text(encoding="utf-8-sig"))
wasm = release["assets"]["wasm"]["sha256"]
for target in (GITHUB / "tenrai-webwallet" / "vendor" / "tenrai-wasm", GITHUB / "tenrai-sdk" / "wasm"):
    for name in FILES:
        shutil.copyfile(PACKAGE / name, target / name)

runtime = GITHUB / "tenrai-sdk" / "src" / "wallet" / "runtime.ts"
text = runtime.read_text(encoding="utf-8")
changed, count = re.subn(r'const WASM_SHA256 = "[0-9a-f]{64}";', f'const WASM_SHA256 = "{wasm}";', text)
if count != 1:
    raise SystemExit("the SDK's WASM pin was not found")
runtime.write_text(changed, encoding="utf-8", newline="\n")
print(json.dumps({"revision": release["revision"], "wasm": wasm, "module": release["assets"]["module"]["sha256"]}))
