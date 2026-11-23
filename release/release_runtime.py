"""Bound each release command to its public source inputs and fresh resource check."""
from argparse import ArgumentParser
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

import release_evidence as evidence

SCHEMA = "tenrai-release-command-boundary/v1"
LOCKS = (
    ".cargo/config", ".cargo/config.toml", "Cargo.toml", "Cargo.lock", "rust-toolchain.toml",
    "crates/tsp2-prover/Cargo.lock", "crates/tsp2-circuits/Cargo.lock",
    "crates/tsp2-prover-wasm/Cargo.lock", "vendor/tsp2-proving-backend/Cargo.lock",
)
FIRST_PARTY = ("wasm", "prover", "vendor/tenrai-wasm", "vendor/tenrai-prover",
               "vendor/tenrai-sdk", "public/vendor/tenq", "node_modules/@tenrai/sdk")
TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}")
POLICY = ("Ignore-aware public source membership using rg --files --hidden --no-require-git, "
          "plus explicit local Cargo/config/toolchain lock inputs; no version-control command. "
          "This is a command source boundary, not proof of a complete compiler/include closure.")

def require(value, message):
    if not value:
        raise evidence.GateError(message)

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def inventory(directory):
    directory = Path(directory).resolve()
    require(directory.is_dir(), f"missing source directory: {directory}")
    result = subprocess.run(["rg", "--files", "--hidden", "--no-require-git", "--null",
                             "--glob", "!.git/**"], cwd=directory, capture_output=True)
    require(result.returncode in (0, 1), f"public source discovery failed in {directory}")
    names = set(result.stdout.decode("utf-8").split("\0")) - {""}
    names = {name.replace("\\", "/") for name in names}
    names.update(name for name in LOCKS if (directory / name).is_file())
    # These generated first-party packages are actual client inputs even when ignored by rg.
    for folder in FIRST_PARTY:
        target = directory / folder
        if target.is_dir():
            names.update(path.relative_to(directory).as_posix() for path in target.rglob("*")
                         if path.is_file() and "node_modules" not in path.relative_to(target).parts)
    require(bool(names), f"no public inputs found in {directory}")
    hashes = {}
    for name in sorted(names):
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts, "invalid source name")
        path = directory / relative
        require(not path.is_symlink() and path.resolve().is_relative_to(directory), f"source link: {path}")
        hashes[name] = evidence.sha(path)  # An unreadable/deleted input refuses the step.
    return {"root": str(directory), "policy": POLICY, "files": hashes, "fingerprint": digest(hashes)}

def sources(repositories):
    roots = [Path(path).resolve() for path in repositories]
    require(len(set(roots)) == len(roots) and bool(roots), "each command needs distinct explicit source roots")
    return {str(path): inventory(path) for path in roots}

CONTROL_DIRECTORIES = (
    "qualification", "release", "p2p-bench", "browser-wallet", "browser-prover",
    "mesh", "storage", "topology", "web-memory",
)
CONTROL_GLOBS = ("*.py", "*.sh", "*.ps1", "*.js", "*.mjs", "*.cjs", "*.ts",
                 "package.json", "package-lock.json")


def controls(envcheck):
    envcheck = Path(envcheck).resolve()
    tests = envcheck.parents[1]
    require(envcheck.parent.name == "qualification" and envcheck.name == "envcheck.py",
            "qualification resource check must be its explicit envcheck.py")
    folders = [name for name in CONTROL_DIRECTORIES if (tests / name).is_dir()]
    require(bool(folders), "qualification controller source directories are missing")
    command = ["rg", "--files", "--hidden", "--no-require-git", "--null", "--glob", "!.git/**"]
    for pattern in CONTROL_GLOBS:
        command += ["--glob", pattern]
    result = subprocess.run(command + folders, cwd=tests, capture_output=True)
    require(result.returncode in (0, 1), "qualification controller discovery failed")
    names = {name.replace(chr(92), "/") for name in result.stdout.decode("utf-8").split("\0") if name}
    paths = set()
    for name in names:
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts
                and relative.parts[0] in CONTROL_DIRECTORIES, "invalid qualification controller name")
        path = tests / relative
        require(not path.is_symlink() and path.resolve().is_relative_to(tests),
                f"qualification controller link: {path}")
        paths.add(path.resolve())
    # A controller may also be executed from a sealed copy outside the tests root.
    paths.update(path.resolve() for path in Path(__file__).resolve().parent.iterdir()
                 if path.is_file() and path.suffix in (".py", ".sh", ".ps1"))
    paths.update((envcheck, tests / "p2p-bench/preflight.py", tests / "p2p-bench/bench_proc.py"))
    # Bind discovery policy as well as source membership without traversing other repositories.
    for folder in (tests, *(tests / name for name in folders)):
        paths.update((folder / name).resolve() for name in (".gitignore", ".ignore", ".rgignore")
                     if (folder / name).is_file())
    return {str(path): evidence.sha(path) for path in sorted(paths)}


def control_changes(saved):
    """The control paths added, removed or changed since `saved`, sorted; empty when they are stable."""
    guards = [Path(name) for name in saved if Path(name).name == "envcheck.py"
              and Path(name).parent.name == "qualification"]
    require(len(guards) == 1, "qualification control receipt needs one explicit resource-check root")
    # Re-discover current membership; checking only old paths misses newly added helpers.
    current = controls(guards[0])
    return sorted(name for name in set(saved) | set(current) if saved.get(name) != current.get(name))


def stable_controls(saved):
    return not control_changes(saved)


def held(work, path):
    work = evidence.artifact_root(work)
    path = Path(path).resolve()
    require(path.is_relative_to(work / "source-boundaries"), "boundary receipt escapes its work directory")
    return path

def changed(before, after):
    result = []
    for root in set(before) | set(after):
        a = before.get(root, {}).get("files", {})
        b = after.get(root, {}).get("files", {})
        result.extend((root, name) for name in set(a) | set(b) if a.get(name) != b.get(name))
    return sorted(result)

def permitted_mutation(before, after, allowed):
    prefixes = []
    for text in allowed:
        root, separator, prefix = text.rpartition("=")
        relative = Path(prefix)
        require(separator and root and prefix and not relative.is_absolute()
                and ".." not in relative.parts, "invalid explicit mutation prefix")
        prefixes.append((str(Path(root).resolve()), relative.as_posix().rstrip("/")))
    return all(any(root == owner and (name == prefix or name.startswith(prefix + "/"))
                   for owner, prefix in prefixes) for root, name in changed(before, after))

def sequence_event(work):
    folder = work / "source-boundaries"
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".bookkeeping.lock"
    # Exclusive creation gives ordering independent of wall-clock adjustments.
    with lock.open("x", encoding="utf-8") as held_lock:
        held_lock.write(str(os.getpid()))
    try:
        counter = folder / ".sequence.json"
        prior = evidence.load(counter)["value"] if counter.exists() else 0
        require(type(prior) is int and 0 <= prior < (1 << 64) - 1, "invalid/exhausted release event counter")
        value = prior + 1
        evidence.write(counter, {"value": value})
        return f"{value:020d}"
    finally:
        lock.unlink()  # One exact file created above, exclusively inside the checked OUT.

def latest(work):
    by_name = {}
    by_root = {}
    for ticket in sorted((work / "source-boundaries").glob("*/before.json")):
        saved = evidence.load(ticket)
        require(saved.get("schema") == SCHEMA, "invalid earlier command boundary")
        sequence = saved["sequence"]
        result = ticket.with_name("after.json")
        after = evidence.load(result) if result.exists() else None
        entry = (sequence, ticket, saved, after)
        if saved["name"] not in by_name or sequence > by_name[saved["name"]][0]:
            by_name[saved["name"]] = entry
        source = after.get("sources_after", saved.get("sources_before", {})) if after else saved.get("sources_before", {})
        completed = after.get("completion_sequence", sequence) if after else sequence
        for root, state in source.items():
            if root not in by_root or completed > by_root[root][0]:
                by_root[root] = (completed, state)
    return by_name, by_root

def continuity(work, before):
    _, roots = latest(work)
    for root, state in before.items():
        require(root not in roots or roots[root][1] == state,
                f"unexplained source change between commands in {root}; use a new release candidate after source edits")

def input_files(files, frozen):
    hashes = {}
    memberships = {}
    for text in files:
        name, separator, expected = text.rpartition("=")
        require(separator and len(expected) >= 16, "invalid explicit binary/input guard")
        path = Path(name).resolve()
        actual = evidence.sha(path)
        require(actual.startswith(expected.lower()), f"input guard differs: {path}")
        hashes[str(path)] = actual
    for name in frozen:
        folder = Path(name).resolve()
        require(folder.is_dir() and not folder.is_symlink(), "frozen input folder is missing or linked")
        listed = {}
        for path in folder.rglob("*"):
            require(not path.is_symlink(), "frozen input is linked")
            if path.is_file():
                listed[path.relative_to(folder).as_posix()] = evidence.sha(path)
        require(bool(listed), "empty frozen input folder")
        memberships[str(folder)] = listed
    return {"files": hashes, "frozen_folders": memberships}

def stable_inputs(saved):
    return (all(evidence.sha(Path(path)) == value for path, value in saved["files"].items())
            and input_files([], saved["frozen_folders"])["frozen_folders"] == saved["frozen_folders"])

def begin(arguments):
    work = evidence.artifact_root(arguments.work)
    require(TOKEN.fullmatch(arguments.name) is not None, "invalid command boundary name")
    evidence.write(work / "COMMAND-SOURCES.json", {"schema": SCHEMA, "status": "NOT TESTED",
                                                  "reason": "a new command boundary began"})
    sequence = sequence_event(work)
    folder = work / "source-boundaries" / (sequence + "-" + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    ticket = folder / "before.json"
    record = {"schema": SCHEMA, "name": arguments.name, "created_utc": datetime.now(timezone.utc).isoformat(),
              "sequence": sequence, "status": "INVALID", "work": str(work)}
    try:
        before = sources(arguments.repo)
        continuity(work, before)
        require(arguments.kind == "mutation" or not arguments.allow_change,
                "only an explicit mutation may change source inputs")
        if arguments.kind == "mutation":
            require(bool(arguments.allow_change), "a mutation needs explicit allowed prefixes")
        record.update(kind=arguments.kind, allowed_changes=arguments.allow_change)
        trusted = controls(arguments.envcheck)
        extra_inputs = input_files(arguments.file, arguments.frozen)
        command = [sys.executable, "-E", "-s", "-B", str(Path(arguments.envcheck).resolve()),
                   "--commit-gb", str(arguments.commit_gb)]
        if arguments.holder:
            command += ["--holder", arguments.holder]
        for room in arguments.room:
            command += ["--room", room]
        for frozen in arguments.frozen:
            command += ["--frozen", frozen]
        for file in arguments.file:
            command += ["--file", file]
        # Hundreds of controls exceed the Windows command line; they go through a list file.
        listed = folder / "controls.txt"
        listed.write_text("".join(f"{path}={value}\n" for path, value in sorted(trusted.items())), encoding="utf-8")
        command += ["--files-from", str(listed)]
        checked = subprocess.run(command, capture_output=True, timeout=120)
        evidence.write(folder / "preflight.json", {
            "command": command, "exit_code": checked.returncode,
            "stdout": checked.stdout.decode("utf-8", errors="replace"),
            "stderr": checked.stderr.decode("utf-8", errors="replace"), "controls_sha256": trusted,
        })
        record.update(sources_before=before, controls_sha256=trusted, input_guards=extra_inputs,
                      preflight_sha256=evidence.sha(folder / "preflight.json"),
                      preflight_exit=checked.returncode)
        require(checked.returncode == 0, "fresh environment check refused this invocation")
        require(stable_controls(trusted), "qualification controls changed during preflight")
        require(stable_inputs(extra_inputs), "explicit/frozen inputs changed during preflight")
        require(sources(arguments.repo) == before, "source inputs changed during preflight")
        record["status"] = "READY"
        evidence.write(ticket, record)
        print(str(ticket), flush=True)
        return 0
    except Exception as error:
        record["reason"] = str(error)
        evidence.write(ticket, record)
        print(f"command boundary refused: {error}", file=sys.stderr)
        return 3

def make_words(text):
    """Split Cargo Make tokens, retaining Windows separators and unescaping only whitespace."""
    words, word, offset = [], "", 0
    while offset < len(text):
        char = text[offset]
        if char == chr(92) and offset + 1 < len(text) and text[offset + 1].isspace():
            word += text[offset + 1]
            offset += 2
            continue
        if char.isspace():
            if word:
                words.append(word)
                word = ""
        else:
            word += char
        offset += 1
    if word:
        words.append(word)
    return words

def rust_inputs(artifact, saved):
    """Corroborate local compiled source membership from Cargo's emitted Make dep-info."""
    info = artifact.with_suffix(".d")
    if artifact.suffix.lower() != ".exe" or not info.is_file():
        return None
    line = info.read_text(encoding="utf-8").splitlines()[0]
    emitted_target, separator, remainder = line.partition(": ")
    require(bool(separator), "unreadable Cargo dep-info rule")
    words = make_words(remainder)
    local = {}
    registry = (Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")) / "registry/src").resolve()
    target_names = make_words(emitted_target)
    require(len(target_names) == 1, "ambiguous Cargo target path")
    actual_target = Path(target_names[0]).resolve()
    target = actual_target.parent.parent if actual_target.parent.name == "deps" else actual_target.parent
    target = target.parent  # Cargo's generated OUT_DIR inputs live under this owned target.
    roots = {Path(root).resolve(): value["files"] for root, value in saved["sources_before"].items()}
    for name in words:
        path = Path(name)
        if not path.is_absolute():
            # rustc records relative paths from the build workspace.
            bases = [root for root in roots if (root / path).exists()]
            if not bases:
                continue  # Unused build-script watch path.
            require(len(bases) == 1, f"compiled input is ambiguous between source roots: {name}")
            path = bases[0] / path
        path = path.resolve()
        if path.is_relative_to(target):
            continue  # Generated sources are outputs of the recorded build scripts.
        for root, files in roots.items():
            if path.is_relative_to(root):
                relative = path.relative_to(root).as_posix()
                if relative not in files and not path.exists():
                    break  # A missing watch path contributes no input.
                require(relative in files, f"compiled local input was outside the before-inventory: {path}")
                require(evidence.sha(path) == files[relative], f"compiled local input changed: {path}")
                local[str(path)] = files[relative]
                break
        else:
            require(path.is_relative_to(registry) or path.is_relative_to(target),
                    f"compiled input belongs to an undeclared source root: {path}")
    require(bool(local), "Cargo dep-info names no source in the explicit input roots")
    return {"path": str(info), "sha256": evidence.sha(info), "emitted_target": str(actual_target), "local_inputs_sha256": local}

def production_profiles(log, artifacts):
    """Bind actual Cargo bin artifacts to default production profiles, never a test harness."""
    messages = []
    for line in Path(log).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if value.get("reason") == "compiler-artifact":
            if value.get("package_id", "").startswith("path+"):
                require(not ({"test-support", "test_support"} & set(value.get("features", []))),
                        "production invocation selected a local test-support feature")
            messages.append(value)
    require(bool(messages), "production build has no actual Cargo artifact messages")
    confirmed = {}
    for artifact in artifacts:
        compiled = rust_inputs(artifact, {"sources_before": artifacts[artifact]["sources"]})
        require(compiled is not None, "production artifact has no Cargo input descriptor")
        emitted = Path(compiled["emitted_target"]).resolve()
        candidates = [value for value in messages
                      if value["target"]["kind"] == ["bin"]
                      and any(Path(name).resolve() == emitted for name in value["filenames"])]
        require(len(candidates) == 1, "production binary is absent/ambiguous in the actual Cargo artifact stream")
        profile = candidates[0]["profile"]
        require(profile["test"] is False and profile["debug_assertions"] is False
                and profile["opt_level"] != "0", "test/debug/unoptimized artifact cannot be a production binary")
        require(evidence.sha(emitted) == evidence.sha(artifact), "published snapshot differs from actual Cargo binary")
        confirmed[str(artifact)] = {"target": candidates[0]["target"]["name"], "profile": profile,
                                    "package_id": candidates[0]["package_id"]}
    return {"log": str(Path(log).resolve()), "sha256": evidence.sha(Path(log)), "artifacts": confirmed}

def end(arguments):
    work = evidence.artifact_root(arguments.work)
    ticket = held(work, arguments.ticket)
    saved = evidence.load(ticket)
    result_path = ticket.with_name("after.json")
    require(not result_path.exists(), "a command boundary result cannot be overwritten")
    require(saved.get("schema") == SCHEMA and saved.get("status") == "READY", "command never passed its preflight")
    require(saved.get("work") == str(work), "command belongs to another work directory")
    record = {"schema": SCHEMA, "name": saved["name"], "sequence": saved["sequence"],
              "completion_sequence": sequence_event(work),
              "status": "INVALID", "native_exit_code": arguments.exit_code,
              "ticket_sha256": evidence.sha(ticket), "completed_utc": datetime.now(timezone.utc).isoformat()}
    try:
        after = sources(saved["sources_before"])
        record["sources_after"] = after
        moved = control_changes(saved["controls_sha256"])
        require(not moved, "qualification controls changed during command: " + ", ".join(moved[:8]))
        require(stable_inputs(saved["input_guards"]), "explicit/frozen inputs changed during command")
        require(evidence.sha(ticket.with_name("preflight.json")) == saved["preflight_sha256"],
                "preflight evidence changed during command")
        if saved.get("kind") == "mutation":
            require(permitted_mutation(saved["sources_before"], after, saved["allowed_changes"]),
                    "source mutation changed an input outside its explicit allowed prefixes")
            record["changed_inputs"] = changed(saved["sources_before"], after)
        else:
            require(after == saved["sources_before"], "public source membership or bytes changed during command")
        artifacts = {}
        compile_inputs = {}
        cargo_artifacts = {Path(path).resolve() for path in getattr(arguments, "cargo_artifact", [])}
        output_files = [Path(path).resolve() for path in arguments.artifact] + sorted(cargo_artifacts)
        for tree in arguments.artifact_tree:
            directory = Path(tree).resolve()
            require(directory.is_dir() and not directory.is_symlink(), "artifact tree is missing or linked")
            for path in directory.rglob("*"):
                require(not path.is_symlink(), "artifact tree is linked")
                if path.is_file():
                    output_files.append(path)
        for file in output_files:
            file = file.resolve()
            require(file.is_file() and not file.is_symlink(), f"missing or linked command artifact: {file}")
            artifacts[str(file)] = evidence.sha(file)
            compiled = rust_inputs(file, saved)
            require(file not in cargo_artifacts or compiled is not None,
                    f"Cargo artifact has no compiled-input dep-info: {file}")
            if compiled is not None:
                compile_inputs[str(file)] = compiled
        record["artifacts_sha256"] = artifacts
        record["artifact_trees"] = input_files([], arguments.artifact_tree)["frozen_folders"]
        record["cargo_local_inputs"] = compile_inputs
        cargo_log = getattr(arguments, "cargo_log", None)
        if cargo_log:
            record["production_profiles"] = production_profiles(
                cargo_log, {artifact: {"sources": saved["sources_before"]} for artifact in cargo_artifacts})
        record["status"] = "PASS" if arguments.exit_code == 0 else "FAIL"
    except Exception as error:
        record["reason"] = str(error)
    evidence.write(result_path, record)
    print(json.dumps({"name": saved["name"], "source_boundary": record["status"],
                      "native_exit_code": arguments.exit_code, "receipt": str(result_path)}), flush=True)
    if record["status"] != "PASS":
        return arguments.exit_code or 2
    return 0

def verify(arguments, allow_identity_mutation=False):
    work = evidence.artifact_root(arguments.work)
    evidence.write(work / "COMMAND-SOURCES.json", {"schema": SCHEMA, "status": "NOT TESTED",
                                                  "reason": "source boundary verification is incomplete"})
    by_name, _ = latest(work)
    require(bool(arguments.require), "verification needs explicit required command names")
    require(set(arguments.require).issubset(by_name), "required command source boundaries are missing")
    results = {}
    # Every latest invocation is checked, including failed/pending child commands.
    for name, (_, ticket, saved, after) in by_name.items():
        if (allow_identity_mutation and name == "records-identity"
                and saved.get("kind") == "mutation" and after is None):
            continue
        require(after is not None, f"{name} has no finished command boundary")
        require(saved.get("status") == "READY" and saved["preflight_exit"] == 0, f"{name} was never admitted")
        require(after.get("status") == "PASS" and after.get("native_exit_code") == 0, f"{name} did not pass")
        require(after["ticket_sha256"] == evidence.sha(ticket), f"{name} source receipt changed")
        require(stable_controls(saved["controls_sha256"]), f"{name} qualification controls changed")
        if saved.get("kind") != "setup":
            require(stable_inputs(saved["input_guards"]), f"{name} explicit/frozen inputs changed")
        require(evidence.sha(ticket.with_name("preflight.json")) == saved["preflight_sha256"],
                f"{name} preflight changed")
        if saved.get("kind") == "mutation":
            require(permitted_mutation(saved["sources_before"], after["sources_after"], saved["allowed_changes"]),
                    f"{name} has an unauthorized source mutation")
        else:
            require(after.get("sources_after") == saved.get("sources_before"), f"{name} sources differ")
        if after.get("production_profiles"):
            profile = after["production_profiles"]
            require(evidence.sha(Path(profile["log"])) == profile["sha256"], f"{name} actual Cargo log changed")
        if saved.get("kind") in ("build", "test"):
            require(sources(saved["sources_before"]) == saved["sources_before"],
                    f"{name} build/test inputs changed afterwards; rebuild this command before release")
            require(input_files([], after.get("artifact_trees", {}))["frozen_folders"]
                    == after.get("artifact_trees", {}), f"{name} artifact membership/bytes changed afterwards")
            require(all(evidence.sha(Path(path)) == value for path, value in after["artifacts_sha256"].items()),
                    f"{name} artifact changed afterwards")
        results[name] = {"kind": saved["kind"], "before_sha256": evidence.sha(ticket),
                         "after_sha256": evidence.sha(ticket.with_name("after.json"))}
    published = published_artifacts(work, by_name) if getattr(arguments, "final_artifacts", False) else None
    evidence.write(work / "COMMAND-SOURCES.json", {
        "schema": SCHEMA, "status": "PASS", "scope": POLICY, "published_artifacts": published,
        "required_commands": results,
        "limitations": [
            "Each actual interval and intervening explicit mutation is recorded; unexplained between-step drift refuses continuation.",
            "All recorded build/test input roots must still match at final verification; stale builds require a rebuild.",
            "Final SOURCES.json remains an end-state inventory and is not renamed as compiled-source evidence.",
            "Compiler/include closure, installed dependency integrity, and final artifact-to-source attestation remain separate gates.",
        ],
    })
    return {"path": str(work / "COMMAND-SOURCES.json"), "sha256": evidence.sha(work / "COMMAND-SOURCES.json"),
            "commands": len(results), "scope": POLICY}

def published_artifacts(work, by_name):
    compiled = {}
    jobs = ("binaries-workspace", "binaries-tenrai-reference-pool", "binaries-tenrai-pool",
            "binaries-tenrai-explorer", "binaries-tenrai-api", "binaries-tenrai-tsp2-prove")
    for job in jobs:
        require(job in by_name and by_name[job][3] is not None, f"missing compile receipt: {job}")
        require(by_name[job][3].get("production_profiles"), f"missing actual production profile confirmation: {job}")
        for path, value in by_name[job][3]["artifacts_sha256"].items():
            name = Path(path).name
            require(name not in compiled or compiled[name] == value, "conflicting compiled artifact identity")
            compiled[name] = value
    expected = {"tenraid.exe", "tenrai-release.exe", "tenrai-wallet.exe", "tenrai-miner.exe",
                "tenrai-stratum-bridge.exe", "tenrai-reference-pool.exe", "tenrai-pool.exe",
                "tenrai-explorer.exe", "tenrai-api.exe", "tenrai-tsp2-prove.exe"}
    manifest = evidence.load(work / "bin/manifest.json")
    files = manifest.get("files")
    require(isinstance(files, dict) and set(files) == expected, "published binary set is incomplete or unexpected")
    for name, value in files.items():
        require(compiled.get(name) == value and evidence.sha(work / "bin" / name) == value,
                f"published artifact does not match its actual compile receipt: {name}")
    desktop = evidence.load(work / "desktop/manifest.json").get("files")
    release = by_name.get("desktop-release-build")
    require(release is not None and release[3] is not None, "missing desktop compile boundary")
    values = set(release[3]["artifacts_sha256"].values())
    require(isinstance(desktop, dict) and len(desktop) == 1, "desktop release set is incomplete")
    for name, value in desktop.items():
        require(Path(name).name == name and value in values
                and evidence.sha(work / "desktop" / name) == value,
                "published desktop does not match its actual release build")
    return {"binaries": files, "desktop": desktop}

def require_final(work, allow_identity_mutation=False):
    """Mandatory for direct records.py release/verify, as well as the driver."""
    from types import SimpleNamespace
    identity = evidence.identity_inputs(work)["record"]
    checks, _ = evidence.required_checks(identity["private_transfers_from_genesis"])
    required = [
        "genesis-tools", "binaries-workspace", "binaries-tenrai-reference-pool", "binaries-tenrai-pool",
        "binaries-tenrai-explorer", "binaries-tenrai-api", "binaries-tenrai-tsp2-prove",
        "wasm-packages", "web-frontends", "web-paper-wasm", "web-paper-wallet",
        "desktop-dependencies", "desktop-test-build", "desktop-release-build", "checks-node-build",
    ]
    required += [
        "wasm-wallet-record", "wasm-wallet-build", "wasm-tenq-record", "wasm-tenq-build",
        "wasm-vendor-wallet-wasm", "wasm-vendor-tenq", "wasm-prover-build", "wasm-vendor-prover",
        "front-sdk-build", "front-sdk-types", "front-sdk-test", "front-vendor-wallet",
        "front-vendor-explorer", "front-refwallet-check", "front-refwallet-test", "front-refwallet-build",
        "front-refexplorer-check", "front-refexplorer-test", "front-refexplorer-build",
        "front-wallet-pin", "front-wallet-check", "front-wallet-test", "front-wallet-verify",
        "front-miner-test", "browser-normal", "browser-reference", "browser-sdk", "browser-gateway",
    ]
    required += ["check-" + name for _, name in checks]
    return verify(SimpleNamespace(work=work, require=required, final_artifacts=True), allow_identity_mutation)

def inventory_record(arguments):
    work = evidence.artifact_root(arguments.work)
    repos = sources(arguments.repo)
    report = {"taken": datetime.now(timezone.utc).isoformat(), "repositories": {}, "scope": POLICY}
    for root, state in repos.items():
        name = Path(root).name
        require(name not in report["repositories"], "duplicate repository name in final inventory")
        hashes = state["files"]
        aggregate = hashlib.sha256("".join(f"{path}\0{value}\n"
            for path, value in sorted(hashes.items())).encode()).hexdigest()
        report["repositories"][name] = {"root": root, "files": len(hashes),
                                        "digest": aggregate, "hashes": hashes, "unreadable": []}
    evidence.write(work / "SOURCES.json", report)
    return 0

def copy_public(arguments):
    work = evidence.artifact_root(arguments.work)
    destination = Path(arguments.destination).resolve()
    require(destination.is_relative_to(work / "scratch") and destination != work / "scratch",
            "source copies must be new children of this release's scratch directory")
    require(not destination.exists(), "a source copy never overwrites an earlier directory")
    source = inventory(arguments.repo)
    destination.mkdir(parents=True, exist_ok=False)
    for name, value in source["files"].items():
        original = Path(source["root"]) / name
        output = destination / name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, output)
        require(evidence.sha(output) == value, "source copy differs")
    require(inventory(arguments.repo) == source, "source changed during copy")
    evidence.write(destination.with_name(destination.name + ".source-copy.json"),
                   {"source": source, "destination": str(destination)})
    return 0

def archive_owned(arguments):
    work = evidence.artifact_root(arguments.work)
    source = Path(arguments.path)
    resolved = source.resolve()
    require(resolved.is_relative_to(work) and resolved != work, "archive target escapes this release")
    require(not source.is_symlink(), "archive target is linked")
    require(resolved.parts[len(work.parts)] not in ("source-boundaries", "evidence-history"),
            "source receipts/history cannot be archived as a mutable artifact")
    if source.exists():
        destination = work / "evidence-history" / ("artifact-" + str(time.time_ns()) + "-" + uuid.uuid4().hex)
        destination.mkdir(parents=True, exist_ok=False)
        source.rename(destination / source.name)
    return 0

def main():
    parser = ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("begin")
    start.add_argument("--work", required=True)
    start.add_argument("--name", required=True)
    start.add_argument("--repo", action="append", default=[], required=True)
    start.add_argument("--envcheck", required=True)
    start.add_argument("--holder")
    start.add_argument("--kind", choices=("build", "test", "setup", "mutation"), default="build")
    start.add_argument("--allow-change", action="append", default=[])
    start.add_argument("--commit-gb", type=float, default=8)
    start.add_argument("--room", action="append", default=[])
    start.add_argument("--frozen", action="append", default=[])
    start.add_argument("--file", action="append", default=[])
    finish = sub.add_parser("end")
    finish.add_argument("--work", required=True)
    finish.add_argument("--ticket", required=True)
    finish.add_argument("--exit-code", type=int, required=True)
    finish.add_argument("--artifact", action="append", default=[])
    finish.add_argument("--cargo-artifact", action="append", default=[])
    finish.add_argument("--cargo-log")
    finish.add_argument("--artifact-tree", action="append", default=[])
    check = sub.add_parser("verify")
    check.add_argument("--work", required=True)
    check.add_argument("--require", action="append", default=[])
    listing = sub.add_parser("inventory")
    listing.add_argument("--work", required=True)
    listing.add_argument("--repo", action="append", default=[], required=True)
    copying = sub.add_parser("copy")
    copying.add_argument("--work", required=True)
    copying.add_argument("--repo", required=True)
    copying.add_argument("--destination", required=True)
    archiving = sub.add_parser("archive")
    archiving.add_argument("--work", required=True)
    archiving.add_argument("--path", required=True)
    args = parser.parse_args()
    try:
        if args.action == "verify":
            value = verify(args) if args.require else require_final(args.work)
            print(json.dumps(value), flush=True)
            return 0
        return {"begin": begin, "end": end, "inventory": inventory_record, "copy": copy_public, "archive": archive_owned}[args.action](args)
    except Exception as error:
        print(f"release command boundary refused: {error}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
