# P2P benchmarks

Run local validators with simulated WAN latency, transaction load and recovery
scenarios. Outputs include cut and finality rates, resource use and node logs.

## Setup

Build the node binaries in release mode. Use a sibling `tenrai-core` checkout
or directory link so Cargo can resolve its local dependencies.

From `p2p-bench/`, build the tools and freeze the node binaries.

```sh
cargo build --locked --release --manifest-path wan-proxy/Cargo.toml
cargo build --locked --release --manifest-path bench-tx/Cargo.toml
py -3.14 freeze.py main
```

Pass the node binary directory as the second argument to `freeze.py` for a
custom build location. Build `bench-private/Cargo.toml` and set
`TENRAI_TSP2_PROVER` to the prover executable for private scenarios.

## Running

```sh
py -3.14 bench.py main --check
py -3.14 bench.py main
```

`--check` runs preflight only. `--fresh` rebuilds the starting snapshot and
`--keep` retains the running nodes and data after the measurement. Use
dedicated data directories. Each run replaces its previous node data.

| Variable | Default |
| --- | --- |
| `TENRAI_REPO` | Sibling `tenrai-core` checkout |
| `BENCH_DATA` | `run/` |
| `BENCH_DATA2` | `run-secondary/` |
| `BENCH_SNAPSHOTS` | `run-snapshots/` |
| `BENCH_NVME` | Optional Windows volume for node data |

`main` uses default network timing, `main3` uses three validators, `main-lan`
omits WAN delay, and `crash` checks recovery after a controlled validator stop.
See `SCENARIOS` in `bench.py` for available profiles and the frozen builds they
use. Results go under `results/`.

Use `summarize.py` to compare reports and `../mesh/round-timeline.py` to merge
validator logs by cut height. `build-variant.py` builds benchmark variants in
separate source copies.
