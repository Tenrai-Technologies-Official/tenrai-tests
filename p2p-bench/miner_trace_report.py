"""Where the empty blocks of a run come from, from the trace lines of the minertrace build.

For each miner: the node's answers to its template requests, the jobs it mined with their transaction
count and where they came from, the time it hashed on full and on empty work and the time it waited for
new work after each find, its finds by kind, and each submission with the time it waited for a slot and
the time the node took to take it. Then the node's own count of empty templates and executed empty blocks
from the validator log, so the two sides can be held against each other.
    py -3.14 miner_trace_report.py RESULT_DIR [first HH:MM] [last HH:MM]
"""

import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\s+([A-Z]+)\s+([\w:]+): (.*)$")
FIELDS = re.compile(r"(\w+)=(\w+)")


def stamp(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").timestamp()


def percentiles(values):
    if not values:
        return "-"
    ordered = sorted(values)
    at = lambda share: ordered[min(len(ordered) - 1, int(share * len(ordered)))]
    return f"n={len(ordered)} p50={at(0.5):.0f} p90={at(0.9):.0f} p99={at(0.99):.0f} max={ordered[-1]:.0f}"


class Miner:
    def __init__(self, name):
        self.name = name
        self.counts = Counter()
        self.jobs = {}
        self.job = None
        self.last_answer = None
        self.found_at = None
        self.time = Counter()
        self.answer_ms = defaultdict(list)
        self.idle_ms = []
        self.submissions = []
        self.first = self.last = None

    def close_job(self, at):
        if self.job is None:
            return
        start, kind = self.job["start"], self.job["kind"]
        if kind == "filled":
            hashed_until = self.found_at if self.found_at is not None else at
            self.time["filled"] += max(0.0, hashed_until - start)
            if self.found_at is not None:
                self.time["idle"] += max(0.0, at - self.found_at)
        else:
            self.time[kind] += max(0.0, at - start)
        self.job = None
        self.found_at = None

    def line(self, at, message):
        self.first = self.first if self.first is not None else at
        self.last = at
        fields = dict(FIELDS.findall(message))
        counts = self.counts
        if message.startswith("trace answer "):
            transactions = int(fields["transactions"])
            self.last_answer = "node"
            counts["answers"] += 1
            counts["answers_empty" if transactions == 0 else "answers_filled"] += 1
            self.answer_ms["after a find" if fields.get("found") == "true" else "on a cut or tick"].append(
                int(fields["answer_ms"]))
        elif message.startswith("trace spent_again"):
            counts["spent_again"] += 1
            self.last_answer = "spent_again"
            self.answer_ms["spent again"].append(int(fields["answer_ms"]))
        elif message.startswith("trace empty_work"):
            counts["empty_work"] += 1
        elif message.startswith("no empty template from the node"):
            counts["empty_work_failed"] += 1
        elif message.startswith("trace same_work"):
            counts["same_work_empty" if fields["transactions"] == "0" else "same_work_filled"] += 1
        elif message.startswith("trace throttled"):
            counts["throttled"] += 1
        elif message.startswith("no template from the node"):
            counts["template_errors"] += 1
        elif message.startswith("trace job "):
            self.close_job(at)
            job = message.split()[2]
            transactions = int(fields["transactions"])
            if transactions:
                kind = "filled"
            else:
                kind = "empty_spent" if self.last_answer == "spent_again" else "empty_node"
            self.job = {"start": at, "kind": kind, "id": job}
            self.jobs[job] = kind
            counts[f"jobs_{kind}"] += 1
        elif message.startswith("new job ") and "after the last find" in message:
            self.idle_ms.append(int(message.split()[2]))
        elif message.startswith("trace found job "):
            job, kind = message.split()[3], message.split()[4]
            if kind == "filled":
                counts["finds_filled"] += 1
                if self.job is not None and self.job["id"] == job and self.found_at is None:
                    self.found_at = at
            elif kind == "empty":
                counts[f"finds_{self.jobs.get(job, 'empty_node')}"] += 1
            else:
                counts[f"finds_{kind}"] += 1
        elif message.startswith("trace found unknown"):
            counts["finds_unknown"] += 1
        elif message.startswith("trace dropped block"):
            counts["dropped"] += 1
        elif message.startswith("trace submitted block "):
            transactions = int(fields["transactions"])
            taken = fields["taken"] == "true"
            self.submissions.append((at, transactions, int(fields["slot_ms"]), int(fields["answer_ms"]), taken))
            counts["submitted_empty" if transactions == 0 else "submitted_filled"] += 1
            if taken:
                counts["taken_empty" if transactions == 0 else "taken_filled"] += 1
        elif message.startswith("block ") and " accepted at height " in message:
            counts["accepted"] += 1
        elif "was taken before its answer was lost" in message:
            counts["known"] += 1
        elif "came too late" in message:
            counts["stale"] += 1
        elif " not submitted: " in message:
            counts["lost"] += 1


def read_miner(path, first, last):
    miner = Miner(path.stem.replace("miner-", ""))
    with open(path, errors="replace") as file:
        for text in file:
            match = LINE.match(text)
            if not match:
                continue
            minute = match.group(1)[11:16]
            if not first <= minute <= last:
                continue
            miner.line(stamp(match.group(1)), match.group(4))
    if miner.last is not None:
        miner.close_job(miner.last)
    return miner


def node_side(folder, first, last):
    """The validator's empty templates by reason and its executed empty blocks, in the same minutes."""
    templates, blocks = Counter(), Counter()
    log = folder / "v1.log"
    if not log.exists():
        return templates, blocks
    with open(log, errors="replace") as file:
        for text in file:
            minute = text[11:16]
            if not first <= minute <= last:
                continue
            if "work templates filled" in text:
                for key, value in re.findall(r"(\w+)=(\d+)", text[60:]):
                    if key in ("templates", "filled", "full", "asked_empty", "empty_pool", "lease_cap", "none_chosen"):
                        templates[key] += int(value)
            elif "cut installed height" in text:
                fields = dict(re.findall(r"(\w+)=(\d+)", text[60:]))
                blocks["cuts"] += 1
                blocks["executed"] += int(fields.get("executed", 0))
                blocks["empty"] += int(fields.get("empty_blocks", 0))
    return templates, blocks


def main():
    folder = Path(sys.argv[1])
    first, last = (sys.argv[2], sys.argv[3]) if len(sys.argv) > 3 else ("00:00", "99:99")
    miners = [read_miner(path, first, last) for path in sorted(folder.glob("miner-*.log"),
                                                                key=lambda p: int(p.stem.split("-")[-1]))]
    miners = [miner for miner in miners if miner.last is not None]
    total = Counter()
    times = Counter()
    answers = defaultdict(list)
    idle, slot, taken_ms = [], [], defaultdict(list)
    for miner in miners:
        total.update(miner.counts)
        times.update(miner.time)
        for key, values in miner.answer_ms.items():
            answers[key].extend(values)
        idle.extend(miner.idle_ms)
        for _, transactions, slot_ms, answer_ms, _ in miner.submissions:
            slot.append(slot_ms)
            taken_ms["empty" if transactions == 0 else "filled"].append(answer_ms)
    print(f"{folder.name}, minutes {first}-{last}, {len(miners)} miners")
    print("\nper miner: jobs filled/empty from the node/empty after spent_again | finds filled/empty/repeat"
          " | submitted empty, taken empty | spent_again | hashing on empty work, waiting after finds (share of"
          " the miner's time) | wait after a find p50")
    for miner in miners:
        c, t = miner.counts, miner.time
        span = max(1e-9, sum(t.values()))
        hashing = t["filled"] + t["empty_node"] + t["empty_spent"]
        waits = sorted(miner.idle_ms)
        median = waits[len(waits) // 2] if waits else 0
        print(f"  {miner.name:8s} {c['jobs_filled']:6d}/{c['jobs_empty_node']:4d}/{c['jobs_empty_spent']:3d}"
              f" | {c['finds_filled']:6d}/{c['finds_empty_node'] + c['finds_empty_spent']:5d}/{c['finds_repeat']:3d}"
              f" | {c['submitted_empty']:5d} {c['taken_empty']:5d} | {c['spent_again']:4d}"
              f" | empty {100 * (t['empty_node'] + t['empty_spent']) / max(hashing, 1e-9):5.1f} % of hashing,"
              f" waiting {100 * t['idle'] / span:5.1f} % | {median} ms")
    hashing = times["filled"] + times["empty_node"] + times["empty_spent"]
    print("\nall miners:")
    print(f"  template answers {total['answers']} ({total['answers_empty']} empty), same work again"
          f" {total['same_work_filled']} full / {total['same_work_empty']} empty, throttled {total['throttled']},"
          f" errors {total['template_errors']}")
    print(f"  spent_again {total['spent_again']}: empty work {total['empty_work']}, failed {total['empty_work_failed']}")
    print(f"  jobs: {total['jobs_filled']} full, {total['jobs_empty_node']} empty from the node,"
          f" {total['jobs_empty_spent']} empty after spent_again")
    print(f"  finds: {total['finds_filled']} full, {total['finds_empty_node']} on empty work from the node,"
          f" {total['finds_empty_spent']} on empty work after spent_again, {total['finds_repeat']} repeats (not sent),"
          f" {total['finds_unknown']} on unknown jobs, {total['dropped']} dropped")
    print(f"  submitted: {total['submitted_filled']} full ({total['taken_filled']} taken),"
          f" {total['submitted_empty']} empty ({total['taken_empty']} taken); answers: {total['accepted']} accepted,"
          f" {total['known']} known, {total['stale']} stale, {total['lost']} lost")
    print(f"  time: hashing full work {times['filled']:.0f} s, empty work {times['empty_node'] + times['empty_spent']:.0f} s"
          f" ({100 * (times['empty_node'] + times['empty_spent']) / max(hashing, 1e-9):.1f} % of hashing),"
          f" waiting after finds {times['idle']:.0f} s"
          f" ({100 * times['idle'] / max(sum(times.values()), 1e-9):.1f} % of all miner time)")
    print("\nlatencies in ms:")
    for key in ("after a find", "on a cut or tick", "spent again"):
        print(f"  template answer {key:17s} {percentiles(answers[key])}")
    print(f"  new job after a find (miner) {percentiles(idle)}")
    print(f"  submission slot wait          {percentiles(slot)}")
    print(f"  submission to answer, full    {percentiles(taken_ms['filled'])}")
    print(f"  submission to answer, empty   {percentiles(taken_ms['empty'])}")
    templates, blocks = node_side(folder, first, last)
    node_empty = sum(templates[key] for key in ("asked_empty", "empty_pool", "lease_cap", "none_chosen"))
    print(f"\nnode: {templates['templates']} templates, {templates['full']} full, {node_empty} empty"
          f" (asked {templates['asked_empty']}, pool {templates['empty_pool']}, lease cap {templates['lease_cap']},"
          f" none chosen {templates['none_chosen']}); {blocks['cuts']} cuts executed {blocks['executed']} blocks,"
          f" {blocks['empty']} empty ({100 * blocks['empty'] / max(blocks['executed'], 1):.1f} %)")


if __name__ == "__main__":
    main()
