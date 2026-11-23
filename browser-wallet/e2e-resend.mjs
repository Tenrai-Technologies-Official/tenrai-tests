// What happens to a payment a node forgets. A node holds an accepted payment only in memory until a block
// carries it, so after a restart it no longer knows it. Each wallet sends it again by itself, the same signed
// bytes: the SDK, the wallet daemon, the normal and the reference web wallet, also when the program restarted
// too. When blocks come each payment runs once, and the balances say so.
//
//   node e2e-resend.mjs --tenraid PATH\tenraid.exe --miner PATH\tenrai-miner.exe --cli PATH\tenrai-wallet.exe
//                       --repo PATH\node-repo --sdk PATH\tenrai-sdk --wallet PATH\tenrai-webwallet
//                       --reference PATH\tenrai-reference-webwallet --data DIR [--out results.json]

import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { browserExecutable } from "./browser-executable.mjs";
import { sleep } from "./browser.mjs";
import { ReferencePage } from "./reference-page.mjs";
import { ATOMS, PASSWORD, WalletPage, trai } from "./wallet-page.mjs";
import { serveWallet } from "./wallet-server.mjs";

const { values } = parseArgs({
  options: {
    tenraid: { type: "string" },
    miner: { type: "string" },
    cli: { type: "string" },
    repo: { type: "string" },
    sdk: { type: "string" },
    wallet: { type: "string" },
    reference: { type: "string" },
    data: { type: "string" },
    chrome: { type: "string", default: browserExecutable("chrome") },
    out: { type: "string", default: "e2e-resend-results.json" },
  },
});
const NODE_PORT = 38751;
const MINING_PORT = 38752;
const DAEMON_PORT = 38753;
const PAGE_PORT = 38754;
const REFERENCE_PORT = 38755;
const FUNDS = 500n * ATOMS;
const PAID = 5n * ATOMS;
const NODE_URL = `http://127.0.0.1:${NODE_PORT}`;
const log = (line) => console.log(`${new Date().toISOString().slice(11, 19)} ${line}`);

const sdk = resolve(values.sdk);
const { TenraiClient } = await import(pathToFileURL(join(sdk, "dist", "index.js")).href);
const { Wallet } = await import(pathToFileURL(join(sdk, "dist", "wallet", "index.js")).href);
const wasm = await import(pathToFileURL(join(sdk, "wasm", "tenrai_wasm.js")).href);
wasm.initSync({ module: readFileSync(join(sdk, "wasm", "tenrai_wasm_bg.wasm")) });

function account(marker) {
  const entropy = new Uint8Array(32).fill(marker);
  const root = new wasm.WalletRoot(entropy, "");
  const address = wasm.addressFromPublicKey(root.derivePublicKey("0", "receive", "0"), "development");
  const phrase = wasm.mnemonicFromEntropy(entropy);
  root.free();
  return { phrase, address };
}

const data = resolve(values.data);
rmSync(data, { recursive: true, force: true });
mkdirSync(data, { recursive: true });

const vault = join(data, "wallet.vault");
const passwordFile = join(data, "wallet.pass");
writeFileSync(passwordFile, PASSWORD);
const vaultArguments = ["--file", vault, "--password-file", passwordFile, "--network", "development"];
const cliProfile = { ...process.env, LOCALAPPDATA: join(data, "cli-home"), HOME: join(data, "cli-home") };
mkdirSync(cliProfile.HOME, { recursive: true });
const init = spawnSync(values.cli, ["--no-color", "init", ...vaultArguments, "--output", "json"], {
  encoding: "utf8",
  env: cliProfile,
});
if (init.status !== 0) throw new Error(`the vault: ${init.stderr}`);
const daemonAddress = JSON.parse(init.stdout).first_address.address;

const sdkAccount = account(0x91);
const pageAccount = account(0x92);
const referenceAccount = account(0x94);
const receiver = account(0x93);
const manifest = JSON.parse(readFileSync(resolve(values.repo, "config", "development.traidag.json"), "utf8"));
manifest.allocations[1].amount -= Number(4n * FUNDS);
for (const address of [sdkAccount.address, pageAccount.address, referenceAccount.address, daemonAddress]) {
  manifest.allocations.push({ address, amount: Number(FUNDS) });
}
manifest.upgrades = [{ height: 3, protocol_version: 3, params: "development-private" }];
const manifestPath = join(data, "e2e.traidag.json");
writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);

const logged = [];
let node;
function startNode() {
  node = spawn(values.tenraid, [
    "--no-color", "run", "--manifest", manifestPath, "--dev-validator", "--data-dir", join(data, "node"),
    "--rpc-bind", `127.0.0.1:${NODE_PORT}`, "--mining-bind", `127.0.0.1:${MINING_PORT}`,
    "--browser-origin", `http://127.0.0.1:${PAGE_PORT}`,
  ], { stdio: ["ignore", "pipe", "pipe"] });
  node.stdout.on("data", (chunk) => logged.push(chunk));
  node.stderr.on("data", (chunk) => logged.push(chunk));
}
async function killNode() {
  const ended = new Promise((done) => node.once("exit", done));
  node.kill();
  await ended;
}
async function nodeUp() {
  await until("the node's API", 120, async () => (await fetch(`${NODE_URL}/v1/chain/head`)).ok);
}

let miner;
function startMiner() {
  miner = spawn(values.miner, [
    "-a", receiver.address, "-s", `127.0.0.1:${MINING_PORT}`, "--cpu-only", "-t", "1", "--cpu-batch", "2",
    "--tenq-mode", "light", "--no-color",
  ], { stdio: "ignore" });
}
async function stopMiner() {
  const ended = new Promise((done) => miner.once("exit", done));
  miner.kill();
  await ended;
}

let daemon;
async function startDaemon() {
  daemon = spawn(values.cli, [
    "--no-color", "daemon", ...vaultArguments, "--service", NODE_URL, "--listen", `127.0.0.1:${DAEMON_PORT}`,
  ], { stdio: ["ignore", "pipe", "pipe"], env: cliProfile });
  daemon.stdout.on("data", (chunk) => logged.push(chunk));
  daemon.stderr.on("data", (chunk) => logged.push(chunk));
  await until("the daemon", 120, async () => (await rpc("status")).result);
}
async function killDaemon() {
  const ended = new Promise((done) => daemon.once("exit", done));
  daemon.kill();
  await ended;
}
async function rpc(method, params = {}) {
  const token = readFileSync(`${vault}.token`, "utf8").trim();
  const reply = await fetch(`http://127.0.0.1:${DAEMON_PORT}/v1/rpc`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  return reply.json();
}

const results = [];
let page;
let reference;
async function step(name, action) {
  try {
    const detail = (await action()) ?? "";
    results.push({ name, passed: true, detail });
    log(`PASS ${name}${detail ? `: ${detail}` : ""}`);
    return true;
  } catch (error) {
    results.push({ name, passed: false, detail: error?.message ?? String(error) });
    log(`FAIL ${name}: ${error?.message ?? String(error)}`);
    await page?.screenshot("failed-page").catch(() => {});
    await reference?.screenshot("failed-reference").catch(() => {});
    return false;
  }
}

async function until(what, seconds, check) {
  const end = Date.now() + seconds * 1000;
  for (;;) {
    let value;
    let problem = "";
    try {
      value = await check();
    } catch (error) {
      problem = `: ${error?.message ?? String(error)}`;
    }
    if (value) return value;
    if (Date.now() > end) throw new Error(`gave up waiting for ${what} after ${seconds} s${problem}`);
    await sleep(1000);
  }
}

/** The node's answer on a transaction: its status, or null while it does not know it. */
async function statusOf(id) {
  const reply = await fetch(`${NODE_URL}/v1/transactions/${id}/status`);
  if (reply.status === 404) return null;
  if (!reply.ok) throw new Error(`status ${reply.status}`);
  return reply.json();
}

const allKnown = (ids) => Promise.all(ids.map(statusOf)).then((all) => all.every(Boolean));

async function balanceOf(address) {
  const answer = await (await fetch(`${NODE_URL}/v1/address/${address}/balance`)).json();
  return { atoms: BigInt(answer.balance), outputs: answer.utxos };
}

async function feeOf(id) {
  return BigInt((await (await fetch(`${NODE_URL}/v1/transactions/${id}`)).json()).fee);
}

/** The payment a web page just sent: what the node's pool holds for the receiver and no other wallet sent. */
async function pooledBeside(known) {
  return until("the page's payment in the pool", 60, async () => {
    const reply = await fetch(`${NODE_URL}/v1/mempool/pending`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_version: 1, addresses: [receiver.address] }),
    });
    const { entries } = await reply.json();
    return entries.map((entry) => entry.transaction_id).find((id) => !known.includes(id));
  });
}

async function sendFromPage(page, to, atoms) {
  await page.eval("document.querySelector('[data-tab=wallet]')?.click()");
  await page.until("!document.getElementById('open-send').disabled", 60, "the send button");
  await page.click("open-send");
  await page.until("document.getElementById('send-dialog').open", 10, "the send dialog");
  await page.fill("send-recipient", to);
  await page.fill("send-amount", trai(atoms));
  await page.until("!document.getElementById('send-review').disabled", 30, "the review button");
  await page.eval("document.getElementById('send-form').requestSubmit(document.getElementById('send-review'))");
  await page.approve();
}

startNode();
const site = serveWallet({
  root: values.wallet,
  port: PAGE_PORT,
  config: {
    api_url: NODE_URL,
    node_url: NODE_URL,
    price: false,
    send_when_not_synced: true,
    test_genesis_manifest: "genesis.json",
  },
  genesis: readFileSync(manifestPath),
  isolated: true,
});
const openPage = (name) => WalletPage.open(name, {
  chrome: values.chrome,
  url: `http://127.0.0.1:${PAGE_PORT}/`,
  shots: join(data, "shots"),
  flags: [],
  loaded: "document.readyState === 'complete' && !document.documentElement.classList.contains('unchecked')",
});

const server = spawn(process.execPath, ["--experimental-strip-types", "server/main.ts"], {
  cwd: values.reference,
  stdio: ["ignore", "pipe", "pipe"],
  env: {
    ...process.env,
    PORT: String(REFERENCE_PORT),
    HOST: "127.0.0.1",
    TENRAI_API_URL: NODE_URL,
    TENRAI_SEND_WHEN_NOT_SYNCED: "1",
    TENRAI_RATE_LIMIT: "0",
    TENRAI_TEST_GENESIS_MANIFEST: manifestPath,
  },
});
server.stdout.on("data", (chunk) => logged.push(chunk));
server.stderr.on("data", (chunk) => logged.push(chunk));

let finished = false;
try {
  await nodeUp();
  const client = new TenraiClient({ url: NODE_URL });
  let sdkWallet = await Wallet.fromMnemonic(sdkAccount.phrase, { network: "development" });
  await until("the first cuts", 120, async () => (await (await fetch(`${NODE_URL}/v1/chain/head`)).json()).height > 3);
  await startDaemon();
  page = await openPage("resend");
  await page.restore(pageAccount.phrase);
  await page.transparentBalance(FUNDS);
  await until("the reference wallet's server", 60, async () => (await fetch(`http://127.0.0.1:${REFERENCE_PORT}/`)).ok);
  reference = await ReferencePage.open("resend-reference", {
    chrome: values.chrome,
    url: `http://127.0.0.1:${REFERENCE_PORT}/`,
    shots: join(data, "shots"),
  });
  await reference.restore(referenceAccount.phrase);

  const offline = (off) =>
    page.send("Network.emulateNetworkConditions", {
      offline: off,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: -1,
    });

  const round = async (name, restartPrograms) => {
    const ids = {};
    let kept = [];
    const sent = await step(`${name}: each wallet sends and the node holds the payment, no block yet`, async () => {
      ids.sdk = (await sdkWallet.send(client, { to: receiver.address, amount: PAID })).transactionId;
      const answer = await rpc("send", { to: receiver.address, amount: String(PAID / ATOMS) });
      if (answer.error) throw new Error(`the daemon: ${JSON.stringify(answer.error)}`);
      ids.daemon = answer.result.transaction;
      await sendFromPage(page, receiver.address, PAID);
      ids.page = await pooledBeside(Object.values(ids));
      await reference.pay(receiver.address, PAID);
      ids.reference = await pooledBeside(Object.values(ids));
      await until("all four in the node", 60, () => allKnown(Object.values(ids)));
      return JSON.stringify(ids);
    });
    if (!sent) throw new Error("no payments to follow");

    await step(`${name}: a hard restart of the node forgets them`, async () => {
      if (restartPrograms) {
        kept = sdkWallet.openTransfers();
        if (kept.length !== 1) throw new Error(`${kept.length} open transfers`);
        await killDaemon();
        await killNode();
        await page.eval("location.reload()");
        await reference.eval("location.reload()");
        await sleep(2000);
        sdkWallet.close();
        sdkWallet = await Wallet.fromMnemonic(sdkAccount.phrase, { network: "development" });
        sdkWallet.keepOpen(kept);
      } else {
        // The page answers within a moment of the node's return, before anyone could look.
        await offline(true);
        await killNode();
      }
      await sleep(3000);
      startNode();
      await nodeUp();
      const known = await Promise.all(Object.values(ids).map(statusOf));
      if (known.some(Boolean)) throw new Error(`still known: ${JSON.stringify(known)}`);
      if (restartPrograms) {
        await startDaemon();
        await page.unlock();
        await reference.unlock();
      } else {
        await offline(false);
      }
    });

    await step(`${name}: each wallet sends it again by itself`, async () => {
      const started = Date.now();
      const taken = {};
      await until("the four payments to be back", 240, async () => {
        await sdkWallet.resend(client).catch(() => []);
        for (const [who, id] of Object.entries(ids)) {
          if (taken[who] === undefined && (await statusOf(id))) {
            taken[who] = Math.round((Date.now() - started) / 1000);
          }
        }
        return Object.keys(taken).length === 4;
      });
      return `seconds until known: ${JSON.stringify(taken)}`;
    });

    await step(`${name}: blocks come and each runs once`, async () => {
      startMiner();
      await until("all four final", 300, async () => {
        const all = await Promise.all(Object.values(ids).map(statusOf));
        return all.every((status) => status?.lifecycle === "finalized");
      });
      await stopMiner();
      return "final";
    });
    return ids;
  };

  const first = await round("first round", false);
  const second = await round("second round, the programs restart too", true);

  await step("the balances show every payment once", async () => {
    await sleep(5000);
    const wallets = {
      sdk: sdkAccount.address,
      page: pageAccount.address,
      reference: referenceAccount.address,
      daemon: daemonAddress,
    };
    const lines = [];
    for (const [who, address] of Object.entries(wallets)) {
      const fees = (await feeOf(first[who])) + (await feeOf(second[who]));
      const expected = FUNDS - 2n * PAID - fees;
      // A wallet's change goes to another of its addresses, so the sender's total comes from its own view.
      const seen = who === "sdk" ? (await sdkWallet.balance(client)).atoms : null;
      if (seen !== null && seen !== expected) throw new Error(`${who} holds ${trai(seen)}, expected ${trai(expected)}`);
      if (who === "page") await page.transparentBalance(expected, 120);
      if (who === "reference") {
        const copy = await Wallet.fromMnemonic(referenceAccount.phrase, { network: "development" });
        const { atoms } = await copy.balance(client);
        copy.close();
        if (atoms !== expected) throw new Error(`the reference wallet holds ${trai(atoms)}, expected ${trai(expected)}`);
      }
      if (who === "daemon") {
        const told = String((await rpc("balance")).result.balance);
        const [whole, part = ""] = told.split(".");
        const atoms = BigInt(whole) * ATOMS + BigInt(part.padEnd(8, "0"));
        if (atoms !== expected) throw new Error(`the daemon holds ${told}, expected ${trai(expected)}`);
      }
      lines.push(`${who} ${address.slice(0, 12)}: ${trai(expected)}`);
    }
    // The receiver mined too, so only its outputs of the payments count here.
    const paid = [...Object.values(first), ...Object.values(second)];
    for (const id of paid) {
      const view = await (await fetch(`${NODE_URL}/v1/transactions/${id}`)).json();
      const toReceiver = view.outputs.filter((output) => output.address === receiver.address);
      if (toReceiver.length !== 1 || BigInt(toReceiver[0].amount) !== PAID) {
        throw new Error(`payment ${id} pays the receiver ${JSON.stringify(toReceiver)}`);
      }
    }
    return lines.join("; ");
  });
  finished = true;
} catch (error) {
  results.push({ name: "the run", passed: false, detail: error?.message ?? String(error) });
  log(`FAIL the run: ${error?.message ?? String(error)}`);
} finally {
  await page?.close().catch(() => {});
  await reference?.close().catch(() => {});
  server.kill();
  site.close();
  for (const child of [daemon, miner, node]) child?.kill();
  writeFileSync(join(data, "node.log"), Buffer.concat(logged));
  const passed = results.filter((result) => result.passed).length;
  log(`${passed} of ${results.length} passed`);
  writeFileSync(values.out, `${JSON.stringify({ results }, null, 2)}\n`);
  process.exit(finished && passed === results.length ? 0 : 1);
}
