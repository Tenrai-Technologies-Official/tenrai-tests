"""Run a local validator benchmark.

    py -3.14 bench.py SCENARIO [--fresh] [--keep] [--check]

Runs start from a common funded snapshot. --fresh rebuilds it, and --check
runs preflight alone. Set BENCH_DATA, BENCH_DATA2 and BENCH_SNAPSHOTS to choose
the primary data, secondary data and snapshot directories.
"""

import gzip
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import preflight
from bench_agree import (read_or_status, Agreement, cut_view, final_answer, final_checks, final_height,
                         final_sample, head, same_height, wait_for_rest)
from bench_analyze import analyze, verdict
from bench_crash import crash_test, print_crash
from bench_determinism import bench_plan, gate_start, run_gate
from bench_private import PrivateLoad
from bench_proc import CpuCap, Sampler, pin, raise_priority
from bench_rate import BlockRateHold
from bench_submit import (BUSY_RETRIES, Payments, count, is_busy, lifecycle, load, resend_open, retry_after, submit,
                          wait_final)

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS.parent))
from release.node_checkout import node_checkout

REPO = node_checkout(TOOLS.parents[1])
# Variants that change the chain's timing and with it the genesis; any other build shares main's snapshot.
TIMINGS = ("cut3000", "t100", "tx12", "day512")
DATA = Path(os.environ.get("BENCH_DATA", TOOLS / "run"))
DATA2 = Path(os.environ.get("BENCH_DATA2", TOOLS / "run-secondary"))
SNAPSHOTS = Path(os.environ.get("BENCH_SNAPSHOTS", TOOLS / "run-snapshots"))
RESULTS = TOOLS / "results"
BENCH_TX = TOOLS / "bench-tx" / "target" / "release" / "bench-tx.exe"
WAN_PROXY = TOOLS / "wan-proxy" / "target" / "release" / "wan-proxy.exe"
BENCH_PRIVATE = TOOLS / "bench-private" / "target" / "release" / "bench-private.exe"
PROVER_CRATE = REPO / "crates/tsp2/prover"
if not (PROVER_CRATE / "Cargo.toml").is_file():
    PROVER_CRATE = REPO / "crates/tsp2-prover"
PROVER = Path(os.environ.get("TENRAI_TSP2_PROVER",
                             PROVER_CRATE / "target/release/tenrai-tsp2-prove.exe"))
SPENDERS = 1000
# The genesis takes 1,024 allocations, so a chain with private transfers funds fewer spenders.
PRIVATE_SPENDERS = 880
PRIVATE_DEPOSITORS = 120
# A node takes a registration only with its fee paid: the sender's and the receiver's each come from a depositor
# coin of its own after those of the ENTERs.
REGISTRATION_FUNDERS = 2
# 70 s at the limit of four statements per 2.5 s cut, in turn ENTER, TRANSFER and EXIT. Each ENTER
# spends a depositor's coin; the transfers and EXITs spend notes that ENTERs before the snapshot made,
# two notes each: two per transfer, one per EXIT.
PRIVATE_FRAMES = 112
PRIVATE_ACTIVATION = 3
BOND = 100_000_000_000_000
SPENDER_FUNDS = 1_000_000_000_000
SUPPLY = 2_000_000_000_000_000
CHAIN_ID = "b5" * 48
NO_WINDOW = 0x08000000
# BENCH_CPUS, a mask such as 0x3ff, keeps every benchmark process but the miners on those logical CPUs. A
# scenario may name its own (`cpus`); without either, they keep off every CPU its miners use.
CPUS = int(os.environ.get("BENCH_CPUS", "0"), 0)
ALL_CPUS = (1 << os.cpu_count()) - 1
# BENCH_RUST_LOG replaces the validators' log filter, e.g. to add tenrai_node::p2p=debug.
# The private relay and the checks of fetched items log each fetch at debug.
RUST_LOG = os.environ.get("BENCH_RUST_LOG", "warn,tenraid=info,tenrai_node=info,tenrai_node::chain=debug,"
                          "tenrai_traidag_storage=debug,tenrai_node::p2p::swarm::private_relay=debug,"
                          "tenrai_node::p2p::swarm::work=debug")

# RTTs between the cities (WonderNetwork, 2026), Tokyo - Sao Paulo at 300 ms for a bad route.
WORLD = {"v1-v2": 86, "v2-v4": 110, "v2-v3": 173, "v1-v4": 198, "v1-v3": 233, "v3-v4": 300}
JITTER = {"v1-v2": 10, "v2-v4": 12, "v2-v3": 20, "v1-v4": 20, "v1-v3": 25, "v3-v4": 35}
LOSS = {"v1-v2": 0.001, "v2-v4": 0.002, "v2-v3": 0.002, "v1-v4": 0.003, "v1-v3": 0.003, "v3-v4": 0.005}
# Megabits a second per pair: hosted validators on good routes, the far one on slower lines.
SPEED = {"v1-v2": 100, "v2-v4": 100, "v1-v4": 50, "v2-v3": 20, "v1-v3": 20, "v3-v4": 10}
# The same pairs on the lines hosted validators usually get; the far validator still has the slowest.
HOSTED = {"v1-v2": 200, "v2-v4": 200, "v1-v4": 100, "v2-v3": 50, "v1-v3": 50, "v3-v4": 25}
SPIKES = {"every_s": 30, "extra_ms": [80, 200], "length_ms": [200, 600]}

# Eight miners, two per region, share the CPUs of `miner_cpus` like many small miners; a CPU cap would
# suspend a whole process, so it would also answer its node late.
BASE = {"bin": "main", "transport": "quic", "validators": 4, "rate": 500, "seconds": 40, "warm": 10,
        "bps": 50, "target": 2.5, "calibration": "mainnet", "miners": {1: 2, 2: 2, 3: 2, 4: 2},
        "miner_cpus": 0xC00, "hashes": 1600, "disks": "nvme", "rtt": WORLD, "loss": LOSS}
MAIN3 = {**BASE, "bin": "cut3000", "target": 3.0}
T100 = {**BASE, "bin": "t100", "rate": 1000, "bps": 100, "target": 1.5, "calibration": "testnet100",
        "miner_cpus": 0xF00, "hashes": 3200}
SOLO = {"validators": 1, "miners": {1: 8}}
# 300 ms on every pair with the world jitter and spikes, 0.5 % loss, BBR, a settled difficulty.
WAN300 = {**BASE, "hashes": 1470, "miners": {1: 4, 2: 4, 3: 4, 4: 4}, "rate": 500, "measure_cpus": 0xC00,
          "quic": "bbr", "rtt": {pair: 300 for pair in WORLD}, "loss": {pair: 0.005 for pair in WORLD}}

# The four cities with each pair's RTT, jitter, spikes, loss and line speed, BBR, a settled difficulty.
CITIES = {**BASE, "hashes": 1470, "miners": {1: 4, 2: 4, 3: 4, 4: 4}, "measure_cpus": 0xC00, "quic": "bbr",
          "rate_mbps": SPEED, "queue_ms": 100}
HOSTED_CITIES = {**CITIES, "rate_mbps": HOSTED}
# Where a node stands that starts from a snapshot over the WAN links: near Sao Paulo, on a usual line.
JOINER_RTT = {"v1-v5": 198, "v2-v5": 110, "v3-v5": 300, "v4-v5": 30}
JOINER_JITTER = {"v1-v5": 20, "v2-v5": 12, "v3-v5": 35, "v4-v5": 5}
JOINER_LOSS = {"v1-v5": 0.003, "v2-v5": 0.002, "v3-v5": 0.005, "v4-v5": 0.001}
JOINER_RATE = {"v1-v5": 100, "v2-v5": 100, "v3-v5": 25, "v4-v5": 200}
# The private statements join the settled transparent load; the window is the last 60 s of both.
SETTLED = {"seconds": 130, "warm": 70, "private": 1.6, "private_after": 60}
STAKE_SHARES = {1: 40, 2: 30, 3: 20, 4: 10}
# Stake moves early in an epoch and a fault falls on its end: the fourth validator gets a delegation that
# lifts it over the second, the third leaves, and the committee of the next epoch holds 1.6, 1.4 and 1.2e14
# of 4.2e14, quorum 2.8e14 + 1. A run starts some 90 cuts before the end, private statements near the fault.
DELEGATED_TRAI = 1_000_000
DELEGATION = DELEGATED_TRAI * 100_000_000
STAKER_FUNDS = DELEGATION + 10_000_000_000
EPOCH_MOVES = {**HOSTED_CITIES, **SETTLED, "bin": "day512", "rate": 475, "seconds": 300, "fault_run": True,
               "private_after": 190, "stakes": STAKE_SHARES, "stake_changes": {"after": 20},
               "stop_before_epoch_end": 120, "epoch_cuts": 512, "small_disk_guard": True}

SCENARIOS = {
    "main": BASE,
    "main3": MAIN3,
    "t100": T100,
    "solo": {**BASE, **SOLO},
    "solo3": {**MAIN3, **SOLO},
    "solo-t100": {**T100, **SOLO},
    "crash": {**BASE, "wan": False, "seconds": 50, "crash_after": 15},
    "main-lan": {**BASE, "wan": False},
    "lan-70": {**BASE, "wan": False, "seconds": 70},
    "lan-70-50": {**BASE, "wan": False, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4}},
    "lan-70-50-250": {**BASE, "wan": False, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                      "rate": 250},
    # One, two or three miner CPUs give about 25, 50 or 75 blocks a second until the DAA pulls the rate back.
    # The validators keep CPUs 0 to 8 in all of them.
    "lan-70-bps25": {**BASE, "wan": False, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                     "rate": 500, "measure_cpus": 0x800, "cpus": 0x1FF},
    "lan-70-bps50": {**BASE, "wan": False, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                     "rate": 500, "measure_cpus": 0xC00, "cpus": 0x1FF},
    "lan-70-bps75": {**BASE, "wan": False, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                     "rate": 750, "measure_cpus": 0xE00, "cpus": 0x1FF},
    "lan-70-bps25-60": {**BASE, "wan": False, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                        "rate": 60, "measure_cpus": 0x800, "cpus": 0x1FF},
    "lan-70-bps50-150": {**BASE, "wan": False, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                         "rate": 150, "measure_cpus": 0xC00, "cpus": 0x1FF},
    # Miners on one physical core (CPUs 10 and 11), the validators on the others.
    # The genesis difficulty assumes 500 H/s for about 75 blocks a second, 1,000 H/s for about 50.
    "lan-70-50n": {**BASE, "wan": False, "seconds": 70, "hashes": 1000, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                   "rate": 500},
    "lan-70-50n-550": {**BASE, "wan": False, "seconds": 70, "hashes": 1000, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                       "rate": 550},
    # Two minutes of 500 tx/s, the 64,000 signed payments of the snapshot nearly all used.
    "lan-120-50n": {**BASE, "wan": False, "seconds": 120, "hashes": 1000, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                    "rate": 500},
    "lan-120-50n-400": {**BASE, "wan": False, "seconds": 120, "hashes": 1000, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                        "rate": 400},
    "lan-120-50n-300": {**BASE, "wan": False, "seconds": 120, "hashes": 1000, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                        "rate": 300},
    # Two validators that must both sign, each with twice the CPU of the four-validator runs.
    "lan-120-50n-2v": {**BASE, "wan": False, "seconds": 120, "hashes": 1000, "validators": 2,
                       "miners": {1: 8, 2: 8}, "rate": 500},
    "lan-70-75b": {**BASE, "wan": False, "seconds": 70, "hashes": 500, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                   "rate": 750},
    "lan-70-75b-500": {**BASE, "wan": False, "seconds": 70, "hashes": 500, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                       "rate": 500},
    # Testnet100 timing: four miner CPUs give about 100 blocks/s, the validators keep CPUs 0 to 7; a t100 build.
    "lan-70-t100": {**T100, "wan": False, "seconds": 70, "hashes": 1600, "miners": {1: 4, 2: 4, 3: 4, 4: 4}},
    "lan-70-t100-500": {**T100, "wan": False, "seconds": 70, "hashes": 1600, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                        "rate": 500},
    "lan-40-t100-1500": {**T100, "wan": False, "seconds": 40, "hashes": 1600, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                         "rate": 1500},
    "lan-split": {**BASE, "wan": False, "disks": "split"},
    "main-bbr": {**BASE, "quic": "bbr"},
    "main3-bbr": {**MAIN3, "quic": "bbr"},
    "t100-bbr": {**T100, "quic": "bbr"},
    # TCP scenarios check that validators refuse TCP listeners.
    "main-tcp": {**BASE, "transport": "tcp"},
    "main-tcp-bbr": {**BASE, "transport": "tcp", "tcp_model": "bbr"},
    "main-split": {**BASE, "disks": "split"},
    "main-lowloss": {**BASE, "loss": {pair: value / 10 for pair, value in LOSS.items()}},
    "main-300": {**BASE, "rtt": {pair: 300 for pair in WORLD}},
    # lan-70-bps50 over the world links, and with 300 ms on every pair; both share its snapshot.
    "wan-70-bps50": {**BASE, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                     "rate": 500, "measure_cpus": 0xC00},
    "wan300-70-bps50": {**BASE, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                        "rate": 500, "measure_cpus": 0xC00, "rtt": {pair: 300 for pair in WORLD}},
    "wan300-70-bps50-bbr": {**BASE, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                            "rate": 500, "measure_cpus": 0xC00, "rtt": {pair: 300 for pair in WORLD},
                            "quic": "bbr"},
    # The same with v3 down from 25 s to 40 s of the load, so its turns as proposer fail and it
    # has to catch up after its restart.
    "wan300-70-bps50-outage": {**BASE, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                               "rate": 500, "measure_cpus": 0xC00, "rtt": {pair: 300 for pair in WORLD},
                               "outage": {"validator": 3, "after": 25, "down": 15}},
    # Both with BBR instead of Cubic on every QUIC connection; the snapshot is the same.
    "wan-70-bps50-bbr": {**BASE, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                         "rate": 500, "measure_cpus": 0xC00, "quic": "bbr"},
    # Start at the difficulty used for the settled-rate scenarios.
    "wan-settled": {**BASE, "seconds": 70, "hashes": 1470, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                    "rate": 500, "measure_cpus": 0xC00},
    "wan-settled-bbr": {**BASE, "seconds": 70, "hashes": 1470, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                        "rate": 500, "measure_cpus": 0xC00, "quic": "bbr"},
    "lan-settled": {**BASE, "wan": False, "seconds": 70, "hashes": 1470, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                    "rate": 500, "measure_cpus": 0xC00},
    # Ten minutes at 300 ms on every link with jitter, spikes and 0.5 % loss; three validators
    # crash one after another and come back, each while the other three hold the quorum.
    "wan300-long": {**BASE, "seconds": 600, "hashes": 1470, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                    "rate": 500, "measure_cpus": 0xC00, "quic": "bbr", "rtt": {pair: 300 for pair in WORLD},
                    "loss": {pair: 0.005 for pair in WORLD},
                    "outages": [{"validator": 3, "after": 150, "down": 30},
                                {"validator": 1, "after": 330, "down": 30},
                                {"validator": 4, "after": 480, "down": 20}]},
    # Three minutes of those links without crashes, each payment on a coin of its own that was final
    # before the load began ("funding": 3): fed to v2 alone, then the same load to every validator
    # in turn. The ten minutes with crashes on own coins, and chained payments as a wallet makes
    # them, each spending the change of the one before only once that one is final.
    # Private statements at their limit beside the transparent target on one chain, without them for
    # comparison; the frames are proven once with the snapshot.
    # The readiness test: the transparent target without and with private statements at their limit
    # (ENTER, TRANSFER and EXIT in turn), the same at 300 ms and 0.5 % loss on every pair, with v3 down
    # for 20 s, and over TCP.
    "cities-private-off": {**CITIES, "seconds": 70, "private": 0},
    "cities-private": {**CITIES, "seconds": 70, "private": 1.6},
    # The same once the transparent load has settled: the private statements join after 60 s and the
    # window is the last 60 s of both, without the step from nothing to the full load at once.
    "cities-settled-private": {**CITIES, **SETTLED},
    "hosted-settled-private": {**HOSTED_CITIES, **SETTLED},
    # The rest of the readiness runs on the links hosted validators get: the step from nothing to the
    # full load, 300 ms and 0.5 % loss on every pair, v3 down for 20 s under the full load, a new
    # pruned node joining under it, and over TCP. The same on the narrow links.
    "hosted-private": {**HOSTED_CITIES, "seconds": 70, "private": 1.6},
    # What the block room carries: 50 blocks a second with ten payments each are exactly 500 a second.
    "hosted-settled-450": {**HOSTED_CITIES, **SETTLED, "rate": 450},
    "hosted-settled-475": {**HOSTED_CITIES, **SETTLED, "rate": 475},
    # The same links without private statements and with a build of twelve payments in a block
    # (build-variant.py tx12): the option of more room, on a chain of its own.
    "hosted-tx12": {**HOSTED_CITIES, "bin": "tx12", "seconds": 130, "warm": 70},
    "hosted-tx12-private": {**HOSTED_CITIES, **SETTLED, "bin": "tx12"},
    # All payments through one node, as wallets behind one gateway: for the others everything is relayed.
    "hosted-one-door": {**HOSTED_CITIES, **SETTLED, "submit": [2]},
    "hosted-tx12-one-door": {**HOSTED_CITIES, **SETTLED, "bin": "tx12", "submit": [2]},
    "hosted300-private": {**HOSTED_CITIES, **SETTLED, "rtt": {pair: 300 for pair in WORLD},
                          "loss": {pair: 0.005 for pair in WORLD}},
    "hosted-private-outage": {**HOSTED_CITIES, **SETTLED, "seconds": 190,
                              "outages": [{"validator": 3, "after": 80, "down": 20}]},
    # v3 down for a minute under the full load, and once more for 10 s while it catches up.
    "hosted-long-outage": {**HOSTED_CITIES, **SETTLED, "rate": 475, "seconds": 300,
                           "outages": [{"validator": 3, "after": 80, "down": 60},
                                       {"validator": 3, "after": 180, "down": 10}]},
    # v3 cut off from every link for 30 s under the full load: alone it cannot finalize, and once the
    # links are back it has to catch up to the same state as the others.
    "hosted-private-netcut": {**HOSTED_CITIES, **SETTLED, "rate": 475, "seconds": 190,
                              "netcut": {"validator": 3, "after": 80, "down": 30}},
    # The network splits two against two under the full load: neither half holds a quorum, so
    # finality has to stop on both sides and go on as one chain when the links are back.
    "hosted-partition": {**HOSTED_CITIES, **SETTLED, "rate": 475, "seconds": 220, "fault_run": True,
                         "partition": {"groups": [[1, 2], [3, 4]], "after": 90, "down": 40}},
    # All four validators end at once under the full load, as a power cut would, and start again:
    # nothing final may go back, and they go on as one chain.
    "hosted-blackout": {**HOSTED_CITIES, **SETTLED, "rate": 475, "seconds": 220, "fault_run": True,
                        "blackout": {"after": 90, "down": 30}},
    "hosted300-partition": {**HOSTED_CITIES, **SETTLED, "rate": 475, "seconds": 220, "fault_run": True,
                            "rtt": {pair: 300 for pair in WORLD}, "loss": {pair: 0.005 for pair in WORLD},
                            "partition": {"groups": [[1, 3], [2, 4]], "after": 90, "down": 40}},
    "hosted300-blackout": {**HOSTED_CITIES, **SETTLED, "rate": 475, "seconds": 220, "fault_run": True,
                           "rtt": {pair: 300 for pair in WORLD}, "loss": {pair: 0.005 for pair in WORLD},
                           "blackout": {"after": 90, "down": 30}},
    # Stakes of 40, 30, 20 and 10 %: votes weigh by stake, so the one with 40 % stops finality alone
    # and finality goes on without the 10 % and then without the 30 %, which leaves just a quorum.
    "hosted-stakes-largest-down": {**HOSTED_CITIES, **SETTLED, "rate": 475, "seconds": 220, "fault_run": True,
                                   "stakes": STAKE_SHARES,
                                   "stake_outages": [{"validator": 1, "after": 90, "down": 40, "stops": True}]},
    "hosted-stakes-smaller-down": {**HOSTED_CITIES, **SETTLED, "rate": 475, "seconds": 340, "fault_run": True,
                                   "stakes": STAKE_SHARES,
                                   "stake_outages": [{"validator": 4, "after": 60, "down": 30, "stops": False},
                                                     {"validator": 2, "after": 120, "down": 30, "stops": False,
                                                      "all_at_the_tip": 160}]},
    # Up to the end of the epoch v1+v4 and v2+v3 hold half of the stake each, so neither side finalizes; all
    # of them pass the end together once the links are back, or after a blackout.
    "hosted-epoch-partition": {**EPOCH_MOVES, "partition": {"groups": [[1, 4], [2, 3]], "before_end": 6, "down": 40}},
    "hosted-epoch-blackout": {**EPOCH_MOVES, "blackout": {"before_end": 6, "down": 30}},
    # v1+v2 hold 70 % and finalize to the end of the epoch. There 2.8e14 is one atom short of the new quorum,
    # so they stop, and v3 and v4 have to catch up through the change before anyone goes on.
    "hosted-epoch-quorum-side": {**EPOCH_MOVES, "seconds": 400,
                                 "partition": {"groups": [[1, 2], [3, 4]], "before_end": 8, "down": 150,
                                               "quorum_side": [1, 2]}},
    # All four validators crash inside one height while private statements run (bench_crash.py).
    "hosted-private-crash": {**HOSTED_CITIES, "rate": 475, "seconds": 90, "private": 1.6, "crash_after": 30},
    "hosted-private-joiner": {**HOSTED_CITIES, **SETTLED, "rate": 475, "seconds": 150,
                              "joiner": {"after": 65, "storage": "pruned"}},
    # Days of 512 cuts on a build of their own: the run passes an epoch end under the full load, every validator
    # writes its snapshot package meanwhile, and a new pruned node starts from one over the WAN links.
    "hosted-snapshot-joiner": {**HOSTED_CITIES, **SETTLED, "bin": "day512", "rate": 475, "seconds": 300,
                               "private_after": 150, "stop_before_epoch_end": 48, "epoch_cuts": 512,
                               "small_disk_guard": True,
                               "joiner": {"after": 20, "storage": "pruned", "snapshot": True}},
    "hosted-private-tcp": {**HOSTED_CITIES, **SETTLED, "transport": "tcp"},
    # At the rate the chain carries without a growing backlog: 300 ms and 0.5 % loss on every pair, and TCP.
    "hosted300-settled-475": {**HOSTED_CITIES, **SETTLED, "rate": 475, "rtt": {pair: 300 for pair in WORLD},
                              "loss": {pair: 0.005 for pair in WORLD}},
    "hosted-tcp-475": {**HOSTED_CITIES, **SETTLED, "rate": 475, "transport": "tcp"},
    # The same load over TCP where the path carries it: BBR at the ends, and a loss of 0.01 % under Cubic.
    "hosted-tcp-bbr-475": {**HOSTED_CITIES, **SETTLED, "rate": 475, "transport": "tcp", "tcp_model": "bbr"},
    "hosted-tcp-clean-475": {**HOSTED_CITIES, **SETTLED, "rate": 475, "transport": "tcp",
                             "loss": {pair: 0.0001 for pair in WORLD}},
    "cities-settled-outage": {**CITIES, **SETTLED, "seconds": 190,
                              "outages": [{"validator": 3, "after": 80, "down": 20}]},
    "cities-settled-joiner": {**CITIES, **SETTLED, "seconds": 150, "joiner": {"after": 65, "storage": "pruned"}},
    "cities300-settled": {**CITIES, **SETTLED, "rtt": {pair: 300 for pair in WORLD},
                          "loss": {pair: 0.005 for pair in WORLD}},
    # A longer run at the rate the chain carries for good: more than five minutes of the transparent load and, in
    # its last 70 s, the private statements at their limit on the frames proven with the snapshot, when state and
    # disks have aged. More private time would cost about 14 s of proving per statement and notes before the snapshot.
    "hosted-long-private": {**HOSTED_CITIES, "rate": 475, "seconds": 340, "warm": 70, "private": 1.6, "private_after": 265},
    # One validator alone on its disk at the full nominal load, no links: what a single node needs. Pinned to
    # fewer CPUs by BENCH_CPUS it shows the least that holds the rate.
    "node-sizing-500": {**BASE, "wan": False, "validators": 1, "miners": {1: 16}, "hashes": 1100, "rate": 500,
                        "seconds": 1200, "warm": 70, "measure_cpus": 0xC00, "small_disk_guard": True},
    "node-sizing-500-short": {**BASE, "wan": False, "validators": 1, "miners": {1: 16}, "hashes": 1100, "rate": 500,
                              "seconds": 240, "warm": 70, "measure_cpus": 0xC00, "small_disk_guard": True},
    # The same with miners that have room: each bench miner finds a block every few tenths of a second, so
    # waiting for a template costs it a large share of its blocks, which a real miner hashing seconds per block
    # does not lose. More hash power than the target lets the block rate hold pause them down to 50 a second.
    "hosted-sustained-475-room": {**HOSTED_CITIES, "rate": 475, "seconds": 440, "warm": 70, "private": 1.6,
                                  "private_after": 365, "small_disk_guard": True, "hashes": 1100},
    # The owner's bar for lasting load: 475 offered over a measured window of more than six minutes.
    "hosted-sustained-475": {**HOSTED_CITIES, "rate": 475, "seconds": 440, "warm": 70, "private": 1.6, "private_after": 365,
                             "small_disk_guard": True},
    "cities300-private": {**CITIES, "seconds": 70, "private": 1.6, "rtt": {pair: 300 for pair in WORLD},
                          "loss": {pair: 0.005 for pair in WORLD}},
    "cities-private-outage": {**CITIES, "seconds": 90, "private": 1.6,
                              "outages": [{"validator": 3, "after": 25, "down": 20}]},
    "cities-private-tcp": {**CITIES, "transport": "tcp", "seconds": 70, "private": 1.6},
    # A new pruned node without a seat joins after 20 s and syncs from genesis while the validators
    # carry the load; its own links are direct, the validators' stay on the city links.
    "cities-private-joiner": {**CITIES, "seconds": 150, "private": 1.6,
                              "joiner": {"after": 20, "storage": "pruned"}},
    # The Testnet100 timing (100 blocks a second, 1.5 s cuts, 1,000 payments a second) on the same links; the
    # private limit stays four statements a cut, 2.67 a second at this cut interval.
    "cities-t100-private": {**T100, "miners": {1: 4, 2: 4, 3: 4, 4: 4}, "quic": "bbr", "rate_mbps": SPEED,
                            "queue_ms": 100, "seconds": 70, "private": 4 / 1.5},
    "lan-private": {**WAN300, "wan": False, "seconds": 70, "private": 1.6},
    "lan-private-off": {**WAN300, "wan": False, "seconds": 70, "private": 0},
    "wan300-private": {**WAN300, "seconds": 70, "private": 1.6},
    "wan300-private-off": {**WAN300, "seconds": 70, "private": 0},
    "wan300-coins-v2": {**WAN300, "seconds": 180, "funding": 3, "submit": [2]},
    "wan300-coins": {**WAN300, "seconds": 180, "funding": 3},
    "wan300-long-coins": {**WAN300, "seconds": 600, "funding": 3,
                          "outages": [{"validator": 3, "after": 150, "down": 30},
                                      {"validator": 1, "after": 330, "down": 30},
                                      {"validator": 4, "after": 480, "down": 20}]},
    "wan300-chained-final": {**WAN300, "seconds": 300, "await_parent": True},
    # Where the load is lost: the own-coins load without the wide area links, on one validator
    # alone (no shared cores or disk, no votes), and with v3 crashing for half a minute so the
    # sync counters show whom a catch-up asks.
    "lan-coins": {**WAN300, "wan": False, "seconds": 180, "funding": 3},
    "solo-coins": {**WAN300, **SOLO, "wan": False, "seconds": 180, "funding": 3},
    "wan300-coins-outage": {**WAN300, "seconds": 240, "funding": 3,
                            "outages": [{"validator": 3, "after": 90, "down": 30}]},
    # Half the load over the same links: the cut rate the protocol keeps at 300 ms when the
    # machine the four validators share still has cores to spare.
    "wan300-coins-250": {**WAN300, "seconds": 180, "funding": 3, "rate": 250},
    # One validator starved under the full load as an overloaded host on a failing uplink would:
    # after 15 s it keeps 1.5 % of the machine's CPU (a fifth of a core) and its links gain 300 ms
    # each way and 5 % loss, for 30 s. Then everything goes back, and in the 120 s left it has to
    # catch up and take part in the finality again on its own: under the full load, or eased to
    # 100 payments a second from its release on.
    "wan300-degrade": {**WAN300, "seconds": 165, "funding": 3, "final_wait": 10,
                       "degrade": {"validator": 3, "after": 15, "for": 30, "cpu": 0.015,
                                   "delay_ms": 300, "loss": 0.05, "catch_up_s": 120}},
    "wan300-degrade-eased": {**WAN300, "seconds": 165, "funding": 3, "final_wait": 10,
                             "degrade": {"validator": 3, "after": 15, "for": 30, "cpu": 0.015,
                                         "delay_ms": 300, "loss": 0.05, "catch_up_s": 120,
                                         "rate_after": 100}},
    # v3 down from 25 s to 40 s of the load and back with a new pacemaker and no reports.
    "wan-settled-outage-bbr": {**BASE, "seconds": 70, "hashes": 1470, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                               "rate": 500, "measure_cpus": 0xC00, "quic": "bbr",
                               "outage": {"validator": 3, "after": 25, "down": 15}},
    "wan300-70-bps50-outage-bbr": {**BASE, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                                   "rate": 500, "measure_cpus": 0xC00, "rtt": {pair: 300 for pair in WORLD},
                                   "outage": {"validator": 3, "after": 25, "down": 15}, "quic": "bbr"},
    # The world links with a speed each: data-centre pairs fast, the far validator on slower lines,
    # router queues of 100 ms. Then the same links with half their rate taken by other traffic, a
    # storm where that traffic holds whole links in bursts, and 1 % loss on every pair. Each in
    # Cubic and BBR; all share the wan-70-bps50 snapshot.
    **{f"wan-{kind}{suffix}": {**BASE, "seconds": 70, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                               "rate": 500, "measure_cpus": 0xC00, **settings, **({"quic": "bbr"} if suffix else {})}
       for kind, settings in {
           "speed": {"rate_mbps": SPEED, "queue_ms": 100},
           "busy": {"rate_mbps": SPEED, "queue_ms": 200, "cross_share": 0.5},
           "storm": {"rate_mbps": SPEED, "queue_ms": 200, "cross_share": 0.5, "cross_bursts": True},
           "loss1": {"loss": {pair: 0.01 for pair in WORLD}},
       }.items() for suffix in ("", "-bbr")},
    # v3 loses its internet for two minutes under load, then reconnects and catches up.
    "lan-netcut": {**BASE, "wan": False, "seconds": 170, "hashes": 800, "miners": {1: 4, 2: 4, 3: 4, 4: 4},
                   "rate": 200, "measure_cpus": 0xC00, "netcut": {"validator": 3, "after": 20, "down": 120}},
}

def port(kind, index):
    return {"p2p": 29110, "rpc": 29120, "mining": 29130, "overlay": 29140,
            "proxy_p2p": 29150, "proxy_overlay": 29160}[kind] + index


def address(port_number, transport):
    if transport == "quic":
        return f"/ip4/127.0.0.1/udp/{port_number}/quic-v1"
    return f"/ip4/127.0.0.1/tcp/{port_number}"


def validator_id(index):
    return f"{0xb0 + index:02x}" * 48


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def run(*args):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"{args[0]} failed: {result.stderr.strip()[-2000:]}")
    return result.stdout


class Bench:
    def __init__(self, name):
        self.name = name
        self.scenario = dict(SCENARIOS[name])
        # BENCH_BIN runs a scenario with another build of the node, such as bin-prof.
        if os.environ.get("BENCH_BIN"):
            self.scenario["bin"] = os.environ["BENCH_BIN"]
        # BENCH_HOLD_BPS holds the miners at another block rate than the profile's, such as 51 so that the blocks
        # found are not what limits a load at the profile's full capacity; the result's settings record it.
        if os.environ.get("BENCH_HOLD_BPS"):
            self.scenario["hold_bps"] = float(os.environ["BENCH_HOLD_BPS"])
        self.bin_source = TOOLS / f"bin-{self.scenario['bin']}"
        self.measure_mask = self.scenario.get("measure_cpus") or self.scenario["miner_cpus"]
        # The validators keep off every CPU the miners use, while the snapshot is made and while a run measures.
        self.miner_mask = self.scenario["miner_cpus"] | self.measure_mask
        self.cpus = self.scenario.get("cpus") or CPUS or ALL_CPUS & ~self.miner_mask
        self.transport = self.scenario["transport"]
        self.nodes = list(range(1, self.scenario["validators"] + 1))
        self.bin = DATA / "bin"
        self.manifest = DATA / "bench.traidag.json"
        self.processes = {}
        self.outages = []
        # Validators that are down; the payments due meanwhile go to the others.
        self.down = set()

    def data(self, index):
        second = self.scenario["disks"] == "nvme" or index in (3, 4)
        return (DATA2 if second else DATA) / f"v{index}"

    def private_frames(self):
        return self.scenario.get("private_frames", PRIVATE_FRAMES)

    def private_setup(self):
        """A transfer spends two notes and an EXIT one, each third of the frames, and an ENTER before the
        snapshot makes two."""
        return self.private_frames() // 2

    def private_depositors(self):
        """One coin per ENTER, before the snapshot and among the frames."""
        return max(PRIVATE_DEPOSITORS, self.private_setup() + self.private_frames() // 3 + 1)

    def snapshot(self):
        """Everything the genesis depends on names the snapshot; BBR and the links do not."""
        scenario = self.scenario
        params = scenario["bin"].split("+")[0]
        params = params if params in TIMINGS else "main"
        funding = "" if self.funding() == 2 else f"-f{self.funding()}"
        private = "-private-funded" if self.private_chain() else ""
        if scenario.get("stakes"):
            private += "-stakes" + "-".join(str(share) for share in scenario["stakes"].values())
        if scenario.get("stake_changes"):
            private += "-moves"
        if self.private_frames() != PRIVATE_FRAMES:
            private += str(self.private_frames())
        return SNAPSHOTS / (f"{params}-{self.transport}-{len(self.nodes)}v-"
                            f"{scenario['bps']}bps-{scenario['hashes']}h-{scenario['miner_cpus']:x}{funding}{private}")

    def private_chain(self):
        return "private" in self.scenario

    def prover_tool(self):
        """A wallet refuses a chain of another parameter profile, so such a build brings its own tool."""
        own = TOOLS / f"bin-{self.scenario['bin']}" / "bench-private.exe"
        return own if own.exists() else BENCH_PRIVATE

    def spenders(self):
        return PRIVATE_SPENDERS + PRIVATE_DEPOSITORS - self.private_depositors() if self.private_chain() else SPENDERS

    def funding(self):
        """Funding rounds before the load: 2 leaves 64,000 coins, 3 splits them again into 512,000."""
        return self.scenario.get("funding", 2)

    def needed_payments(self):
        return self.scenario.get("rate", 0) * self.scenario.get("seconds", 0)

    def signing(self, held_coins):
        """What bench-tx signs after the funding: chained rounds over 64,000 coins, or one payment on
        each of the first coins of the third round."""
        if self.funding() == 3:
            return [1, 3, self.needed_payments()]
        return [max(1, -(-self.needed_payments() // held_coins))]

    def check_preconditions(self):
        """Refuses the run, exit code 3, if one of its preconditions fails (preflight.py)."""
        tool_paths = {"bench-tx.exe": BENCH_TX, "wan-proxy.exe": WAN_PROXY}
        if self.private_chain():
            tool_paths.update({"bench-private.exe": self.prover_tool(), "tenrai-tsp2-prove.exe": PROVER})
        earlier = [DATA] + [DATA2 / f"v{index}" for index in range(1, 6)]
        self.preflight, reasons = preflight.check(self, DATA, DATA2, SNAPSHOTS, tool_paths,
                                                  [DATA, TOOLS / "wan-proxy"], earlier)
        log("preflight:", preflight.summary(self.preflight))
        if reasons:
            refuse(reasons)

    def kill_leftovers(self):
        folders = [str(DATA).replace("/", "\\"), str(TOOLS / "wan-proxy").replace("/", "\\")]
        script = ("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and ("
                  + " -or ".join(f"$_.ExecutablePath.StartsWith('{folder}')" for folder in folders)
                  + ") } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        subprocess.run(["powershell", "-NoProfile", "-Command", script])

    def clean(self):
        self.kill_leftovers()
        time.sleep(1)
        for folder in [DATA] + [DATA2 / f"v{index}" for index in range(1, 6)]:
            if folder.exists():
                shutil.rmtree(folder)
        self.bin.mkdir(parents=True)
        DATA2.mkdir(parents=True, exist_ok=True)
        for name in preflight.BINARIES:
            # The contents only: a read-only copy would keep the next run from deleting this folder.
            shutil.copyfile(self.bin_source / name, self.bin / name)
            if preflight.sha256(self.bin / name) != self.preflight["binaries"][name]:
                raise SystemExit(f"the copy of {name} differs from the frozen {self.bin_source / name}")
        shutil.copy(WAN_PROXY, self.bin / "wan-proxy.exe")

    def create_keys(self):
        wallet = self.bin / "tenrai-wallet.exe"
        validators, bonds = [], []
        for index in self.nodes:
            folder = self.data(index)
            folder.mkdir(parents=True)
            (folder / "wallet.pass").write_text(secrets.token_hex(16))
            (folder / "validator-key.pass").write_text(secrets.token_hex(16))
            init = json.loads(run(wallet, "--no-color", "init", "--file", folder / "wallet.vault",
                                  "--password-file", folder / "wallet.pass", "--network", "development",
                                  "--output", "json"))
            owner = init["first_address"]["address"]
            key = json.loads(run(wallet, "--no-color", "validator-key-export", "--file", folder / "wallet.vault",
                                 "--password-file", folder / "wallet.pass",
                                 "--validator-key-file", folder / "validator.key",
                                 "--validator-key-password-file", folder / "validator-key.pass",
                                 "--output", "json"))
            route = address(port("proxy_overlay", index), self.transport)
            endpoint = run(self.bin / "tenraid.exe", "--no-color", "validator-endpoint", "--network", "development",
                           "--data-dir", folder, "--route", route).strip()
            self.protect(folder)
            (folder / "address.txt").write_text(owner)
            bonds.append({"address": owner, "amount": self.bond_of(index)})
            if index == 3 and self.scenario.get("stake_changes"):
                second = json.loads(run(wallet, "--no-color", "address", "--file", folder / "wallet.vault",
                                        "--password-file", folder / "wallet.pass", "--index", "1",
                                        "--network", "development", "--output", "json"))
                bonds.append({"address": second["address"], "amount": STAKER_FUNDS})
            validators.append({
                "validator_id": validator_id(index),
                "owner_address": owner,
                "reward_address": owner,
                "consensus_public_key": key["consensus_public_key"],
                "overlay_endpoint_hex": endpoint,
                "self_bond_address": owner,
                "commission_bps": 100,
            })
        return validators, bonds

    def bond_of(self, index):
        """A validator's self-bond: equal, or its share of the same total stake."""
        shares = self.scenario.get("stakes")
        return BOND * len(self.nodes) * shares[index] // 100 if shares else BOND

    def protect(self, folder):
        """A copy inherits its folder's access rules, and the node and the wallet refuse keys others could read."""
        private = [folder / "validator.key", folder / "validator-key.pass"]
        private += [path for path in folder.glob("wallet.*") if path.is_file()]
        private += [path for path in (folder / "network").rglob("*") if path.suffix in (".key", ".seed")]
        for secret in private:
            run("icacls", secret, "/inheritance:r", "/grant:r", f"{os.environ['USERNAME']}:F")

    def create_manifest(self, validators, bonds, path):
        scenario = self.scenario
        spenders = run(BENCH_TX, "addresses", "development", self.spenders()).split()
        manifest = json.loads((REPO / "config" / "development.traidag.json").read_text())
        funding = manifest["allocations"][1]["address"]
        depositors = []
        if self.private_chain():
            depositors = run(self.prover_tool(), "addresses", "development",
                             self.private_depositors() + REGISTRATION_FUNDERS).split()
            manifest["upgrades"] = [{"height": PRIVATE_ACTIVATION,
                                     "protocol_version": int(run(self.prover_tool(), "version")),
                                     "params": "development-private"}]
        rest = SUPPLY - sum(bond["amount"] for bond in bonds) - SPENDER_FUNDS * (len(spenders) + len(depositors))
        manifest["chain_id"] = CHAIN_ID
        manifest["allocations"] = (bonds + [{"address": coin, "amount": SPENDER_FUNDS}
                                            for coin in spenders + depositors]
                                   + [{"address": funding, "amount": rest}])
        manifest["validators"] = validators
        manifest["difficulty_calibration"] = {"starting_bps": scenario["bps"],
                                              "aggregate_hashes_per_second": scenario["hashes"]}
        target = run(self.bin / "tenrai-release.exe", "difficulty-target", "--network", scenario["calibration"],
                     "--starting-bps", scenario["bps"], "--aggregate-hashes-per-second", scenario["hashes"],
                     "--pow-limit", manifest["pow_limit"])
        manifest["initial_target"] = target.split("initial_target=")[1].split()[0]
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        log(run(self.bin / "tenraid.exe", "--no-color", "genesis", "--manifest", path).strip())

    def write_configs(self):
        template = json.loads((REPO / "config" / "node.example.json").read_text())
        for index in self.nodes:
            folder = self.data(index)
            config = dict(template)
            config["api_landing_page"] = False
            config["rpc_bind"] = f"127.0.0.1:{port('rpc', index)}"
            config["mining_bind"] = f"127.0.0.1:{port('mining', index)}"
            config["public_p2p"] = {
                "listen_addrs": [address(port("p2p", index), self.transport)],
                "bootstrap_peers": [address(port("proxy_p2p", other), self.transport)
                                    for other in self.nodes if other != index],
                "enable_upnp": False,
                "allow_private_discovery": False,
                "quic_congestion_control": self.scenario.get("quic", "cubic"),
            }
            if len(self.nodes) == 1:
                config["public_p2p"] = None
            config["validator_overlay"] = {"listen_addrs": [address(port("overlay", index), self.transport)]}
            config["tenq"] = {"full_dataset": {"build_threads": 2}}
            config["validator"] = {
                "consensus_key_file": str(folder / "validator.key"),
                "password_file": str(folder / "validator-key.pass"),
                "heartbeat": None,
            }
            if self.scenario.get("small_disk_guard"):
                # A long run on a disk with little room: its guard would stop the run, not protect a node.
                config["storage"] = {**template["storage"], "disk_pressure": {
                    **template["storage"]["disk_pressure"], "hard_free_bytes": 1 << 28,
                    "soft_free_bytes": 1 << 29, "recovery_free_bytes": 1 << 30}}
            (folder / "node.json").write_text(json.dumps(config, indent=2) + "\n")
        protocol = "udp" if self.transport == "quic" else "tcp"
        proxies = [{"node": f"v{index}", "listen": port(f"proxy_{kind}", index), "target": port(kind, index),
                    "protocol": protocol} for index in self.nodes for kind in ["p2p", "overlay"]]
        sources = {str(port(kind, index)): f"v{index}" for index in self.nodes for kind in ["p2p", "overlay"]}
        over_wan = (self.scenario.get("joiner") or {}).get("snapshot")
        if over_wan:
            sources[str(port("p2p", 5))] = "v5"
        proxy_config = {"rtt_ms": {**self.scenario["rtt"], **(JOINER_RTT if over_wan else {})},
                        "jitter_ms": {**JITTER, **(JOINER_JITTER if over_wan else {})},
                        "loss": {**self.scenario["loss"], **(JOINER_LOSS if over_wan else {})},
                        "spikes": SPIKES, "sources": sources, "pids_file": str(DATA / "pids.json"),
                        "switch_file": str(DATA / "wan.txt"), "cut_file": str(DATA / "cut.txt"),
                        "slow_file": str(DATA / "slow.txt"),
                        "proxies": proxies,
                        "tcp_model": self.scenario.get("tcp_model", "cubic"),
                        "rate_mbps": ({**self.scenario["rate_mbps"], **JOINER_RATE} if over_wan
                                      else self.scenario.get("rate_mbps", 0)),
                        "queue_ms": self.scenario.get("queue_ms", 0),
                        "cross_share": self.scenario.get("cross_share", 0),
                        "cross_bursts": self.scenario.get("cross_bursts", False)}
        (DATA / "proxy.json").write_text(json.dumps(proxy_config, indent=2) + "\n")
        (DATA / "wan.txt").write_text("off")
        (DATA / "cut.txt").write_text("")

    def create_snapshot(self, until_notes=False):
        """Keys, genesis, signed transactions and the funded chain, stopped once it is idle.

        A private chain proves its notes and its frames with the nodes stopped. With until_notes it stops after the
        notes, and the next call goes on with the frames from the same folders, so that each call fits in an hour."""
        target = self.snapshot()
        notes_ready = target / "notes-ready"
        if self.private_chain() and notes_ready.exists() and not until_notes:
            log("going on with snapshot", target.name, "after its notes")
        else:
            log("building snapshot", target.name)
            if target.exists():
                shutil.rmtree(target)
            self.clean()
            validators, bonds = self.create_keys()
            self.create_manifest(validators, bonds, self.manifest)
            for index, validator in zip(self.nodes, validators):
                log(run(self.bin / "tenraid.exe", "--no-color", "prepare-genesis-validator-signer", "--network",
                        "development", "--manifest", self.manifest, "--data-dir", self.data(index),
                        "--consensus-public-key", validator["consensus_public_key"], "--confirm").strip())
            target.mkdir(parents=True)
            log(run(BENCH_TX, "sign", self.manifest, self.spenders(), target / "tx",
                    *(self.signing(self.spenders() * 64) if self.funding() == 3 else [])).strip())
            self.write_configs()
            try:
                self.start()
                self.wait_height(5)
                self.fund("round 1", target / "tx" / "round1.txt", 250, 300)
                self.fund("round 2", target / "tx" / "round2.txt", 350, 600)
                if self.funding() == 3:
                    self.fund("round 3", target / "tx" / "round3.txt", 400, 900)
                if self.private_chain():
                    self.register_recipients()
                self.settle()
                self.stop_near_epoch_end()
                if self.private_chain():
                    self.save_private(DATA / "private-setup")
            finally:
                self.stop()
            if self.private_chain():
                self.prepare_notes(DATA / "private-setup", target / "private")
                notes_ready.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
                if until_notes:
                    log("notes ready; the next call proves the frames")
                    return
        if self.private_chain():
            self.prove_frames(target / "private")
        shutil.copy(self.manifest, target / "bench.traidag.json")
        for index in self.nodes:
            shutil.copytree(self.data(index), target / f"v{index}",
                            ignore=shutil.ignore_patterns("node.log", "node.json", "LOCK"))
        (target / "ready").write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        log("snapshot ready")

    def stop_near_epoch_end(self):
        """The chain stops shortly before an epoch ends, so that a run passes that end under load."""
        before = self.scenario.get("stop_before_epoch_end")
        if before is None:
            return
        cuts = self.scenario["epoch_cuts"]
        height = max(self.head(index) for index in self.nodes)
        end = (height // cuts + 1) * cuts - 1
        if end - before <= height:
            end += cuts
        log(f"waiting at {height} for {end - before}, {before} cuts before the epoch ends at {end}")
        self.wait_height(end - before, (end - before - height) * 5 + 120)

    def pinned_over_wan(self, index):
        """A validator as a peer that has to be this very node, reached through its proxy."""
        log_text = (self.data(index) / "node.log").read_text(errors="replace")
        identity, peer_id = re.search(r"--peer (\w+)@\S+/p2p/(\w+)", log_text).groups()
        return f"{identity}@{address(port('proxy_p2p', index), self.transport)}/p2p/{peer_id}"

    def await_package(self, budget=900):
        """Waits for the first package a validator writes in this run."""
        deadline = time.time() + budget
        while time.time() < deadline:
            for index in self.nodes:
                for line in (self.data(index) / "node.log").read_text(errors="replace").splitlines():
                    if "snapshot package written" in line:
                        log(f"v{index} wrote a package: {line[:160]}")
                        return
            time.sleep(1)
        raise SystemExit("no validator wrote a snapshot package")

    def read_api(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{port('rpc', 1)}{path}", timeout=30) as answer:
            return answer.read()

    def post_api(self, path, body):
        """A busy node is asked again after the wait it names, as payments ask it (bench_submit)."""
        data = json.dumps(body).encode()
        for attempt in range(BUSY_RETRIES + 1):
            request = urllib.request.Request(f"http://127.0.0.1:{port('rpc', 1)}{path}", data,
                                             {"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=60) as answer:
                    return json.loads(answer.read())
            except urllib.error.HTTPError as error:
                answer = error.read()
                if attempt == BUSY_RETRIES or not is_busy(error.code, answer):
                    raise
                time.sleep(retry_after(answer))

    def register_recipients(self):
        """The sender, whose notes the run spends, and the receiver its transfers pay."""
        anchor = DATA / "private-anchor.json"
        anchor.write_bytes(self.read_api("/v1/private/anchor"))
        for funder, account in enumerate(("sender", "receiver"), start=self.private_depositors()):
            signed = run(self.prover_tool(), "registration", self.manifest, anchor, account, funder).strip()
            self.post_api("/v1/private/registrations", {"bytes": signed})
        deadline = time.time() + 120
        while json.loads(self.read_api("/v1/private/status"))["notes"]["records"] < 2:
            if time.time() > deadline:
                raise SystemExit("the registrations never reached a final cut")
            time.sleep(1)
        log("sender and receiver registered")

    def save_private(self, folder):
        """What frames are proven against, read while the nodes still run."""
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "anchor.json").write_bytes(self.read_api("/v1/private/anchor"))
        (folder / "ledger.json").write_bytes(self.read_api("/v1/transactions/manifest"))
        records = json.loads(self.read_api("/v1/private/records?from=0&limit=512"))["records"]
        (folder / "records.json").write_text(json.dumps(records))
        notes, start = [], 0
        while True:
            page = json.loads(self.read_api(f"/v1/private/notes?from={start}&limit=512"))
            notes += page["notes"]
            if page.get("next") is None:
                break
            start = page["next"]
        (folder / "notes.json").write_text(json.dumps(notes))

    def start_quiet(self):
        """The validators and their links without miners: cuts with private items only."""
        for index in self.nodes:
            proxy = self.spawn(f"proxy-v{index}", [self.bin / "wan-proxy.exe", DATA / "proxy.json", f"v{index}"],
                               DATA / f"proxy-v{index}.log")
            raise_priority(proxy.pid)
        self.start_nodes()
        self.wait_api()

    def prepare_notes(self, setup, folder):
        """ENTERs that leave the sender the notes the run spends, proven with the nodes stopped and run
        before the snapshot."""
        log(f"proving {self.private_setup()} ENTERs that make the notes of the run")
        subprocess.run([str(self.prover_tool()), "prepare", str(self.manifest), str(setup / "anchor.json"),
                        str(setup / "records.json"), str(setup / "ledger.json"), str(self.private_setup()),
                        str(setup / "frames")],
                       check=True, env={**os.environ, "TENRAI_TSP2_PROVER": str(PROVER)})
        corpus = json.loads((setup / "frames" / "corpus.json").read_text())
        try:
            self.start_quiet()
            for entry in corpus:
                frame = (setup / "frames" / entry["file"]).read_text()
                self.post_api("/v1/private/enters", {"bytes": frame})
            deadline = time.time() + 300
            for entry in corpus:
                while json.loads(self.read_api(f"/v1/private/statements/{entry['digest']}")).get("height") is None:
                    if time.time() > deadline:
                        raise SystemExit("the ENTERs that make the notes never ran")
                    time.sleep(1)
            log(f"{len(corpus)} ENTERs ran")
            self.wait_height(max(self.head(index) for index in self.nodes) + 4, 120)
            self.save_private(folder)
        finally:
            self.stop()
        shutil.copy(setup / "frames" / "corpus.json", folder / "setup.json")

    def prove_frames(self, folder):
        """About 15 s and 2.5 GiB per frame, one at a time, with the nodes stopped."""
        log(f"proving {self.private_frames()} statements: ENTER, TRANSFER and EXIT in turn")
        subprocess.run([str(self.prover_tool()), "prove", str(self.manifest), str(folder / "anchor.json"),
                        str(folder / "records.json"), str(folder / "ledger.json"), str(folder / "notes.json"),
                        str(self.private_setup()), str(self.private_frames()), str(folder / "frames")],
                       check=True, env={**os.environ, "TENRAI_TSP2_PROVER": str(PROVER)})

    def bench_wallets(self):
        """The transparent addresses the bench pays from and to, the EXIT address last."""
        manifest = json.loads(Path(self.manifest).read_text())
        funded = [entry["address"] for entry in manifest["allocations"]
                  if int(entry["amount"]) == SPENDER_FUNDS]
        return funded + ([run(self.prover_tool(), "exit-address", "development").strip()]
                         if self.private_chain() else [])

    def expected_private(self, executed):
        """What the setup and the statements that ran moved, in atoms."""
        folder = self.snapshot() / "private"
        moved = {"sender": 0, "receiver": 0, "exit_address": 0, "private": 0}
        entries = json.loads((folder / "setup.json").read_text())
        entries += [entry for entry in json.loads((folder / "frames" / "corpus.json").read_text())
                    if entry["digest"] in executed]
        for entry in entries:
            for key in moved:
                moved[key] += int(entry[key])
        return moved

    def restore(self):
        source = self.snapshot()
        self.clean()
        shutil.copy(source / "bench.traidag.json", self.manifest)
        for index in self.nodes:
            shutil.copytree(source / f"v{index}", self.data(index))
            self.protect(self.data(index))
        self.write_configs()

    def spawn(self, name, args, log_path, env=None):
        handle = open(log_path, "ab")
        process = subprocess.Popen([str(arg) for arg in args], stdout=handle, stderr=subprocess.STDOUT,
                                   creationflags=NO_WINDOW, env=env)
        pin(process.pid, self.cpus)
        self.processes[name] = process
        return process

    def start(self, cpus=None):
        for index in self.nodes:
            proxy = self.spawn(f"proxy-v{index}", [self.bin / "wan-proxy.exe", DATA / "proxy.json", f"v{index}"],
                               DATA / f"proxy-v{index}.log")
            raise_priority(proxy.pid)
        self.start_nodes()
        self.start_miners(cpus or self.scenario["miner_cpus"])

    def start_nodes(self):
        for index in self.nodes:
            self.start_node(index)
        pids = {str(self.processes[f"v{index}"].pid): f"v{index}" for index in self.nodes}
        (DATA / "pids.json").write_text(json.dumps(pids))
        self.wait_api()

    def start_node(self, index):
        folder = self.data(index)
        env = dict(os.environ, RUST_LOG=RUST_LOG)
        return self.spawn(f"v{index}", [self.bin / "tenraid.exe", "--no-color", "-d", "run", "--network",
                                        "development", "--manifest", self.manifest, "--data-dir", folder],
                          folder / "node.log", env=env)

    def join(self, plan, started):
        """Starts a new node without a seat in the plan's storage mode and waits until it holds the
        head the validators reached; from then on it is compared like them."""
        time.sleep(max(0.0, started + plan["after"] - time.time()))
        if plan.get("snapshot"):
            self.await_package()
        folder = DATA2 / "v5"
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        template = json.loads((REPO / "config" / "node.example.json").read_text())
        config = dict(template)
        config["api_landing_page"] = False
        config["rpc_bind"] = f"127.0.0.1:{port('rpc', 5)}"
        config["mining_bind"] = f"127.0.0.1:{port('mining', 5)}"
        config["public_p2p"] = {
            "listen_addrs": [address(port("p2p", 5), self.transport)],
            "bootstrap_peers": [address(port("p2p", other), self.transport) for other in self.nodes],
            "enable_upnp": False,
            "allow_private_discovery": False,
            "quic_congestion_control": self.scenario.get("quic", "cubic"),
        }
        config["storage"] = {**template["storage"], "mode": plan["storage"], "prune_days": 1}
        if plan.get("snapshot"):
            config["public_p2p"]["bootstrap_peers"] = [self.pinned_over_wan(other) for other in self.nodes]
            config["storage"]["peer_snapshot_bootstrap"] = True
        config["tenq"] = {"full_dataset": {"build_threads": 2}}
        (folder / "node.json").write_text(json.dumps(config, indent=2) + "\n")
        env = dict(os.environ, RUST_LOG=RUST_LOG)
        validators_head = max(self.head(index) or 0 for index in self.nodes)
        self.spawn("v5", [self.bin / "tenraid.exe", "--no-color", "-d", "run", "--network", "development",
                          "--manifest", self.manifest, "--data-dir", folder], folder / "node.log", env=env)
        joined = time.time()
        log(f"v5 joined as a {plan['storage']} node without a seat")
        rpc = port("rpc", 5)
        reached, deadline = None, joined + 600
        while time.time() < deadline:
            own = head(rpc)
            target = max(self.head(index) or 0 for index in self.nodes)
            if own is not None and own >= target - 1:
                reached = time.time()
                break
            time.sleep(0.5)
        log(f"v5 caught up after {reached - joined:.1f} s" if reached else "v5 did not catch up")
        if hasattr(self, "watch"):
            self.watch.add("v5", rpc)
        if reached:
            with urllib.request.urlopen(f"http://127.0.0.1:{rpc}/v1/storage", timeout=30) as answer:
                self.joined_declares = json.loads(answer.read())
            log("v5 declares", json.dumps(self.joined_declares))
        self.joined = {"storage": plan["storage"], "joined": joined, "caught_up": reached,
                       "seconds": reached - joined if reached else None, "validators_head": validators_head}

    def outage(self, plan, started):
        """Ends one validator as a crash would, starts it again unchanged after a pause and waits until
        it holds the head the others reached meanwhile. Payments go to the others meanwhile."""
        index = plan["validator"]
        time.sleep(max(0.0, started + plan["after"] - time.time() - 0.5))
        self.down.add(f"v{index}")
        time.sleep(0.5)
        process = self.processes[f"v{index}"]
        process.kill()
        process.wait()
        down = time.time()
        log(f"v{index} stopped")
        time.sleep(plan["down"])
        self.start_node(index)
        up = time.time()
        log(f"v{index} started again")
        target = max(self.head(other) or 0 for other in self.nodes if other != index)
        deadline = up + 120
        while time.time() < deadline and (self.head(index) or 0) < target:
            time.sleep(0.5)
        caught_up = time.time() if (self.head(index) or 0) >= target else None
        log(f"v{index} back at height {target}" if caught_up else f"v{index} did not reach height {target}")
        self.down.discard(f"v{index}")
        self.outages.append({"validator": index, "down": down, "up": up, "caught_up": caught_up,
                             "height": target})

    def netcut(self, plan, started):
        """Cuts every link of one validator as an internet outage would, restores them after a pause
        and waits until it holds the head the others reached meanwhile."""
        index = plan["validator"]
        time.sleep(max(0.0, started + plan["after"] - time.time()))
        (DATA / "cut.txt").write_text(f"v{index}")
        down = time.time()
        log(f"v{index} links cut at height {self.head(index)}")
        time.sleep(plan["down"])
        restored_at = self.head(index) or 0
        (DATA / "cut.txt").write_text("")
        up = time.time()
        target = max(self.head(other) or 0 for other in self.nodes if other != index)
        log(f"v{index} links restored at height {restored_at}, the others at {target}")
        deadline = up + 180
        while time.time() < deadline and (self.head(index) or 0) < target:
            time.sleep(0.5)
        caught_up = time.time() if (self.head(index) or 0) >= target else None
        log(f"v{index} back at height {target} {caught_up - up:.1f} s after the links came back"
            if caught_up else f"v{index} did not reach height {target}")
        self.outages.append({"validator": index, "kind": "netcut", "down": down, "up": up,
                             "caught_up": caught_up, "height": target, "height_at_restore": restored_at})

    def all_heads(self, budget=15):
        """Every validator's head; a node under full load may take a moment to answer."""
        heads = {}
        deadline = time.time() + budget
        while time.time() < deadline:
            for index in self.nodes:
                if index not in heads:
                    height = self.head(index)
                    if height is not None:
                        heads[index] = height
            if len(heads) == len(self.nodes):
                break
            time.sleep(0.2)
        return heads

    def all_at_the_tip(self, budget):
        """Waits until every validator holds the last height but one, as far as `budget` goes."""
        began = time.time()
        while time.time() < began + budget:
            heads = [self.head(index) for index in self.nodes]
            if None not in heads and max(heads) - min(heads) <= 1:
                log(f"all at the tip after {time.time() - began:.1f} s: {heads}")
                return True
            time.sleep(0.5)
        log(f"not all at the tip within {budget} s: {heads}")
        return False

    def stake_outages(self, plans, started):
        """Ends validators one after the other as a crash would and starts each again. Whether
        finality has to stop follows from their stake: without a quorum no height may finalize once
        what was certified before has landed; with one, the others go on. Either way it has to go on
        as one chain once the validator is back."""
        results = []
        for plan in plans:
            index, settle = plan["validator"], plan.get("settle", 10)
            others = [other for other in self.nodes if other != index]
            time.sleep(max(0.0, started + plan["after"] - time.time() - 0.5))
            # The 70 % that stay need every vote, so the one that just came back has to be there.
            at_the_tip = self.all_at_the_tip(plan["all_at_the_tip"]) if plan.get("all_at_the_tip") else True
            self.down.add(f"v{index}")
            time.sleep(0.5)
            process = self.processes[f"v{index}"]
            process.kill()
            process.wait()
            down = time.time()
            log(f"v{index} stopped at height {max(self.head(other) or 0 for other in others)}")
            time.sleep(settle)
            settled = {other: self.head(other) or 0 for other in others}
            time.sleep(max(0.0, plan["down"] - settle))
            at_restart = {other: self.head(other) or 0 for other in others}
            moved = {other: at_restart[other] - settled[other] for other in others}
            log(f"moved while v{index} was down, after {settle} s: {moved}")
            self.start_node(index)
            up = time.time()
            top = max(at_restart.values())
            deadline = up + plan.get("resume_s", 300)
            resumed = None
            while time.time() < deadline:
                heads = [self.head(other) for other in self.nodes]
                if None not in heads and min(heads) > top + 1:
                    resumed = time.time()
                    break
                time.sleep(0.5)
            self.down.discard(f"v{index}")
            log(f"all four went on {resumed - up:.1f} s after v{index} came back" if resumed
                else f"they did not go on within {plan.get('resume_s', 300)} s")
            results.append({"validator": index, "share": self.scenario["stakes"][index], "stops": plan["stops"],
                            "down": down, "up": up, "settled": settled, "at_restart": at_restart,
                            "moved": moved, "resumed_s": resumed - up if resumed else None,
                            "all_at_the_tip_before": at_the_tip,
                            "safe": at_the_tip and (not any(moved.values()) if plan["stops"]
                                                    else all(moved.values()))})
        self.fault = {"kind": "stake_outages", "outages": results, "safe": all(one["safe"] for one in results),
                      "resumed_s": max((one["resumed_s"] for one in results if one["resumed_s"]), default=None)
                      if all(one["resumed_s"] for one in results) else None}

    def leaving_wallet(self, *command):
        """One command of the leaving validator's wallet, final when it returns. A node under full load
        may refuse a request for a moment, and a coin spent once cannot pay a second attempt."""
        folder = self.data(3)
        wanted = [str(self.bin / "tenrai-wallet.exe"), "--no-color", *command, "--network", "development",
                  "--file", str(folder / "wallet.vault"), "--password-file", str(folder / "wallet.pass"),
                  "--service", f"http://127.0.0.1:{port('rpc', 3)}", "--fallback", "none",
                  "--send-when-not-synced", "--fee", "1", "--wait", "--output", "json"]
        for attempt in range(12):
            done = subprocess.run(wanted, capture_output=True, text=True)
            log(f"{command[0]}, attempt {attempt + 1}: exit {done.returncode}", done.stdout.strip()[:300],
                done.stderr.strip()[:300])
            if done.returncode == 0:
                return True
            time.sleep(5)
        return False

    def change_stakes(self, plan, started):
        """The delegation and the exit, both final long before the fault."""
        time.sleep(max(0.0, started + plan["after"] - time.time()))
        before = read_or_status(port("rpc", 3), "/v1/chain/epoch")
        delegated = self.leaving_wallet("stake", "--validator", validator_id(4), "--amount", str(DELEGATED_TRAI))
        left = self.leaving_wallet("validator-exit", "--validator", validator_id(3))
        self.stake_moves = {"before": before, "delegated": delegated, "left": left, "final_at": self.all_heads()}
        log(f"stake moved at heights {self.stake_moves['final_at']}: delegated {delegated}, left {left}")

    def past_epoch_end(self, budget=300):
        """A few cuts of the next committee, which show that it works."""
        deadline = time.time() + budget
        while time.time() < deadline:
            heads = [self.head(index) for index in self.nodes]
            if None not in heads and min(heads) > self.epoch_end + 8:
                log(f"past the end of the epoch at {self.epoch_end}: {heads}")
                return
            time.sleep(1)
        log(f"not past the end of the epoch at {self.epoch_end} within {budget} s")

    def committee_after(self):
        """The committee the moved stake made, as every validator tells it at one height."""
        ports = self.ports()
        epochs = same_height(ports, "/v1/chain/epoch")
        listed = same_height(ports, "/v1/staking/validators")
        fourth = same_height(ports, f"/v1/staking/validators/{validator_id(4)}/positions")
        epoch = epochs["v1"]
        views = {view["validator_id"]: view for view in listed["v1"]["validators"]}
        weight = sum(int(position["principal"]) for position in fourth["v1"]["positions"]
                     if position["state"] == "bonded")
        moved = self.stake_moves
        checks = {"moves_went_final": moved["delegated"] and moved["left"],
                  "every_validator_alike": all(answers[name] == answers["v1"] for answers in (epochs, listed, fourth)
                                               for name in ports),
                  "committee_changed": epoch["validator_set_id"] != moved["before"]["validator_set_id"],
                  "three_in_it": epoch["active_validators"] == 3,
                  "third_left": not views[validator_id(3)]["is_active"],
                  "others_in_it": all(views[validator_id(index)]["is_active"] for index in (1, 2, 4)),
                  "delegation_counts": weight == self.bond_of(4) + DELEGATION}
        log("committee after the moves:", json.dumps(checks), json.dumps(epoch))
        return {**moved, "epoch": epoch, "third": views[validator_id(3)], "fourth": views[validator_id(4)],
                "fourth_weight": str(weight), "checks": checks}

    def fault_moment(self, plan, started, lead=0.0):
        """Waits for the time a plan names, or until the chain stands the cuts it names before the epoch ends."""
        if "before_end" in plan:
            self.wait_height(self.epoch_end - plan["before_end"], 900)
        else:
            time.sleep(max(0.0, started + plan["after"] - time.time() - lead))

    def partition(self, plan, started):
        """Splits the validators into two groups no packet passes between. A cut certified just before
        may still install, then neither side may finalize anything until the links are back; after that
        all of them have to go on from one chain."""
        groups = plan["groups"]
        settle = plan.get("settle", 10)
        self.fault_moment(plan, started)
        before = self.all_heads()
        (DATA / "cut.txt").write_text("|".join(",".join(f"v{index}" for index in group) for group in groups))
        down = time.time()
        log(f"validators parted into {groups} at heights {before}")
        time.sleep(settle)
        settled = self.all_heads()
        time.sleep(max(0.0, plan["down"] - settle))
        at_restore = self.all_heads()
        (DATA / "cut.txt").write_text("")
        up = time.time()
        moved = {index: at_restore[index] - settled[index] for index in self.nodes}
        log(f"links restored at heights {at_restore}; moved while parted, after {settle} s: {moved}")
        top = max(at_restore.values())
        deadline = up + plan.get("resume_s", 300)
        resumed = None
        while time.time() < deadline:
            heads = [self.head(index) for index in self.nodes]
            if None not in heads and min(heads) > top + 1:
                resumed = time.time()
                break
            time.sleep(0.5)
        log(f"finality went on {resumed - up:.1f} s after the links came back" if resumed
            else f"finality did not go on within {plan.get('resume_s', 300)} s")
        # A side that holds a quorum goes on, at most to the end of its committee's epoch; the others stand.
        side = plan.get("quorum_side", [])
        safe = (not any(moved[index] for index in self.nodes if index not in side)
                and all(at_restore[index] <= self.epoch_end for index in side))
        self.fault = {"kind": "partition", "groups": groups, "down": down, "up": up, "before": before,
                      "settled": settled, "at_restore": at_restore, "moved_while_parted": moved,
                      "side_at_epoch_end": [index for index in side if at_restore[index] == self.epoch_end],
                      "safe": safe, "resumed_s": resumed - up if resumed else None}

    def blackout(self, plan, started):
        """Ends all validators at once as a power cut would and starts them again unchanged. Each must
        open at least at the height it held, every cut and every transaction final before must be as it
        was, and they have to go on as one chain."""
        names = [f"v{index}" for index in self.nodes]
        ports = self.ports()
        self.fault_moment(plan, started, 0.5)
        self.down.update(names)
        time.sleep(0.5)
        heads = self.all_heads()
        common = min(heads.values())
        views = {name: cut_view(ports[name], common) for name in names}
        held = final_sample(ports[names[0]], common, plan.get("sampled_cuts", 16))
        for name in names:
            self.processes[name].kill()
        for name in names:
            self.processes[name].wait()
        down = time.time()
        log(f"all validators stopped at heights {heads}; {len(held)} final transactions noted")
        time.sleep(plan["down"])
        for index in self.nodes:
            self.start_node(index)
        up = time.time()
        pids = {str(self.processes[f"v{index}"].pid): f"v{index}" for index in self.nodes}
        (DATA / "pids.json").write_text(json.dumps(pids))
        # Timed on its own: the checks below take a minute when every node is asked of every
        # noted transaction.
        top = max(heads.values())
        resumed = {}

        def watch_resume():
            deadline = up + plan.get("resume_s", 300)
            while time.time() < deadline:
                current = [self.head(index) for index in self.nodes]
                if None not in current and min(current) > top + 1:
                    resumed["at"] = time.time()
                    return
                time.sleep(0.5)
        watcher = threading.Thread(target=watch_resume, daemon=True)
        watcher.start()
        opened = self.all_heads(300)
        self.down.clear()
        log(f"all validators started again, {time.time() - up:.1f} s to their APIs, at heights {opened}")
        went_back = [index for index in self.nodes if opened.get(index, 0) < heads[index]]
        changed = [name for name in names if cut_view(ports[name], common) != views[name]]
        asked = {(name, id_): final_answer(ports[name], id_) for name in names for id_ in held}
        undone = [pair for pair, (height, _, _) in asked.items() if height != held[pair[1]]]
        refusals = [refusal for _, _, refused in asked.values() for refusal in refused]
        if refusals:
            told = sorted({(refusal["status"], (refusal.get("body") or {}).get("message"))
                           for refusal in refusals}, key=str)
            log(f"{len(refusals)} status reads refused at first and asked again, as {told}")
        if went_back or changed or undone:
            log(f"after the restart went back {went_back}, changed {changed}, undone {undone[:8]}")

        def answers(pairs):
            return [{"node": name, "transaction": id_, "noted_height": held[id_],
                     "first": asked[(name, id_)][1], "refused_before": asked[(name, id_)][2][:4],
                     "status": read_or_status(ports[name], f"/v1/transactions/{id_}/status")}
                    for name, id_ in pairs[:16]]
        undone_detail = answers(undone)
        if undone:
            time.sleep(30)
            later = [(name, id_) for name, id_ in undone if final_height(ports[name], id_) != held[id_]]
            undone_detail.append({"after_30_s_still_undone": len(later), "of": len(undone)})
            undone_detail.extend(answers(later))
            log(f"30 s later {len(later)} of {len(undone)} still differ")
        watcher.join(max(0.0, up + plan.get("resume_s", 300) - time.time()))
        resumed = resumed.get("at")
        log(f"finality went on {resumed - up:.1f} s after the restart" if resumed
            else f"finality did not go on within {plan.get('resume_s', 300)} s")
        self.fault = {"kind": "blackout", "down": down, "up": up, "heads": heads, "opened": opened,
                      "compared_height": common, "final_transactions_noted": len(held),
                      "went_back": went_back, "changed": changed, "undone": undone[:32],
                      "undone_detail": undone_detail, "status_refusals": len(refusals),
                      "safe": not (went_back or changed or undone),
                      "resumed_s": resumed - up if resumed else None}

    def degrade(self, plan, started):
        """Starves one validator for a while: its process keeps a share of the machine's CPU and
        its links gain delay and loss. Then both go back, and it has to reach the head the others
        hold on its own; the heights and times count, the logs show how it rejoined."""
        index = plan["validator"]
        name = f"v{index}"
        time.sleep(max(0.0, started + plan["after"] - time.time()))
        cap = CpuCap(self.processes[name].pid)
        cap.limit(plan["cpu"])
        (DATA / "slow.txt").write_text(f"{name} {plan['delay_ms']} {plan['loss']}\n")
        down = time.time()
        log(f"{name} starved at height {self.head(index)}: {plan['cpu']:.0%} of the CPU, "
            f"+{plan['delay_ms']} ms each way, {plan['loss']:.0%} loss")
        time.sleep(plan["for"])
        (DATA / "slow.txt").write_text("")
        cap.lift()
        up = time.time()
        restored_at = self.head(index) or 0
        others = max(self.head(other) or 0 for other in self.nodes if other != index)
        log(f"{name} released at height {restored_at}, the others at {others}")
        deadline = up + plan.get("catch_up_s", 180)
        caught_up = None
        while time.time() < deadline:
            # Only heights every node answered count: a node that stopped answers nothing.
            own = self.head(index)
            heads = [self.head(other) for other in self.nodes if other != index]
            if own is not None and None not in heads and own >= max(heads):
                caught_up = time.time()
                break
            time.sleep(0.5)
        log(f"{name} reached the head {caught_up - up:.1f} s after its release" if caught_up
            else f"{name} did not reach the head within {plan.get('catch_up_s', 180)} s")
        self.outages.append({"validator": index, "kind": "degrade", "down": down, "up": up,
                             "caught_up": caught_up, "height_at_restore": restored_at,
                             "others_at_restore": others})

    def start_miners(self, cpus):
        for index, count in self.scenario["miners"].items():
            owner = (self.data(index) / "address.txt").read_text()
            for number in range(1, count + 1):
                name = f"miner-v{index}-{number}"
                # BENCH_MINER_DEBUG=1 makes the miners log every block they drop and why.
                debug = ["-d"] if os.environ.get("BENCH_MINER_DEBUG") else []
                miner = self.spawn(name, [self.bin / "tenrai-miner.exe", "-a", owner,
                                          "-s", f"127.0.0.1:{port('mining', index)}", "--cpu-only",
                                          "-t", 1, "--cpu-batch", 2, "--tenq-mode", os.environ.get("BENCH_MINER_MODE", "light"), "--no-api",
                                          "--no-color", *debug],
                                   DATA / f"{name}.log")
                pin(miner.pid, cpus)

    def head(self, index):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port('rpc', index)}/v1/chain/head", timeout=3) as reply:
                return json.loads(reply.read()).get("height")
        except Exception:
            return None

    def compare_cuts(self):
        """Cut ID and state root of every height up to the lowest head, side by side on every
        validator: validators that installed a cut at different times must hold the same one."""
        heads = [height for height in (self.head(index) for index in self.nodes) if height is not None]
        top = min(heads) if heads else 0
        mismatches, missing = [], 0
        for height in range(1, top + 1):
            seen = {}
            for index in self.nodes:
                try:
                    url = f"http://127.0.0.1:{port('rpc', index)}/v1/cuts/{height}"
                    with urllib.request.urlopen(url, timeout=10) as reply:
                        view = json.loads(reply.read())
                    seen[index] = (view["cut"]["cut_id"], view["cut"]["state_root"])
                except Exception:
                    missing += 1
            if len(set(seen.values())) > 1:
                mismatches.append({"height": height, "views": seen})
        return {"heights": top, "missing": missing, "mismatches": mismatches}

    def wait_api(self):
        deadline = time.time() + 300
        while time.time() < deadline:
            if all(self.head(index) is not None for index in self.nodes):
                return
            for name, process in self.processes.items():
                if process.poll() is not None:
                    raise SystemExit(f"{name} exited with {process.returncode}")
            time.sleep(1)
        raise SystemExit("the node APIs did not come up")

    def wait_height(self, height, budget=600):
        deadline = time.time() + budget
        while time.time() < deadline:
            heights = {index: self.head(index) for index in self.nodes}
            if all(value is not None and value >= height for value in heights.values()):
                log("heights", heights)
                return
            time.sleep(1)
        raise SystemExit(f"height {height} not reached: {heights}")

    def settle(self):
        """Without miners the next cuts are empty, so no open round names blocks a restart would lose."""
        for name in [name for name in self.processes if name.startswith("miner")]:
            process = self.processes.pop(name)
            process.kill()
            process.wait()
        self.wait_height(max(self.head(index) for index in self.nodes) + 4, 120)

    def come_to_rest(self, ports):
        """No new blocks from here on: what the validators hold stops changing once the work of the
        run has landed, so the final checks compare one state."""
        for name in [name for name in self.processes if name.startswith("miner")]:
            process = self.processes.pop(name)
            process.kill()
            process.wait()
        return wait_for_rest(ports, self.bench_wallets(), log, private=self.private_chain())

    def gate(self, checked, final):
        """The determinism gate over every node of the run, once the chain came to rest."""
        return run_gate(checked, bench_plan(self, checked, final, port("rpc", self.steady_validator()), SPENDER_FUNDS),
                        log)

    def stop(self):
        for process in self.processes.values():
            if process.poll() is None:
                process.kill()
        for process in self.processes.values():
            process.wait()
        self.processes.clear()

    def fund(self, label, path, rate, budget):
        """A full mempool refuses or drops part of a round; those are sent again until all are final."""
        transactions = load(path)
        wire = dict(transactions)
        waiting = transactions
        for _ in range(10):
            sent = waiting
            for _ in range(20):
                records = submit(self.ports(), sent, rate, label, log)
                again = [sent[index] for index, record in enumerate(records)
                         if record["status"] == -1 or "mempool is full" in (record["error"] or "")]
                lost = [record for record in records
                        if record["status"] not in (200, -1) and "mempool is full" not in (record["error"] or "")]
                if lost:
                    log(f"{label}: {len(lost)} refused, first: {lost[0]['error']}")
                if not again:
                    break
                log(f"{label}: {len(again)} wait for room in the mempool")
                time.sleep(5)
                sent = again
            # Dropped counts as an answer here: the funding sends those payments again.
            final, pending = wait_final(port("rpc", 1), [txid for txid, _ in waiting], budget, label, log,
                                        terminal=lambda status: lifecycle(status) in ("finalized", "failed"))
            if pending:
                raise SystemExit(f"{label}: {len(pending)} transactions not final")
            dropped = [txid for txid, status in final.items() if str(status.get("lifecycle", "")).lower() == "failed"]
            if not dropped:
                return
            log(f"{label}: {len(dropped)} dropped, sending them again")
            waiting = [(txid, wire[txid]) for txid in dropped]
        raise SystemExit(f"{label}: transactions keep being dropped")

    def ports(self):
        return {f"v{index}": port("rpc", index) for index in self.nodes}

    def sign_enough_payments(self):
        """A run longer than the funded coins pay for gets more rounds of payments, each spending the
        change of the round before. The signer is deterministic, so the funding it writes again
        must be the funding the snapshot's chain holds."""
        snapshot = self.snapshot()
        needed = self.needed_payments()
        held = count(snapshot / "tx" / "pay.txt")
        if needed <= held:
            return
        coins = count(snapshot / "tx" / "round2.txt") * 8
        if self.funding() == 3 and needed > coins * 8:
            raise SystemExit(f"{needed} payments need more than the {coins * 8} coins of the third round")
        scratch = snapshot / "tx-rounds"
        if scratch.exists():
            shutil.rmtree(scratch)
        log(run(BENCH_TX, "sign", snapshot / "bench.traidag.json", self.spenders(), scratch,
                *self.signing(coins)).strip())
        for funding in ["round1.txt", "round2.txt"] + (["round3.txt"] if self.funding() == 3 else []):
            if (scratch / funding).read_bytes() != (snapshot / "tx" / funding).read_bytes():
                raise SystemExit(f"the signer wrote another {funding} than the snapshot was funded with")
        shutil.copy(scratch / "pay.txt", snapshot / "tx" / "pay.txt")
        shutil.rmtree(scratch)
        log(f"payments signed again, {count(snapshot / 'tx' / 'pay.txt')} in all")

    def steady_validator(self):
        """A validator no crash or cut takes down, to read the payment statuses from."""
        planned = {plan["validator"] for plan in (self.scenario.get("outages") or [])
                   + [self.scenario.get("outage") or {}, self.scenario.get("netcut") or {}] if plan}
        return next((index for index in self.nodes if index not in planned), self.nodes[0])

    def parent_step(self):
        """How far back in pay.txt the payment lies whose change a chained payment spends."""
        if not self.scenario.get("await_parent"):
            return 0
        if self.funding() != 2:
            raise SystemExit("await_parent needs the chained payments of the two-round funding")
        return count(self.snapshot() / "tx" / "round2.txt") * 8

    def measure(self):
        scenario = self.scenario
        self.gate_start = gate_start(self.ports())
        # A degrade plan with `rate_after` eases the load from the starved validator's release on.
        plan = scenario.get("degrade") or {}
        slower = None
        count = scenario["rate"] * scenario["seconds"]
        if plan.get("rate_after"):
            released = plan["after"] + plan["for"]
            slower = (released, plan["rate_after"])
            count = scenario["rate"] * released + plan["rate_after"] * (scenario["seconds"] - released)
        payments = Payments(self.snapshot() / "tx" / "pay.txt", count)
        watched = [(name, process.pid) for name, process in self.processes.items()]
        watched.append(("bench", os.getpid()))
        if scenario.get("wan", True):
            (DATA / "wan.txt").write_text("on")
            log("wide area links on")
        sampler = Sampler(watched, by_thread=[f"v{index}" for index in self.nodes])
        sampler.start()
        miners = [process.pid for name, process in self.processes.items() if name.startswith("miner-")]
        hold = BlockRateHold(miners, self.data(self.steady_validator()) / "node.log",
                             scenario.get("hold_bps", scenario["bps"]), log)
        hold.start()
        # Payments go to every validator in turn, or to those the scenario names; one that is down
        # is skipped until it caught up again.
        ports = self.ports()
        if scenario.get("submit"):
            ports = {f"v{index}": ports[f"v{index}"] for index in scenario["submit"]}
        plans, fault = scenario.get("outages") or [scenario.get("outage")], self.outage
        if scenario.get("netcut"):
            plans, fault = [scenario["netcut"]], self.netcut
        if scenario.get("degrade"):
            plans, fault = [scenario["degrade"]], self.degrade
        if scenario.get("partition"):
            plans, fault = [scenario["partition"]], self.partition
        if scenario.get("blackout"):
            plans, fault = [scenario["blackout"]], self.blackout
        if scenario.get("stake_outages"):
            plans, fault = [scenario["stake_outages"]], self.stake_outages
        plans = [plan for plan in plans if plan]
        if scenario.get("epoch_cuts"):
            cuts = scenario["epoch_cuts"]
            self.epoch_end = (max(self.head(index) for index in self.nodes) // cuts + 1) * cuts - 1
            log(f"this epoch ends at {self.epoch_end}")
        stopper = None
        if plans:
            started = time.time()
            stopper = threading.Thread(target=lambda: [fault(plan, started) for plan in plans], daemon=True)
            stopper.start()
        joiner = None
        if scenario.get("joiner"):
            joiner = threading.Thread(target=self.join, args=(scenario["joiner"], time.time()), daemon=True)
            joiner.start()
        mover = None
        if scenario.get("stake_changes"):
            mover = threading.Thread(target=self.change_stakes, args=(scenario["stake_changes"], time.time()),
                                     daemon=True)
            mover.start()
        steady = port("rpc", self.steady_validator())
        self.watch = Agreement(self.ports(), DATA / "divergence", log)
        self.watch.start()
        statements = None
        if scenario.get("private"):
            frames = sorted((self.snapshot() / "private" / "frames").glob("*.hex"))
            statements = PrivateLoad(frames, self.ports(), scenario["private"], steady, log, self.down,
                                     scenario.get("private_after", 0))
            statements.start()
        records = submit(ports, payments, scenario["rate"], "payments", log, down=self.down,
                         parent_step=self.parent_step(), status_port=steady, slower=slower)
        if stopper:
            stopper.join()
        if joiner:
            joiner.join()
        if mover:
            mover.join()
            self.past_epoch_end()
        ended = max(record["sent"] for record in records)
        hold.stop()
        final, pending = wait_final(steady, [record["txid"] for record in records if record["status"] == 200],
                                    scenario.get("final_wait", 45), "payments", log)
        if scenario.get("fault_run"):
            self.resent = resend_open(steady, payments, pending, log)
            final.update(self.resent.pop("final"))
            pending = self.resent["still_open"]
        sampler.stop()
        if statements:
            statements.finish()
        checked = dict(self.ports())
        if getattr(self, "joined", None) and self.joined["caught_up"]:
            checked["v5"] = port("rpc", 5)
        if self.private_chain():
            self.rest_seconds = self.come_to_rest(checked)
        if statements:
            self.private_result = statements.summary({f"v{index}": port("rpc", index) for index in self.nodes})
            log("private statements:", json.dumps({key: value for key, value in self.private_result.items()
                                                   if key not in ("executed_digests", "ran_at")}))
        self.continuous = self.watch.finish()
        log("heights compared while the run went on:", json.dumps({key: value for key, value in self.continuous.items()
                                                                   if key != "pool_samples"}))
        if self.private_chain():
            executed = set(getattr(self, "private_result", None) and self.private_result["executed_digests"] or [])
            restored = ("v5",) if "v5" in checked and (scenario.get("joiner") or {}).get("snapshot") else ()
            self.final_state = final_checks(checked, self.prover_tool(), Path(self.manifest), self.bench_wallets(),
                                            self.expected_private(executed), log, restored,
                                            (0, self.bond_of(3)) if mover else (0,))
        if mover:
            self.stake_moves = self.committee_after()
        if not self.private_chain():
            self.rest_seconds = self.come_to_rest(checked)
        self.determinism = self.gate(checked, final)
        first_sent = min(record["sent"] for record in records)
        return records, final, pending, sampler.rows, (first_sent + scenario["warm"], ended)

    def report(self, records, final, pending, samples, window):
        logs = {f"v{index}": self.data(index) / "node.log" for index in self.nodes}
        proxy_lines = []
        for index in self.nodes:
            proxy_lines += (DATA / f"proxy-v{index}.log").read_text(errors="replace").splitlines()
        result = analyze(logs, window, self.scenario["target"], records, final, samples, proxy_lines)
        result["scenario"] = self.name
        result["window"] = list(window)
        result["settings"] = {key: value for key, value in self.scenario.items() if key not in ("rtt", "loss")}
        result["rtt_ms"] = self.scenario["rtt"]
        result["loss"] = self.scenario["loss"]
        result["not_final"] = len(pending)
        result["outages"] = self.outages
        result["fault"] = getattr(self, "fault", None)
        result["agreement"] = getattr(self, "agreement", None)
        result["private"] = getattr(self, "private_result", None)
        result["continuous_agreement"] = getattr(self, "continuous", None)
        result["final_state"] = getattr(self, "final_state", None)
        result["joiner"] = getattr(self, "joined", None)
        result["rest_seconds"] = getattr(self, "rest_seconds", None)
        result["resent"] = getattr(self, "resent", None)
        result["stake_moves"] = getattr(self, "stake_moves", None)
        result["determinism"] = getattr(self, "determinism", None)
        result["verdict"] = verdict(result, self.scenario, self.scenario.get("private", 0), result["final_state"],
                                    result["continuous_agreement"], result["agreement"])
        result["environment"] = preflight.validity(result)
        folder = RESULTS / f"{self.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        folder.mkdir(parents=True)
        (folder / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
        (folder / "preflight.json").write_text(json.dumps(self.preflight, indent=2, default=str) + "\n")
        # Each payment as sent and as it ended, for lifecycle.py.
        with gzip.open(folder / "payments.json.gz", "wt") as file:
            json.dump({"records": records, "statuses": final}, file)
        for index in self.nodes:
            shutil.copy(self.data(index) / "node.log", folder / f"v{index}.log")
        # The node that joined, too: the next run's start deletes its data.
        if (DATA2 / "v5" / "node.log").exists():
            shutil.copy(DATA2 / "v5" / "node.log", folder / "v5.log")
        for path in DATA.glob("*.log"):
            shutil.copy(path, folder / path.name)
        return result, folder

    def save_crash(self, outcome):
        folder = RESULTS / f"{self.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        folder.mkdir(parents=True)
        (folder / "result.json").write_text(json.dumps(outcome, indent=2) + "\n")
        (folder / "preflight.json").write_text(json.dumps(self.preflight, indent=2, default=str) + "\n")
        for index in self.nodes:
            shutil.copy(self.data(index) / "node.log", folder / f"v{index}.log")
        # The node that joined, too: the next run's start deletes its data.
        if (DATA2 / "v5" / "node.log").exists():
            shutil.copy(DATA2 / "v5" / "node.log", folder / "v5.log")
        for path in DATA.glob("*.log"):
            shutil.copy(path, folder / path.name)
        return folder


def refuse(reasons):
    """A run whose preconditions fail does not start; exit code 3 tells a queue to check again later."""
    for reason in reasons:
        log("preflight refused:", reason)
    sys.exit(3)


def main():
    if os.name != "nt":
        raise SystemExit("bench.py drives Windows processes (icacls, Win32 process counters); see README")
    name = sys.argv[1]
    bench = Bench(name)
    pin(os.getpid(), bench.cpus)
    log("scenario", name, json.dumps({k: v for k, v in bench.scenario.items() if k not in ("rtt", "loss")}))
    bench.check_preconditions()
    if "--check" in sys.argv:
        # The preflight alone, for a queue that checks every run before it starts the first.
        ready = (bench.snapshot() / "ready").exists()
        if ready:
            bench.preflight["snapshot"] = preflight.snapshot_identity(bench)
            reasons = preflight.compare(bench.preflight)
            if reasons:
                refuse(reasons)
        log("preflight passed" + ("" if ready else "; the run makes its snapshot first"))
        return
    if "--fresh" in sys.argv or not (bench.snapshot() / "ready").exists():
        if "--fresh" in sys.argv:
            (bench.snapshot() / "notes-ready").unlink(missing_ok=True)
        bench.create_snapshot(until_notes="--until-notes" in sys.argv)
        if not (bench.snapshot() / "ready").exists():
            return
    bench.sign_enough_payments()
    bench.preflight["snapshot"] = preflight.snapshot_identity(bench)
    if "--snapshot-only" in sys.argv:
        # The snapshot and the payments of a run as a step of their own, so the run's hour holds the run alone.
        log("snapshot ready:", bench.snapshot())
        return
    reasons = preflight.compare(bench.preflight)
    if reasons:
        refuse(reasons)
    bench.restore()
    aborted = None
    try:
        bench.start(bench.scenario.get("measure_cpus"))
        wrong = preflight.placed(bench.processes, bench.cpus, bench.measure_mask)
        if wrong:
            raise SystemExit("processes on the wrong CPUs: " + "; ".join(wrong))
        base = max(bench.head(index) for index in bench.nodes)
        bench.wait_height(base + 4, 180)
        if bench.scenario.get("crash_after"):
            outcome = crash_test(bench, port, log)
        else:
            records, final, pending, samples, window = bench.measure()
            bench.agreement = bench.compare_cuts()
            log("cuts compared on every validator:", json.dumps(bench.agreement))
    except (SystemExit, Exception) as error:
        aborted = error
    finally:
        if "--keep" not in sys.argv:
            bench.stop()
    if aborted is not None:
        # The next scenario overwrites the nodes' logs, and a run that broke off is the one to read them for.
        log("aborted:", aborted, "; logs in", bench.save_crash({
            "aborted": str(aborted), "passed": False, "fault": getattr(bench, "fault", None),
            "continuous_agreement": getattr(bench, "continuous", None)}))
        raise aborted
    if bench.scenario.get("crash_after"):
        log("results in", bench.save_crash(outcome))
        print_crash(outcome)
        sys.exit(0 if outcome["passed"] else 1)
    result, folder = bench.report(records, final, pending, samples, window)
    log("results in", folder)
    subprocess.run([sys.executable, str(TOOLS / "summarize.py"), str(folder)])
    # Only a passed verdict ends with 0; verdict_check.py reads the same from result.json.
    sys.exit(0 if result["verdict"]["passed"] else 1)


if __name__ == "__main__":
    main()
