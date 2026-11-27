"""The records of a release: the documents that state the mainnet identity, and the release record.

    py -3.14 records.py begin --work DIR --phase PHASE
    py -3.14 records.py identity --work DIR [--write]
    py -3.14 records.py release --work DIR --out RC_DIR

`identity` sets the genesis ID and the parameter hash from DIR/genesis/identity.json in every document that states the
current mainnet identity, and the wallet WASM's revision and hash where such a document names them; without --write
it prints what it would change. A chain ID line must already hold the chain ID. Qualification records of earlier
candidates keep what they qualified and are not listed. The Genesis section of the mainnet files' README is written
anew from the manifest and the validators' public files.
`release` writes RC_DIR/RELEASE.json: the identities, the treasury's public values, the split, the hashes of the
binaries, the desktop app, the SDK package and the browser packages, and each recorded command with its exit code and log.
The mandatory release checks and exact fresh built-in Node receipt are validated before writing a PASS.
The recorded-exit summary remains a separate narrower fact, and owner approval/security gates stay independent.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from itertools import combinations
from pathlib import Path

from node_checkout import node_checkout

import release_evidence as evidence
import release_runtime as runtime

GITHUB = Path(__file__).resolve().parents[2]
REPO = node_checkout(GITHUB)
FILES = Path(os.environ.get("TENRAI_MAINNET_FILES", GITHUB / "tenrai-mainnet-files"))
# Each document with the section that states the identity (None: the whole file).
DOCUMENTS = [
    (GITHUB / "tenrai-docs" / "src" / "content" / "docs" / "en" / "understand" / "reference" / "parameters.md",
     "## Mainnet identity"),
    (FILES / "genesis" / "native-locks-protocol-v2.md", None),
    (GITHUB / "tenrai-api" / "README.md", None),
]
LABELS = [
    ("chain_id", re.compile(r"chain[ _-]?id", re.I), 96),
    ("genesis_id", re.compile(r"genesis[ _-]?id", re.I), 96),
    ("params_hash", re.compile(r"param(?:eter)?s?[ _-]?hash", re.I), 96),
    ("wasm_revision", re.compile(r"browser source-tree fingerprint", re.I), 64),
    ("wasm_sha256", re.compile(r"browser WASM SHA-?256", re.I), 64),
]
ATOMS_PER_TRAI = 100_000_000


def quorum(total):
    """`quorum_weight` of crates/traidag/src/cut/committee.rs."""
    return 2 * (total // 3) + (total % 3) * 2 // 3 + 1


def section(lines, heading):
    if heading is None:
        return 0, len(lines)
    start = next((index for index, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        raise SystemExit(f"no section {heading!r}")
    level = heading.split(" ", 1)[0]
    end = next((index for index in range(start + 1, len(lines))
                if lines[index].startswith("#") and lines[index].split(" ", 1)[0] <= level), len(lines))
    return start, end


def set_values(path, heading, values, write):
    lines = path.read_text(encoding="utf-8").split("\n")
    start, end = section(lines, heading)
    found = set()
    for index in range(start, end):
        for key, label, width in LABELS:
            if not label.search(lines[index]):
                continue
            hexes = re.findall(rf"(?<![0-9a-f])[0-9a-f]{{{width}}}(?![0-9a-f])", lines[index])
            if len(hexes) != 1:
                continue
            found.add(key)
            if key == "chain_id":
                if hexes[0] != values["chain_id"]:
                    raise SystemExit(f"{path}:{index + 1} names chain ID {hexes[0][:12]}..., not the chain's")
            elif hexes[0] != values[key]:
                print(f"  {path.name}:{index + 1} {key} {hexes[0][:12]}... -> {values[key][:12]}...")
                lines[index] = lines[index].replace(hexes[0], values[key])
            break
    missing = {"genesis_id", "params_hash"} - found
    if missing:
        raise SystemExit(f"{path} states no {', '.join(sorted(missing))} where expected")
    if write:
        path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def trai(atoms):
    whole, rest = divmod(atoms, ATOMS_PER_TRAI)
    text = f"{whole:,}"
    return text if rest == 0 else f"{text}.{rest:08d}".rstrip("0")


def readme_genesis(identity, manifest):
    """Public identity/allocation documentation; no key-custody or release-qualification attestation."""
    publics = {name: json.loads((FILES / name / "public.json").read_text(encoding="utf-8")) for name in identity["validators"]}
    amounts = {entry["address"]: int(entry["amount"]) for entry in manifest["allocations"]}
    weights = {name: amounts[public["wallet_address"]] for name, public in publics.items()}
    total = sum(weights.values())
    needed = quorum(total)
    names = list(weights)
    in_every = [name for name in names if total - weights[name] < needed]
    away = [name for name in names if total - weights[name] >= needed]
    forkers = []
    for name in names:
        others = [other for other in names if other != name]
        groups = [set(group) for size in range(len(others) + 1) for group in combinations(others, size)
                  if weights[name] + sum(weights[other] for other in group) >= needed]
        if any(not (first & second) for first, second in combinations(groups, 2)):
            forkers.append(name)
    treasury = identity["treasury"]
    key_source = identity.get("treasury_key_source", treasury.get("key_source", "not_recorded"))
    treasury_role = ("Existing owner-selected treasury" if key_source == "existing_current_genesis"
                     else "Owner-selected treasury; key provenance not established here")
    rows = []
    for name, public in publics.items():
        role = " (anchor)" if name in in_every and len(in_every) == 1 else ""
        rows.append(f"| Self-bond {name}{role} | `{public['wallet_address']}` | {trai(weights[name])} |")
        rows.append(f"| Heartbeat fees {name} | `{public['heartbeat_fee_address']}` | "
                    f"{trai(amounts[public['heartbeat_fee_address']])} |")
    rows.append(f"| {treasury_role}, fingerprint `{treasury['recovery_fingerprint']}` | "
                f"`{treasury['address']}` | {trai(treasury['atoms'])} |")
    rows.append(f"| Total | | {trai(sum(amounts.values()))} |")
    fork = (f"A compromised key of {' or '.join(forkers)} could sign conflicting quorums during a partition."
            if forkers else "No single key can sign two conflicting finalizing quorums by itself.")
    private = "enabled from genesis in the recorded profile" if identity["private_transfers_from_genesis"] else "inactive until an upgrade"
    return "\n".join([
        "## Genesis",
        "",
        "Mainnet has not launched. These are public identity and allocation records; deriving or recording them "
        "does not establish a qualified release. Launch requires the owner's approval.",
        "",
        "| Public identity | Recorded value |",
        "| --- | --- |",
        f"| Chain ID | `{identity['chain_id']}` |",
        f"| Genesis ID | `{identity['genesis_id']}` |",
        f"| Parameter hash | `{identity['params_hash']}` |",
        f"| Private transfers | {private} |",
        "| Manifest | `genesis/mainnet.traidag.json`; the matching built-in `config/mainnet.traidag.json` "
        "must be checked on the freshly built Node CLI |",
        "| Calibration | 5,000,000 H/s, 50 blocks/s |",
        "",
        "A library-derived public identity or a Node CLI run with an external manifest is separate from the final "
        "fresh built-in check. Run the hash-recorded release Node's `tenraid --no-color genesis --network mainnet` "
        "without `--manifest` and compare chain ID, genesis ID, parameter hash and rules digest exactly. "
        "The required security and release qualification results remain separate evidence.",
        "",
        "| Allocation | Address | TRAI |",
        "| --- | --- | ---: |",
        *rows,
        "",
        f"Quorum {needed:,} of {total:,} atoms. Required in every quorum: "
        f"{', '.join(in_every) or 'none'}. May be absent individually: "
        f"{' or '.join(away) or 'none'}. {fork}",
        "",
        f"Treasury source: `{key_source}`. This generator records public values and creates no key. "
        "Public address/fingerprint metadata does not establish key-generation location or custody. "
        "`treasury/public.json` contains the public values selected by the owner.",
        "",
        "",
    ])


def identity_records(arguments):
    work = Path(arguments.work)
    if arguments.write:
        # No active identity document is rewritten from a library-only or incomplete release record.
        try:
            evidence.qualify(work, work, require_config=False)
            runtime.require_final(work, allow_identity_mutation=True)
        except Exception as error:
            invalidate_record(work, error)
            raise SystemExit(str(error)) from error
    identity = evidence.identity_inputs(work)["record"]
    release = json.loads((REPO / "crates" / "wasm" / "pkg" / "release.json").read_text(encoding="utf-8-sig"))
    values = dict(identity, wasm_revision=release["revision"], wasm_sha256=release["assets"]["wasm"]["sha256"])
    for path, heading in DOCUMENTS:
        print(path)
        set_values(path, heading, values, arguments.write)
    readme = FILES / "README.md"
    text = readme.read_text(encoding="utf-8")
    start, end = text.index("## Genesis\n"), text.index("## Ordner\n")
    manifest = json.loads((work / "genesis" / "mainnet.traidag.json").read_text(encoding="utf-8"))
    changed = text[:start] + readme_genesis(identity, manifest) + text[end:]
    print(f"{readme}: Genesis section {'unchanged' if changed == text else 'written anew'}")
    if arguments.write and changed != text:
        readme.write_text(changed, encoding="utf-8", newline="\n")
    if not arguments.write:
        print("nothing written: --write writes these changes")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def begin_attempt(arguments):
    """Preserve old reports, then retire success before a release phase can run."""
    out = evidence.artifact_root(arguments.work)
    evidence.require(out.is_dir(), "release output folder does not exist")
    evidence.require(arguments.phase in ("genesis", "binaries", "wasm", "web", "desktop", "checks", "records"),
                     "release begin needs a driver phase")
    previous = [name for name in (
        "RELEASE.json", "README.md", "checks.tsv", "SOURCES.json", "COMMAND-SOURCES.json", "genesis/built-in.json",
        "bin/manifest.json", "desktop/manifest.json",
    ) if (out / name).is_file()]
    history = None
    if previous:
        history = evidence.artifact_root(out / "evidence-history" /
            (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex))
        history.mkdir(parents=True, exist_ok=False)
        preserved = {}
        for name in previous:
            source = out / name
            before = evidence.sha(source)
            destination = history / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            evidence.require(evidence.sha(destination) == before == evidence.sha(source),
                             "previous release report changed while being preserved")
            preserved[name] = before
        evidence.write(history / "archive.json", {
            "status": "NOT TESTED", "scope": "historical_report_copies_only",
            "resumed_phase": arguments.phase, "files_sha256": preserved,
            "publication_authorized": False,
        })
    reason = f"A release attempt began at phase {arguments.phase}; mandatory verification has not completed."
    if history is not None:
        reason += f" Earlier public reports are preserved in {history.relative_to(out).as_posix()}."
    invalidate_record(out, evidence.GateError(reason, "NOT TESTED"))
    print("NOT TESTED: " + reason)
    return history


def invalidate_record(out, error):
    status = getattr(error, "status", "INVALID")
    if status not in ("FAIL", "INVALID", "NOT TESTED"):
        status = "INVALID"
    details = getattr(error, "details", {})
    evidence.write(Path(out) / "RELEASE.json", {
        "status": status, "qualification_status": status, "qualification_scope": evidence.SCOPE,
        "recorded_checks_passed": details.get("recorded_checks_passed", False),
        "checks": details.get("checks", []), "reason": str(error), "publication_authorized": False,
    })
    (Path(out) / "README.md").write_text(
        f"# Release evidence\n\nStatus: {status}. {error}\n\n"
        "This attempt did not qualify a release record. See RELEASE.json for the current evidence.\n",
        encoding="utf-8", newline="\n")


def frozen_files(folder):
    manifest = evidence.load(folder / "manifest.json")
    files = manifest.get("files")
    evidence.require(isinstance(files, dict) and bool(files), "frozen artifact manifest has no files")
    for name, digest in files.items():
        evidence.require(isinstance(name, str) and Path(name).name == name
                         and isinstance(digest, str) and evidence.HEX64.fullmatch(digest) is not None,
                         "invalid frozen artifact name/hash")
        path = evidence.held_file(folder, name, "frozen artifact " + name)
        evidence.require(evidence.sha(path) == digest, "frozen artifact changed: " + name)
    return files


def source_inventory(out):
    path = evidence.held_file(out, "SOURCES.json", "release source inventory")
    report = evidence.load(path)
    repositories = report.get("repositories")
    evidence.require(isinstance(repositories, dict) and bool(repositories), "source inventory has no repositories")
    for entry in repositories.values():
        hashes = entry.get("hashes")
        evidence.require(isinstance(hashes, dict) and bool(hashes) and type(entry.get("files")) is int and entry["files"] == len(hashes),
                         "source inventory has missing or inconsistent file hashes")
        for relative, digest in hashes.items():
            evidence.require(not Path(relative).is_absolute() and ".." not in Path(relative).parts
                             and isinstance(digest, str) and evidence.HEX64.fullmatch(digest) is not None,
                             "invalid source inventory path or hash")
        digest = hashlib.sha256("".join(f"{name}\0{value}\n" for name, value in sorted(hashes.items())).encode()).hexdigest()
        evidence.require(entry.get("digest") == digest, "source inventory aggregate changed")
    return sha(path)


def release_record(arguments):
    work, out = Path(arguments.work), Path(arguments.out)
    # Invalidate a previous PASS before any validation that can stop this attempt.
    invalidate_record(out, evidence.GateError("release record generation has not completed"))
    try:
        qualification = evidence.qualify(work, out)
        command_sources = runtime.require_final(work)
        identity = evidence.identity_inputs(work)["record"]
        prover = REPO / "crates/tsp2/prover-wasm"
        if not (prover / "Cargo.toml").is_file():
            prover = REPO / "crates/tsp2-prover-wasm"
        packages = {}
        for name, folder in (("wallet", REPO / "crates/wasm/pkg"), ("tenq", REPO / "crates/tenq-wasm/pkg"),
                             ("prover", prover / "package")):
            release = evidence.load(folder / "release.json")
            packages[name] = {key: value for key, value in release.items() if key != "files"}
        record = {
            "made": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "PASS", "identity": identity,
            "binaries": frozen_files(out / "bin"), "desktop": frozen_files(out / "desktop"),
            "sdk": {path.name: sha(path) for path in sorted((out / "sdk").glob("*.tgz"))},
            "browser_packages": packages, "checks": qualification["checks"],
            "recorded_checks_passed": qualification["recorded_checks_passed"],
            "qualification_status": qualification["status"], "qualification_scope": evidence.SCOPE,
            "qualification": qualification,
            "identity_evidence": {
                "scope": "public_identity_values",
                "library_computation": identity.get("identity_computation"),
                "external_manifest_derivation": identity.get("identity_derivation"),
                "fresh_builtin_node_cli": qualification["builtin_node_cli"],
            },
            "sources": "SOURCES.json", "command_sources": command_sources, "publication_authorized": False,
        }
        evidence.require(bool(record["sdk"]), "release contains no SDK package", "NOT TESTED")
        record["sources_sha256"] = source_inventory(out)
        evidence.require(evidence.qualify(work, out) == qualification, "release evidence changed during record generation")
        evidence.require(runtime.require_final(work) == command_sources, "source boundaries changed during record generation")
        evidence.write(out / "RELEASE.json", record)
        (out / "README.md").write_text(readme_release(out.name, record), encoding="utf-8", newline="\n")
        print(f"PASS: wrote {out / 'RELEASE.json'} and README.md with all {len(qualification['mandatory_checks'])} mandatory checks")
    except Exception as error:
        invalidate_record(out, error)
        raise SystemExit(str(error)) from error


def verify_record(arguments):
    work, out = Path(arguments.work), Path(arguments.out or arguments.work)
    try:
        qualification = evidence.qualify(work, out)
        command_sources = runtime.require_final(work)
        record = evidence.load(out / "RELEASE.json")
        evidence.require(record.get("status") == "PASS" and record.get("qualification_status") == "PASS"
                         and record.get("qualification_scope") == evidence.SCOPE, "release record did not pass its declared evidence scope")
        evidence.require(record.get("qualification") == qualification, "release record belongs to other gate evidence")
        evidence.require(record.get("command_sources") == command_sources, "release source boundaries are missing or changed")
        evidence.require(record.get("binaries") == frozen_files(out / "bin")
                         and record.get("desktop") == frozen_files(out / "desktop"), "release artifact manifests changed")
        evidence.require(record.get("identity") == evidence.identity_inputs(work)["record"], "release public identity changed")
        evidence.require(record.get("sources_sha256") == source_inventory(out), "release source record changed")
        evidence.require(record.get("sdk") == {path.name: sha(path) for path in sorted((out / "sdk").glob("*.tgz"))},
                         "release SDK artifact changed")
        print("PASS: mandatory release command evidence and the exact fresh built-in identity match this record")
    except Exception as error:
        invalidate_record(out, error)
        raise SystemExit(str(error)) from error


def readme_release(name, record):
    """The release folder's README: what it holds, its identity, its files and how its checks went."""
    identity = record["identity"]
    private = "from the first cut" if identity["private_transfers_from_genesis"] else "off until an upgrade"
    words = identity["treasury"]["verification_words"]
    words = " ".join(words) if isinstance(words, list) else words
    lines = [
        f"# Tenrai mainnet candidate record {name}",
        "",
        "Generating this inventory does not publish or launch mainnet. The owner approves those actions.",
        "`RELEASE.json` records public identities, artifact hashes and the listed command results; `SOURCES.json`",
        "records source state. PASS covers the required command evidence and sealed built-in identity described below;",
        "owner approval and the separate security/activation/platform requirements remain independent gates.",
        "",
        "| Identity | Value |",
        "| --- | --- |",
        f"| Chain ID | `{identity['chain_id']}` |",
        f"| Genesis ID | `{identity['genesis_id']}` |",
        f"| Parameter hash | `{identity['params_hash']}` |",
        f"| Private transfers on mainnet | {private} |",
        f"| Self-bonds of {', '.join(identity['validators'])} | "
        f"{' / '.join(f'{bond:,}' for bond in identity['bonds_trai'])} TRAI |",
        f"| Treasury | `{identity['treasury']['address']}`, fingerprint `{identity['treasury']['recovery_fingerprint']}`, "
        f"verification words *{words}* |",
        "",
        "| Folder | What it holds |",
        "| --- | --- |",
        "| `bin/` | The binaries for Windows x86-64, read-only; `manifest.json` names each SHA-256 |",
        "| `desktop/` | The desktop wallet with the native prover inside, frozen with its SHA-256 |",
        "| `web/` | The web wallet, the reference wallet and explorer, the paper wallet, the explorer's pages, the browser miner |",
        "| `sdk/` | The SDK as an npm package |",
        "| `config/` | The built-in mainnet manifest, chain and genesis anchors, example settings of the node and both pools |",
        "| `docs/run/` | Install and operation guides |",
        "| `docs/qualification/` | Committee and stake |",
        "| `genesis/`, `previous/`, `logs/`, `checks/` | The genesis step, the files it replaced, every step's log, check results |",
        "",
        "| Binary | SHA-256 |",
        "| --- | --- |",
        *[f"| `{file}` | `{digest}` |" for file, digest in sorted(record["binaries"].items())],
        *[f"| `{file}` | `{digest}` |" for file, digest in sorted(record["desktop"].items())],
        "",
        "`tenrai-api.exe` is the hosted gateway: private, not for distribution.",
        "",
        "| Browser package | Revision |",
        "| --- | --- |",
        *[f"| `{package.get('package', key)}` | `{package.get('revision', '')}` |"
          for key, package in sorted(record["browser_packages"].items())],
        "",
        "| Check | Exit | Log |",
        "| --- | ---: | --- |",
        *[f"| {check['check']} | {check['exit']} | `{check['log']}` |" for check in record["checks"]],
        "",
        ("Every recorded command returned exit code 0." if record["recorded_checks_passed"]
         else "Recorded commands are missing or contain failures; inspect the logs."),
        "The recorded-exit summary is narrower than the required gate: every mandatory check and distinct nonempty log",
        "must be present, and the fresh built-in Node receipt must match this binary, manifest, identity and all five fields.",
        "Library-derived public values and an external-manifest CLI run do not replace that built-in receipt.",
        "This evidence scope does not authorize publication or establish the separate security/activation/platform gates.",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["begin", "identity", "release", "verify"])
    parser.add_argument("--work", required=True)
    parser.add_argument("--out")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--phase")
    arguments = parser.parse_args()
    if arguments.action == "release" and not arguments.out:
        raise SystemExit("release needs --out")
    {"begin": begin_attempt, "identity": identity_records, "release": release_record,
     "verify": verify_record}[arguments.action](arguments)


if __name__ == "__main__":
    main()
