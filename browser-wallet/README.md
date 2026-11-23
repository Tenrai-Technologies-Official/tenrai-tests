# Wallet integration

The runners start local development nodes and create their own wallets, miner
data and browser profiles. Use fresh scratch directories and built binaries.

## Release integration

Run `e2e-production.mjs` from this directory. Supply an unused absolute
`--data` directory and paths to the node checkout, built SDK and binaries.

```sh
node e2e-production.mjs --mode native --data "PATH_TO_NEW_DATA" --repo "PATH_TO_CORE" --sdk "PATH_TO_SDK" --tenraid "PATH_TO_TENRAID" --cli "PATH_TO_CLI" --miner "PATH_TO_MINER"
```

| Mode | Coverage | Additional options |
| --- | --- | --- |
| `native` | Native locks, claim and refund | `--capture` for public fixture vectors |
| `conflict` | Single spend across validators and restart | |
| `sdk` | Private transfers and wallet recovery | `--prover` |
| `daemon` | Durable requests and restart | `--prover` |
| `classic`, `reference` | Browser wallet and loaded assets | `--chrome` and `--wallet` |
| `pool` | Mining credits and payouts | `--pool` |

Browser runs require the built wallet assets and matching SDK. The process
supervisor uses Python 3 on Windows and Linux. `--python` selects its
executable. Each run writes `result.json` and logs under `--data`.

## Scenario runners

| Script | Coverage |
| --- | --- |
| `e2e.mjs` | Web wallet, private transfers and interrupted sessions |
| `e2e-reference.mjs` | Reference wallet with its own server |
| `e2e-sdk.mjs` | SDK with the native prover |
| `e2e-desktop.mjs` | Windows desktop wallet and WebView2 |
| `e2e-resend.mjs` | Resubmission after a node restart |
| `e2e-live.mjs` | Local validation of the release programs |
| `demo.mjs` | Wallet and explorer preview with test funds |

```sh
node e2e.mjs --tenraid "PATH_TO_TENRAID" --repo "PATH_TO_CORE" --wallet "PATH_TO_WEBWALLET" --data "PATH_TO_DATA" --out results.json
node e2e-sdk.mjs --tenraid "PATH_TO_TENRAID" --repo "PATH_TO_CORE" --sdk "PATH_TO_SDK" --prover "PATH_TO_PROVER" --data "PATH_TO_DATA" --out results.json
```

Use `--chrome` or `CHROME` for a custom Chrome executable. `e2e.mjs` also
accepts `--firefox`. See the script headers for desktop setup and the available
options. Process and journal tests are under `production/tests/`.
