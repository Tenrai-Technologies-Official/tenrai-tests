"""Runs a filter of a frozen test binary for the qualification, only after its preflight.

  py -3.14 testrun.py freeze NAME EXE
      copies EXE read-only into the store as NAME.exe and keeps its SHA-256 in NAME.json
  py -3.14 testrun.py check NAME FILTER [options]
  py -3.14 testrun.py run NAME FILTER [options] [-- arguments for the test binary]

Both `check` and `run` refuse with exit code 3 unless the frozen binary still matches its hash, the filter
names at least --min-tests tests (default 1) and every --expect test among them, the --smoke test (a short one,
at most five minutes) passes,
enough commit (--commit-gb, default 8) and room (--room PATH=GB, any number) are free, and no build, bench,
node, prover, other test binary or test browser runs. `run` then runs the filter with --include-ignored (not with
--no-ignored, as for a whole suite) with --threads test threads (default 1) under --timeout seconds, and writes preflight.json, test.log and result.json
(passed, failed and their names, seconds, exit code) to a folder of its own under --out. Its exit code is
the test binary's, 124 when the timeout ended it, or 2 when a native success lacks a qualifying summary.
With --expect, every effectively selected test must pass and none may remain ignored.
--shards N runs the selection in N processes at once, the tests spread longest first by --weight SUBSTRING=MINUTES
(10 minutes otherwise); for tests that wait on real time and each start their own network. Every process must
report its own valid summary.

--allow IMAGE explicitly permits that background process image for functional tests. The default remains
exclusive. Shared runs retain memory, disk, binary, smoke and selection checks and record their background
allowlist; their timings do not qualify as isolated performance measurements.

Set TENRAI_TEST_STORE for frozen binaries, TENRAI_TEST_CWD for their working directory, or --out for results.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p2p-bench"))
import preflight  # noqa: E402

RUN_DIR = Path(__file__).resolve().with_name("run")
STORE = Path(os.environ.get("TENRAI_TEST_STORE", RUN_DIR / "frozen"))
RESULT = re.compile(r"^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored(?:;[^\r\n]*)?\r?$", re.M)
SMOKE_SECONDS = 300


def freeze(name, exe):
    STORE.mkdir(parents=True, exist_ok=True)
    target = STORE / f"{name}.exe"
    record = STORE / f"{name}.json"
    if target.exists() or record.exists():
        raise SystemExit(f"{target} is frozen already; earlier results name its hash, so pick another name")
    shutil.copy2(exe, target)
    digest = preflight.sha256(target)
    os.chmod(target, stat.S_IREAD)
    record.write_text(json.dumps({"name": name, "source": str(exe), "sha256": digest,
                                  "frozen": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2) + "\n")
    os.chmod(record, stat.S_IREAD)
    print(f"{digest[:16]}  {target}")


def options(arguments):
    found = {"min_tests": 1, "expect": [], "smoke": None, "commit_gb": 8.0, "room": {}, "threads": 1,
             "timeout": 14400, "out": RUN_DIR / "results", "env": {}, "extra": [], "ignored": True,
             "holder": None, "envcheck": None, "shards": 1, "weights": {}, "allowed_images": []}
    if "--" in arguments:
        cut = arguments.index("--")
        arguments, found["extra"] = arguments[:cut], arguments[cut + 1:]
    index = 0
    while index < len(arguments):
        key, value = arguments[index], arguments[index + 1] if index + 1 < len(arguments) else None
        if key == "--no-ignored":
            found["ignored"] = False
            index += 1
            continue
        if key == "--min-tests":
            found["min_tests"] = int(value)
        elif key == "--expect":
            found["expect"].append(value)
        elif key == "--smoke":
            found["smoke"] = value
        elif key == "--commit-gb":
            found["commit_gb"] = float(value)
        elif key == "--room":
            path, gb = value.rsplit("=", 1)
            found["room"][path] = float(gb)
        elif key == "--threads":
            found["threads"] = int(value)
        elif key == "--timeout":
            found["timeout"] = int(value)
        elif key == "--out":
            found["out"] = value
        elif key == "--allow":
            if value is None or re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None:
                raise SystemExit("--allow requires one process image name")
            if value.lower() not in found["allowed_images"]:
                found["allowed_images"].append(value.lower())
        elif key == "--holder":
            found["holder"] = value
        elif key == "--envcheck":
            found["envcheck"] = value
        elif key == "--env":
            variable, setting = value.split("=", 1)
            found["env"][variable] = setting
        elif key == "--shards":
            found["shards"] = int(value)
            if not 1 <= found["shards"] <= 8:
                raise SystemExit("--shards must be between 1 and 8")
        elif key == "--weight":
            part, minutes = value.rsplit("=", 1)
            found["weights"][part] = float(minutes)
        else:
            raise SystemExit(f"unknown option {key}")
        index += 2
    return found


def test_arguments(filter_, settings):
    """The same effective Rust harness options are used for listing and execution."""
    ignored = ["--include-ignored"] if settings["ignored"] else []
    return [*ignored, "--nocapture", f"--test-threads={settings['threads']}", *settings["extra"], filter_]


def listed(exe, filter_, env, settings):
    output = subprocess.run([str(exe), *test_arguments(filter_, settings), "--list"],
                            capture_output=True, text=True, env=env)
    names = [line[:-len(": test")] for line in output.stdout.splitlines() if line.endswith(": test")]
    return names, output.returncode


def qualified_result(text, selected, settings, native_exit):
    """A native success qualifies only the effective selection the preflight checked."""
    summaries = list(RESULT.finditer(text))
    passed = sum(int(item.group(1)) for item in summaries)
    failed = sum(int(item.group(2)) for item in summaries)
    ignored = sum(int(item.group(3)) for item in summaries)
    reasons = []
    if native_exit != 0:
        reasons.append(f"native test process exited {native_exit}")
    if len(summaries) != 1:
        reasons.append(f"expected one valid test summary, found {len(summaries)}")
    elif not summaries[0].group(0).startswith("test result: ok."):
        reasons.append("test summary did not report success")
    if passed < max(1, settings["min_tests"]):
        reasons.append(f"only {passed} tests passed; at least {max(1, settings['min_tests'])} required")
    if failed:
        reasons.append(f"test summary reports {failed} failures")
    if passed + failed + ignored != selected:
        reasons.append(f"summary covers {passed + failed + ignored} tests; effective selection named {selected}")
    if settings["expect"] and (ignored or failed or passed != selected):
        reasons.append("required named tests need all selected tests passed and zero ignored")
    qualified = not reasons
    return {
        "exit": native_exit if native_exit else (0 if qualified else 2),
        "native_exit": native_exit, "qualified": qualified, "qualification_failures": reasons,
        "passed": passed, "failed": failed, "ignored": ignored,
        "failures": sorted(set(re.findall(r"^test (\S+) \.\.\. FAILED$", text, re.M))),
        "expected_tests": selected,
        "required_tests_passed": list(settings["expect"]) if qualified else [],
    }


def shard_plan(tests, settings):
    """Longest first, each test onto the shard with the least planned minutes."""
    def minutes(test):
        return max((weight for part, weight in settings["weights"].items() if part in test), default=10.0)
    shards = [[] for _ in range(min(settings["shards"], len(tests)))]
    load = [0.0] * len(shards)
    for test in sorted(tests, key=lambda test: (-minutes(test), test)):
        index = load.index(min(load))
        shards[index].append(test)
        load[index] += minutes(test)
    return [sorted(shard) for shard in shards]


def combined_result(parts, settings, selected):
    """Every shard qualifies on its own selection; together they cover the whole catalog once."""
    reasons, failures = [], []
    for index, (group, part) in enumerate(parts, start=1):
        reasons += [f"shard {index}: {reason}" for reason in part["qualification_failures"]]
        failures += part["failures"]
    passed = sum(part["passed"] for _, part in parts)
    failed = sum(part["failed"] for _, part in parts)
    ignored = sum(part["ignored"] for _, part in parts)
    if sum(len(group) for group, _ in parts) != selected:
        reasons.append(f"the shards name {sum(len(group) for group, _ in parts)} tests; the selection names {selected}")
    if passed != selected:
        reasons.append(f"{passed} of the {selected} selected tests passed")
    if passed < max(1, settings["min_tests"]):
        reasons.append(f"only {passed} tests passed; at least {max(1, settings['min_tests'])} required")
    native = next((part["native_exit"] for _, part in parts if part["native_exit"]), 0)
    qualified = not reasons
    return {
        "exit": native if native else (0 if qualified else 2),
        "native_exit": native, "qualified": qualified, "qualification_failures": reasons,
        "passed": passed, "failed": failed, "ignored": ignored, "failures": sorted(set(failures)),
        "expected_tests": selected, "required_tests_passed": list(settings["expect"]) if qualified else [],
        "shards": [{"tests": group, "native_exit": part["native_exit"], "passed": part["passed"],
                    "failed": part["failed"], "qualified": part["qualified"]} for group, part in parts],
    }


def fresh_admission(exe, expected, settings):
    """Explicit release mode admits each native catalog/smoke/run separately."""
    if not (settings["envcheck"] or settings["holder"] or settings.get("allowed_images")):
        return None
    script = Path(settings["envcheck"] or Path(__file__).with_name("envcheck.py")).resolve()
    command = [sys.executable, "-E", "-s", "-B", str(script),
               "--commit-gb", str(settings["commit_gb"]), "--file", str(exe) + "=" + expected]
    if settings["holder"]:
        command += ["--holder", settings["holder"]]
    for path, room in settings["room"].items():
        command += ["--room", str(path) + "=" + str(room)]
    for image in settings.get("allowed_images", []):
        command += ["--allow", image]
    before = preflight.sha256(script)
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    stable = preflight.sha256(script) == before
    return {"command": command, "native_exit": result.returncode, "stdout": result.stdout,
            "stderr": result.stderr, "envcheck_sha256": before, "controls_unchanged": stable,
            "admitted": result.returncode == 0 and stable}

def check(name, filter_, settings):
    """The record of the binary and the run, and every reason the run must not start."""
    reasons = []
    exe, record_path = STORE / f"{name}.exe", STORE / f"{name}.json"
    record = {"binary": str(exe), "filter": filter_, "settings": settings}
    if not record_path.exists() or not exe.exists():
        return record, [f"{exe} is not frozen: testrun.py freeze {name} <exe>"]
    expected = json.loads(record_path.read_text())["sha256"]
    record["sha256"] = preflight.sha256(exe)
    if record["sha256"] != expected:
        reasons.append(f"{exe} no longer matches its hash")
    if os.access(exe, os.W_OK):
        reasons.append(f"{exe} is writable")
    env = dict(os.environ, **settings["env"])
    admission = fresh_admission(exe, expected, settings)
    if admission is not None:
        record["catalog_admission"] = admission
        if reasons or not admission["admitted"]:
            return record, reasons + ([] if admission["admitted"] else ["fresh catalog environment check refused"])
    tests, catalog_exit = listed(exe, filter_, env, settings)
    record["tests"] = len(tests)
    record["test_names"] = tests
    record["selection_arguments"] = test_arguments(filter_, settings)
    record["catalog_exit"] = catalog_exit
    if catalog_exit != 0:
        reasons.append(f"test catalog exited {catalog_exit}")
    if not tests:
        reasons.append(f"the filter {filter_} names no tests")
    if len(set(tests)) != len(tests):
        reasons.append("test catalog contains duplicate names")
    if len(tests) < settings["min_tests"]:
        reasons.append(f"the filter {filter_} names {len(tests)} tests, {settings['min_tests']} expected")
    for test in settings["expect"]:
        if test not in tests:
            reasons.append(f"the filter {filter_} does not name {test}")
    record["memory"] = preflight.memory()
    if record["memory"]["free_commit_gb"] < settings["commit_gb"]:
        reasons.append(f"{record['memory']['free_commit_gb']} GB of commit free, {settings['commit_gb']} GB needed")
    record["room_gb"] = {}
    for path, gb in settings["room"].items():
        Path(path).mkdir(parents=True, exist_ok=True)
        free = round(shutil.disk_usage(path).free / 2**30, 1)
        record["room_gb"][path] = free
        if free < gb:
            reasons.append(f"{free} GB free on {path}, {gb} GB needed")
    competitors = preflight.competing([])
    allowed = set(settings.get("allowed_images", []))
    record["allowed_background"] = [item for item in competitors if item.split(" ")[0].lower() in allowed]
    record["competing"] = [item for item in competitors if item.split(" ")[0].lower() not in allowed]
    if allowed:
        record["execution_scope"] = "shared_functional_tests"
    if record["competing"]:
        reasons.append("other processes would share the machine: " + ", ".join(record["competing"]))
    if not reasons and settings["smoke"]:
        admission = fresh_admission(exe, expected, settings)
        if admission is not None:
            record["smoke_admission"] = admission
            if not admission["admitted"]:
                return record, ["fresh smoke environment check refused"]
        start = time.time()
        try:
            smoke = subprocess.run([str(exe), "--include-ignored", "--exact", settings["smoke"]], capture_output=True,
                                   text=True, env=env, timeout=SMOKE_SECONDS)
        except subprocess.TimeoutExpired:
            record["smoke"] = {"test": settings["smoke"], "exit": None, "seconds": SMOKE_SECONDS}
            reasons.append(f"the smoke test {settings['smoke']} did not end within {SMOKE_SECONDS} s; a smoke test "
                           "must be short")
        else:
            smoke_settings = dict(settings, min_tests=1, expect=[settings["smoke"]])
            smoke_result = qualified_result(smoke.stdout, 1, smoke_settings, smoke.returncode)
            record["smoke"] = {"test": settings["smoke"], "exit": smoke_result["exit"],
                               "native_exit": smoke.returncode, "qualified": smoke_result["qualified"],
                               "qualification_failures": smoke_result["qualification_failures"],
                               "seconds": round(time.time() - start, 1)}
            if not smoke_result["qualified"]:
                reasons.append(f"the smoke test {settings['smoke']} did not qualify: "
                               + "; ".join(smoke_result["qualification_failures"]))
    record["checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return record, reasons


def run_shards(exe, record, settings, env, cwd, folder, start):
    groups = shard_plan(record["test_names"], settings)
    ignored = ["--include-ignored"] if settings["ignored"] else []
    running = []
    for index, group in enumerate(groups, start=1):
        log = open(folder / f"test-{index}.log", "wb")
        command = [str(exe), *ignored, "--nocapture", f"--test-threads={settings['threads']}", *settings["extra"],
                   "--exact", *group]
        ports = dict(env, TENRAI_TEST_PORTS=f"{39000 + 1000 * index}-{39999 + 1000 * index}")
        running.append((group, log, subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=ports, cwd=cwd)))
    killed = set()
    for _, _, process in running:
        try:
            process.wait(timeout=max(1.0, start + settings["timeout"] - time.time()))
        except subprocess.TimeoutExpired:
            killed = {id(other) for _, _, other in running if other.poll() is None}
            for _, _, other in running:
                if other.poll() is None:
                    other.kill()
            break
    parts = []
    with open(folder / "test.log", "w", encoding="utf-8", errors="replace") as combined:
        for index, (group, log, process) in enumerate(running, start=1):
            process.wait()
            log.close()
            code = 124 if id(process) in killed else process.returncode
            text = (folder / f"test-{index}.log").read_text(errors="replace")
            combined.write(f"==== shard {index}: {len(group)} tests, exit {code}\n{text}\n")
            shard_settings = dict(settings, min_tests=1, expect=[test for test in settings["expect"] if test in group])
            part = qualified_result(text, len(group), shard_settings, code)
            # --exact selects exactly the group, so a full count proves every one of its tests passed.
            if part["passed"] != len(group) or part["failed"] or part["ignored"]:
                part["qualification_failures"].append(f"{part['passed']} of its {len(group)} tests passed")
                part["qualified"] = False
            parts.append((group, part))
    result = combined_result(parts, settings, record["tests"])
    result.update(timed_out=bool(killed))
    return result


def run(name, filter_, settings):
    record, reasons = check(name, filter_, settings)
    if reasons:
        for reason in reasons:
            print("preflight refused:", reason, flush=True)
        sys.exit(3)
    admission = fresh_admission(Path(record["binary"]), record["sha256"], settings)
    if admission is not None:
        record["run_admission"] = admission
        if not admission["admitted"]:
            print("preflight refused: fresh native-run environment check refused", flush=True)
            sys.exit(3)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = Path(settings["out"]) / f"{name}-{re.sub(r'[^A-Za-z0-9_]+', '_', filter_)}-{stamp}"
    folder.mkdir(parents=True)
    if settings["shards"] > 1:
        record["shard_plan"] = shard_plan(record["test_names"], settings)
    (folder / "preflight.json").write_text(json.dumps(record, indent=2) + "\n")
    env = dict(os.environ, **settings["env"])
    cwd = env.get("TENRAI_TEST_CWD", folder.resolve())
    start = time.time()
    if settings["shards"] > 1:
        result = run_shards(STORE / f"{name}.exe", record, settings, env, cwd, folder, start)
    else:
        command = [str(STORE / f"{name}.exe"), *test_arguments(filter_, settings)]
        with open(folder / "test.log", "wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=cwd)
            try:
                code = process.wait(timeout=settings["timeout"])
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                code = 124
        text = (folder / "test.log").read_text(errors="replace")
        result = qualified_result(text, record["tests"], settings, code)
        result.update(timed_out=code == 124)
    result.update(seconds=round(time.time() - start, 1))
    if settings.get("allowed_images"):
        result["execution_scope"] = "shared_functional_tests"
        result["allowed_background_images"] = settings["allowed_images"]
        result["allowed_background_at_check"] = record.get("allowed_background", [])
    (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"{name} {filter_}: exit {result['exit']} (native {result['native_exit']}), {result['passed']} passed, {result['failed']} failed of "
          f"{record['tests']} in {result['seconds']} s; {folder}", flush=True)
    for failure in result["failures"]:
        print("    FAILED", failure, flush=True)
    for reason in result["qualification_failures"]:
        print("    NOT QUALIFIED:", reason, flush=True)
    sys.exit(result["exit"])


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    action = sys.argv[1]
    if action == "freeze":
        freeze(sys.argv[2], sys.argv[3])
    elif action in ("check", "run"):
        settings = options(sys.argv[4:])
        if action == "check":
            record, reasons = check(sys.argv[2], sys.argv[3], settings)
            print(json.dumps(record, indent=2))
            for reason in reasons:
                print("preflight refused:", reason)
            sys.exit(3 if reasons else 0)
        run(sys.argv[2], sys.argv[3], settings)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
