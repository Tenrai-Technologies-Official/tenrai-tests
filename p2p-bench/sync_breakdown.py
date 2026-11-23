"""Where a starved validator's catch-up spends its time: py -3.14 sync_breakdown.py RESULT_FOLDER [VALIDATOR]

For the starved validator of a degrade run, or the one named, from its release on: each sync cut's
fetch, the checks after its last body, the wait for the install and the install itself; each sync
body answer, by the peer that served it; on every serving peer how long its requests waited for a
serve worker, how long the read took and how long the answer waited for the swarm loop; the bytes
each link carried to and from it; and CPU and memory of every node. Runs before the per-request
logs existed show only the answers that took two seconds or more.
"""
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

LISTENING = re.compile(r"listening for peers on .*/p2p/(\w+)")
CLOCK = re.compile(r"^\S+ (\d\d:\d\d:\d\d\.\d+) ")
APPLIED = re.compile(r"sync cut applied height=(\d+) bodies=(\d+) prefetched=(\d+) requests=(\d+) "
                     r"refusals=(\d+) plan=(\d+) fetched=(\d+) staged=(\d+) checking=(\d+) pow=(\d+) "
                     r"prepare_wait=(\d+) prepare=(\d+) authorize=(\d+) commit_wait=(\d+) commit=(\d+) "
                     r"applying=(\d+) apply=(\d+)")
ANSWER = re.compile(r"public swarm (?:got a sync body answer|sync body answer came slowly) peer=(\w+) "
                    r"shared=(\w+) bodies=(\d+) answered=(\w+) millis=(\d+)")
HELD = re.compile(r"takes up a history request that waited for its serve worker peer=(\w+) millis=(\d+)")
READ = re.compile(r"public swarm (?:read history bodies|took long to read history bodies) peer=(\w+) "
                  r"bodies=(\d+) millis=(\d+)")
SENT = re.compile(r"public swarm sends a history answer (?:after waiting for the swarm loop|late: it waited "
                  r"for the swarm loop) peer=(\w+) millis=(\d+)")
REFUSED = re.compile(r"public swarm refused (?:work bodies|history)[^ ]* .*?peer=(\w+)|public swarm refused "
                     r"(?:work bodies|history).*$")
PROXY = re.compile(r"^(\d\d):(\d\d):(\d\d)Z (v\d)->(v\d) packets (\d+) bytes (\d+) dropped (\d+) mean ([\d.]+) ms")
REQUESTS = re.compile(r"public swarm requests by peer peer=(\w+) body_requests=(\d+) bodies_asked=(\d+) "
                      r"hedged_requests=(\d+) bodies_taken=(\d+) bodies_stale=(\d+)")
TAKEN_BACK = re.compile(r"takes back the bodies a stuck request still owes peer=(\w+) waited=(\d+) bodies=(\d+)")
HEDGED = re.compile(r"asks another peer for the bodies a slow peer owes slow=(\w+) asked=(\w+) bodies=(\d+)")
DELIVERY = re.compile(r"public swarm sync delivery by peer peer=(\w+) rate_kib_s=(\S+) owed_limit=(\d+) "
                      r"body_bytes=(\d+)")
QUIC_PATH = re.compile(r"quic path peer=(\w+) rtt_ms=(\d+) cwnd=(\d+) mtu=(\d+) sent_bytes_s=(\d+) "
                       r"received_bytes_s=(\d+) sent=(\d+) lost=(\d+) congestion_events=(\d+) "
                       r"blocked_sending=(\d+) peer_blocked=(\d+)")


def seconds(clock):
    hours, minutes, rest = clock.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def local_seconds(unix):
    moment = time.localtime(unix)
    return moment.tm_hour * 3600 + moment.tm_min * 60 + moment.tm_sec + unix % 1


def spread(values):
    values = sorted(values)
    if not values:
        return "none"
    pick = lambda share: values[min(len(values) - 1, int(share * len(values)))]
    return f"median {pick(0.5)}, p90 {pick(0.9)}, max {values[-1]} (n={len(values)})"


def lines_after(log, since):
    for line in log.read_text(errors="replace").splitlines():
        match = CLOCK.match(line)
        if match and seconds(match.group(1)) >= since:
            yield line


def main():
    folder = Path(sys.argv[1])
    result = json.loads((folder / "result.json").read_text())
    # A run without a starved validator is read for the one named, from the start of its window.
    degrade = next((outage for outage in result["outages"] or [] if outage.get("kind") == "degrade"),
                   {"validator": sys.argv[2][1:] if len(sys.argv) > 2 else 1, "up": result["window"][0]})
    starved = sys.argv[2] if len(sys.argv) > 2 else f"v{degrade['validator']}"
    up = local_seconds(degrade["up"])
    logs = {path.stem: path for path in sorted(folder.glob("v*.log"))}
    ids = {}
    for node, log in logs.items():
        for line in log.read_text(errors="replace").splitlines():
            if match := LISTENING.search(line):
                ids[match.group(1)] = node
                break
    starved_id = next(peer for peer, node in ids.items() if node == starved)
    name = lambda peer: ids.get(peer, peer[-6:])
    print(f"{folder.name} (bin-{result['settings']['bin']}): {starved} from its release on")

    cuts = [tuple(map(int, match.groups())) for line in lines_after(logs[starved], up)
            if (match := APPLIED.search(line))]
    print(f"  sync cuts {len(cuts)}, bodies {sum(cut[1] for cut in cuts)}, fetched ahead "
          f"{sum(cut[2] for cut in cuts)}, requests {sum(cut[3] for cut in cuts)}, refusals "
          f"{sum(cut[4] for cut in cuts)}")
    fetch = [cut[6] for cut in cuts]
    after_fetch = [max(0, cut[7] - cut[6]) for cut in cuts]
    before_install = [max(0, cut[15] - max(cut[7], cut[6])) for cut in cuts]
    install = [cut[16] for cut in cuts]
    print(f"  per cut, ms: fetch {spread(fetch)}")
    print(f"               checks after the last body {spread(after_fetch)}")
    print(f"               wait for the install {spread(before_install)}")
    print(f"               install {spread(install)}")
    steps = ("pow", "prepare_wait", "prepare", "authorize", "commit_wait", "commit")
    print("  check work summed over all cuts, ms: " + ", ".join(
        f"{step} {sum(cut[9 + index] for cut in cuts)}" for index, step in enumerate(steps))
          + f" (checking {sum(cut[8] for cut in cuts)})")

    answers = defaultdict(list)
    unanswered = defaultdict(int)
    for line in lines_after(logs[starved], up):
        if match := ANSWER.search(line):
            peer = name(match.group(1))
            answers[peer].append(int(match.group(5)))
            unanswered[peer] += match.group(4) != "true"
    for peer in sorted(answers):
        print(f"  answers from {peer}, ms: {spread(answers[peer])}, refused or empty {unanswered[peer]}")

    # The request counters add up from the start; what came after the release is the last value
    # less the last one before it.
    before, last = {}, {}
    taken_back, hedged, delivery = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), {}
    for line in logs[starved].read_text(errors="replace").splitlines():
        clock = CLOCK.match(line)
        if not clock:
            continue
        after = seconds(clock.group(1)) >= up
        if match := REQUESTS.search(line):
            counts = tuple(int(value) for value in match.groups()[1:])
            (last if after else before)[name(match.group(1))] = counts
        elif after and (match := TAKEN_BACK.search(line)):
            entry = taken_back[name(match.group(1))]
            entry[0] += 1
            entry[1] += int(match.group(3))
        elif after and (match := HEDGED.search(line)):
            entry = hedged[name(match.group(1))]
            entry[0] += 1
            entry[1] += int(match.group(3))
        elif after and (match := DELIVERY.search(line)):
            delivery[name(match.group(1))] = (match.group(2), match.group(3), match.group(4))
    asked_all = stale_all = 0
    for peer in sorted(last):
        start = before.get(peer, (0,) * 5)
        requests, asked, hedges, taken, stale = (now - then for now, then in zip(last[peer], start))
        asked_all += asked
        stale_all += stale
        rate = delivery.get(peer)
        print(f"  {peer} after the release: {requests} requests, {asked} bodies asked, {taken} taken, {stale} stale"
              f" ({stale / max(1, asked):.0%}), {hedges} asked again; taken back from it {taken_back[peer][0]} times"
              f" ({taken_back[peer][1]} bodies), asked elsewhere for its bodies {hedged[peer][0]} times"
              f" ({hedged[peer][1]} bodies)"
              + (f"; last rate {rate[0]} KiB/s, may owe {rate[1]} bodies of {rate[2]} bytes" if rate else ""))
    print(f"  stale after the release: {stale_all} of {asked_all} bodies asked ({stale_all / max(1, asked_all):.0%})")

    # The sender's QUIC view of each link into the starved node, where a node logged it.
    for node, log in logs.items():
        if node == starved:
            continue
        paths = [tuple(int(value) for value in match.groups()[1:]) for line in lines_after(log, up)
                 if starved_id in line and (match := QUIC_PATH.search(line))]
        if not paths:
            continue
        # rtt, cwnd, mtu, sent bytes/s, received bytes/s, sent, lost, congestion events, blocked, peer blocked
        column = lambda index: [path[index] for path in paths]
        sent, lost = sum(column(5)), sum(column(6))
        print(f"  quic {node}->{starved}: sent {spread([value // 1024 for value in column(3)])} KiB/s,"
              f" cwnd {spread([value // 1024 for value in column(1)])} KiB, rtt {spread(column(0))} ms,"
              f" lost {lost} of {sent} packets ({lost / max(1, sent):.2%}),"
              f" congestion events {sum(column(7))}, blocked by flow control {sum(column(8))} times")

    for node, log in logs.items():
        if node == starved:
            continue
        held, read, sent, refused, bodies = [], [], [], 0, 0
        for line in lines_after(log, up):
            if starved_id not in line:
                continue
            if match := HELD.search(line):
                held.append(int(match.group(2)))
            elif match := READ.search(line):
                read.append(int(match.group(3)))
                bodies += int(match.group(2))
            elif match := SENT.search(line):
                sent.append(int(match.group(2)))
            elif "public swarm refused" in line:
                refused += 1
        print(f"  {node} serving {starved}: {len(read)} reads of {bodies} bodies; waited for a worker "
              f"{spread(held)}; read {spread(read)}; answer waited for the swarm loop {spread(sent)}; "
              f"refused {refused}")

    # A link's packets pass the proxy of whichever end was dialed, and the other end's proxy logs the
    # same link with no packets, so each ten-second interval sums over both proxies.
    offset = time.localtime(degrade["up"]).tm_gmtoff
    links = defaultdict(lambda: defaultdict(lambda: [0, 0, []]))
    for proxy in sorted(folder.glob("proxy-v*.log")):
        for line in proxy.read_text(errors="replace").splitlines():
            if not (match := PROXY.match(line)):
                continue
            at = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + offset
            source, target = match.group(4), match.group(5)
            if at % 86400 < up % 86400:
                continue
            interval = links[f"{source}->{target}"][at]
            interval[0] += int(match.group(7))
            interval[1] += int(match.group(8))
            if int(match.group(6)):
                interval[2].append(float(match.group(9)))
    mean_rate = lambda intervals: sum(interval[0] for interval in intervals.values()) / (len(intervals) * 10) / 1e6
    others = [mean_rate(intervals) for link, intervals in links.items() if starved not in link.split("->")]
    if others:
        print(f"  links between the other nodes: {sum(others) / len(others):.2f} MB/s mean each")
    for link, intervals in sorted(links.items()):
        if starved not in link.split("->"):
            continue
        sent_bytes = sum(interval[0] for interval in intervals.values())
        dropped = sum(interval[1] for interval in intervals.values())
        peak = max(interval[0] for interval in intervals.values())
        delays = sorted(delay for interval in intervals.values() for delay in interval[2])
        delay = delays[len(delays) // 2] if delays else 0
        print(f"  link {link}: {sent_bytes / (len(intervals) * 10) / 1e6:.2f} MB/s mean, peak {peak / 10 / 1e6:.2f} "
              f"MB/s, dropped {dropped}, one-way delay median {delay:.0f} ms")

    print(f"  machine CPU mean {result['machine_cpu']['mean']:.0%}, p90 {result['machine_cpu']['p90']:.0%}; "
          f"cuts/min {result['cuts_per_min']:.1f}, unique final TPS {result.get('finalized_unique_tps', 0):.0f}")
    for node, resources in sorted(result.get("resources", {}).items()):
        if not node.startswith("v"):
            continue
        print(f"  {node}: CPU cores mean {resources['cpu_cores_mean']:.2f}, max {resources['cpu_cores_max']:.2f}; "
              f"RSS max {resources['rss_mb_max']:.0f} MB; writes {resources['write_mb_s']:.1f} MB/s")


if __name__ == "__main__":
    main()
