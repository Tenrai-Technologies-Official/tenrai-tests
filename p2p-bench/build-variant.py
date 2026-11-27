"""Build a benchmark variant in a separate copy and freeze its binaries.

    py -3.14 build-variant.py VARIANT

Combine variant names with +. VARIANT_COPY_ONLY creates the source copy
without building. VARIANT_BASE selects the frozen binaries to reuse, and
VARIANT_NAME selects the output name. Set VARIANT_WORK to place the source
copy elsewhere or BUILD_CPUS to set the Windows CPU affinity mask.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import block_patches
import miner_patches
import prof_patches
import relay_patches
import rotation_patches
import tsp2_genesis_patches
from preflight import BINARIES, freeze, frozen

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS.parent))
from release.node_checkout import node_checkout

REPO = node_checkout(TOOLS.parents[1])
WORK = Path(os.environ.get("VARIANT_WORK", TOOLS / "run-build"))
# VARIANT_BASE names another frozen folder to take the binaries a variant does not build from.
MAIN = TOOLS / os.environ.get("VARIANT_BASE", "bin-main")
PARAMS = Path("crates/primitives/src/params/consensus.rs")

def positions_per_validator(held, records):
    """Positions a validator may hold and payout records a cut may pay, measured on a copy. The smallest
    delegation comes down to 10 TRAI so that the development genesis funds them all; the payout bound stays
    the same when `records` is `held` + 256."""
    rule_set = Path("crates/primitives/src/params/rule_set.rs")
    return [
        (Path("crates/traidag/src/pos/epoch.rs"), "self.max_stake_positions / self.max_validator_records", str(held)),
        (rule_set, "max_stake_payout_records: 512,", f"max_stake_payout_records: {records},"),
        (rule_set, "minimum_delegation_atoms: 100_000_000_000,", "minimum_delegation_atoms: 1_000_000_000,"),
        (Path("crates/primitives/src/params/staking.rs"),
         "pub const MIN_DELEGATION_ATOMS: u128 = 1_000 * ATOMS_PER_TRAI as u128;",
         "pub const MIN_DELEGATION_ATOMS: u128 = 10 * ATOMS_PER_TRAI as u128;"),
        (Path("crates/traidag/src/settlement/pool.rs"),
         "pub const MAX_STAKE_PAYOUT_RECORDS: u32 = 512;",
         f"pub const MAX_STAKE_PAYOUT_RECORDS: u32 = {records};"),
        # A unit test pays 1,500 positions in three slices of the cap of 512; under a larger cap the same
        # three slices need three caps less 36, which is 1,500 again at 512.
        (Path("crates/node/src/chain/tests/stake.rs"), "let all: Vec<usize> = (0..1_500).collect();",
         "let all: Vec<usize> = (0..3 * MAX_STAKE_PAYOUT_RECORDS as usize - 36).collect();"),
        (Path("crates/node/src/chain/tests/stake.rs"),
         "assert_eq!(third.len(), 1_500 - 2 * MAX_STAKE_PAYOUT_RECORDS as usize);",
         "assert_eq!(third.len(), MAX_STAKE_PAYOUT_RECORDS as usize - 36);"),
    ]


PATCHES = {
    "pos256": positions_per_validator(256, 512),
    "pos512": positions_per_validator(512, 768),
    "pos1024": positions_per_validator(1_024, 1_280),
    # Twelve payments in a block on the development and mainnet profiles: the option of more block room,
    # measured on a copy. It changes the genesis, so it has a snapshot of its own, and the miner asks for
    # templates of that size, so it comes from the variant too.
    "tx12": [
        (PARAMS, """pub const MAINNET: ConsensusParams = ConsensusParams {
    work_blocks_per_second: 50,
    cut_interval_millis: 2_500,
    transactions_per_work_block: 10,""",
         """pub const MAINNET: ConsensusParams = ConsensusParams {
    work_blocks_per_second: 50,
    cut_interval_millis: 2_500,
    transactions_per_work_block: 12,"""),
        (PARAMS, "pub const WORK_BODY_BYTES: usize = 46 * 1024;", "pub const WORK_BODY_BYTES: usize = 56 * 1024;"),
        (PARAMS, "pub const WORK_TRANSACTIONS: u32 = 10;", "pub const WORK_TRANSACTIONS: u32 = 12;"),
        (PARAMS, "pub const WORK_AUTHORIZATIONS: u32 = 10;", "pub const WORK_AUTHORIZATIONS: u32 = 12;"),
        (PARAMS, "pub const WORK_PAYLOAD_MASS: u64 = 96_000;", "pub const WORK_PAYLOAD_MASS: u64 = 116_000;"),
    ],
    "cut3000": [
        (PARAMS, """pub const MAINNET: ConsensusParams = ConsensusParams {
    work_blocks_per_second: 50,
    cut_interval_millis: 2_500,
    transactions_per_work_block: 10,
    registration_block_cap: 250,
    execution_block_cap: 256,""",
         """pub const MAINNET: ConsensusParams = ConsensusParams {
    work_blocks_per_second: 50,
    cut_interval_millis: 3_000,
    transactions_per_work_block: 10,
    registration_block_cap: 300,
    execution_block_cap: 300,"""),
    ],
    "t100": [
        (PARAMS, """pub const DEVELOPMENT: ConsensusParams = ConsensusParams {
    full_tenq_dataset: false,
    ..MAINNET
};""",
         """pub const DEVELOPMENT: ConsensusParams = ConsensusParams {
    full_tenq_dataset: false,
    ..TESTNET_100
};"""),
    ],
    # Days, and so epochs and snapshot packages, of 512 cuts, as the node's tests shorten them. The run's chain
    # has a genesis of its own: its manifest is the root of bridges and packages, as on a testnet.
    "day512": [
        (Path("crates/traidag/src/genesis/trusted.rs"), "            Network::Testnet | Network::Testnet100 => Some(Self {",
         "            Network::Testnet | Network::Testnet100 | Network::Development => Some(Self {"),
        (Path("crates/traidag/src/genesis/trusted.rs"), "            Network::Mainnet | Network::Development => None,",
         "            Network::Mainnet => None,"),
        (Path("crates/traidag/src/genesis/trusted.rs"),
         "        Network::Development => compiled_development_trusted_genesis_finality_bundle(),\n", ""),
        (Path("crates/traidag/src/daa/rate.rs"), "accounting_interval_cuts: params.cuts_per_day(),",
         "accounting_interval_cuts: 512,"),
        (Path("crates/traidag/src/daa/rate.rs"), """    #[cfg(not(any(test, feature = "test-support")))]
    const fn shortened_day_for_test(self) -> bool {
        false
    }""", """    #[cfg(not(any(test, feature = "test-support")))]
    const fn shortened_day_for_test(self) -> bool {
        true
    }"""),
    ],
    "prof": prof_patches.PATCHES,
    "relay": relay_patches.PATCHES,
    "rotation-b": rotation_patches.PATCHES,
    "tsp2-genesis": tsp2_genesis_patches.PATCHES,
    "minertrace": miner_patches.PATCHES,
    "norefresh": [
        (Path("bins/miner/src/source/node.rs"), """                // A transaction fits in one block only. A template without transactions, found once, has
                // served too: the next block should carry what the node holds by now, so the miner stops
                // and asks for new work at once. A block already found on it still goes out.
                if template.body().transactions().is_empty() {
                    if work.is_current_serial(solution.serial) {
                        work.publish(None);
                        found_at = Some(Instant::now());
                        carried.refresh();
                    }
                } else {
""", """                // A transaction fits in one block only; an empty template keeps finding more.
                if !template.body().transactions().is_empty() {
"""),
        (Path("crates/mining/src/node/feed.rs"), """            // A cut, the fallback tick, or a find: a found template, with transactions or without,
            // has served, and new work follows at once.
            tokio::select! {
                _ = self.cuts.recv() => {}
                _ = self.fallback.tick() => {}
                () = self.carried.found.notified() => {}
            }
""", """            let found = tokio::select! {
                _ = self.cuts.recv() => false,
                _ = self.fallback.tick() => false,
                () = self.carried.found.notified() => true,
            };
            // Any number of blocks may carry a template without transactions.
            if found
                && self
                    .latest
                    .as_ref()
                    .is_some_and(|latest| latest.body().transactions().is_empty())
            {
                continue;
            }
"""),
    ],
    "blocks": block_patches.PATCHES,
    "plain": [
        (Path("crates/node/src/chain/finality/service.rs"),
         "        self.prefixes = Some(PrefixWorker::start());\n", ""),
        (Path("bins/tenraid/src/run.rs"),
         """    if let Err(error) = tenrai_auth::remember_verified_signatures(VERIFIED_SIGNATURE_ENTRIES) {
        warn!("every signature is checked in full: {error}");
    }
""", """    let _ = VERIFIED_SIGNATURE_ENTRIES;
    warn!("this build checks every signature in full and runs no prefix ahead");
"""),
    ],
    # The earlier template rule: a relayed transaction joins the node's own after one cut interval.
    "oldrule": [
        (Path("crates/node/src/chain/mempool.rs"), """        let reference = level
            .iter()
            .filter(|entry| entry.arrival.submitted_here)
            .map(|entry| entry.arrival.at)
            .min()
            .unwrap_or(now);""", """        let reference = now;"""),
    ],
}


def copy_source(variant):
    source = WORK / variant
    if source.exists():
        shutil.rmtree(source)
    source.mkdir(parents=True)
    # New file times: variants share one target directory, cargo's hash of a path crate does not name its
    # folder, and with the original times it took a crate another variant had patched as still fresh.
    # protocol/ holds the vectors the crates' tests read through relative paths.
    for name in ["crates", "bins", "vendor", "config", ".cargo", "protocol"]:
        shutil.copytree(REPO / name, source / name, ignore=shutil.ignore_patterns("target", "node_modules"),
                        copy_function=shutil.copy)
    for name in ["Cargo.toml", "Cargo.lock", "rust-toolchain.toml"]:
        shutil.copy(REPO / name, source / name)
    patches = [patch for part in variant.split("+") for patch in PATCHES[part]]
    for path, old, new in patches:
        file = source / path
        if old is None:
            file.write_bytes(new.encode())
            continue
        if callable(old):
            file.write_bytes(old(file.read_bytes().decode()).encode())
            continue
        text = file.read_bytes().decode()
        if text.count(old) != 1:
            raise SystemExit(f"{variant}: {path} no longer looks as expected: {old!r}")
        file.write_bytes(text.replace(old, new).encode())
    return source


def main():
    variant = sys.argv[1]
    if os.environ.get("BUILD_CPUS"):
        from bench_proc import pin
        pin(os.getpid(), int(os.environ["BUILD_CPUS"], 0))
    source = copy_source(variant)
    if os.environ.get("VARIANT_COPY_ONLY"):
        print("patched", source)
        return
    # What a variant does not build it takes from the frozen main build, so every folder holds a whole set.
    _, reasons = frozen(MAIN)
    if reasons:
        raise SystemExit("; ".join(reasons))
    target = WORK / "target"
    built = target / "release"
    env = dict(os.environ, CARGO_TARGET_DIR=str(target))
    jobs = os.environ.get("BUILD_JOBS", "4")
    # A variant with another block size needs a miner that asks for it, a run that moves stake on
    # shorter days a wallet that accepts their rules.
    extras = {"tenrai-miner": "tenrai-miner.exe"} if "tx12" in variant else (
        {"tenrai-wallet-cli": "tenrai-wallet.exe"} if variant == "day512" else {})
    packages = ["tenraid", "tenrai-release"] + list(extras)
    nodes = built
    # The traced miner and the one without the refresh run against the node of the main build itself.
    if variant in ("minertrace", "norefresh"):
        extras, packages, nodes = {"tenrai-miner": "tenrai-miner.exe"}, ["tenrai-miner"], MAIN
    if variant == "norefresh":
        # Built as the release workspace is, so the miner of a comparison differs from the release one in its
        # source only and not in the features cargo unifies across the workspace.
        subprocess.run(["cargo", "build", "--release", "--offline", "--workspace", "-j", jobs],
                       cwd=source, env=env, check=True)
    else:
        for package in packages:
            subprocess.run(["cargo", "build", "--release", "--offline", "-j", jobs, "-p", package],
                           cwd=source, env=env, check=True)
    files = {name: MAIN / name for name in BINARIES}
    for name in ["tenraid.exe", "tenrai-release.exe"]:
        files[name] = nodes / name
    for name in extras.values():
        files[name] = built / name
    # A wallet refuses a chain of another parameter profile, so the prover tool of the private runs follows.
    if variant.split("+")[0] in ("tx12", "t100", "day512"):
        tool = source / "bench-private"
        if tool.exists():
            shutil.rmtree(tool)
        shutil.copytree(TOOLS / "bench-private", tool, ignore=shutil.ignore_patterns("target"))
        manifest = tool / "Cargo.toml"
        manifest.write_bytes(manifest.read_bytes().replace(b"../../../tenrai/crates", b"../crates"))
        subprocess.run(["cargo", "build", "--release", "--offline", "-j", jobs], cwd=tool, env=env, check=True)
        files["bench-private.exe"] = built / "bench-private.exe"
    # VARIANT_NAME freezes it under another name, so an earlier folder that results name stays as it is.
    out = TOOLS / f"bin-{os.environ.get('VARIANT_NAME', variant)}"
    manifest = freeze(files, out)
    for name, digest in manifest["files"].items():
        print(f"{digest[:16]}  {name}")
    print("built", out)


if __name__ == "__main__":
    main()
