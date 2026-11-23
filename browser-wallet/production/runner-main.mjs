// Each run owns a fresh development network and its process data.
import assert from "node:assert/strict";
import { copyFile, readFile, writeFile, mkdir } from "node:fs/promises";
import { createHash } from "node:crypto";
import { basename, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { configureOwnedPython } from "./owned-process.mjs";
import { ownedRun, until, json } from "./processes.mjs";
import { freePort, prepareNetwork } from "./network.mjs";
import { sdkPrivateFlow } from "./sdk-flows.mjs";
import { nativeFlow } from "./native-flows.mjs";
import { conflictFlow } from "./conflict-flow.mjs";
import { daemonFlow } from "./daemon-flow.mjs";
import { browserFlow } from "./browser-flows.mjs";
import { poolFlow } from "./pool-flow.mjs";

const { values } = parseArgs({ options: {
  mode: { type: "string", default: "native" }, data: { type: "string" }, repo: { type: "string" },
  sdk: { type: "string" }, tenraid: { type: "string" }, cli: { type: "string" }, miner: { type: "string" },
  python: { type: "string" }, prover: { type: "string" }, chrome: { type: "string" }, wallet: { type: "string" }, gateway: { type: "string" }, pool: { type: "string" },
  shots: { type: "string" }, capture: { type: "boolean", default: false }, pruned: { type: "boolean", default: false },
} });
assert(["native", "conflict", "sdk", "daemon", "classic", "reference", "pool"].includes(values.mode), "select an explicit supported mode");
for (const name of ["data", "repo", "sdk", "tenraid", "cli", "miner"]) assert(values[name], `--${name} is required`);
if (["sdk", "daemon"].includes(values.mode)) assert(values.prover, "--prover is required for genuine native proving");
if (["classic", "reference"].includes(values.mode)) for (const name of ["chrome", "wallet"]) assert(values[name], `--${name} is required`);
if (values.mode === "pool") assert(values.pool, "--pool PATH/tenrai-pool.exe is required");
configureOwnedPython(values.python);
const run = await ownedRun(values.data);
const interrupt = () => { void run.stop().catch(() => {}); };
process.once("SIGINT", interrupt); process.once("SIGTERM", interrupt);
// Run binaries from scratch to keep their sidecar settings out of the build.
const programs = join(run.data, "programs"); await mkdir(programs, { mode: 0o700 });
const digest = async (path) => createHash("sha256").update(await readFile(path)).digest("hex");
for (const name of ["tenraid", "cli", "miner", "pool", "gateway", "prover"]) {
  if (!values[name]) continue;
  const source = resolve(values[name]), copy = join(programs, basename(source));
  await copyFile(source, copy);
  assert.equal(await digest(copy), await digest(source), `the copy of ${source} differs from it`);
  values[name] = copy;
}
const sdkDirectory = resolve(values.sdk);
const publicSdk = await import(pathToFileURL(join(sdkDirectory, "dist/index.js")));
const sdk = await import(pathToFileURL(join(sdkDirectory, "dist/wallet/index.js")));
const wasm = await import(pathToFileURL(join(sdkDirectory, "wasm/tenrai_wasm.js")));
const directory = join(run.data, "clients"); await mkdir(directory, { mode: 0o700 });
const results = [], started = new Date().toISOString();
let failed, network;
const step = async (name, action) => {
  const begin = performance.now();
  try { await action(); results.push({ name, passed: true, elapsed_ms: Math.round(performance.now() - begin) }); console.log(`PASS ${name}`); }
  catch (error) { results.push({ name, passed: false, elapsed_ms: Math.round(performance.now() - begin), error: String(error.message ?? error).slice(0, 2000) }); throw error; }
};
try {
  const pagePort = ["classic", "reference"].includes(values.mode) ? await freePort() : undefined;
  network = await prepareNetwork(run, { ...values, browserOrigins: pagePort === undefined ? [] : [`http://127.0.0.1:${pagePort}`] });
  let url = network.url;
  if (values.gateway) {
    const port = await freePort();
    const gate = run.start("external-api", resolve(values.gateway), ["serve", "--network", "development", "--chain-id", network.manifest.chain_id,
      "--bind", `127.0.0.1:${port}`, "--mode", "direct", "--node-url", network.url,
      "--node-trust-file", join(run.data, "gateway-trust"), "--archive-db", join(run.data, "gateway.sqlite"),
      ...(pagePort === undefined ? [] : ["--cors-origins", `http://127.0.0.1:${pagePort}`])]);
    url = `http://127.0.0.1:${port}`;
    await until("signed external API forwarding", 120_000, async () => (await json(`${url}/v1/private/status`)).active, () => { network.healthy(); gate.healthy(); });
  }
  const client = new publicSdk.TenraiClient({ url, target: values.gateway ? "api" : "node", timeoutMillis: 30_000 });
  const context = { sdk, wasm, sdkDirectory, client, network, directory, step };
  if (values.mode === "conflict") await conflictFlow(context);
  else if (values.mode === "native") await nativeFlow({ ...context, captureFile: values.capture ? join(run.data, "synthetic-native-public-vectors.json") : undefined });
  else if (values.mode === "sdk") await sdkPrivateFlow({ ...context, proverPath: resolve(values.prover) });
  else if (values.mode === "daemon") await daemonFlow({ ...context, proverPath: resolve(values.prover), endpoint: url });
  else if (values.mode === "pool") await poolFlow({ ...context, poolPath: resolve(values.pool) });
  else await browserFlow({ ...context, options: { ...values, apiUrl: url }, pagePort });
} catch (error) {
  failed = error; console.error(`FAIL ${String(error.message ?? error).slice(0, 2000)}`);
} finally {
  await run.stop().catch((error) => { failed ??= error; });
  const wasmBytes = await readFile(join(sdkDirectory, "wasm/tenrai_wasm_bg.wasm"));
  await writeFile(join(run.data, "result.json"), `${JSON.stringify({ format: 1, mode: values.mode, started, finished: new Date().toISOString(),
    passed: !failed, validators: 3, synthetic_private_genesis: true,
    transport: "three independent processes over QUIC", archival_provider_required: false, retention_floor_advance_exercised: false,
    browser_loaded_assets: network?.browserAssets,
    sdk_wasm_sha256: createHash("sha256").update(wasmBytes).digest("hex"), gateway: Boolean(values.gateway),
    results, error: failed ? String(failed.message ?? failed).slice(0, 2000) : undefined }, null, 2)}\n`, { flag: "wx" });
}
if (failed) process.exitCode = 1;
