# Release qualification

The release driver builds and verifies a mainnet candidate from the selected
source checkouts and genesis inputs. It uses Git Bash, PowerShell, Python
3.14, Node.js and the pinned Rust toolchain on Windows.

Keep the component repositories beside this checkout. `TENRAI_REPO` selects
the node checkout, and `TENRAI_MAINNET_FILES` selects the operator's mainnet
inputs. Use an output directory outside the source repositories. Set
`TENRAI_RELEASE_OUT` or pass `--out` to choose it.

```sh
bash build-release.sh --treasury "TREASURY_ADDRESS" --fingerprint "TRAI-XXXX-XXXX" \
  --bonds "BOND_1,BOND_2,BOND_3" --private-from-genesis yes --out "PATH_TO_CANDIDATE"
```

Pass the three validator bonds in whole TRAI. The private-transfer activation
flag must match the node rules. Use `--reuse-current-treasury` when retaining
the current treasury. `--rehearse` checks the genesis separately.
`--from PHASE` resumes a candidate with its recorded inputs, and
`--until PHASE` limits a run to the named phase.

| Phase | Output |
| --- | --- |
| `genesis` | Candidate manifest, chain identity and built-in genesis checks |
| `binaries` | Frozen release binaries and file hashes |
| `wasm` | Wallet, TenQ and prover packages with source inventories |
| `web` | Web applications and SDK distribution |
| `desktop` | Windows desktop installer |
| `checks` | Integration results, build checks and logs |
| `records` | Public identity, source revisions and verified release evidence |

Keep `checks.tsv`, the logs and built-in genesis receipt with the candidate.
`records.py verify --work DIR --out DIR` revalidates the release evidence
and built-in identity against the frozen binary.

The signing tool reads key files from the mainnet input directory. Override
the paths with `TENRAI_RELEASE_MLDSA_KEY` and `TENRAI_RELEASE_SLHDSA_KEY`.
`TENRAI_PRIVATE_WORK` selects the parent directory for the identity check's
temporary working files. Keep it and private key material outside the source
repositories.
