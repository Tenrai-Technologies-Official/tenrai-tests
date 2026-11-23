"""When in the cut cycle the miners got empty templates, and how long those windows last.

For every template answer of the traced miners, the time since the node's last cut install. An empty
answer is a lease-cap answer in this run (623 of 658). Windows: runs of answers, of any miner, that were
all empty, closed by the first full one.

    cap_windows.py <results folder> <first HH:MM> <last HH:MM>
"""
import bisect
import datetime
import pathlib
import re
import sys

STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")
ANSWER = re.compile(r"trace answer answer_ms=(\d+) transactions=(\d+)")


def parse(stamp):
    return datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f")


def main():
    folder = pathlib.Path(sys.argv[1])
    first, last = sys.argv[2], sys.argv[3]
    installs = []
    with open(folder / "v1.log", "rb") as handle:
        for raw in handle:
            line = raw.decode("utf-8", "replace")
            if "cut installed height" in line:
                found = STAMP.match(line)
                if found:
                    installs.append(parse(found.group(1)))
    installs.sort()
    answers = []
    for path in sorted(folder.glob("miner-*.log")):
        with open(path, "rb") as handle:
            for raw in handle:
                line = raw.decode("utf-8", "replace")
                found = ANSWER.search(line)
                stamp = STAMP.match(line)
                if found and stamp:
                    at = parse(stamp.group(1))
                    minute = at.strftime("%H:%M")
                    if first <= minute <= last:
                        answers.append((at, int(found.group(2)) == 0))
    answers.sort()
    phase = {True: [], False: []}
    for at, empty in answers:
        index = bisect.bisect_right(installs, at) - 1
        if index >= 0:
            phase[empty].append((at - installs[index]).total_seconds())
    for empty in (True, False):
        values = sorted(phase[empty])
        if not values:
            continue
        pick = lambda q: values[min(len(values) - 1, int(q * len(values)))]
        print(f"{'empty' if empty else 'full '} answers {len(values):6}: since the last install "
              f"p10 {pick(0.1):.2f} p50 {pick(0.5):.2f} p90 {pick(0.9):.2f} s")
    windows = []
    start = None
    count = 0
    for at, empty in answers:
        if empty:
            if start is None:
                start, count = at, 0
            count += 1
        elif start is not None:
            windows.append(((at - start).total_seconds(), count))
            start = None
    if windows:
        lengths = sorted(length for length, _ in windows)
        counts = sorted(count for _, count in windows)
        pick = lambda values, q: values[min(len(values) - 1, int(q * len(values)))]
        span = (answers[-1][0] - answers[0][0]).total_seconds()
        print(f"windows of empty answers only: {len(windows)}, length p50 {pick(lengths, 0.5):.3f} "
              f"p90 {pick(lengths, 0.9):.3f} max {lengths[-1]:.3f} s, answers per window p50 "
              f"{pick(counts, 0.5)} max {counts[-1]}; {sum(lengths):.1f} s of {span:.0f} s "
              f"({sum(lengths) / span * 100:.2f} %)")


if __name__ == "__main__":
    main()
