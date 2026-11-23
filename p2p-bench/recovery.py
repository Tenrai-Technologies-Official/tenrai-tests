"""How a validator that fell behind came back: py -3.14 recovery.py RESULT_FOLDER [VALIDATOR]

Reads every validator's debug log of one run. For each height it notes when and how a node
installed it: through its own finality ("finality certificate installed") or as a sync cut ("sync
cut applied"). The network head at any moment is the highest height another validator has
installed. For the starved validator of a degrade run (or the one named) it prints the largest
gap to that head, when it reached the head again after its release, and when it rejoined the
finality: the first of REJOIN_RUN heights in a row it installed through its own certificate,
together with the share of later heights it took part in with a non-nil precommit.
"""
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

REJOIN_RUN = 5
INSTALLED = re.compile(r"^\S+ (\S+) .* cut installed height=(\d+)")
FINALITY = re.compile(r"^\S+ (\S+) .* finality certificate installed height=(\d+)")
SYNCED = re.compile(r"^\S+ (\S+) .* sync cut applied height=(\d+)")
PRECOMMIT = re.compile(r"^\S+ (\S+) .* finality vote signed phase=Precommit height=(\d+) view=(\d+) nil=(true|false)")
PROPOSAL_TIMEOUT = re.compile(r"^\S+ \S+ .* finality phase timed out phase=Proposal height=(\d+) view=(\d+)")
TOOK_AVAILABILITY = re.compile(r"^\S+ \S+ .* finality took an availability proposal height=(\d+)")
WAITS_FOR_DATA = re.compile(r"^\S+ (\S+) .* availability proposal waits for local data")
WANTED_HELD = re.compile(r"^\S+ (\S+) .* public swarm holds a block its finality waited for .*wanted_ms=(\d+)"
                         r"(?: queued_ms=(\d+))?")


def seconds(clock):
    hours, minutes, rest = clock.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def read(log):
    installed, finality, synced, precommits = {}, set(), set(), defaultdict(set)
    waits, wanted, nils, timeouts, data_heights = [], [], [], set(), set()
    availability_height = None
    for line in log.read_text(errors="replace").splitlines():
        if match := INSTALLED.match(line):
            installed.setdefault(int(match.group(2)), seconds(match.group(1)))
        elif match := FINALITY.match(line):
            finality.add(int(match.group(2)))
        elif match := SYNCED.match(line):
            synced.add(int(match.group(2)))
        elif match := PRECOMMIT.match(line):
            height, view, nil = int(match.group(2)), int(match.group(3)), match.group(4) == "true"
            precommits[height].add(not nil)
            if nil:
                nils.append((seconds(match.group(1)), height, view))
        elif match := PROPOSAL_TIMEOUT.match(line):
            timeouts.add((int(match.group(1)), int(match.group(2))))
        elif match := TOOK_AVAILABILITY.match(line):
            availability_height = int(match.group(1))
        elif match := WAITS_FOR_DATA.match(line):
            waits.append(seconds(match.group(1)))
            if availability_height is not None:
                data_heights.add(availability_height)
        elif match := WANTED_HELD.match(line):
            queued = int(match.group(3)) if match.group(3) else None
            wanted.append((seconds(match.group(1)), int(match.group(2)), queued))
    return {"installed": installed, "finality": finality, "synced": synced, "precommits": precommits,
            "waits": waits, "wanted": wanted, "nils": nils, "timeouts": timeouts,
            "data_heights": data_heights}


def nil_causes(own, since=None):
    """Nil precommits by what preceded them: a proposal of that height whose blocks the node
    lacked (its proposal timer may run out meanwhile), no proposal in time, or anything else,
    such as a proposal that came after the prevote quorum."""
    causes = {"missing blocks": 0, "no proposal": 0, "other": 0}
    for at, height, view in own["nils"]:
        if since is not None and at < since:
            continue
        if height in own["data_heights"]:
            causes["missing blocks"] += 1
        elif (height, view) in own["timeouts"]:
            causes["no proposal"] += 1
        else:
            causes["other"] += 1
    return ", ".join(f"{cause} {count}" for cause, count in causes.items())


def percentiles(values):
    values = sorted(values)
    if not values:
        return "none"
    pick = lambda share: values[min(len(values) - 1, int(share * len(values)))]
    return f"median {pick(0.5)} ms, p90 {pick(0.9)} ms, max {values[-1]} ms ({len(values)})"


def local_seconds(unix):
    """Seconds since local midnight, the clock the logs are written in."""
    moment = time.localtime(unix)
    return moment.tm_hour * 3600 + moment.tm_min * 60 + moment.tm_sec + unix % 1


def clock(at):
    at %= 86400
    return f"{int(at // 3600):02d}:{int(at % 3600 // 60):02d}:{at % 60:06.3f}"


def main():
    folder = Path(sys.argv[1])
    result = json.loads((folder / "result.json").read_text())
    nodes = sorted(path.stem for path in folder.glob("v*.log"))
    logs = {node: read(folder / f"{node}.log") for node in nodes}
    degrade = next((outage for outage in result.get("outages") or [] if outage.get("kind") == "degrade"), None)
    starved = sys.argv[2] if len(sys.argv) > 2 else (f"v{degrade['validator']}" if degrade else None)
    print(f"{folder.name}: cuts/min {result['cuts_per_min']:.1f}, unique final TPS "
          f"{result.get('finalized_unique_tps', 0):.0f}")
    for node in nodes:
        own = logs[node]
        nil = sum(1 for votes in own["precommits"].values() if False in votes)
        print(f"  {node}: {len(own['installed'])} cuts installed, {len(own['finality'])} through its own "
              f"finality, {len(own['synced'])} as sync cuts; {nil} heights with a nil precommit "
              f"(views by cause: {nil_causes(own)}), {len(own['waits'])} proposals waiting for its "
              "local data")
        if own["wanted"]:
            totals = [total for _, total, _ in own["wanted"]]
            queued = [wait for _, _, wait in own["wanted"] if wait is not None]
            fetch = [total - wait for _, total, wait in own["wanted"] if wait is not None]
            print(f"      blocks its finality waited for: in all {percentiles(totals)}; "
                  f"fetching {percentiles(fetch)}; queued to admitted {percentiles(queued)}")
    if not starved:
        return
    own = logs[starved]
    others = [logs[node] for node in nodes if node != starved]
    network = {}
    for other in others:
        for height, at in other["installed"].items():
            network[height] = min(network.get(height, at), at)
    # The nodes start from snapshots at their own pace; the gap counts from the load on.
    start = local_seconds(result["window"][0]) if result.get("window") else 0
    begun = {"network": max((height for height, at in network.items() if at < start), default=0),
             "own": max((height for height, at in own["installed"].items() if at < start), default=0)}
    events = sorted([(at, "network", height) for height, at in network.items() if at >= start]
                    + [(at, "own", height) for height, at in own["installed"].items() if at >= start])
    head = dict(begun)
    gap, gap_at = 0, None
    for at, who, height in events:
        head[who] = max(head[who], height)
        if head["network"] - head["own"] > gap:
            gap, gap_at = head["network"] - head["own"], at
    print(f"  {starved}: largest gap {gap} cuts" + (f" at {clock(gap_at)}" if gap_at else ""))
    if degrade:
        down, up = local_seconds(degrade["down"]), local_seconds(degrade["up"])
        print(f"    starved {clock(down)} to {clock(up)}, at its release {degrade['height_at_restore']} against "
              f"{degrade['others_at_restore']}")
        head = dict(begun)
        reached = None
        for at, who, height in events:
            head[who] = max(head[who], height)
            if at >= up and head["own"] >= head["network"] and reached is None:
                reached = at
        print("    reached the head " + (f"{reached - up:.1f} s after its release at {clock(reached)}"
                                         if reached else "not again in the run"))
        released = {height for at, height, _ in own["nils"] if at >= up}
        print(f"    after its release {len(released)} heights with a nil precommit "
              f"(views by cause: {nil_causes(own, since=up)})")
        later = sorted(height for height, at in own["installed"].items() if at >= up)
        rejoined = next((height for height in later
                         if all(height + step in own["finality"] for step in range(REJOIN_RUN))), None)
        if rejoined is None:
            print(f"    did not install {REJOIN_RUN} heights in a row through its own finality after its release")
        else:
            at = own["installed"][rejoined]
            after = [height for height in later if height >= rejoined]
            final = sum(height in own["finality"] for height in after)
            voted = sum(any(own["precommits"].get(height, ())) for height in after)
            print(f"    rejoined the finality at height {rejoined}, {at - up:.1f} s after its release; of the "
                  f"{len(after)} heights from there {final} through its own certificate, {voted} with a "
                  "non-nil precommit")


if __name__ == "__main__":
    main()
