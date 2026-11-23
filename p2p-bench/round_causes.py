"""Where the time of each height went and why a height left view 0: py -3.14 round_causes.py RESULT_FOLDER

Reads the debug logs of all validators of one run. They share a clock, so the steps of one height
line up across them: the cut before was installed, availability was certified, the next cut's
prefix was executed, the proposer built and signed the view 0 proposal, the others checked it,
votes made the certificate, the cut was installed.

A height counts as failed in view 0 only when its certificate carries a later view. A phase timeout
alone says nothing: it also fires after a proposal that arrived just in time. A failed height is
put down to the first of these that holds:

  data         an availability proposal named blocks a validator did not hold before it timed out
  no_proposal  the proposer signed no view 0 proposal
  late_proposal_insufficient_vote_budget
               the proposal was signed, but fewer than a quorum had checked it when their proposal
               wait ended, so too few could vote for it
  votes        a quorum had checked the proposal in time and the view closed anyway

The table gives every step in ms after the proposer opened the height. The summary splits the time
between the prefix being ready and the proposal being checked by a quorum.
"""

import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+\w+\s+\S+\s+(.*)$")
FIELD = re.compile(r"(\w+)=(\S+)")


def read(log):
    events = []
    with open(log, errors="replace") as file:
        for line in file:
            match = STAMP.match(line)
            if match:
                when = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp()
                events.append((when, match.group(2), dict(FIELD.findall(match.group(2)))))
    return events


def heights(events):
    """What one validator logged at each height, by the cut installs around it."""
    rounds, height = {}, None
    for when, text, fields in events:
        if text.startswith("cut installed"):
            installed = int(fields["height"])
            if installed in rounds:
                rounds[installed]["installed"] = when
                rounds[installed]["install_ms"] = int(fields["millis"])
            height = installed + 1
            rounds[height] = {"opened": when, "timeouts": [], "data": [], "used": []}
            continue
        if text.startswith("finality certificate installed"):
            done = rounds.get(int(fields["height"]))
            if done is not None:
                done["view"], done["certificate"] = int(fields["view"]), when - int(fields["millis"]) / 1000
            continue
        if height is None:
            continue
        current = rounds[height]
        named = int(fields["height"]) if "height" in fields else None
        first_view = fields.get("view") == "0"
        if text.startswith(("availability proposal accepted", "availability proposal signed")):
            current.setdefault("available_proposal", when)
        elif text.startswith("availability certified"):
            current.setdefault("certified", when)
        elif text.startswith("availability proposal waits for local data"):
            current["data"].append(when)
        elif named != height:
            continue
        elif text.startswith("cut prefix executed ahead"):
            current["prefix_done"], current["prefix_ms"] = when, int(fields["millis"])
        elif text.startswith("cut prefix used"):
            current["used"].append((when, fields["source"], int(fields["millis"])))
        elif text.startswith("finality proposal signed") and first_view:
            current["signed"] = (when, int(fields["build_millis"]), int(fields["millis"]))
        elif text.startswith("finality proposal accepted") and first_view:
            current.setdefault("accepted", (when, int(fields["millis"])))
        elif text.startswith("finality phase timed out") and first_view:
            current["timeouts"].append((when, fields["phase"]))
        elif text.startswith("finality vote signed") and first_view:
            current.setdefault(fields["phase"].lower(), (when, fields["nil"] == "true"))
    return {height: entry for height, entry in rounds.items() if "installed" in entry and "view" in entry}


def proposal_wait_end(entry):
    return min((when for when, phase in entry["timeouts"] if phase == "Proposal"), default=None)


def checked_in_time(entry):
    """Whether this validator had the view 0 proposal before its proposal wait ended."""
    if "signed" in entry:
        return True
    if "accepted" not in entry:
        return False
    end = proposal_wait_end(entry)
    return end is None or entry["accepted"][0] <= end


def cause(nodes, quorum):
    if all(entry["view"] == 0 for entry in nodes.values()):
        return None
    for entry in nodes.values():
        end = proposal_wait_end(entry)
        if end is not None and any(when <= end + 0.05 for when in entry["data"]):
            return "data"
    if not any("signed" in entry for entry in nodes.values()):
        return "no_proposal"
    if sum(checked_in_time(entry) for entry in nodes.values()) < quorum:
        return "late_proposal_insufficient_vote_budget"
    return "votes"


def prefix_wait(entry, before):
    """How long the build or the check waited for the prefix, from the last use logged before it."""
    waits = [(when, millis) for when, source, millis in entry["used"] if when <= before and source == "awaited"]
    return max(waits)[1] if waits else 0


def steps(nodes, quorum):
    """The path of one height, in ms after its proposer opened it."""
    proposer = next((name for name, entry in nodes.items() if "signed" in entry), None)
    if proposer is None:
        return None
    lead = nodes[proposer]
    zero = lead["opened"]
    after = lambda when: (when - zero) * 1000
    signed, build_ms, total_ms = lead["signed"]
    waited = prefix_wait(lead, signed)
    path = {
        "proposer": proposer,
        "certified": after(lead["certified"]) if "certified" in lead else None,
        "prefix_ms": lead.get("prefix_ms"),
        "prefix_done": after(lead["prefix_done"]) if "prefix_done" in lead else None,
        "build_start": after(signed) - total_ms,
        "prefix_wait": waited,
        "rest": build_ms - waited,
        "sign": total_ms - build_ms,
        "signed": after(signed),
    }
    checks = []
    for name, entry in nodes.items():
        if name == proposer or "accepted" not in entry:
            continue
        accepted, check_ms = entry["accepted"]
        own_wait = prefix_wait(entry, accepted)
        end = proposal_wait_end(entry)
        checks.append({
            "at": after(accepted),
            "transit": after(accepted) - check_ms - after(signed),
            "prefix_wait": own_wait,
            "check": check_ms - own_wait,
            "slack": (end - accepted) * 1000 if end is not None else None,
        })
    checks.sort(key=lambda check: check["at"])
    path["checks"] = checks
    path["quorum"] = checks[quorum - 2] if len(checks) >= quorum - 1 else None
    path["certificate"] = after(min(entry["certificate"] for entry in nodes.values()))
    path["installed"] = after(sorted(entry["installed"] for entry in nodes.values())[quorum - 1])
    return path


def spread(values):
    values = sorted(value for value in values if value is not None)
    if not values:
        return "      -"
    pick = lambda share: values[min(len(values) - 1, int(len(values) * share))]
    return f"{pick(0.5):6.0f} {pick(0.9):6.0f} {values[-1]:6.0f}"


def show(value, width=6):
    return f"{value:{width}.0f}" if value is not None else " " * (width - 1) + "-"


def main():
    folder = Path(sys.argv[1])
    result = json.loads((folder / "result.json").read_text())
    window = result.get("window") or (0, float("inf"))
    logs = {log.stem: heights(read(log)) for log in sorted(folder.glob("v*.log"))}
    quorum = len(logs) * 2 // 3 + 1
    shared = sorted(set.intersection(*(set(rounds) for rounds in logs.values())))
    shared = [height for height in shared
              if all(window[0] <= rounds[height]["opened"] <= window[1] for rounds in logs.values())]

    print(f"== {folder}: {len(shared)} heights, {len(logs)} validators, quorum {quorum}")
    print("   ms after the proposer opened the height")
    print("   height view  by   avail.   prefix    prefix   build  proposal  checked by   least  cert.  installed  cause")
    print("                     certif.  took      done     start  signed    a quorum     slack")
    causes, paths, failed = Counter(), [], []
    for height in shared:
        nodes = {name: rounds[height] for name, rounds in logs.items()}
        reason = cause(nodes, quorum)
        causes[reason or "view 0"] += 1
        path = steps(nodes, quorum)
        view = max(entry["view"] for entry in nodes.values())
        if path is None:
            print(f"   {height:6d} {view:4d}  -    no view 0 proposal was signed{'':52} {reason or ''}")
            continue
        slacks = [check["slack"] for check in path["checks"] if check["slack"] is not None]
        at_quorum = path["quorum"]["at"] if path["quorum"] else None
        print(f"   {height:6d} {view:4d}  {path['proposer']:3}  {show(path['certified'])}  {show(path['prefix_ms'])}"
              f"    {show(path['prefix_done'])}  {show(path['build_start'])}  {show(path['signed'])}"
              f"      {show(at_quorum)}    {show(min(slacks) if slacks else None, 7)} {show(path['certificate'])}"
              f"     {show(path['installed'])}  {reason or ''}")
        (failed if reason else paths).append(path)

    print(f"\n   causes: {dict(causes)}")
    for label, group in (("heights that finished in view 0", paths), ("heights that left view 0", failed)):
        if not group:
            continue
        quorums = [path["quorum"] for path in group if path["quorum"]]
        print(f"\n   {label}: {len(group)}                              median    p90    max  (ms)")
        rows = [
            ("opened -> availability certified", [path["certified"] for path in group]),
            ("opened -> prefix done", [path["prefix_done"] for path in group]),
            ("   prefix took", [path["prefix_ms"] for path in group]),
            ("opened -> build starts", [path["build_start"] for path in group]),
            ("prefix done -> proposal signed", [path["signed"] - path["prefix_done"] for path in group
                                                if path["prefix_done"] is not None]),
            ("   build waited for the prefix", [path["prefix_wait"] for path in group]),
            ("   rest of the cut", [path["rest"] for path in group]),
            ("   signing and storing", [path["sign"] for path in group]),
            ("proposal signed -> checked by a quorum", [check["at"] - path["signed"] for path in group
                                                        if (check := path["quorum"])]),
            ("   on the way and queued", [check["transit"] for check in quorums]),
            ("   check waited for the prefix", [check["prefix_wait"] for check in quorums]),
            ("   check", [check["check"] for check in quorums]),
            ("checked by a quorum -> certificate", [path["certificate"] - check["at"] for path in group
                                                    if (check := path["quorum"])]),
            ("certificate -> installed", [path["installed"] - path["certificate"] for path in group]),
            ("opened -> installed", [path["installed"] for path in group]),
        ]
        for name, values in rows:
            print(f"   {name:58} {spread(values)}")


if __name__ == "__main__":
    main()
