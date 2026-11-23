"""Merged per-height timeline of the four validators: python round-timeline.py ROOT [FIRST LAST]

Offsets are milliseconds since the first validator installed the previous cut.
With `summary` as the last argument only one line per height is printed.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\s+\w+\s+(.*)$")
FIELD = re.compile(r"(\w+)=(\S+)")

EVENTS = [
    ("cut installed height=", "INSTALL"),
    ("availability proposal signed", "avail-proposal-signed"),
    ("availability proposal accepted", "avail-proposal-accepted"),
    ("availability proposal waits for local data", "avail-proposal-WAITS-FOR-DATA"),
    ("availability vote signed", "avail-vote-signed"),
    ("availability certified", "avail-certified"),
    ("availability receipt accepted", "avail-receipt-accepted"),
    ("finality proposal signed", "proposal-signed"),
    ("finality proposal accepted", "proposal-accepted"),
    ("finality vote signed", "vote-signed"),
    ("finality vote received", "vote-received"),
    ("finality phase timed out", "TIMEOUT"),
    ("finality certificate installed", "certificate-installed"),
    ("finality joined the view", "JOINED-VIEW"),
    ("finality moved to a later view", "MOVED-VIEW"),
    ("asking peers for work", "ASK-WORK"),
    ("durable write was slow", "slow-write"),
    ("consensus message rejected", "REJECTED"),
    ("availability message rejected", "AVAIL-REJECTED"),
]


def parse(path):
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        match = STAMP.match(line)
        if not match:
            continue
        text = match.group(2)
        for needle, name in EVENTS:
            if needle in text:
                stamp = match.group(1)
                fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in stamp else "%Y-%m-%d %H:%M:%S"
                rows.append((datetime.strptime(stamp, fmt), name, dict(FIELD.findall(text))))
                break
    return rows


def describe(name, fields):
    keep = {
        "INSTALL": ["registered", "millis"],
        "avail-proposal-signed": ["blocks", "millis"],
        "avail-proposal-accepted": ["proposer", "blocks"],
        "avail-vote-signed": ["millis"],
        "avail-certified": ["votes"],
        "proposal-signed": ["view", "build_millis", "millis"],
        "proposal-accepted": ["view", "millis"],
        "vote-signed": ["phase", "view", "nil", "millis"],
        "vote-received": ["signer", "phase", "view", "nil"],
        "TIMEOUT": ["phase", "view", "backoff"],
        "certificate-installed": ["view", "millis"],
        "JOINED-VIEW": ["view"],
        "MOVED-VIEW": ["view"],
        "ASK-WORK": ["blocks"],
        "slow-write": ["millis", "at"],
        "REJECTED": ["kind", "height", "view", "error"],
    }.get(name, [])
    parts = []
    for key in keep:
        if key in fields:
            value = fields[key]
            if key == "at":
                value = value.split("rocks")[-1].lstrip("\\/")
            parts.append(f"{key}={value}")
    return " ".join(parts)


def main():
    root = Path(sys.argv[1])
    args = [arg for arg in sys.argv[2:] if arg != "summary"]
    summary = "summary" in sys.argv[2:]
    logs = {}
    for index in range(1, 5):
        for candidate in [root / f"v{index}" / "node.log", root / f"v{index}.log"]:
            if candidate.exists():
                logs[index] = parse(candidate)
    installs = {}
    for index, rows in logs.items():
        for when, name, fields in rows:
            if name == "INSTALL":
                installs.setdefault(int(fields["height"]), {})[index] = when
    heights = sorted(installs)
    first = int(args[0]) if args else heights[0] + 1
    last = int(args[1]) if len(args) > 1 else heights[-1]
    previous_start = None
    for height in range(first, last + 1):
        if height - 1 not in installs or height not in installs:
            continue
        start = min(installs[height - 1].values())
        end = max(installs[height].values())
        merged = []
        for index, rows in logs.items():
            low = installs[height - 1].get(index, start)
            high = installs[height].get(index, end)
            for when, name, fields in rows:
                if low < when <= high or (name == "INSTALL" and when == high):
                    if name == "INSTALL" and int(fields["height"]) != height:
                        continue
                    merged.append((when, index, name, fields))
        merged.sort(key=lambda row: row[0])
        took = (min(installs[height].values()) - start).total_seconds()
        views = sorted({fields.get("view") for _, _, name, fields in merged
                        if name == "certificate-installed"})
        nil = sum(1 for _, _, name, fields in merged
                  if name == "vote-signed" and fields.get("nil") == "true")
        timeouts = sum(1 for _, _, name, _ in merged if name == "TIMEOUT")
        slow = [int(fields.get("millis", 0)) for _, _, name, fields in merged if name == "slow-write"]
        print(f"height {height}: {took:.2f} s, view {','.join(view or '?' for view in views)}, "
              f"{nil} nil votes, {timeouts} timeouts, {len(slow)} slow writes"
              + (f" (max {max(slow)} ms)" if slow else ""))
        if summary:
            continue
        for when, index, name, fields in merged:
            if name == "vote-received":
                continue
            offset = int((when - start).total_seconds() * 1000)
            print(f"  +{offset:5d} v{index} {name} {describe(name, fields)}")


main()
