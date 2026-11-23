# Tenrai Tests

Integration tests and diagnostic tools for Tenrai. Network tests start local
development nodes with disposable data and test wallets.

Use Python 3.14, Node.js 24, and the Rust toolchain in `rust-toolchain.toml` as
required by each tool. Scripts that use the node checkout look for a sibling
directory named `tenrai-core` by default. Set `TENRAI_REPO` or pass `--repo`
where supported. For Cargo's benchmark dependencies, a sibling checkout or
directory link named `tenrai-core` works.

| Tool | Purpose |
| --- | --- |
| [P2P benchmarks](p2p-bench/README.md) | Validator networks, load, finality and recovery |
| [Peer topology](topology/README.md) | Peer discovery, connection limits and churn |
| [Cut timeline](mesh/round-timeline.py) | Merge validator logs by cut height |
| [Storage](storage/README.md) | RocksDB and disk I/O diagnostics |
| [Browser prover](browser-prover/README.md) | Proof verification and resource measurements |
| [Wallet integration](browser-wallet/README.md) | Browser, desktop, SDK, daemon and pool scenarios |
| [Browser memory](web-memory/README.md) | Browser process and page memory |
| [Release qualification](release/README.md) | Build, check and record a mainnet release |

Generated binaries, logs and test data belong in disposable output directories.
