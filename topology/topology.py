"""A public network of plain nodes on one machine, each on a loopback address of its own (127.N.0.1),
so the per-address limits and network groups act as on separate hosts. Every node starts with two or
three seeds and finds the rest through peer exchange; the run then reads which peers each node holds
and in which direction. Afterwards a quarter of the nodes stop, and the others have to fill their
outbound slots again from the peers they learned.

    py -3.14 topology.py [NODES] [SETTLE_SECONDS]

TOPOLOGY_ROOT picks the data folder (default run/ here), TENRAI_REPO the node build.
"""

import ctypes
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = Path(os.environ.get("TENRAI_REPO", TOOLS.parents[1] / "tenrai-core"))
TENRAID = REPO / "target" / "release" / "tenraid.exe"
ROOT = Path(os.environ.get("TOPOLOGY_ROOT", TOOLS / "run"))
RESULTS = TOOLS / "results"
NO_WINDOW = 0x08000000
# The outbound connections a standard node keeps (PUBLIC_OUTBOUND_CONNECTIONS).
OUTBOUND = 8
# No further nodes below this much free commit: the machine runs other work too.
COMMIT_FLOOR_GIB = 1.5


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def commit_free_gib():
    class Memory(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)]
        _fields_ += [(name, ctypes.c_ulonglong) for name in (
            "total", "free", "commit_total", "commit_free", "virtual_total", "virtual_free", "extended")]
    memory = Memory()
    memory.length = ctypes.sizeof(Memory)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
    return memory.commit_free / 2**30


def rpc_port(index):
    return 29300 + index


def p2p_address(index):
    return f"/ip4/127.{10 + index}.0.1/udp/{29400 + index}/quic-v1"


def peers(index):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{rpc_port(index)}/v1/node/peers", timeout=5) as reply:
            return json.loads(reply.read())
    except OSError:
        return None


def node_index(address):
    """The node a peer address belongs to, from its 127.N.0.1 host."""
    if not address or "/ip4/127." not in address:
        return None
    second = int(address.split("/ip4/127.")[1].split(".")[0])
    return second - 10


class Network:
    def __init__(self, count):
        self.count = count
        self.processes = {}
        random.seed(20261104)

    def prepare(self):
        if ROOT.exists():
            shutil.rmtree(ROOT)
        template = json.loads((REPO / "config" / "node.example.json").read_text())
        for index in range(self.count):
            folder = ROOT / f"n{index:02}"
            folder.mkdir(parents=True)
            earlier = list(range(index)) or [1, 2]
            seeds = random.sample(earlier, min(len(earlier), random.choice([2, 3])))
            config = dict(template)
            config["api_landing_page"] = False
            config["rpc_bind"] = f"127.0.0.1:{rpc_port(index)}"
            config["mining_bind"] = f"127.0.0.1:{29600 + index}"
            config["public_p2p"] = {
                **template["public_p2p"],
                "listen_addrs": [p2p_address(index)],
                "bootstrap_peers": [p2p_address(seed) for seed in seeds],
                "enable_upnp": False,
                "allow_private_discovery": True,
            }
            config["tenq"] = {"light": True}
            (folder / "node.json").write_text(json.dumps(config, indent=2) + "\n")

    def start(self, index):
        folder = ROOT / f"n{index:02}"
        handle = open(folder / "node.log", "ab")
        self.processes[index] = subprocess.Popen(
            [str(TENRAID), "--no-color", "run", "--network", "development", "--data-dir", str(folder)],
            stdout=handle, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)

    def stop(self, index):
        process = self.processes.pop(index, None)
        if process and process.poll() is None:
            process.kill()
            process.wait()

    def stop_all(self):
        for index in list(self.processes):
            self.stop(index)

    def survey(self, running):
        """Per running node: its peers by direction, and the graph they span."""
        rows, edges = {}, set()
        for index in running:
            answer = peers(index)
            if answer is None:
                rows[index] = None
                continue
            listed = answer.get("peers", [])
            outbound = sum(1 for peer in listed if peer.get("outbound") is True)
            inbound = sum(1 for peer in listed if peer.get("outbound") is False)
            rows[index] = {"peers": len(listed), "outbound": outbound, "inbound": inbound}
            for peer in listed:
                other = node_index(peer.get("address"))
                if other is not None and other != index:
                    edges.add(tuple(sorted((index, other))))
        reached = self.reachable(running, edges)
        answered = [row for row in rows.values() if row]
        summary = {
            "nodes": len(running),
            "answered": len(answered),
            "connected_component": len(reached),
            "edges": len(edges),
        }
        for key in ("peers", "outbound", "inbound"):
            values = sorted(row[key] for row in answered)
            if values:
                summary[key] = {"min": values[0], "median": statistics.median(values), "max": values[-1]}
        summary["without_outbound"] = sorted(index for index, row in rows.items() if row and row["outbound"] == 0)
        summary["below_outbound_target"] = sum(1 for row in answered if row["outbound"] < OUTBOUND)
        return summary, rows

    @staticmethod
    def reachable(running, edges):
        if not running:
            return set()
        neighbours = {index: set() for index in running}
        for first, second in edges:
            if first in neighbours and second in neighbours:
                neighbours[first].add(second)
                neighbours[second].add(first)
        start = min(running)
        seen, queue = {start}, deque([start])
        while queue:
            for other in neighbours[queue.popleft()]:
                if other not in seen:
                    seen.add(other)
                    queue.append(other)
        return seen


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    settle = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    network = Network(count)
    network.prepare()
    before = commit_free_gib()
    result = {"asked_nodes": count, "settle_s": settle}
    try:
        for index in range(count):
            if commit_free_gib() < COMMIT_FLOOR_GIB:
                count = index
                log(f"stopped adding nodes at {count}: little free commit left")
                break
            network.start(index)
            time.sleep(0.5)
        result["nodes"] = count
        log(f"{count} nodes started")
        time.sleep(settle)
        result["commit_per_node_gib"] = round((before - commit_free_gib()) / count, 3)
        log("commit per node", result["commit_per_node_gib"], "GiB")
        running = list(range(count))
        result["settled"], result["settled_rows"] = network.survey(running)
        log("settled:", json.dumps(result["settled"]))
        stopped = sorted(random.sample(running, count // 4))
        for index in stopped:
            network.stop(index)
        log("stopped", stopped)
        running = [index for index in running if index not in stopped]
        time.sleep(settle)
        result["stopped"] = stopped
        result["after_stop"], result["after_stop_rows"] = network.survey(running)
        log("after the stop:", json.dumps(result["after_stop"]))
    finally:
        network.stop_all()
    RESULTS.mkdir(exist_ok=True)
    target = RESULTS / f"topology-{count}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    target.write_text(json.dumps(result, indent=2, default=str) + "\n")
    log("results in", target)


if __name__ == "__main__":
    main()
