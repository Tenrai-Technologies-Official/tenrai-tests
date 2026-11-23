"""Refuses a release from anything but pushed commits, since a release names the commit it comes from."""
from argparse import ArgumentParser
from pathlib import Path
import subprocess
import sys


def git(repository, *arguments):
    return subprocess.run(["git", "-C", str(repository), *arguments], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def changed_paths(repository):
    entries = git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.split("\0")
    paths, index = [], 0
    while index < len(entries) and entries[index]:
        entry = entries[index]
        paths.append(entry[3:])
        # A rename or copy names its old path in the next entry.
        index += 2 if "R" in entry[:2] or "C" in entry[:2] else 1
    return paths


def problems(repository, allowed=()):
    name = Path(repository).name
    head = git(repository, "rev-parse", "HEAD")
    if head.returncode != 0:
        return [f"{name}: no commit to name"]
    found = []
    changed = [path for path in changed_paths(repository) if path not in allowed]
    if changed:
        found.append(f"{name}: {len(changed)} uncommitted files, the first is {changed[0]}")
    upstream = git(repository, "rev-parse", "@{upstream}")
    if upstream.returncode != 0:
        found.append(f"{name}: its branch has no upstream")
    elif upstream.stdout.strip() != head.stdout.strip():
        found.append(f"{name}: HEAD {head.stdout.strip()[:12]} is not the pushed {upstream.stdout.strip()[:12]}")
    return found


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", required=True)
    parser.add_argument("--allow-change", action="append", default=[], metavar="REPO=PATH")
    arguments = parser.parse_args()
    allowed = {}
    for entry in arguments.allow_change:
        repository, path = entry.rsplit("=", 1)
        allowed.setdefault(Path(repository).resolve(), set()).add(path)
    found = [problem for repository in arguments.repo
             for problem in problems(repository, allowed.get(Path(repository).resolve(), ()))]
    for problem in found:
        print(f"release refused: {problem}", file=sys.stderr)
    return 2 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
