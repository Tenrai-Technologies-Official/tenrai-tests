"""Validate exact release evidence. Only local public-history Git reads run; no product command starts."""
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
import release_identity as identity_history

TOOLS = Path(__file__).resolve().parent
FIELDS = ("network", "chain_id", "genesis_id", "params_hash", "rules_digest")
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX96 = re.compile(r"[0-9a-f]{96}")
SCOPE = "release_pipeline_required_checks_and_builtin_identity"


class GateError(RuntimeError):
    def __init__(self, message, status="INVALID", details=None):
        if status not in ("PASS", "FAIL", "INVALID", "NOT TESTED"):
            raise ValueError("invalid evidence status")
        super().__init__(message)
        self.status = status
        self.details = details or {}


def require(condition, message, status="INVALID"):
    if not condition:
        raise GateError(message, status)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load(path):
    path = Path(path)
    require(path.is_file(), f"missing evidence file: {path}", "NOT TESTED")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, ValueError) as error:
        raise GateError(f"invalid evidence JSON: {path}: {error}") from error


def artifact_root(path):
    path = Path(path).resolve()
    require(not any((parent / ".git").exists() for parent in (path, *path.parents)),
            "raw qualification records belong outside source repositories")
    return path


def write(path, value):
    """Replace a single public evidence file atomically; never leave a partial PASS."""
    path = Path(path).resolve()
    artifact_root(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with pending.open("xb") as destination:
            destination.write((json.dumps(value, indent=2) + "\n").encode())
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(pending, path)
    finally:
        if pending.exists():
            pending.unlink()


def cli_values(output):
    values = {}
    for line in output.splitlines():
        if line.startswith("upgrade="):
            raise GateError("mainnet built-in identity must not name a scheduled upgrade")
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in FIELDS:
            require(key not in values, f"duplicate Node CLI identity field: {key}")
            values[key] = value
    require(values.get("network") == "mainnet", "Node CLI did not identify mainnet")
    for key in FIELDS[1:]:
        require(HEX96.fullmatch(values.get(key, "")) is not None, f"missing or invalid Node CLI {key}")
    return values


def identity_inputs(work):
    work = Path(work)
    identity_path = work / "genesis/identity.json"
    manifest_path = work / "genesis/mainnet.traidag.json"
    identity = load(identity_path)
    manifest = load(manifest_path)
    values = {key: identity.get(key) for key in FIELDS}
    require(values["network"] == "mainnet" and manifest.get("network") == "mainnet", "identity/manifest is not mainnet")
    for key in FIELDS[1:]:
        require(isinstance(values[key], str) and HEX96.fullmatch(values[key]) is not None, f"invalid recorded {key}")
    require(manifest.get("chain_id") == values["chain_id"], "manifest chain differs from identity")
    require(type(identity.get("private_transfers_from_genesis")) is bool, "missing explicit private activation choice")
    if "private_transfers_from" in identity:
        height = identity["private_transfers_from"]
        require(height is None or (type(height) is int and height >= 0), "invalid private activation height")
        require(identity["private_transfers_from_genesis"] == (height == 0),
                "private activation fields disagree")
    manifest_hash = sha(manifest_path)
    require(identity.get("manifest_sha256") == manifest_hash, "manifest changed after public identity derivation")
    try:
        history = identity_history.verify(identity_history.public_files(), values["chain_id"],
                                          values["genesis_id"], identity.get("identity_history"))
    except identity_history.IdentityHistoryError as error:
        raise GateError("public identity history refused this release: " + str(error)) from error
    return {"record": identity, "values": values, "identity_sha256": sha(identity_path),
            "manifest_sha256": manifest_hash, "identity_history": history}


def phase_body(source, name):
    found = re.findall(r"^" + re.escape(name) + r"\(\) \{\n(.*?)^\}\s*$", source, re.M | re.S)
    require(len(found) == 1, f"release driver needs exactly one readable {name}; review its mandatory checks")
    return found[0]


def required_checks(private_active, driver=None):
    """Read real check calls; the sole recognized conditional is private activation."""
    driver = Path(driver) if driver else TOOLS / "build-release.sh"
    source = driver.read_text(encoding="utf-8")
    body = phase_body(source, "phase_checks")
    conditional = re.search(r'^\s*if \[ "\$private" = yes \]; then\n(.*?)^\s*fi\s*$', body, re.M | re.S)
    require(conditional is not None and len(re.findall(r"^\s*if\s", body, re.M)) == 1,
            "review unsupported release-check conditionals before qualifying a release")
    names = re.findall(r"^\s*check\s+([a-z0-9][a-z0-9_-]*)\s+", body, re.M)
    private_names = re.findall(r"^\s*check\s+([a-z0-9][a-z0-9_-]*)\s+", conditional.group(1), re.M)
    require(private_names == ["private-from-genesis"], "private activation gate changed unexpectedly")
    desktop = re.findall(r"^\s*check\s+([a-z0-9][a-z0-9_-]*)\s+", phase_body(source, "phase_desktop"), re.M)
    require("desktop-e2e" in desktop, "release driver lost its required desktop end-to-end gate")
    require(len(names) == len(re.findall(r"^\s*check\s+", body, re.M))
            and len(desktop) == len(re.findall(r"^\s*check\s+", phase_body(source, "phase_desktop"), re.M)),
            "dynamic mandatory check names need explicit review")
    require(bool(names) and len(set(names + desktop)) == len(names + desktop), "duplicate or absent mandatory check names")
    required = [("desktop", name) for name in desktop]
    required += [("checks", name) for name in names if private_active or name not in private_names]
    return required, {"file": driver.name, "sha256": sha(driver)}


def held_file(root, value, label, allow_empty=False):
    root = Path(root).resolve()
    path = Path(value)
    path = path if path.is_absolute() else root / path
    require(not path.is_symlink() and path.resolve().is_relative_to(root), f"{label} escapes its evidence folder")
    require(path.is_file(), f"missing {label}: {path}", "NOT TESTED")
    require(allow_empty or bool(path.read_bytes().strip()), f"empty {label}: {path}")
    return path


def checked_commands(work, required):
    work = Path(work)
    table = held_file(work, "checks.tsv", "mandatory check table")
    table_hash = sha(table)
    rows, names, logs = [], set(), set()
    for number, line in enumerate(table.read_text(encoding="utf-8").splitlines(), 1):
        columns = line.split("\t")
        require(len(columns) == 4, f"malformed checks.tsv row {number}")
        phase, name, code, log = columns
        require(re.fullmatch(r"-?(0|[1-9][0-9]*)", code) is not None, f"invalid exit code at row {number}")
        require(name not in names, f"duplicate recorded check: {name}")
        names.add(name)
        path = held_file(work, log, f"log for {name}")
        require(path.resolve() not in logs, f"one log was reused by several checks: {path}")
        logs.add(path.resolve())
        rows.append({"phase": phase, "check": name, "exit": int(code),
                     "log": path.resolve().relative_to(work.resolve()).as_posix(), "log_sha256": sha(path)})
    found = {(row["phase"], row["check"]) for row in rows}
    missing = set(required) - found
    if missing:
        raise GateError("missing mandatory release checks: " + ", ".join(f"{phase}/{name}" for phase, name in sorted(missing)),
                        "NOT TESTED", {"checks": rows,
                        "recorded_checks_passed": bool(rows) and all(row["exit"] == 0 for row in rows)})
    require(sha(table) == table_hash, "check table changed during qualification")
    return rows, table_hash


def builtin_receipt(work, out, inputs, require_config):
    work, out = Path(work), Path(out)
    receipt_path = work / "genesis/built-in.json"
    receipt = load(receipt_path)
    require(receipt.get("status") == "PASS" and type(receipt.get("native_exit_code")) is int
            and receipt["native_exit_code"] == 0, "built-in Node CLI did not pass", "NOT TESTED")
    require(receipt.get("identity_sha256") == inputs["identity_sha256"]
            and receipt.get("manifest_sha256") == inputs["manifest_sha256"], "built-in receipt belongs to another identity/manifest")
    frozen = load(out / "bin/manifest.json")
    node_hash = frozen.get("files", {}).get("tenraid.exe")
    require(isinstance(node_hash, str) and HEX64.fullmatch(node_hash) is not None, "frozen manifest has no valid Node hash")
    node = held_file(out, "bin/tenraid.exe", "frozen release Node")
    require(sha(node) == node_hash == receipt.get("executable_sha256"), "built-in receipt is not sealed to this frozen Node")
    command = receipt.get("command")
    require(isinstance(command, list) and len(command) == 5 and command[1:] == ["--no-color", "genesis", "--network", "mainnet"],
            "built-in receipt did not run the required CLI without an external manifest")
    require(receipt.get("values") == inputs["values"], "built-in receipt identity fields differ")
    output = held_file(work, receipt.get("stdout_log", ""), "built-in stdout")
    errors = held_file(work, receipt.get("stderr_log", ""), "built-in stderr", allow_empty=True)
    require(sha(output) == receipt.get("stdout_sha256") and sha(errors) == receipt.get("stderr_sha256"), "built-in output logs changed")
    require(cli_values(output.read_text(encoding="utf-8")) == inputs["values"], "built-in stdout is not the recorded identity")
    require(output.resolve() != errors.resolve(), "built-in stdout/stderr reused one log")
    require(isinstance(receipt.get("checked_at_utc"), str) and bool(receipt["checked_at_utc"]),
            "built-in receipt has no concrete run timestamp")
    preflight_path = held_file(work, receipt.get("preflight_log", ""), "built-in environment check")
    require(preflight_path.resolve() not in (output.resolve(), errors.resolve()), "built-in preflight reused a native output log")
    require(sha(preflight_path) == receipt.get("preflight_sha256"), "built-in environment evidence changed")
    preflight = load(preflight_path)
    require(preflight.get("status") == "PASS" and type(preflight.get("native_exit_code")) is int
            and preflight["native_exit_code"] == 0, "built-in environment check did not pass", "NOT TESTED")
    for name in ("release_genesis.py", "release_evidence.py", "release_identity.py"):
        require(receipt.get("tool_sha256", {}).get(name) == sha(TOOLS / name), "built-in receipt used another evidence tool")
    if require_config:
        require(sha(held_file(out, "config/mainnet.traidag.json", "release manifest")) == inputs["manifest_sha256"],
                "release config manifest differs from the checked built-in identity")
        for name, key in (("mainnet.chain-id", "chain_id"), ("mainnet.genesis-id", "genesis_id")):
            require(held_file(out, "config/" + name, "release " + name).read_text(encoding="utf-8").strip() == inputs["values"][key],
                    "release config anchor differs from built-in identity")
    return {"status": "PASS", "file": "genesis/built-in.json", "sha256": sha(receipt_path),
            "executable_sha256": node_hash, "values": inputs["values"]}


def qualify(work, out, require_config=True):
    work, out = artifact_root(work), artifact_root(out)
    inputs = identity_inputs(work)
    required, source = required_checks(inputs["record"]["private_transfers_from_genesis"])
    checks, table_hash = checked_commands(work, required)
    passed = bool(checks) and all(check["exit"] == 0 for check in checks)
    if not passed:
        failed = [check["check"] for check in checks if check["exit"] != 0]
        raise GateError("recorded release checks failed: " + ", ".join(failed), "FAIL",
                        {"checks": checks, "recorded_checks_passed": False})
    try:
        builtin = builtin_receipt(work, out, inputs, require_config)
    except GateError as error:
        error.details.update(checks=checks, recorded_checks_passed=passed)
        raise
    # Recheck every file whose earlier content could otherwise be replaced during recording.
    for check in checks:
        require(sha(held_file(work, check["log"], "recorded log")) == check["log_sha256"], "a recorded check log changed")
    require(sha(Path(work) / "checks.tsv") == table_hash, "recorded check table changed")
    require(identity_inputs(work) == inputs, "identity inputs changed during evidence qualification")
    require(builtin_receipt(work, out, inputs, require_config) == builtin, "built-in evidence changed during qualification")
    require(required_checks(inputs["record"]["private_transfers_from_genesis"])[1] == source,
            "required-check source changed during qualification")
    return {"status": "PASS", "scope": SCOPE, "checks": checks, "recorded_checks_passed": passed,
            "mandatory_checks": [{"phase": phase, "check": name} for phase, name in required],
            "mandatory_checks_source": source, "checks_tsv_sha256": table_hash,
            "identity_sha256": inputs["identity_sha256"], "manifest_sha256": inputs["manifest_sha256"],
            "builtin_node_cli": builtin}
