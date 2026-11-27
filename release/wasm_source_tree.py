"""The source inputs of a browser package in the node repository's working tree, by the rule of the frozen-source tool.

Every file of each local package `cargo tree` resolves for the package's crate on wasm32, plus the workspace,
toolchain, packaging and genesis inputs. Build output folders are not inputs. The digest of the sorted file list is
the package revision, which `scripts/package-wasm.ps1 -Revision` writes into the package's release.json.

    py -3.14 wasm_source_tree.py record OUT_DIR [--package wallet|tenq]     writes the inventory, prints the revision
    py -3.14 wasm_source_tree.py finalize OUT_DIR [--package wallet|tenq]   checks the built package against it, adds
                                                                            its declaration hashes, copies it into pkg/

The wallet's inventory is browser-source-inputs.json, TenQ's tenq-source-inputs.json; the web wallet, the SDK and the
browser miner check them against release.json when they take a package.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from node_checkout import node_checkout

REPO = node_checkout(Path(__file__).resolve().parents[2])
VERIFIER_MANIFEST = "crates/tsp2/verifier/Cargo.toml"
if not (REPO / VERIFIER_MANIFEST).is_file():
    VERIFIER_MANIFEST = "crates/tsp2-verifier/Cargo.toml"
SKIP = {"target", "pkg", "pkg-threads", "node_modules", ".git", "__pycache__"}
PACKAGES = {
    "wallet": {
        "crate": "tenrai-wasm",
        "release": "tenrai-wasm",
        "folder": "crates/wasm",
        "inventory": "browser-source-inputs.json",
        "required": ["crates/wasm/Cargo.toml", VERIFIER_MANIFEST],
        "assets": {"module": "tenrai_wasm.js", "wasm": "tenrai_wasm_bg.wasm"},
        "declarations": ["tenrai_wasm.d.ts", "tenrai_wasm_bg.wasm.d.ts"],
    },
    "tenq": {
        "crate": "tenrai-tenq-wasm",
        "release": "tenrai-tenq-wasm",
        "folder": "crates/tenq-wasm",
        "inventory": "tenq-source-inputs.json",
        "required": ["crates/tenq-wasm/Cargo.toml"],
        "assets": {"module": "tenrai_tenq_wasm.js", "wasm": "tenrai_tenq_wasm_bg.wasm",
                   "threads_module": "tenrai_tenq_wasm_threads.js", "threads_wasm": "tenrai_tenq_wasm_threads_bg.wasm",
                   "webgpu": "tenq-webgpu.js", "shader": "tenq-shader.js"},
        "declarations": ["tenrai_tenq_wasm.d.ts", "tenrai_tenq_wasm_bg.wasm.d.ts", "tenrai_tenq_wasm_threads.d.ts"],
    },
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cargo(*arguments):
    result = subprocess.run(["cargo", *arguments], cwd=REPO, text=True, encoding="utf-8",
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise SystemExit(f"cargo {' '.join(arguments)} failed:\n{result.stderr[-8000:]}")
    return result.stdout


def files_under(root):
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(REPO)
        if any(part in SKIP for part in relative.parts) or not path.is_file() or path.is_symlink():
            continue
        yield relative.as_posix(), path


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def base_revision():
    """The commit the working tree started from, read from .git without running git; "unknown" when it is packed away."""
    git = REPO / ".git"
    head = (git / "HEAD").read_text(encoding="utf-8").strip() if (git / "HEAD").is_file() else ""
    if not head.startswith("ref: "):
        return head or "unknown"
    ref = head.removeprefix("ref: ")
    if (git / ref).is_file():
        return (git / ref).read_text(encoding="utf-8").strip()
    packed = git / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    return "unknown"


def record(out, package):
    metadata = json.loads(cargo("metadata", "--locked", "--offline", "--filter-platform",
                                "wasm32-unknown-unknown", "--format-version", "1"))
    tree = cargo("tree", "--locked", "--offline", "-p", package["crate"], "--target", "wasm32-unknown-unknown",
                 "--edges", "normal,build", "--prefix", "none", "--format", "{p}|{f}")
    selected = {}
    for line in tree.splitlines():
        if not line.strip():
            continue
        package_text, feature_text = line.split("|", 1)
        match = re.fullmatch(r"([\w-]+) v([^\s]+)(?: \((.*?)\))?(?: \(\*\))?", package_text)
        if not match:
            raise SystemExit(f"unknown cargo tree entry: {package_text}")
        name, version, location = match.groups()
        candidates = [entry for entry in metadata["packages"] if entry["name"] == name and entry["version"] == version]
        if len(candidates) > 1 and location:
            candidates = [entry for entry in candidates
                          if str(Path(entry["manifest_path"]).parent.resolve()).casefold()
                          == str(Path(location).resolve()).casefold()]
        if len(candidates) != 1:
            raise SystemExit(f"ambiguous resolved package: {package_text}")
        found = candidates[0]
        entry = selected.setdefault(found["id"], {
            "name": name, "version": version, "source": found["source"], "features": set(),
            "manifest_path": found["manifest_path"],
        })
        entry["features"].update(filter(None, feature_text.removesuffix(" (*)").split(",")))

    roots, resolved = [], []
    for entry in selected.values():
        entry["features"] = sorted(entry["features"])
        if entry["source"] is None:
            relative = Path(entry["manifest_path"]).parent.resolve().relative_to(REPO.resolve()).as_posix()
            roots.append(relative)
            entry["local_directory"] = relative
        entry.pop("manifest_path")
        resolved.append(entry)

    inputs = {}
    for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "scripts/package-wasm.ps1"):
        if (REPO / name).is_file():
            inputs[name] = sha(REPO / name)
    for folder in [".cargo", "config", *roots]:
        if (REPO / folder).is_dir():
            for name, path in files_under(REPO / folder):
                inputs[name] = sha(path)
    for required in package["required"]:
        if required not in inputs:
            raise SystemExit(f"{required} is missing from the source closure")
    revision = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    status = subprocess.run(["git", "--no-optional-locks", "status", "--porcelain"], cwd=REPO,
                            capture_output=True, check=False)
    write_json(out / package["inventory"], {
        "format": 2,
        "package": package["release"],
        "target": "wasm32-unknown-unknown",
        "source_tree_sha256": revision,
        "git_base_revision": base_revision(),
        "candidate_has_uncommitted_changes": status.returncode != 0 or bool(status.stdout),
        "input_scope": f"all files of the local packages cargo tree resolves for {package['crate']} on wasm32, "
                       "plus workspace, toolchain, packaging and genesis config inputs; build output folders excluded",
        "proof_verification_bypassed": False,
        "local_package_roots": sorted(root + "/" for root in roots),
        "resolved_packages": sorted(resolved, key=lambda entry: (entry["name"], entry["version"])),
        "files": inputs,
    })
    print(revision)


def finalize(out, package):
    saved = json.loads((out / package["inventory"]).read_text(encoding="utf-8"))
    for name, digest in saved["files"].items():
        if sha(REPO / name) != digest:
            raise SystemExit(f"a source input changed during the build: {name}")
    built = REPO / package["folder"] / "pkg"
    release = json.loads((built / "release.json").read_text(encoding="utf-8-sig"))
    if (release.get("format") != 1 or release.get("package") != package["release"]
            or release.get("revision") != saved["source_tree_sha256"]):
        raise SystemExit("the built package does not name this source revision")
    for kind, name in package["assets"].items():
        entry = release["assets"].get(kind)
        if entry is None or entry["file"] != name or sha(built / name) != entry["sha256"]:
            raise SystemExit(f"a built asset fails its release hash: {name}")
    saved["revision_kind"] = "source_tree_sha256"
    saved["declaration_sha256"] = {name: sha(built / name) for name in package["declarations"]}
    write_json(out / package["inventory"], saved)
    shutil.copyfile(out / package["inventory"], built / package["inventory"])
    print(json.dumps({"package": package["release"], "revision": saved["source_tree_sha256"],
                      "files": len(saved["files"]), "wasm_sha256": release["assets"]["wasm"]["sha256"],
                      "module_sha256": release["assets"]["module"]["sha256"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["record", "finalize"])
    parser.add_argument("out", type=Path)
    parser.add_argument("--package", choices=sorted(PACKAGES), default="wallet")
    arguments = parser.parse_args()
    {"record": record, "finalize": finalize}[arguments.action](arguments.out, PACKAGES[arguments.package])


if __name__ == "__main__":
    main()
