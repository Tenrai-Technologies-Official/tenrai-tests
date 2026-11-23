"""The genesis step of the release: checks its inputs, records the identities the node computes, installs the genesis.

    py -3.14 release_genesis.py inputs   --treasury ADDRESS --fingerprint TRAI-XXXX-XXXX --bonds A,B,C
                                         --private-from-genesis yes|no
    py -3.14 release_genesis.py identity --work DIR --tenraid EXE --wallet EXE (and the inputs above)
    py -3.14 release_genesis.py install  --work DIR
    py -3.14 release_genesis.py built-in --work DIR --tenraid EXE

`inputs` refuses an address or fingerprint of the wrong form, any validator wallet or fee address as treasury,
reuse of the current treasury unless explicitly selected, a split that is not three whole amounts, and
a working tree whose mainnet does not run private transfers as --private-from-genesis says or whose testnet runs them.
`identity` reads what `tenraid genesis --manifest` prints for DIR/genesis/mainnet.traidag.json and writes
DIR/genesis/identity.json; its Chain ID/genesis pair must match the append-only public identity history. `install` puts the manifest and the genesis ID into the node's
config/ and the mainnet files, with the treasury's public values; every file it replaces is kept under DIR/previous/
first. `built-in` checks all five identity fields without --manifest and writes a binary/manifest/identity-sealed
receipt only after a fresh successful environment check. An unsuccessful attempt invalidates its earlier receipt.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import release_evidence as evidence
import release_identity as identity_history
import time
from pathlib import Path

GITHUB = Path(__file__).resolve().parents[2]
REPO = Path(os.environ.get("TENRAI_REPO", GITHUB / "tenrai-core"))
FILES = Path(os.environ.get("TENRAI_MAINNET_FILES", GITHUB / "tenrai-mainnet-files"))
CONSENSUS = REPO / "crates" / "primitives" / "src" / "params" / "consensus.rs"
VALIDATORS = ["validator1", "validator2", "validator3"]
ADDRESS = re.compile(r"trai1[02-9ac-hj-np-z]{20,}")
FINGERPRINT = re.compile(r"TRAI-[0-9A-F]{4}-[0-9A-F]{4}")
HEX96 = re.compile(r"[0-9a-f]{96}")


def private_transfers(name, source):
    """The private_transfers value of the parameter set `name` in consensus.rs, following `..OTHER` and `= OTHER;`."""
    found = re.search(rf"pub const {name}: ConsensusParams = (\w+);", source)
    if found:
        return private_transfers(found.group(1), source)
    found = re.search(rf"pub const {name}: ConsensusParams = ConsensusParams \{{(.*?)\}};", source, re.S)
    if not found:
        raise SystemExit(f"{CONSENSUS} defines no parameter set {name}")
    body = found.group(1)
    explicit = re.search(r"^\s*private_transfers: (true|false),", body, re.M)
    if explicit:
        return explicit.group(1) == "true"
    base = re.search(r"\.\.(\w+)", body)
    if not base:
        raise SystemExit(f"the parameter set {name} names no private_transfers")
    return private_transfers(base.group(1), source)


def validator_addresses():
    used = set()
    for name in VALIDATORS:
        public = json.loads((FILES / name / "public.json").read_text(encoding="utf-8"))
        used.update(public[key] for key in ("wallet_address", "heartbeat_fee_address"))
    return used


def current_treasury():
    manifest = json.loads((REPO / "config" / "mainnet.traidag.json").read_text(encoding="utf-8"))
    validators = validator_addresses()
    addresses = {allocation["address"] for allocation in manifest["allocations"]
                 if allocation["address"] not in validators}
    if len(addresses) != 1:
        raise SystemExit("the current genesis does not identify one separate treasury address")
    return addresses.pop()


def used_addresses():
    used = set()
    manifest = json.loads((REPO / "config" / "mainnet.traidag.json").read_text(encoding="utf-8"))
    used.update(allocation["address"] for allocation in manifest["allocations"])
    for name in VALIDATORS:
        public = json.loads((FILES / name / "public.json").read_text(encoding="utf-8"))
        used.update(public[key] for key in ("wallet_address", "heartbeat_fee_address"))
    return used


def retired_addresses():
    path = FILES / "treasury" / "retired.json"
    if not path.exists():
        return set()
    return {entry["address"] for entry in json.loads(path.read_text(encoding="utf-8"))["addresses"]}


def check_inputs(arguments):
    if not ADDRESS.fullmatch(arguments.treasury):
        raise SystemExit(f"--treasury {arguments.treasury} is not a mainnet address")
    if not FINGERPRINT.fullmatch(arguments.fingerprint):
        raise SystemExit(f"--fingerprint {arguments.fingerprint} is not of the form TRAI-XXXX-XXXX")
    bonds = arguments.bonds.split(",")
    if len(bonds) != len(VALIDATORS) or not all(bond.isdigit() for bond in bonds):
        raise SystemExit(f"--bonds {arguments.bonds} must be three whole TRAI amounts")
    if arguments.treasury in retired_addresses():
        raise SystemExit("the treasury is a retired address: its recovery words were exposed")
    if arguments.treasury in validator_addresses():
        raise SystemExit("the treasury must differ from every validator wallet and heartbeat fee address")
    reuse = getattr(arguments, "reuse_current_treasury", False)
    if reuse:
        if arguments.treasury != current_treasury():
            raise SystemExit("--reuse-current-treasury requires the current separate treasury address")
    elif arguments.treasury in used_addresses():
        raise SystemExit("reusing the current treasury requires --reuse-current-treasury")
    source = CONSENSUS.read_text(encoding="utf-8")
    mainnet, testnet = private_transfers("MAINNET", source), private_transfers("TESTNET", source)
    wanted = arguments.private_from_genesis == "yes"
    if mainnet != wanted:
        raise SystemExit(f"the working tree runs private transfers on mainnet from genesis: {'yes' if mainnet else 'no'}; "
                         f"--private-from-genesis says {arguments.private_from_genesis}")
    if testnet:
        raise SystemExit("the working tree runs private transfers on testnet; its activation is a decision of its own")
    print(f"inputs: treasury {arguments.treasury}, fingerprint {arguments.fingerprint}, bonds {arguments.bonds}; "
          f"private transfers from genesis on mainnet: {arguments.private_from_genesis}, on testnet: no")


def run(command):
    result = subprocess.run(command, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False)
    if result.returncode:
        raise SystemExit(f"{' '.join(map(str, command))} failed:\n{result.stdout[-2000:]}{result.stderr[-2000:]}")
    return result.stdout


def printed(output):
    return evidence.cli_values(output)


def identity(arguments):
    check_inputs(arguments)
    work = Path(arguments.work)
    manifest = work / "genesis" / "mainnet.traidag.json"
    pinned_manifest = manifest.read_bytes()
    manifest_hash = hashlib.sha256(pinned_manifest).hexdigest()
    manifest_document = json.loads(pinned_manifest.decode("utf-8"), object_pairs_hook=evidence.unique_pairs)
    chain_path = REPO / "config" / "mainnet.chain-id"
    pinned_chain = chain_path.read_bytes()
    chain = pinned_chain.decode("utf-8").strip()
    if manifest_document.get("network") != "mainnet" or manifest_document.get("chain_id") != chain:
        raise SystemExit("the work manifest does not match the configured launch Chain ID")
    # A concurrent editor cannot change the native input into another genesis while the
    # caller's mutable manifest is being reviewed. Retain its exact public content as evidence.
    snapshot_dir = work / "genesis" / "identity-inputs"
    snapshot_dir.mkdir(exist_ok=True)
    if snapshot_dir.is_symlink() or snapshot_dir.resolve().parent != (work / "genesis").resolve():
        raise SystemExit("the retained genesis snapshot directory escapes its work folder")
    snapshot = snapshot_dir / (manifest_hash + ".json")
    if snapshot.exists():
        if not snapshot.is_file() or snapshot.is_symlink() or snapshot.read_bytes() != pinned_manifest:
            raise SystemExit("the retained genesis input snapshot is inconsistent")
    else:
        with snapshot.open("xb") as output:
            output.write(pinned_manifest)
            output.flush()
            os.fsync(output.fileno())
    values = printed(run([arguments.tenraid, "genesis", "--manifest", str(snapshot)]))
    if values["network"] != "mainnet" or values["chain_id"] != chain:
        raise SystemExit(f"the manifest is for {values['network']} with chain ID {values['chain_id']}, not {chain}")
    history = identity_history.authorize(FILES, values["chain_id"], values["genesis_id"])
    words = json.loads(run([arguments.wallet, "--output", "json", "address-words", arguments.treasury]).splitlines()[0])
    if words.get("network") != "mainnet" or words.get("address") != arguments.treasury:
        raise SystemExit(f"the wallet reads the treasury address as {words}")
    allocations = manifest_document["allocations"]
    record = {
        "made": time.strftime("%Y-%m-%d %H:%M:%S"),
        "network": "mainnet",
        "chain_id": values["chain_id"],
        "genesis_id": values["genesis_id"],
        "params_hash": values["params_hash"],
        "rules_digest": values["rules_digest"],
        "identity_derivation": "node_cli_external_manifest",
        "private_transfers_from_genesis": arguments.private_from_genesis == "yes",
        "treasury_key_source": ("existing_current_genesis" if arguments.reuse_current_treasury
                                else "separately_supplied"),
        "bonds_trai": [int(bond) for bond in arguments.bonds.split(",")],
        "validators": VALIDATORS,
        "treasury": {
            "address": arguments.treasury,
            "recovery_fingerprint": arguments.fingerprint,
            "verification_words": words["verification_words"],
            "atoms": sum(int(entry["amount"]) for entry in allocations if entry["address"] == arguments.treasury),
        },
        "manifest_sha256": manifest_hash,
        "identity_history": history,
    }
    identity_history.verify(FILES, record["chain_id"], record["genesis_id"], history)
    if (manifest.read_bytes() != pinned_manifest or snapshot.read_bytes() != pinned_manifest
            or chain_path.read_bytes() != pinned_chain):
        raise SystemExit("genesis manifest or configured Chain ID changed during native identity derivation")
    (work / "genesis" / "identity.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    for key in ("chain_id", "genesis_id", "params_hash", "rules_digest"):
        print(f"{key}={record[key]}")
    print(f"treasury {arguments.treasury} ({arguments.fingerprint}), verification words: {words['verification_words']}")


def replace(work, target, content):
    """Writes `content` to `target`; a different file there is kept under work/previous/ first, once."""
    if target.exists():
        if target.read_bytes() == content:
            print(f"unchanged {target}")
            return
        kept = work / "previous" / target.resolve().as_posix().replace(":", "")
        if not kept.exists():
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, kept)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    print(f"wrote {target}")


def install(arguments):
    work = Path(arguments.work)
    # The shared reader revalidates current history even for a handcrafted or once-valid
    # work receipt, before any existing public/config file can be replaced.
    inputs = evidence.identity_inputs(work)
    record = inputs["record"]
    manifest = (work / "genesis" / "mainnet.traidag.json").read_bytes()
    if hashlib.sha256(manifest).hexdigest() != record["manifest_sha256"]:
        raise SystemExit("the manifest changed after its identities were recorded")
    chain = (REPO / "config/mainnet.chain-id").read_text(encoding="utf-8").strip()
    if chain != record["chain_id"]:
        raise SystemExit("the recorded identity does not match the configured launch Chain ID")

    def guard():
        identity_history.verify(FILES, record["chain_id"], record["genesis_id"], record["identity_history"])
        evidence.require(evidence.identity_inputs(work) == inputs,
                         "identity inputs changed during public installation")

    def publish(target, content):
        guard()
        replace(work, target, content)

    guard()
    genesis_id = (record["genesis_id"] + "\n").encode()
    publish(REPO / "config" / "mainnet.traidag.json", manifest)
    publish(REPO / "config" / "mainnet.genesis-id", genesis_id)
    publish(FILES / "genesis" / "mainnet.traidag.json", manifest)
    publish(FILES / "genesis" / "mainnet.genesis-id", genesis_id)
    publish(FILES / "genesis" / "identity.json", (json.dumps(record, indent=2) + "\n").encode())
    treasury = dict(record["treasury"], role="treasury; only public values here",
                    key_source=record.get("treasury_key_source", "separately_supplied"))
    publish(FILES / "treasury" / "public.json", (json.dumps(treasury, indent=2) + "\n").encode())
    guard()


def built_in(arguments):
    work = Path(arguments.work).resolve()
    receipt_path = work / "genesis/built-in.json"
    evidence.write(receipt_path, {"status": "INVALID", "reason": "built-in identity attempt has not completed"})
    receipt = {"status": "INVALID", "native_exit_code": None}
    try:
        inputs = evidence.identity_inputs(work)
        executable = Path(arguments.tenraid).resolve()
        evidence.require(executable.is_file(), "built-in Node executable is missing", "NOT TESTED")
        executable_hash = evidence.sha(executable)
        tools = {name: evidence.sha(Path(__file__).with_name(name))
                 for name in ("release_genesis.py", "release_evidence.py", "release_identity.py")}
        envcheck = Path(__file__).resolve().parents[1] / "qualification/envcheck.py"
        preflight_command = [sys.executable, str(envcheck), "--commit-gb", "8"]
        if getattr(arguments, "holder", None):
            preflight_command += ["--holder", arguments.holder]
        preflight_sources = [envcheck, envcheck.parents[1] / "p2p-bench/preflight.py",
                             envcheck.parents[1] / "p2p-bench/bench_proc.py"]
        preflight_hashes = {str(path): evidence.sha(path) for path in preflight_sources}
        preflight = subprocess.run(preflight_command, capture_output=True, timeout=60)
        evidence.require(all(evidence.sha(Path(path)) == digest for path, digest in preflight_hashes.items()),
                         "environment-check sources changed during preflight")
        preflight_path = work / "genesis/built-in.preflight.json"
        evidence.write(preflight_path, {
            "status": "PASS" if preflight.returncode == 0 else "FAIL",
            "native_exit_code": preflight.returncode, "command": preflight_command,
            "envcheck_sha256": evidence.sha(envcheck), "source_sha256": preflight_hashes,
            "stdout": preflight.stdout.decode("utf-8", errors="replace"),
            "stderr": preflight.stderr.decode("utf-8", errors="replace"),
        })
        evidence.require(preflight.returncode == 0, "built-in environment check refused the native command", "NOT TESTED")
        evidence.require(evidence.identity_inputs(work) == inputs and evidence.sha(executable) == executable_hash,
                         "built-in inputs changed during preflight")
        command = [str(executable), "--no-color", "genesis", "--network", "mainnet"]
        result = subprocess.run(command, capture_output=True, timeout=120)
        output_path = work / "genesis/built-in.stdout.log"
        error_path = work / "genesis/built-in.stderr.log"
        output_path.write_bytes(result.stdout)
        error_path.write_bytes(result.stderr)
        receipt.update({
            "native_exit_code": result.returncode, "command": command,
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "executable_sha256": executable_hash,
            "identity_sha256": inputs["identity_sha256"], "manifest_sha256": inputs["manifest_sha256"],
            "stdout_log": "genesis/built-in.stdout.log", "stdout_sha256": evidence.sha(output_path),
            "stderr_log": "genesis/built-in.stderr.log", "stderr_sha256": evidence.sha(error_path),
            "preflight_log": "genesis/built-in.preflight.json", "preflight_sha256": evidence.sha(preflight_path),
            "tool_sha256": tools,
        })
        evidence.require(result.returncode == 0, "native built-in Node CLI failed", "FAIL")
        values = printed(result.stdout.decode("utf-8"))
        evidence.require(values == inputs["values"], "built-in Node CLI network/chain/genesis/params/rules differ from the public record")
        evidence.require(evidence.identity_inputs(work) == inputs and evidence.sha(executable) == executable_hash,
                         "built-in identity inputs or executable changed during the native run")
        evidence.require(all(evidence.sha(Path(__file__).with_name(name)) == digest for name, digest in tools.items()),
                         "built-in evidence tools changed during the native run")
        receipt.update(status="PASS", values=values)
        evidence.write(receipt_path, receipt)
        print(json.dumps(receipt), flush=True)
    except Exception as error:
        receipt.update(status=getattr(error, "status", "INVALID"), reason=str(error))
        evidence.write(receipt_path, receipt)
        raise SystemExit(str(error)) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["inputs", "identity", "install", "built-in"])
    parser.add_argument("--treasury")
    parser.add_argument("--fingerprint")
    parser.add_argument("--reuse-current-treasury", action="store_true",
                        help="explicitly preserve the current separate treasury, as selected by the owner")
    parser.add_argument("--bonds")
    parser.add_argument("--private-from-genesis", choices=["yes", "no"])
    parser.add_argument("--work")
    parser.add_argument("--tenraid")
    parser.add_argument("--wallet")
    parser.add_argument("--holder", help="forward the explicitly authorized machine holder to envcheck; no process allow-list override")
    arguments = parser.parse_args()
    needed = {"inputs": ["treasury", "fingerprint", "bonds", "private_from_genesis"],
              "identity": ["treasury", "fingerprint", "bonds", "private_from_genesis", "work", "tenraid", "wallet"],
              "install": ["work"], "built-in": ["work", "tenraid"]}[arguments.action]
    missing = [name for name in needed if getattr(arguments, name) is None]
    if missing:
        raise SystemExit(f"{arguments.action} needs --{', --'.join(name.replace('_', '-') for name in missing)}")
    {"inputs": check_inputs, "identity": identity, "install": install, "built-in": built_in}[arguments.action](arguments)


if __name__ == "__main__":
    main()
