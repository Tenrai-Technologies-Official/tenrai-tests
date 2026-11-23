# Browser prover

Measure the TSP-2 WebAssembly prover and check its proofs with the native
verifier. Build the browser package with the node's `scripts/package-prover.ps1`
and use inputs generated from disposable test accounts.

```sh
node serve.mjs --package "PATH_TO_PACKAGE" --inputs "PATH_TO_TEST_INPUTS"
node measure.mjs --browser chrome --out chrome.json
node measure-firefox.mjs --out firefox.json
py -3.14 verify-results.py chrome.json "PATH_TO_CAPTURE_LOG"
```

Measurements include elapsed time, proof size and memory. `--binary` selects
the browser executable. `CHROME`, `EDGE` and `FIREFOX` also set browser paths.
`--threads 0` selects the single-threaded package and `--only NAME` selects an
input. The capture log supplies statement digests for verification.

To compare both provers, build `p2p-bench/bench-private` and run the same test
inputs through the native and browser packages.

```sh
node compare.mjs --inputs "PATH_TO_TEST_INPUTS" --prover "PATH_TO_PROVER" --package "PATH_TO_PACKAGE" --work "PATH_TO_SCRATCH"
```

For input capture, set `TEE_PROVER_REAL` to the native prover executable,
`TEE_PROVER_OUT` to a private scratch directory, and `TENRAI_TSP2_PROVER` to
`tee-prover.cmd`. Prover inputs contain spending secrets. Use disposable
test accounts for captures.
