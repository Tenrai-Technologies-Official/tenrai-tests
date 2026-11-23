"""The source state of a release, without version control: py -3.14 source_state.py OUT.json REPO...

For each repository, the SHA-256 of every source file (build output, dependencies and editor folders left out) and
one digest over the sorted list of paths and file hashes. Two trees with the same digest hold the same sources, so a
release names exactly what it was built from, and a later change shows as another digest.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

SKIP_DIRS = {"target", "node_modules", ".git", "dist", ".vite", "__pycache__", ".astro", "run", "run-build", "results",
             "bin-main", "pkg-threads"}
SKIP_SUFFIXES = (".exe", ".pdb", ".dll", ".log", ".sqlite", ".vault", ".pass", ".token", ".key", ".seed", ".sealed")


def files(root):
    for directory, names, entries in os.walk(root):
        names[:] = sorted(name for name in names if name not in SKIP_DIRS and not name.startswith("bin-"))
        for entry in sorted(entries):
            if entry.endswith(SKIP_SUFFIXES):
                continue
            path = Path(directory) / entry
            yield path.relative_to(root).as_posix(), path


def state(root):
    root = Path(root)
    listed = {}
    unreadable = []
    for relative, path in files(root):
        digest = hashlib.sha256()
        # A file this account may not read is not a source it builds from, such as a test wallet left by another
        # account; it is named, not hashed, so the report shows what the digest leaves out.
        try:
            with open(path, "rb") as file:
                for block in iter(lambda: file.read(1 << 20), b""):
                    digest.update(block)
        except PermissionError:
            unreadable.append(relative)
            continue
        listed[relative] = digest.hexdigest()
    tree = hashlib.sha256("".join(f"{path}\0{digest}\n" for path, digest in sorted(listed.items())).encode())
    return {"root": str(root), "files": len(listed), "digest": tree.hexdigest(), "unreadable": unreadable,
            "hashes": listed}


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    out = Path(sys.argv[1])
    report = {"taken": time.strftime("%Y-%m-%d %H:%M:%S"), "repositories": {}}
    for root in sys.argv[2:]:
        report["repositories"][Path(root).name] = state(root)
        entry = report["repositories"][Path(root).name]
        print(f"{entry['digest'][:16]}  {entry['files']:6} files  {root}")
        for relative in entry["unreadable"]:
            print(f"    not readable, left out: {relative}")
    out.write_text(json.dumps(report, indent=1) + "\n")


if __name__ == "__main__":
    main()
