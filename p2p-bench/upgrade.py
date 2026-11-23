"""Four validators carry one chain through a rule change: py -3.14 upgrade.py [--fresh] [--keep]

The development chain runs on mainnet timing (2.5 s cuts, 50 blocks/s) and switches at UPGRADE to the
Testnet100 parameters (1.5 s cuts, 100 blocks/s, protocol version 2) without a new genesis. The four
validators run under different local conditions: v1 the release build, v2 the plain variant (no prefix
run ahead, every signature checked in full), v3 a one-thread dataset without the next epoch built ahead,
v4 TenQ light verification. Payments signed under protocol version 1 flow across the upgrade, v4
restarts just before it and v3 just after it, and a fifth node with pruned storage (validators may not
prune) then replays the chain from genesis. Every node must report the same cut ID and state root for
every height.

UPGRADE_HEIGHT (default 300) sets the height; the funded snapshot has to end well before it. Needs the
frozen release binaries in bin-main (freeze.py) and bin-plain from build-variant.py plain; the preflight
of bench.py checks both before the run starts.
"""

import json
import os
import sys
import threading
import time
import urllib.request

import bench
from bench import Bench, address, log, port
from bench_submit import lifecycle, load, submit, wait_final

UPGRADE = int(os.environ.get("UPGRADE_HEIGHT", "300"))
AFTER = 80
MAX_RATE = 200
PLAIN = bench.TOOLS / "bin-plain"
FOLLOWER = 5

bench.SCENARIOS["upgrade"] = {**bench.BASE, "wan": False, "hashes": 1000,
                              "miners": {1: 4, 2: 4, 3: 4, 4: 4}}


def follower_folder():
    """The validators fill the fast second disk; the replaying node writes to the first."""
    return bench.DATA / f"v{FOLLOWER}"


def get(index, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port('rpc', index)}{path}", timeout=10) as reply:
            return json.loads(reply.read())
    except Exception:
        return None


class UpgradeBench(Bench):
    def snapshot(self):
        return super().snapshot().with_name(f"upgrade{UPGRADE}-{super().snapshot().name}")

    def check_preconditions(self):
        files, reasons = bench.preflight.frozen(PLAIN)
        if reasons:
            bench.refuse(reasons)
        self.plain = files["tenraid.exe"]
        super().check_preconditions()
        self.preflight["plain"] = self.plain

    def clean(self):
        super().clean()
        bench.shutil.copyfile(PLAIN / "tenraid.exe", self.bin / "tenraid-plain.exe")
        if bench.preflight.sha256(self.bin / "tenraid-plain.exe") != self.plain:
            raise SystemExit(f"the copy of {PLAIN / 'tenraid.exe'} differs from its manifest")

    def create_manifest(self, validators, bonds, path):
        super().create_manifest(validators, bonds, path)
        manifest = json.loads(path.read_text())
        manifest["upgrades"] = [{"height": UPGRADE, "protocol_version": 2, "params": "development100"}]
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        log(bench.run(self.bin / "tenraid.exe", "--no-color", "genesis", "--manifest", path).strip())

    def write_configs(self):
        super().write_configs()
        for index, tenq in [(3, {"full_dataset": {"build_threads": 1, "prefetch_heights": 0}}),
                            (4, {"light": True})]:
            config_path = self.data(index) / "node.json"
            config = json.loads(config_path.read_text())
            config["tenq"] = tenq
            config_path.write_text(json.dumps(config, indent=2) + "\n")

    def node_args(self, index):
        binary = self.bin / ("tenraid-plain.exe" if index == 2 else "tenraid.exe")
        return [binary, "--no-color", "-d", "run", "--network", "development", "--manifest", self.manifest,
                "--data-dir", self.data(index)]

    def start_nodes(self):
        env = dict(os.environ, RUST_LOG=bench.RUST_LOG)
        for index in self.nodes:
            self.spawn(f"v{index}", self.node_args(index), self.data(index) / "node.log", env=env)
        self.write_pids()
        self.wait_api()

    def write_pids(self):
        pids = {str(process.pid): name for name, process in self.processes.items() if name.startswith("v")}
        (bench.DATA / "pids.json").write_text(json.dumps(pids))

    def restart(self, index):
        process = self.processes.pop(f"v{index}")
        process.kill()
        process.wait()
        log(f"v{index} stopped at height {self.head(1)}")
        env = dict(os.environ, RUST_LOG=bench.RUST_LOG)
        self.spawn(f"v{index}", self.node_args(index), self.data(index) / "node.log", env=env)
        self.write_pids()

    def start_follower(self):
        folder = follower_folder()
        folder.mkdir(parents=True)
        config = json.loads((self.data(1) / "node.json").read_text())
        config["rpc_bind"] = f"127.0.0.1:{port('rpc', FOLLOWER)}"
        config["mining_bind"] = f"127.0.0.1:{port('mining', FOLLOWER)}"
        config["public_p2p"]["listen_addrs"] = [address(port("p2p", FOLLOWER), self.transport)]
        config["public_p2p"]["bootstrap_peers"] = [address(port("p2p", index), self.transport)
                                                  for index in self.nodes]
        config["validator_overlay"] = None
        config["validator"] = None
        config["storage"] = {**config["storage"], "mode": "pruned", "prune_days": 1}
        (folder / "node.json").write_text(json.dumps(config, indent=2) + "\n")
        env = dict(os.environ, RUST_LOG=bench.RUST_LOG)
        self.spawn(f"v{FOLLOWER}", [self.bin / "tenraid.exe", "--no-color", "-d", "run", "--network",
                                    "development", "--manifest", self.manifest, "--data-dir", folder],
                   folder / "node.log", env=env)
        return folder


class Heights(threading.Thread):
    """Wall time of every head height v1 reports, for cuts per minute on both sides of the upgrade."""

    def __init__(self, node):
        super().__init__(daemon=True)
        self.node, self.seen, self.running = node, {}, True

    def run(self):
        while self.running:
            height = get(self.node, "/v1/chain/head")
            if height is not None:
                self.seen.setdefault(height["height"], time.time())
            time.sleep(0.25)


def rate_between(seen, first, last):
    heights = [height for height in seen if first <= height <= last]
    if len(heights) < 2:
        return None
    span = seen[max(heights)] - seen[min(heights)]
    return round((max(heights) - min(heights)) / span * 60, 1) if span > 0 else None


def compare(nodes, top):
    """Cut ID, state root and protocol version of every height on every node."""
    mismatches, missing, versions = [], [], {}
    for height in range(1, top + 1):
        views = {}
        for index in nodes:
            view = get(index, f"/v1/cuts/{height}")
            if view is None:
                missing.append((index, height))
                continue
            views[index] = (view["cut"]["cut_id"], view["cut"]["state_root"], view["protocol_version"])
        if len(set(views.values())) > 1:
            mismatches.append({"height": height, "views": views})
        if views:
            versions[height] = next(iter(views.values()))[2]
    return mismatches, missing, versions


def log_findings(folder):
    lines = (folder / "node.log").read_text(errors="replace").splitlines()
    return [line for line in lines if " ERROR " in line or " WARN " in line]


def main():
    if os.name != "nt":
        raise SystemExit("upgrade.py drives Windows processes, as bench.py does")
    run = UpgradeBench("upgrade")
    log("upgrade at height", UPGRADE)
    run.check_preconditions()
    if "--fresh" in sys.argv or not (run.snapshot() / "ready").exists():
        run.create_snapshot()
    run.restore()
    heights = Heights(1)
    result = {"upgrade": UPGRADE}
    try:
        run.start()
        base = max(run.head(index) for index in run.nodes)
        result["start_height"] = base
        if base + 30 > UPGRADE:
            raise SystemExit(f"the snapshot ends at {base}, too close to the upgrade at {UPGRADE}")
        heights.start()
        status = get(1, "/v1/node/status")
        result["status_before"] = status
        log("status before", json.dumps(status))

        # Spread the payments so that they last until well after the upgrade.
        payments = load(run.snapshot() / "tx" / "pay.txt")
        seconds = (UPGRADE - base) * 2.5 + AFTER * 1.5
        rate = min(MAX_RATE, max(1, int(len(payments) / seconds)))
        payments = payments[:int(rate * seconds)]
        result["payment_rate"] = rate
        log(f"{len(payments)} payments at {rate} a second")
        records = []
        sender = threading.Thread(target=lambda: records.extend(
            submit(run.ports(), payments, rate, "payments", log)), daemon=True)
        sender.start()

        run.wait_height(UPGRADE - 3, 3600)
        run.restart(4)
        run.wait_height(UPGRADE + 4, 900)
        run.restart(3)
        run.wait_height(UPGRADE + AFTER, 1200)
        sender.join()
        result["status_after"] = {f"v{index}": get(index, "/v1/node/status") for index in run.nodes}
        result["daa_after"] = get(1, "/v1/chain/daa")

        accepted = [record["txid"] for record in records if record["status"] == 200]
        final, pending = wait_final(port("rpc", 1), accepted, 120, "payments", log)
        # A validator stopped hard loses its pool, so the signed bytes go out again, as a wallet does.
        lost = set(pending)
        resent = [payment for payment in payments if payment[0] in lost]
        if resent:
            log(f"payments: {len(resent)} lost with a stopped validator, sending them again")
            submit({"v1": port("rpc", 1)}, resent, rate, "payments again", log)
            again, pending = wait_final(port("rpc", 1), [txid for txid, _ in resent], 120, "payments again", log)
            final.update(again)
        by_height = {}
        for status in final.values():
            if lifecycle(status) == "finalized":
                cut = status["finalized_cut"]["height"]
                by_height[cut] = by_height.get(cut, 0) + 1
        result["payments"] = {
            "sent": len(records), "accepted": len(accepted), "finalized": sum(by_height.values()),
            "failed": sum(1 for status in final.values() if lifecycle(status) == "failed"),
            "pending": len(pending), "resent": len(resent),
            "finalized_before_upgrade": sum(n for h, n in by_height.items() if h < UPGRADE),
            "finalized_from_upgrade": sum(n for h, n in by_height.items() if h >= UPGRADE),
        }

        # The miners stop, so empty cuts keep the disks small while the fifth node catches up.
        for name in [name for name in run.processes if name.startswith("miner")]:
            run.processes.pop(name).kill()
        follower = run.start_follower()
        top = min(run.head(index) for index in run.nodes)
        deadline = time.time() + 3600
        while time.time() < deadline and (get(FOLLOWER, "/v1/chain/head") or {}).get("height", 0) < top:
            time.sleep(2)
        result["follower_height"] = (get(FOLLOWER, "/v1/chain/head") or {}).get("height")

        nodes = run.nodes + [FOLLOWER]
        mismatches, missing, versions = compare(nodes, top)
        result["compared_heights"] = top
        result["mismatches"] = mismatches
        result["missing"] = missing[:50]
        result["wrong_version"] = [height for height, version in versions.items()
                                   if version != (2 if height >= UPGRADE else 1)]
        seen = heights.seen
        result["cuts_per_minute"] = {"before": rate_between(seen, base + 10, UPGRADE - 1),
                                     "after": rate_between(seen, UPGRADE, top)}
        result["finalized_per_cut"] = {
            "before": round(sum(n for h, n in by_height.items() if base + 10 <= h < UPGRADE)
                            / max(1, UPGRADE - base - 10), 1),
            "after": round(sum(n for h, n in by_height.items() if UPGRADE <= h <= top)
                           / max(1, top - UPGRADE + 1), 1),
        }
        result["log_findings"] = {f"v{index}": log_findings(run.data(index)) for index in run.nodes}
        result["log_findings"][f"v{FOLLOWER}"] = log_findings(follower)
    finally:
        heights.running = False
        if "--keep" not in sys.argv:
            run.stop()
        folder = bench.RESULTS / f"upgrade-{time.strftime('%Y%m%d-%H%M%S')}"
        folder.mkdir(parents=True)
        for index in run.nodes + [FOLLOWER]:
            source = (run.data(index) if index != FOLLOWER else follower_folder()) / "node.log"
            if source.exists():
                bench.shutil.copy(source, folder / f"v{index}.log")
        (folder / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
        log("results in", folder)
    announced = (result.get("status_before") or {}).get("next_upgrade") or {}
    after = result.get("status_after") or {}
    result["status_checks"] = {
        "next_upgrade_announced": announced.get("height") == UPGRADE,
        "all_on_version_2": all((status or {}).get("protocol_version") == 2 for status in after.values()),
        "no_update_flags": not any((status or {}).get("update_required") or (status or {}).get("update_announced")
                                   for status in after.values()),
    }
    (folder / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    passed = (not result["mismatches"] and not result["missing"] and not result["wrong_version"]
              and all(result["status_checks"].values()))
    log("PASSED" if passed else "FAILED", json.dumps({key: result[key] for key in (
        "compared_heights", "cuts_per_minute", "finalized_per_cut", "payments", "follower_height",
        "status_checks")}))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
