"""Signs a release folder with both release keys, and checks the signing end to end with every refusal.

    py -3.14 release_signing.py sign  --release DIR --tool EXE [--mldsa-key FILE] [--slhdsa-key FILE]
    py -3.14 release_signing.py check --tool EXE [--mldsa-key FILE] [--slhdsa-key FILE] [--keyset FILE]

The keys default to TENRAI_RELEASE_MLDSA_KEY and TENRAI_RELEASE_SLHDSA_KEY; only tenrai-release reads them.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from node_checkout import node_checkout

GITHUB = Path(__file__).resolve().parents[2]
REPO = node_checkout(GITHUB)
FILES = Path(os.environ.get("TENRAI_MAINNET_FILES", GITHUB / "tenrai-mainnet-files"))
KEYS = FILES / "release-signing"
PUBLISHED = ["bin", "web", "sdk", "wasm", "desktop", "config", "docs", "genesis", "checks"]
RECORDS = ["RELEASE.json", "README.md", "SOURCES.json", "COMMAND-SOURCES.json", "checks.tsv", "inputs.txt"]
MAX_ARTIFACTS = 1024
KEYSET_DOMAIN = b"TRAI/RELEASE/KEYSET/1"


def tool_run(tool, *args):
    done = subprocess.run([str(tool), *map(str, args)], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=900)
    values = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    return done.returncode, values, done.stderr.strip()


def keyset_fingerprint(path):
    keys = json.loads(Path(path).read_text(encoding="utf-8"))
    digest = hashlib.sha384()
    digest.update(KEYSET_DOMAIN)
    digest.update(int(keys["format"]).to_bytes(2, "big"))
    digest.update(bytes.fromhex(keys["mldsa65"]))
    digest.update(bytes.fromhex(keys["slhdsa192s"]))
    return digest.hexdigest()


def key_paths(arguments):
    mldsa = arguments.mldsa_key or os.environ.get("TENRAI_RELEASE_MLDSA_KEY") or KEYS / "mldsa65.key"
    slhdsa = arguments.slhdsa_key or os.environ.get("TENRAI_RELEASE_SLHDSA_KEY") or KEYS / "slhdsa192s.key"
    return Path(mldsa), Path(slhdsa)


def sign_folder(tool, root, artifacts, version, commit, mldsa, slhdsa, out):
    out.mkdir(parents=True, exist_ok=True)
    manifest, sig = out / "release.json", out / "release.sig.json"
    code, values, error = tool_run(tool, "manifest", "--root", root, "--version", version, "--commit", commit,
                                   *[item for path in artifacts for item in ("--artifact", path)], "--output", manifest)
    if code:
        return False, f"manifest refused: {error}"
    digest = values.get("manifest_sha384", "")
    for command, key, partial in (("sign-mldsa", mldsa, out / "release.mldsa.json"),
                                  ("sign-slhdsa", slhdsa, out / "release.slhdsa.json")):
        code, _, error = tool_run(tool, command, "--manifest", manifest, "--key", key, "--approve-manifest", digest,
                                  "--output", partial)
        if code:
            return False, f"{command} refused: {error}"
    code, _, error = tool_run(tool, "combine", "--manifest", manifest, "--mldsa-partial", out / "release.mldsa.json",
                              "--slhdsa-partial", out / "release.slhdsa.json", "--output", sig)
    if code:
        return False, f"combine refused: {error}"
    code, values, error = tool_run(tool, "verify", "--manifest", manifest, "--signatures", sig, "--root", root)
    if code or values.get("signatures_verified") != "mldsa65,slhdsa192s" \
            or values.get("artifacts_verified") != str(len(artifacts)):
        return False, f"verify failed: {error or values}"
    return True, f"{len(artifacts)} files, manifest SHA-384 {digest}, keyset {values.get('keyset_fingerprint')}"


def workspace_version():
    text = (REPO / "Cargo.toml").read_text(encoding="utf-8")
    section = text.split("[workspace.package]", 1)[1]
    return re.search(r'^version = "([^"]+)"', section, re.M).group(1)


def head_commit():
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def sign(arguments):
    root = Path(arguments.release).resolve()
    out = root / "signing"
    if out.exists():
        raise SystemExit(f"{out} exists; a release is signed once")
    artifacts = sorted(path for part in PUBLISHED if (root / part).is_dir()
                       for path in (root / part).rglob("*") if path.is_file())
    artifacts += [root / name for name in RECORDS if (root / name).is_file()]
    if not artifacts or len(artifacts) > MAX_ARTIFACTS:
        raise SystemExit(f"{len(artifacts)} release files; a manifest holds 1 to {MAX_ARTIFACTS}")
    mldsa, slhdsa = key_paths(arguments)
    ok, message = sign_folder(arguments.tool, root, artifacts, arguments.version or workspace_version(),
                              arguments.commit or head_commit(), mldsa, slhdsa, out)
    print(f"{'PASS' if ok else 'FAIL'} release signed and verified: {message}")
    sys.exit(0 if ok else 1)


def check(arguments):
    tool = Path(arguments.tool)
    mldsa, slhdsa = key_paths(arguments)
    results = []

    def record(name, passed, detail=""):
        results.append(passed)
        print(f"{'PASS' if passed else 'FAIL'} {name}{': ' + detail if detail else ''}", flush=True)

    def refused(name, *args):
        code, _, error = tool_run(tool, *args)
        record(f"refused: {name}", code != 0, error.splitlines()[-1][:160] if code and error else "accepted")

    keyset = Path(arguments.keyset or FILES / "release-signing" / "release-keys.json")
    code, values, error = tool_run(tool, "trust-anchor")
    expected = keyset_fingerprint(keyset)
    record("the tool's compiled trust anchor is the published keyset", code == 0
           and values.get("keyset_fingerprint") == expected, expected[:16])

    scratch = Path(tempfile.mkdtemp(prefix="tenrai-release-signing-"))
    try:
        root = scratch / "dist"
        (root / "bin").mkdir(parents=True)
        (root / "docs").mkdir()
        (root / "bin" / "tenraid").write_bytes(secrets.token_bytes(1 << 20))
        (root / "docs" / "README.md").write_text("release notes\n", encoding="utf-8")
        (root / "RELEASE.json").write_text('{"release": "check"}\n', encoding="utf-8")
        artifacts = [root / "bin" / "tenraid", root / "docs" / "README.md", root / "RELEASE.json"]
        commit = "a" * 40
        out = scratch / "signed"
        ok, message = sign_folder(tool, root, artifacts, "0.1.0", commit, mldsa, slhdsa, out)
        record("both keys sign one manifest, the signatures combine and verify against every file", ok, message)
        if not ok:
            return results
        manifest, sig = out / "release.json", out / "release.sig.json"
        code, values, error = tool_run(tool, "verify", "--manifest", manifest, "--signatures", sig, "--public-keys", keyset)
        record("verify accepts the published keyset as a comparison", code == 0, error[:160])

        refused("signing for a digest other than the manifest's", "sign-mldsa", "--manifest", manifest, "--key", mldsa,
                "--approve-manifest", "00" * 48, "--output", scratch / "wrong-approval.json")
        refused("signing without an approval digest and without a terminal", "sign-slhdsa", "--manifest", manifest,
                "--key", slhdsa, "--output", scratch / "no-approval.json")
        refused("combining swapped partial signatures", "combine", "--manifest", manifest,
                "--mldsa-partial", out / "release.slhdsa.json", "--slhdsa-partial", out / "release.mldsa.json",
                "--output", scratch / "swapped.sig.json")
        refused("writing over an existing signature", "combine", "--manifest", manifest,
                "--mldsa-partial", out / "release.mldsa.json", "--slhdsa-partial", out / "release.slhdsa.json",
                "--output", sig)

        (root / "docs" / "README.md").write_text("other release notes\n", encoding="utf-8")
        other = scratch / "other"
        ok, _ = sign_folder(tool, root, artifacts, "0.1.0", commit, mldsa, slhdsa, other)
        record("a second manifest over changed files signs on its own", ok)
        refused("a partial signature of another manifest", "combine", "--manifest", manifest,
                "--mldsa-partial", other / "release.mldsa.json", "--slhdsa-partial", out / "release.slhdsa.json",
                "--output", scratch / "foreign.sig.json")
        refused("a file that changed after signing", "verify", "--manifest", manifest, "--signatures", sig,
                "--root", root)

        altered = json.loads(sig.read_text(encoding="utf-8"))
        flipped = "0" if altered["mldsa65"][10] != "0" else "1"
        altered["mldsa65"] = altered["mldsa65"][:10] + flipped + altered["mldsa65"][11:]
        (scratch / "altered.sig.json").write_text(json.dumps(altered), encoding="utf-8")
        refused("an altered ML-DSA-65 signature", "verify", "--manifest", manifest, "--signatures",
                scratch / "altered.sig.json")
        altered = json.loads(sig.read_text(encoding="utf-8"))
        flipped = "0" if altered["slhdsa192s"][10] != "0" else "1"
        altered["slhdsa192s"] = altered["slhdsa192s"][:10] + flipped + altered["slhdsa192s"][11:]
        (scratch / "altered-slh.sig.json").write_text(json.dumps(altered), encoding="utf-8")
        refused("an altered SLH-DSA signature", "verify", "--manifest", manifest, "--signatures",
                scratch / "altered-slh.sig.json")

        throwaway = scratch / "throwaway"
        made = [tool_run(tool, "keygen-mldsa", "--key", throwaway / "m.key", "--public-key", throwaway / "m.pub")[0],
                tool_run(tool, "keygen-slhdsa", "--key", throwaway / "s.key", "--public-key", throwaway / "s.pub")[0],
                tool_run(tool, "keyset", "--mldsa-public-key", throwaway / "m.pub", "--slhdsa-public-key",
                         throwaway / "s.pub", "--output", throwaway / "keys.json")[0]]
        record("a throwaway keyset for the refusals", made == [0, 0, 0])
        # The manifest's own digest: a refusal can then come only from the key.
        digest = json.loads((out / "release.mldsa.json").read_text(encoding="utf-8"))["manifest_sha384"]
        refused("an ML-DSA-65 key outside the compiled keyset", "sign-mldsa", "--manifest", manifest,
                "--key", throwaway / "m.key", "--approve-manifest", digest, "--output", scratch / "outside.json")
        refused("an SLH-DSA key outside the compiled keyset", "sign-slhdsa", "--manifest", manifest,
                "--key", throwaway / "s.key", "--approve-manifest", digest, "--output", scratch / "outside-slh.json")
        refused("another keyset offered for comparison", "verify", "--manifest", manifest, "--signatures", sig,
                "--public-keys", throwaway / "keys.json")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["sign", "check"])
    parser.add_argument("--tool", required=True)
    parser.add_argument("--release")
    parser.add_argument("--version")
    parser.add_argument("--commit")
    parser.add_argument("--mldsa-key")
    parser.add_argument("--slhdsa-key")
    parser.add_argument("--keyset")
    arguments = parser.parse_args()
    if arguments.action == "sign":
        if not arguments.release:
            raise SystemExit("sign needs --release DIR")
        sign(arguments)
    results = check(arguments)
    print(f"{sum(results)} of {len(results)} signing checks passed")
    sys.exit(0 if results and all(results) else 1)


if __name__ == "__main__":
    main()
