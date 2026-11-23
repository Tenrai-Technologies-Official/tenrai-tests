"""Copies the SDK's package files into a reference app's vendor folder and its installed copy, as an install of the
vendored package would; files the package no longer has are removed from both.

    py -3.14 vendor_sdk.py tenrai-reference-webwallet
"""

import json
import shutil
import sys
from pathlib import Path

GITHUB = Path(__file__).resolve().parents[2]
SDK = GITHUB / "tenrai-sdk"


def package_files():
    package = json.loads((SDK / "package.json").read_text(encoding="utf-8"))
    names = {"package.json", "LICENSE"}
    for entry in package["files"]:
        path = SDK / entry
        if path.is_dir():
            names.update(item.relative_to(SDK).as_posix() for item in path.rglob("*") if item.is_file())
        elif path.is_file():
            names.add(entry)
        else:
            raise SystemExit(f"the SDK misses {entry}")
    return sorted(names)


def mirror(target, names):
    if target.exists():
        for old in [path for path in target.rglob("*") if path.is_file()]:
            relative = old.relative_to(target).as_posix()
            if relative not in names and not relative.startswith("node_modules/"):
                old.unlink()
    for name in names:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SDK / name, destination)


if len(sys.argv) != 2:
    raise SystemExit(__doc__)
app = GITHUB / sys.argv[1]
names = package_files()
mirror(app / "vendor" / "tenrai-sdk", names)
mirror(app / "node_modules" / "@tenrai" / "sdk", names)
print(f"{app.name}: {len(names)} files")
