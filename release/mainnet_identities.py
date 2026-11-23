"""Every mainnet identity against its published values and the installed genesis; prints public values and PASS or
FAIL only, never a phrase, password or key. Works on copies in a folder only this account opens, wiped afterwards.

    py -3.14 mainnet_identities.py [--wallet EXE] [--tenraid EXE] [--repo NODE_REPO] [--sealed] [--before-release]
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
REPO = Path(os.environ.get("TENRAI_REPO", WORKSPACE / "tenrai-core"))
FILES = Path(os.environ.get("TENRAI_MAINNET_FILES", WORKSPACE / "tenrai-mainnet-files"))
VALIDATORS = ["validator1", "validator2", "validator3"]
TOTAL_ATOMS = 20_000_000 * 100_000_000
results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"{'PASS' if passed else 'FAIL'} {name}{': ' + detail if detail else ''}", flush=True)


def short(value):
    text = str(value)
    return text if len(text) <= 28 else f"{text[:14]}…{text[-8:]}"


def words():
    for source in Path.home().glob(".cargo/registry/src/*/bip39-2.2.2/src/language/english.rs"):
        listed = re.findall(r'"([a-z]+)",', source.read_text(encoding="utf-8"))
        if len(listed) == 2048:
            return set(listed)
    raise SystemExit("the BIP39 English word list is not in the cargo cache")


def phrase_of(text, wordlist):
    """The 24 list words of a text in their order, or None; the text itself is never shown."""
    found = [token for token in re.findall(r"[a-z]+", text.lower()) if token in wordlist]
    return " ".join(found) if len(found) in (12, 15, 18, 21, 24) else None


def sheet_phrases(wordlist):
    """The phrase of each section of the owner's sheet (treasury, validator1-3); never shown."""
    sections, current = {}, None
    for line in (FILES / "mainnet-seeds.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("TREASURY"):
            current = "treasury"
        elif line.startswith("VALIDATOR "):
            current = "validator" + line.split()[1]
        elif line.startswith("NICHT IN"):
            current = None
        elif current and line.startswith("Woerter:"):
            sections[current] = phrase_of(line.split(":", 1)[1], wordlist)
    return sections


def retired_addresses():
    return {entry["address"] for entry in
            json.loads((FILES / "treasury" / "retired.json").read_text(encoding="utf-8"))["addresses"]}


def private_folder():
    folder = Path(tempfile.mkdtemp(prefix="tenrai-mainnet-verify-",
                                   dir=os.environ.get("TENRAI_PRIVATE_WORK")))
    account = subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"],
                   capture_output=True, check=True)
    return folder


def explicit(path):
    """Gives a copy rules of its own for this account only, as the tools require of a private file."""
    account = subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()
    targets = [path, *path.rglob("*")] if path.is_dir() else [path]
    for target in targets:
        subprocess.run(["icacls", str(target), "/inheritance:r", "/grant:r", f"{account}:F"], capture_output=True, check=True)


def wipe(folder):
    for path in sorted(folder.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_file():
            size = path.stat().st_size
            with open(path, "r+b") as handle:
                handle.write(secrets.token_bytes(size))
                handle.flush()
                os.fsync(handle.fileno())
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    folder.rmdir()


class Tools:
    def __init__(self, wallet, tenraid, work):
        self.wallet, self.tenraid, self.work = wallet, tenraid, work
        profile = work / "profile"
        profile.mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.upper().startswith("TENRAI_")}
        self.env.update(LOCALAPPDATA=str(profile), APPDATA=str(profile), HOME=str(profile),
                        XDG_CONFIG_HOME=str(profile), XDG_DATA_HOME=str(profile), XDG_CACHE_HOME=str(profile))

    def run(self, executable, args):
        """Runs a command whose output holds only public values; its output is parsed, never printed whole."""
        done = subprocess.run([str(executable), "--no-color", *args], capture_output=True, text=True,
                              env=self.env, cwd=self.work, timeout=300)
        if done.returncode != 0:
            # The tools never print a secret in an error; the first line names what they refused.
            print(f"INFO {Path(executable).name} {args[0]} failed: {done.stderr.strip()[:240]}")
            return None
        return done.stdout

    def json(self, args):
        out = self.run(self.wallet, [*args, "--output", "json"])
        if out is None:
            return None
        lines = [line for line in out.strip().splitlines() if line.startswith("{")]
        return json.loads(out) if out.strip().startswith("{") else (json.loads(lines[-1]) if lines else None)

    def password_file(self, name):
        path = self.work / name
        path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        explicit(path)
        return path


def check_sealed(tools, name, base, public):
    """The server's sealed keys, read back as the node reads them, against the published public values."""
    work = tools.work / name / "sealed"
    work.mkdir()
    for label, command, stem, option, field in [
        ("validator key", "validator-key-check", "validator-key", "validator-key", "consensus_public_key"),
        ("heartbeat fee key", "validator-heartbeat-fee-key-check", "heartbeat-fee-key", "heartbeat-fee-key",
         "heartbeat_fee_address"),
    ]:
        sealed, password = work / f"{stem}.sealed", work / f"{stem}.pass"
        shutil.copyfile(base / "server" / f"{stem}.sealed", sealed)
        shutil.copyfile(base / "server" / f"{stem}.pass", password)
        explicit(sealed)
        explicit(password)
        read = tools.json([command, "--network", "mainnet", f"--{option}-file", str(sealed),
                           f"--{option}-password-file", str(password)])
        value = read and read.get(field)
        record(f"{name}: the server's sealed {label} opens with its password and holds the published {field}",
               value == public[field], short(value))


def check_validator(tools, wordlist, name, manifest, sealed, sheet):
    base = FILES / name
    public = json.loads((base / "public.json").read_text(encoding="utf-8"))
    work = tools.work / name
    work.mkdir()
    vault, password = work / "validator.vault", work / "validator-wallet.pass"
    shutil.copyfile(base / "wallet" / "validator.vault", vault)
    shutil.copyfile(base / "wallet" / "validator-wallet.pass", password)
    explicit(vault)
    explicit(password)
    unlock = ["--file", str(vault), "--password-file", str(password), "--network", "mainnet"]

    first = tools.json(["address", *unlock, "--index", "0"])
    vault_address = first and first.get("address")
    record(f"{name}: the vault's first address is the published wallet address", vault_address == public["wallet_address"],
           short(vault_address))

    phrase = phrase_of((base / "wallet" / "validator-wallet-phrase.txt").read_text(encoding="utf-8"), wordlist)
    record(f"{name}: the 24-word file holds a whole phrase", phrase is not None and len(phrase.split()) == 24)
    record(f"{name}: the owner's sheet holds the same 24 words", phrase is not None and sheet.get(name) == phrase)
    if phrase:
        mnemonic = work / "phrase.txt"
        mnemonic.write_text(phrase, encoding="utf-8")
        explicit(mnemonic)
        restored = tools.json(["import", "--file", str(work / "restored.vault"), "--mnemonic-file", str(mnemonic),
                               "--password-file", str(tools.password_file(f"{name}-restored.pass")), "--network", "mainnet"])
        restored_address = restored and restored.get("first_address", {}).get("address")
        record(f"{name}: the 24 words restore the published wallet address", restored_address == public["wallet_address"],
               short(restored_address))
        record(f"{name}: the 24 words give the published recovery fingerprint",
               bool(restored) and restored.get("recovery_fingerprint") == public["recovery_fingerprint"],
               str(restored and restored.get("recovery_fingerprint")))

    init = json.loads((base / "wallet" / "validator-wallet-init.json").read_text(encoding="utf-8"))
    record(f"{name}: the init output names the published wallet address",
           init.get("first_address", {}).get("address") == public["wallet_address"])
    init_phrase = phrase_of(str(init.get("recovery_phrase", "")), wordlist)
    record(f"{name}: the init output's phrase is the 24-word file's", init_phrase is not None and init_phrase == phrase)

    exported = tools.json(["validator-key-export", *unlock, "--validator-key-file", str(work / "check-validator.key"),
                           "--validator-key-password-file", str(tools.password_file(f"{name}-key.pass"))])
    derived_key = exported and exported.get("consensus_public_key")
    record(f"{name}: the wallet derives the published consensus key", derived_key == public["consensus_public_key"],
           short(derived_key))
    fee = tools.json(["validator-heartbeat-fee-key-export", *unlock, "--heartbeat-fee-key-file", str(work / "check-fee.key"),
                      "--heartbeat-fee-key-password-file", str(tools.password_file(f"{name}-fee.pass"))])
    derived_fee = fee and fee.get("heartbeat_fee_address")
    record(f"{name}: the wallet derives the published heartbeat fee address", derived_fee == public["heartbeat_fee_address"],
           short(derived_fee))
    for file, key in [("validator-key-export.json", "consensus_public_key"), ("heartbeat-fee-key-export.json", "heartbeat_fee_address")]:
        document = json.loads((base / "wallet" / file).read_text(encoding="utf-8"))
        record(f"{name}: {file} names the published {key}", document.get(key) == public[key])

    server = work / "server"
    shutil.copytree(base / "server" / "network", server / "network")
    explicit(server)
    endpoint = tools.run(tools.tenraid, ["validator-endpoint", "--network", "mainnet", "--data-dir", str(server),
                                         "--route", public["host"]])
    endpoint = endpoint and endpoint.strip().splitlines()[-1].strip()
    record(f"{name}: the server's overlay keys give the published endpoint", endpoint == public["overlay_endpoint_hex"],
           short(endpoint))

    node = json.loads((base / "server" / "node.json").read_text(encoding="utf-8"))
    heartbeat = (node.get("validator") or {}).get("heartbeat") or {}
    record(f"{name}: node.json names the published validator ID", heartbeat.get("validator_id") == public["validator_id"],
           short(heartbeat.get("validator_id")))

    entry = next((row for row in manifest["validators"] if row["validator_id"] == public["validator_id"]), None)
    record(f"{name}: the mainnet manifest holds this validator", entry is not None)
    if entry:
        for field, value in [("owner_address", public["wallet_address"]), ("reward_address", public["wallet_address"]),
                             ("self_bond_address", public["wallet_address"]), ("consensus_public_key", public["consensus_public_key"]),
                             ("overlay_endpoint_hex", public["overlay_endpoint_hex"])]:
            record(f"{name}: the manifest's {field} is the published one", entry.get(field) == value)
    if sealed:
        check_sealed(tools, name, base, public)
    check_permissions(name, base)
    return public


def check_permissions(name, base):
    account = subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip().lower()
    allowed = {account, "nt authority\\system", "nt-autorität\\system", "builtin\\administrators",
               "vordefiniert\\administratoren"}
    for path in sorted((base / "server").rglob("*")) + sorted((base / "wallet").rglob("*")):
        if not path.is_file():
            continue
        listed = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
        principals = set()
        for line in listed.splitlines():
            line = line.replace(str(path), "").strip()
            if ":" in line and not line.lower().startswith("successfully") and not line.lower().startswith("erfolgreich"):
                principals.add(line.rsplit(":", 1)[0].strip().lower())
        others = sorted(principal for principal in principals if principal and principal not in allowed)
        if others:
            record(f"{name}: only this account opens {path.relative_to(FILES)}", False, ", ".join(others))
    record(f"{name}: the secret files are closed to other accounts", all(passed for check, passed in results
           if check.startswith(f"{name}: only this account")))


def check_treasury(tools, wordlist, used, sheet):
    treasury = json.loads((FILES / "treasury" / "public.json").read_text(encoding="utf-8"))
    record("treasury: its address is none of a validator's wallet or fee addresses", treasury["address"] not in used)
    record("treasury: its address is no retired address", treasury["address"] not in retired_addresses())
    work = tools.work / "treasury"
    work.mkdir()
    vault, password = work / "treasury.vault", work / "treasury.vault.pass"
    shutil.copyfile(FILES / "treasury" / "treasury.vault", vault)
    shutil.copyfile(FILES / "treasury" / "treasury.vault.pass", password)
    explicit(vault)
    explicit(password)
    unlock = ["--file", str(vault), "--password-file", str(password), "--network", "mainnet"]
    first = tools.json(["address", *unlock, "--index", "0"])
    record("treasury: the wallet file opens with its password and gives the published address",
           bool(first) and first.get("address") == treasury["address"], short(first and first.get("address")))
    out = tools.run(tools.wallet, ["export-recovery", *unlock, "--i-understand-this-exposes-my-recovery-phrase"])
    phrase = sheet.get("treasury")
    record("treasury: the owner's sheet holds the wallet file's 24 words",
           phrase is not None and phrase_of(out or "", wordlist) == phrase)
    record("treasury: its phrase file holds the same 24 words", phrase is not None and phrase_of(
        (FILES / "treasury" / "treasury-wallet-phrase.txt").read_text(encoding="utf-8"), wordlist) == phrase)
    mnemonic = work / "phrase.txt"
    mnemonic.write_text(phrase or "", encoding="utf-8")
    explicit(mnemonic)
    restored = tools.json(["import", "--file", str(work / "restored.vault"), "--mnemonic-file", str(mnemonic),
                           "--password-file", str(tools.password_file("treasury-restored.pass")), "--network", "mainnet"])
    address = restored and restored.get("first_address", {}).get("address")
    record("treasury: the sheet's 24 words restore the published address", address == treasury["address"], short(address))
    record("treasury: the sheet's 24 words give the published fingerprint",
           bool(restored) and restored.get("recovery_fingerprint") == treasury["recovery_fingerprint"],
           treasury["recovery_fingerprint"])
    seed = FILES / "seed.md"
    if seed.exists():
        record("treasury: the retired words of seed.md are not the treasury's",
               phrase_of(seed.read_text(encoding="utf-8"), wordlist) != phrase)
    return treasury


SECRETS = ["mainnet-seeds.txt", "treasury/treasury.vault", "treasury/treasury.vault.pass",
           "treasury/treasury-wallet-phrase.txt", "release-signing/mldsa65.key", "release-signing/slhdsa192s.key"]


def check_private_files():
    """Every secret of the mainnet files exists and git ignores it."""
    for name in SECRETS:
        ignored = subprocess.run(["git", "-C", str(FILES), "check-ignore", "-q", name], capture_output=True).returncode == 0
        record(f"git ignores {name}", (FILES / name).is_file() and ignored)
    tracked = subprocess.run(["git", "-C", str(FILES), "ls-files"], capture_output=True, text=True).stdout.split()
    record("git tracks no secret of the mainnet files", not set(SECRETS) & set(tracked))


def check_release_keys(repo):
    published = FILES / "release-signing" / "release-keys.json"
    compiled = repo / "config" / "release-keyset.json"
    keys = json.loads(published.read_text(encoding="utf-8"))
    record("release: the compiled keyset is the published one", compiled.read_bytes() == published.read_bytes())
    for algorithm in ("mldsa65", "slhdsa192s"):
        public = (FILES / "release-signing" / f"{algorithm}.pub").read_text(encoding="utf-8").strip()
        record(f"release: the {algorithm} key's public half is the keyset's", public == keys[algorithm])
        record(f"release: the {algorithm} private key is there", (FILES / "release-signing" / f"{algorithm}.key").is_file())


def check_genesis(manifest, treasury, publics, repo, before_release):
    allocations = {row["address"]: int(row["amount"]) for row in manifest["allocations"]}
    record("genesis: the allocations add up to the 20,000,000 TRAI of genesis", sum(allocations.values()) == TOTAL_ATOMS,
           f"{sum(allocations.values()) / 1e8:,.0f} TRAI")
    record("genesis: the treasury holds its published amount", allocations.get(treasury["address"]) == int(treasury["atoms"]))
    for (name, public), bond in zip(publics, [400_000, 300_000, 300_000]):
        record(f"genesis: {name} bonds {bond:,} TRAI", allocations.get(public["wallet_address"]) == bond * 100_000_000)
        record(f"genesis: {name}'s heartbeat fee address holds 100 TRAI",
               allocations.get(public["heartbeat_fee_address"]) == 100 * 100_000_000)
    record("genesis: no other allocation", len(allocations) == 1 + 2 * len(publics))
    record("genesis: no retired address holds an allocation", not (retired_addresses() & set(allocations)))
    node_manifest = json.loads((repo / "config" / "mainnet.traidag.json").read_text(encoding="utf-8"))
    record("genesis: the node's built-in manifest is the mainnet files' manifest", node_manifest == manifest)
    record("genesis: the node's chain ID is the manifest's",
           (repo / "config" / "mainnet.chain-id").read_text(encoding="utf-8").strip() == manifest["chain_id"])
    files_id = (FILES / "genesis" / "mainnet.genesis-id").read_text(encoding="utf-8").strip()
    node_id = (repo / "config" / "mainnet.genesis-id").read_text(encoding="utf-8").strip()
    if before_release and node_id != files_id:
        print(f"INFO genesis IDs differ until the release build installs the new genesis: node {short(node_id)}, "
              f"files {short(files_id)}")
    else:
        record("genesis: the node's genesis ID is the mainnet files' one", node_id == files_id,
               f"node {short(node_id)}, files {short(files_id)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wallet", type=Path)
    parser.add_argument("--tenraid", type=Path)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--sealed", action="store_true")
    parser.add_argument("--before-release", action="store_true")
    arguments = parser.parse_args()
    arguments.repo = arguments.repo.resolve()
    binaries = arguments.repo / "target" / "release"
    wallet = (arguments.wallet or binaries / "tenrai-wallet.exe").resolve()
    tenraid = (arguments.tenraid or binaries / "tenraid.exe").resolve()
    wordlist = words()
    sheet = sheet_phrases(wordlist)
    manifest = json.loads((FILES / "genesis" / "mainnet.traidag.json").read_text(encoding="utf-8"))
    work = private_folder()
    try:
        tools = Tools(wallet, tenraid, work)
        publics = [(name, check_validator(tools, wordlist, name, manifest, arguments.sealed, sheet))
                   for name in VALIDATORS]
        used = {public[key] for _, public in publics for key in ("wallet_address", "heartbeat_fee_address")}
        treasury = check_treasury(tools, wordlist, used, sheet)
        check_private_files()
        check_genesis(manifest, treasury, publics, Path(arguments.repo), arguments.before_release)
        check_release_keys(Path(arguments.repo))
    finally:
        wipe(work)
    failed = [name for name, passed in results if not passed]
    print(f"{len(results) - len(failed)} of {len(results)} checks passed")
    for name in failed:
        print(f"FAILED: {name}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
