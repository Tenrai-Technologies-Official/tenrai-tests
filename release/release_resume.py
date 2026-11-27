"""Resume a release's checks phase check by check: keep each check whose result still belongs to this build.

A row of checks.tsv stays when its check passed, its log is the one its command boundary recorded, that boundary is
the latest of its name and still meets every rule release_runtime.verify applies, and the test runs within it ran one
frozen test binary of this release whose bytes are still the ones it was frozen with. Every other row goes, and its
check runs again. The release's final verification is unchanged and checks every latest boundary once more.
  release_resume.py resumable --work DIR --phase checks       the attempt whose frozen test binary the kept checks ran
  release_resume.py keep --work DIR --phase checks --attempt NAME|none --report FILE
  release_resume.py frozen --store DIR --name NAME --binary EXE   the build gave the frozen bytes"""
from argparse import ArgumentParser
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

import release_evidence as evidence
import release_runtime as runtime

require = runtime.require


def utc(text):
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def written(path):
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def same_path(first, second):
    return os.path.normcase(str(Path(first).resolve())) == os.path.normcase(str(Path(second).resolve()))


def table(work):
    """The rows of checks.tsv as (phase, name, exit, log)."""
    path = work / "checks.tsv"
    rows = []
    if path.is_file():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line:
                columns = line.split("\t")
                require(len(columns) == 4, f"malformed checks.tsv row {number}")
                rows.append(tuple(columns))
    names = [row[1] for row in rows]
    require(len(set(names)) == len(names), "checks.tsv names a check twice")
    return rows


def boundaries(work):
    """Every command boundary of the release in the order the release recorded them."""
    found = []
    for ticket in sorted((work / "source-boundaries").glob("*/before.json")):
        saved = evidence.load(ticket)
        require(saved.get("schema") == runtime.SCHEMA, "invalid earlier command boundary")
        result = ticket.with_name("after.json")
        found.append((saved["sequence"], ticket, saved, evidence.load(result) if result.exists() else None))
    return found


def producing(name, log, found):
    """The boundary of check `name` that recorded `log`, which must still have the bytes it recorded."""
    runs = [entry for entry in found if entry[2].get("name") == "check-" + name and entry[3] is not None
            and entry[3].get("log") and same_path(entry[3]["log"]["path"], log)]
    require(bool(runs), "no command boundary recorded its log")
    require(len(runs) == 1, "several command boundaries recorded its log")
    require(runs[0][3]["log"]["sha256"] == evidence.sha(log), "its log changed after its command")
    return runs[0]


def test_runs(work, saved, after):
    """The frozen-binary runs testrun.py finished while the command ran; a release runs one command at a time."""
    begun, ended = utc(saved["created_utc"]), utc(after["completed_utc"])
    return [result.parent for result in sorted((work / "checks" / "filters").glob("*/result.json"))
            if begun <= written(result) <= ended]


def attempt_of(work, binary):
    """The attempt folder of this release whose frozen test binary `binary` is."""
    store, attempt = binary.parent, binary.parent.parent
    require(store.name == "frozen-tests" and same_path(attempt.parent, work / "scratch")
            and binary.name == f"release-{attempt.name}.exe", f"not a frozen test binary of this release: {binary}")
    return attempt.name


def frozen_binary(work, runs):
    """The one frozen test binary of this release the runs used, unchanged since its freeze; None without runs."""
    used = set()
    for run in runs:
        result = evidence.load(run / "result.json")
        preflight = evidence.load(run / "preflight.json")
        require(result.get("exit") == 0 and result.get("qualified") is True, f"its test run {run.name} did not pass")
        binary = Path(preflight.get("binary") or "missing").resolve()
        record = binary.with_suffix(".json")
        require(binary.is_file() and record.is_file(), f"its frozen test binary is missing: {binary}")
        frozen = evidence.load(record).get("sha256")
        require(preflight.get("sha256") == frozen and evidence.sha(binary) == frozen,
                f"its frozen test binary changed: {binary}")
        used.add(binary)
    require(len(used) <= 1, "its test runs used several frozen test binaries")
    if not used:
        return None
    binary = used.pop()
    attempt_of(work, binary)
    return binary


def kept_checks(work, phase):
    """{"kept": names, "dropped": {name: reason}, "attempt": folder or None} for the rows of `phase`."""
    work = evidence.artifact_root(work)
    found = boundaries(work)
    newest = {entry[2]["name"]: entry[0] for entry in found}
    passed, dropped = [], {}
    for row_phase, name, code, log in table(work):
        if row_phase != phase:
            continue
        try:
            require(code == "0", f"it ended with exit {code}")
            path = Path(log).resolve()
            require(path.is_relative_to(work) and path.is_file() and bool(path.read_bytes().strip()),
                    "its log is missing or empty")
            sequence, ticket, saved, after = producing(name, path, found)
            require(newest["check-" + name] == sequence, "it ran again after this result")
            runtime.check_boundary("check-" + name, ticket, saved, after)
            passed.append((sequence, name, frozen_binary(work, test_runs(work, saved, after))))
        except evidence.GateError as error:
            dropped[name] = str(error)
    # The checks still to run use the frozen binary of one attempt; a kept check that ran another one runs again.
    binaries = [binary for _, _, binary in sorted(passed, key=lambda entry: entry[0]) if binary is not None]
    binary = binaries[-1] if binaries else None
    kept = []
    for _, name, used in passed:
        if used is not None and used != binary:
            dropped[name] = "it ran another frozen test binary than the newest kept check"
        else:
            kept.append(name)
    return {"phase": phase, "kept": kept, "dropped": dropped,
            "attempt": attempt_of(work, binary) if binary is not None else None}


def resumable(arguments):
    print(kept_checks(arguments.work, arguments.phase)["attempt"] or "")
    return 0


def keep(arguments):
    """Rewrite checks.tsv with the kept rows of the phase; the rows of other phases stay as they are."""
    work = evidence.artifact_root(arguments.work)
    result = kept_checks(work, arguments.phase)
    expected = None if arguments.attempt == "none" else arguments.attempt
    require(result["attempt"] == expected, "the kept checks ran another frozen test binary than this attempt resumes")
    rows = [row for row in table(work) if row[0] != arguments.phase or row[1] in result["kept"]]
    pending = work / "checks.tsv.kept"
    pending.write_text("".join("\t".join(row) + "\n" for row in rows), encoding="utf-8", newline="\n")
    os.replace(pending, work / "checks.tsv")
    evidence.write(Path(arguments.report), result)
    for name in result["kept"]:
        print(f"kept {name}: its result still belongs to this build")
    for name, reason in result["dropped"].items():
        print(f"runs again {name}: {reason}")
    print(f"{len(result['kept'])} checks kept, {len(result['dropped'])} run again")
    return 0


def frozen(arguments):
    """A resumed attempt's frozen test binary must hold exactly the bytes its build just gave."""
    store = Path(arguments.store).resolve()
    binary, record = store / f"{arguments.name}.exe", store / f"{arguments.name}.json"
    require(binary.is_file() and record.is_file(), f"no frozen test binary {arguments.name} in {store}")
    value = evidence.load(record).get("sha256")
    require(evidence.sha(binary) == value, f"the frozen test binary {arguments.name} changed after its freeze")
    built = evidence.sha(Path(arguments.binary))
    require(built == value, f"the build gave {built[:16]}, the frozen test binary {arguments.name} is {str(value)[:16]}")
    print(f"{arguments.name}: the build gave its frozen bytes {value}")
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("resumable", "keep"):
        command = sub.add_parser(action)
        command.add_argument("--work", required=True)
        command.add_argument("--phase", required=True)
        if action == "keep":
            command.add_argument("--attempt", required=True)
            command.add_argument("--report", required=True)
    same = sub.add_parser("frozen")
    same.add_argument("--store", required=True)
    same.add_argument("--name", required=True)
    same.add_argument("--binary", required=True)
    arguments = parser.parse_args()
    try:
        return {"resumable": resumable, "keep": keep, "frozen": frozen}[arguments.action](arguments)
    except Exception as error:
        print(f"release resume refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
