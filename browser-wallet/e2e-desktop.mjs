// The desktop app, built with the native prover, against a real node with private transfers active. It opens the
// app's window for the time of the test and drives its web view through WebView2's debugging port (Windows only).
//
//   node e2e-desktop.mjs prepare --repo PATH\node-repo --wasm PATH\vendor\tenrai-wasm --out DIR
//       writes the test network's genesis and config.json for the build:
//       node build.mjs --no-linux --config DIR\config.json --test-genesis DIR\genesis.json --prover PATH\tenrai-tsp2-prove.exe
//   node e2e-desktop.mjs run --tenraid PATH\tenraid.exe --app PATH\tenrai-wallet-desktop.exe --prepared DIR --data DIR
//       [--shots DIR] [--out results.json]

import { execFileSync, spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { sleep } from "./browser.mjs";
import { ATOMS, READY, WalletPage, trai } from "./wallet-page.mjs";

const { values, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    repo: { type: "string" },
    wasm: { type: "string" },
    out: { type: "string" },
    tenraid: { type: "string" },
    app: { type: "string" },
    prepared: { type: "string" },
    data: { type: "string" },
    shots: { type: "string" },
  },
});
const NODE_PORT = 38731;
const MINING_PORT = 38732;
const DEBUG_PORT = 9555;
const FEE = 33_831_080n;
const FUNDS = 420n * ATOMS;
const log = (line) => console.log(`${new Date().toISOString().slice(11, 19)} ${line}`);
const proverRunning = () => execFileSync("tasklist", ["/FI", "IMAGENAME eq tenrai-tsp2-prove.exe", "/NH"], { encoding: "utf8" }).includes("tenrai-tsp2-prove");

async function accounts(folder) {
  const wasm = await import(pathToFileURL(join(folder, "tenrai_wasm.js")).href);
  wasm.initSync({ module: readFileSync(join(folder, "tenrai_wasm_bg.wasm")) });
  const entropy = new Uint8Array(32).fill(0x91);
  const root = new wasm.WalletRoot(entropy, "");
  const addresses = ["0", "1", "2"].map((index) => wasm.addressFromPublicKey(root.derivePublicKey("0", "receive", index), "development"));
  const phrase = wasm.mnemonicFromEntropy(entropy);
  root.free();
  return { phrase, addresses };
}

if (positionals[0] === "prepare") {
  const wallet = await accounts(resolve(values.wasm));
  const manifest = JSON.parse(readFileSync(resolve(values.repo, "config", "development.traidag.json"), "utf8"));
  manifest.allocations[1].amount -= Number(FUNDS);
  for (const address of wallet.addresses) manifest.allocations.push({ address, amount: Number(FUNDS / 3n) });
  manifest.upgrades = [{ height: 3, protocol_version: 3, params: "development-private" }];
  const out = resolve(values.out);
  mkdirSync(out, { recursive: true });
  writeFileSync(join(out, "genesis.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  writeFileSync(join(out, "config.json"), `${JSON.stringify({
    api_url: `http://127.0.0.1:${NODE_PORT}`,
    node_url: `http://127.0.0.1:${NODE_PORT}`,
    price: false,
    send_when_not_synced: true,
    test_genesis_manifest: "genesis.json",
  }, null, 2)}\n`);
  writeFileSync(join(out, "phrase.txt"), `${wallet.phrase}\n`);
  log(`prepared ${out}`);
  process.exit(0);
}

const prepared = resolve(values.prepared);
const phrase = readFileSync(join(prepared, "phrase.txt"), "utf8").trim();
const data = resolve(values.data);
rmSync(data, { recursive: true, force: true });
mkdirSync(data, { recursive: true });
const logged = [];
const node = spawn(values.tenraid, [
  "--no-color", "run", "--manifest", join(prepared, "genesis.json"), "--dev-validator", "--data-dir", join(data, "node"),
  "--rpc-bind", `127.0.0.1:${NODE_PORT}`, "--mining-bind", `127.0.0.1:${MINING_PORT}`,
  "--browser-origin", "http://tauri.localhost",
], { stdio: ["ignore", "pipe", "pipe"] });
node.stdout.on("data", (chunk) => logged.push(chunk));
node.stderr.on("data", (chunk) => logged.push(chunk));
const results = [];
let page;
let app;
const step = async (name, action) => {
  try {
    const detail = (await action()) ?? "";
    results.push({ name, passed: true, detail });
    log(`PASS ${name}${detail ? `: ${detail}` : ""}`);
  } catch (error) {
    results.push({ name, passed: false, detail: error?.message ?? String(error) });
    log(`FAIL ${name}: ${error?.message ?? String(error)}`);
    await page?.screenshot(`desktop-failed-${results.length}`).catch(() => {});
  }
};

try {
  for (let attempt = 0; ; attempt += 1) {
    try {
      if ((await (await fetch(`http://127.0.0.1:${NODE_PORT}/v1/private/status`)).json()).active) break;
    } catch {}
    if (attempt > 240) throw new Error(`private transfers never started: ${Buffer.concat(logged).toString("utf8").slice(-400)}`);
    await sleep(500);
  }
  app = spawn(values.app, [], {
    stdio: "ignore",
    env: {
      ...process.env,
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${DEBUG_PORT}`,
      WEBVIEW2_USER_DATA_FOLDER: join(data, "webview"),
      LOCALAPPDATA: join(data, "local"),
    },
  });
  page = await WalletPage.attach("desktop", {
    port: DEBUG_PORT,
    shots: values.shots,
    stop: () => app.kill(),
    loaded: "location.protocol.startsWith('http') && document.readyState === 'complete' && !document.documentElement.classList.contains('unchecked')",
  });
  // The app's own profile must stay out of this: a person may keep a real wallet in it.
  if (!existsSync(join(data, "webview", "EBWebView"))) throw new Error("the web view does not use this run's own profile");
  let privateBalance = 0n;
  await step("the page runs in the app, cross-origin isolated", async () => {
    const answer = await page.eval("JSON.stringify({ isolated: self.crossOriginIsolated, bridge: Boolean(window.__TAURI__?.core?.invoke), origin: location.origin })");
    const facts = JSON.parse(answer);
    if (!facts.bridge || !facts.isolated) throw new Error(`not as the app must run: ${answer}`);
    return answer;
  });
  await step("the app carries the native prover", async () => {
    if (!(await page.eval("window.__TAURI__.core.invoke('native_prover_available')"))) throw new Error("this build has no native prover");
  });
  await step("restore the wallet and publish its private account", async () => {
    await page.restore(phrase);
    await page.publish();
    await page.until(READY, 180, "the registration");
    await page.screenshot("desktop-registered");
  });
  await step("an ENTER with a proof from the native prover", async () => {
    await page.enter(100n * ATOMS);
    await page.approve();
    let seen = false;
    const watch = setInterval(() => { seen ||= proverRunning(); }, 1000);
    const took = await page.proofRuns();
    clearInterval(watch);
    if (!seen) throw new Error("no native prover process ran");
    await page.noneOnWay();
    privateBalance = FUNDS / 3n - FEE;
    await page.privateBalance(privateBalance);
    await page.screenshot("desktop-after-enter");
    if ((await page.workers()) > 0) throw new Error("the proof ran in web workers, not in the native prover");
    return `proof and send in ${took.toFixed(0)} s, private ${trai(privateBalance)}`;
  });
  await step("a cancel ends the native prover's process and holds nothing", async () => {
    await page.enter(5n * ATOMS);
    await page.approve();
    await page.until(page.visible("private-proof"), 90, "the proof to start");
    await sleep(2000);
    await page.click("private-proof-cancel");
    await page.until(`!${page.visible("private-proof")}`, 30, "the proof to end");
    await sleep(500);
    if (proverRunning()) throw new Error("the prover process runs on after the cancel");
    if (await page.onWay()) throw new Error("a transaction stays on its way");
    await page.privateBalance(privateBalance);
  });
  await step("an EXIT with a proof from the native prover", async () => {
    const address = JSON.parse(readFileSync(join(prepared, "genesis.json"), "utf8")).allocations.at(-1).address;
    await page.exit(address, 2);
    await page.approve();
    const took = await page.proofRuns();
    await page.noneOnWay();
    await page.privateBalance(0n);
    return `proof and send in ${took.toFixed(0)} s`;
  });
} catch (error) {
  results.push({ name: "the run", passed: false, detail: error?.message ?? String(error) });
  log(`FAIL the run: ${error?.message ?? String(error)}`);
} finally {
  await page?.close().catch(() => {});
  app?.kill();
  node.kill();
  writeFileSync(join(data, "node.log"), Buffer.concat(logged));
  const passed = results.filter((result) => result.passed).length;
  log(`${passed} of ${results.length} passed`);
  writeFileSync(values.out ?? join(data, "results.json"), `${JSON.stringify({ results }, null, 2)}\n`);
  process.exit(passed === results.length ? 0 : 1);
}
