// A test network to look at and try by hand: a development node with private transfers active, two wallets with
// transparent coins and private notes, the normal and the reference web wallet and both explorers on it. It prints
// the pages, the recovery phrases and what to look up in the explorers, and runs until Ctrl+C or its window closes.
// Test funds only; every start begins a new chain.
//
//   node demo.mjs --tenraid PATH\tenraid.exe --repo PATH\node-repo --sdk PATH\tenrai-sdk --prover PATH\tenrai-tsp2-prove.exe
//       --wallet PATH\tenrai-webwallet --reference-wallet PATH\tenrai-reference-webwallet
//       --explorer PATH\tenrai-explorer --reference-explorer PATH\tenrai-reference-explorer --data DIR
//       [--gateway PATH\tenrai-api.exe] [--open]
//
// With --gateway the explorer reads the gateway and draws its charts from the gateway's history, as on the hosted API.

import { spawn, spawnSync } from "node:child_process";
import { createWriteStream, mkdirSync, readFileSync, rmSync, statfsSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { serveWallet } from "./wallet-server.mjs";

const { values } = parseArgs({
  options: {
    tenraid: { type: "string" },
    repo: { type: "string" },
    sdk: { type: "string" },
    prover: { type: "string" },
    wallet: { type: "string" },
    "reference-wallet": { type: "string" },
    explorer: { type: "string" },
    "reference-explorer": { type: "string" },
    gateway: { type: "string" },
    data: { type: "string" },
    open: { type: "boolean", default: false },
  },
});
const PORTS = { node: 38751, mining: 38752, gateway: 38755, explorer: 38790, wallet: 38791, referenceExplorer: 38793, referenceWallet: 38794 };
/** Below this much free disk the steady miner and the payments stop. */
const LEAST_FREE_BYTES = 5 * 2 ** 30;
const NODE = `http://127.0.0.1:${PORTS.node}`;
const site = (port) => `http://127.0.0.1:${port}`;
const ATOMS = 100_000_000n;
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const log = (line) => console.log(`${new Date().toISOString().slice(11, 19)} ${line}`);
const trai = (atoms) => `${atoms / ATOMS}.${String(atoms % ATOMS).padStart(8, "0")}`;

const sdk = resolve(values.sdk);
const { TenraiClient, parseTrai } = await import(pathToFileURL(join(sdk, "dist", "index.js")).href);
const { Wallet, nativeProver } = await import(pathToFileURL(join(sdk, "dist", "wallet", "index.js")).href);
const wasm = await import(pathToFileURL(join(sdk, "wasm", "tenrai_wasm.js")).href);
wasm.initSync({ module: readFileSync(join(sdk, "wasm", "tenrai_wasm_bg.wasm")) });

/** A wallet from fixed entropy: its phrase and first receiving addresses. */
function account(marker) {
  const entropy = new Uint8Array(32).fill(marker);
  const root = new wasm.WalletRoot(entropy, "");
  const addresses = ["0", "1", "2", "3", "4"].map((index) => wasm.addressFromPublicKey(root.derivePublicKey("0", "receive", index), "development"));
  const phrase = wasm.mnemonicFromEntropy(entropy);
  root.free();
  return { phrase, addresses };
}

const first = account(0xa1);
const second = account(0xa2);
const mining = account(0xa3);
const paying = account(0xa4);
const data = resolve(values.data);
rmSync(data, { recursive: true, force: true });
mkdirSync(data, { recursive: true });
const manifest = JSON.parse(readFileSync(resolve(values.repo, "config", "development.traidag.json"), "utf8"));
const funds = [
  ...first.addresses.map((address) => [address, 300n]),
  [second.addresses[0], 100n],
  [second.addresses[1], 100n],
  ...paying.addresses.map((address) => [address, 1000n]),
];
for (const [address, amount] of funds) {
  manifest.allocations[1].amount -= Number(amount * ATOMS);
  manifest.allocations.push({ address, amount: Number(amount * ATOMS) });
}
manifest.upgrades = [{ height: 3, protocol_version: 3, params: "development-private" }];
const manifestText = `${JSON.stringify(manifest, null, 2)}\n`;
const manifestPath = join(data, "demo.traidag.json");
writeFileSync(manifestPath, manifestText);

const children = [];
function start(name, command, args, options = {}) {
  const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"], ...options });
  const out = createWriteStream(join(data, `${name}.log`), { flags: "a" });
  child.stdout.pipe(out);
  child.stderr.pipe(out);
  child.on("exit", (code) => {
    if (!stopping && !child.stopped) log(`${name} ended with ${code}; see ${join(data, `${name}.log`)}`);
  });
  children.push(child);
  return child;
}
function stop(child) {
  child.stopped = true;
  if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
  else child.kill();
  children.splice(children.indexOf(child), 1);
}
let stopping = false;
function stopAll() {
  stopping = true;
  for (const child of [...children]) stop(child);
}

let mineAlways = false;

/** One CPU thread at the development chain's lowest difficulty fills the disk by the hour; nothing fills it up. */
function diskIsShort() {
  const disk = statfsSync(data);
  return Number(disk.bavail) * Number(disk.bsize) < LEAST_FREE_BYTES;
}

/**
 * Mines all the time while `mineAlways` holds, so that the pages show a running network; else while transparent
 * payments wait, and stops 5 s after. Private statements need no block.
 */
function mineOnDemand(client) {
  let miner = null;
  let idleSince = Date.now();
  setInterval(async () => {
    if (mineAlways && diskIsShort()) {
      mineAlways = false;
      log("the disk runs short: the steady miner and the payments stop");
    }
    const waiting = await client.mempool.info().then((info) => info.transactions, () => 0);
    if (waiting > 0 || mineAlways) {
      idleSince = Date.now();
      miner ??= start("miner", join(dirname(values.tenraid), "tenrai-miner.exe"), [
        "--mining-address", mining.addresses[0], "--node", `127.0.0.1:${PORTS.mining}`, "--cpu-only", "--threads", "1",
        "--tenq-mode", "light", "--no-tenq-prefetch", "--no-api", "--no-color",
      ]);
    } else if (miner && Date.now() - idleSince > 5000) {
      stop(miner);
      miner = null;
    }
  }, 1000);
}
/** A wallet of its own pays itself every few seconds, so that the pages show a network in use. */
function payAlong(client, wallet, addresses) {
  let turn = 0;
  let busy = false;
  let told = "";
  setInterval(async () => {
    if (busy || !mineAlways) return;
    busy = true;
    turn = (turn + 1) % addresses.length;
    await wallet.send(client, { to: addresses[turn], amount: parseTrai("1") }).catch((error) => {
      if (error.message !== told) log(`a payment did not go out: ${error.message}`);
      told = error.message;
    });
    busy = false;
  }, 3000);
}

for (const signal of ["SIGINT", "SIGHUP", "SIGBREAK", "SIGTERM"]) {
  process.on(signal, () => {
    stopAll();
    process.exit(0);
  });
}

async function until(what, seconds, check) {
  const end = Date.now() + seconds * 1000;
  for (;;) {
    let value;
    try {
      value = await check();
    } catch {
      value = false;
    }
    if (value) return value;
    if (Date.now() > end) throw new Error(`gave up waiting for ${what} after ${seconds} s`);
    await sleep(1500);
  }
}

/** Refreshes until nothing is on its way and the private balance reads `atoms`. */
async function settled(notes, atoms) {
  return until(`the private balance to read ${trai(atoms)}`, 240, async () => {
    const read = await notes.refresh();
    if (notes.onTheWay().length) {
      const ended = (await notes.check(read.height)).find((outcome) => outcome.outcome !== "final");
      if (ended) throw new Error(`a private transaction ended ${ended.outcome}`);
    }
    return notes.onTheWay().length === 0 && read.balanceAtoms === atoms ? read : null;
  });
}

function memoryStore() {
  const held = new Map();
  return { read: async (name) => held.get(name) ?? null, write: async (name, bytes) => void held.set(name, bytes) };
}

try {
  start("node", values.tenraid, [
    "--no-color", "run", "--manifest", manifestPath, "--dev-validator", "--data-dir", join(data, "node"),
    "--rpc-bind", `127.0.0.1:${PORTS.node}`, "--mining-bind", `127.0.0.1:${PORTS.mining}`,
    "--browser-origin", site(PORTS.wallet), "--browser-origin", site(PORTS.explorer),
  ]);
  const client = new TenraiClient({ url: NODE });
  mineOnDemand(client);
  await until("private transfers to start", 240, async () => (await client.privateTransfers.status()).active);
  log("node up, private transfers active");
  mineAlways = true;
  if (values.gateway) {
    const { chain_id: chainId } = await client.node.info();
    start("gateway", values.gateway, [
      "serve", "--network", "development", "--chain-id", chainId, "--bind", `127.0.0.1:${PORTS.gateway}`,
      "--node-url", NODE, "--node-trust-file", join(data, "gateway-trusted-node"), "--cors-origins", site(PORTS.explorer),
      "--history-file", join(data, "gateway-history.json"),
    ], { cwd: data });
    await until("the gateway", 60, async () => (await fetch(`${site(PORTS.gateway)}/v1/private/status`)).ok);
    log("gateway up, it keeps the history of the charts");
  }
  const proving = { prover: nativeProver({ path: values.prover }) };
  const open = async (who) => {
    const wallet = await Wallet.fromMnemonic(who.phrase, { network: "development" });
    return { wallet, notes: await wallet.privateNotes(client, { store: memoryStore(), genesisManifest: manifestText }) };
  };
  const a = await open(first);
  const b = await open(second);
  // The rules are active before the first private cut is final, which the notes are read from.
  await until("the first private cut", 120, async () => {
    await a.notes.refresh();
    await b.notes.refresh();
    return true;
  });
  await a.notes.register();
  await b.notes.register();
  await until("both registrations", 180, async () => {
    await a.notes.refresh();
    await b.notes.refresh();
    return (await a.notes.registered()) && (await b.notes.registered());
  });
  log("both private accounts published");

  const sent = await a.wallet.send(client, { to: second.addresses[2], amount: parseTrai("5") });
  await until("the transparent payment", 120, async () => (await client.transactions.statuses([sent.transactionId])).statuses[0]?.lifecycle === "finalized");
  log(`transparent payment ${sent.transactionId}`);

  const fee = BigInt((await client.privateTransfers.status()).fees.transfer_fee);
  const entered = await a.notes.enter({ amount: parseTrai("100") }, proving);
  let privateA = 300n * ATOMS - fee;
  await settled(a.notes, privateA);
  const enterTransaction = (await a.wallet.activity(client, { limit: 5 })).sort((x, y) => y.height - x.height)[0]?.transactionId;
  log(`ENTER ${entered.digest}, transaction ${enterTransaction}`);

  const transfer = await a.notes.transfer({ to: b.notes.address, amount: parseTrai("25") }, proving);
  privateA -= 25n * ATOMS + fee;
  await settled(a.notes, privateA);
  await settled(b.notes, 25n * ATOMS);
  log(`private send ${transfer.digest}`);

  const more = await a.notes.enter({ amount: parseTrai("40") }, proving);
  privateA += 300n * ATOMS - fee;
  const readA = await settled(a.notes, privateA);
  log(`second ENTER ${more.digest}`);

  const smallest = [...readA.notes].sort((x, y) => (x.valueAtoms < y.valueAtoms ? -1 : 1))[0];
  const exit = await a.notes.exit({ to: second.addresses[1], notes: [smallest.note] }, proving);
  privateA -= smallest.valueAtoms;
  await settled(a.notes, privateA);
  log(`EXIT ${exit.digest}`);

  // A private send spends two notes: B gets a second one, and A makes more to send from.
  await a.notes.transfer({ to: b.notes.address, amount: parseTrai("15") }, proving);
  privateA -= 15n * ATOMS + fee;
  await settled(a.notes, privateA);
  const finalB = await settled(b.notes, 40n * ATOMS);
  await a.notes.enter({ amount: parseTrai("50") }, proving);
  privateA += 300n * ATOMS - fee;
  const finalA = await settled(a.notes, privateA);
  log("B holds two notes, A three");
  const balanceA = (await a.wallet.balance(client)).atoms;
  // B holds 200 TRAI from the genesis, the 5 TRAI paid to it and the note paid out to it, less the fee.
  const paidOut = 205n * ATOMS + smallest.valueAtoms - fee;
  const balanceB = (await until("the payout to show", 60, async () => {
    const balance = await b.wallet.balance(client);
    return balance.atoms === paidOut ? balance : null;
  })).atoms;
  const privateAddressA = a.notes.address;
  const privateAddressB = b.notes.address;
  a.notes.close();
  b.notes.close();

  const genesis = readFileSync(manifestPath);
  const pages = serveWallet({
    root: values.wallet,
    port: PORTS.wallet,
    config: {
      api_url: NODE,
      node_url: NODE,
      price: false,
      explorer_url: site(PORTS.explorer),
      send_when_not_synced: true,
      test_genesis_manifest: "genesis.json",
    },
    genesis,
  });
  pages.on("error", (error) => log(`the web wallet's page server: ${error.message}`));
  start("reference-wallet", process.execPath, ["--experimental-strip-types", "server/main.ts"], {
    cwd: values["reference-wallet"],
    env: {
      ...process.env,
      PORT: String(PORTS.referenceWallet),
      HOST: "127.0.0.1",
      TENRAI_API_URL: NODE,
      TENRAI_EXPLORER_URL: site(PORTS.explorer),
      TENRAI_SEND_WHEN_NOT_SYNCED: "1",
      TENRAI_RATE_LIMIT: "0",
      TENRAI_TEST_GENESIS_MANIFEST: manifestPath,
    },
  });
  start("explorer", join(values.explorer, "target", "release", "tenrai-explorer.exe"), [
    "--bind", `127.0.0.1:${PORTS.explorer}`, "--root", values.explorer, "--network", "development",
    "--node-url", NODE, "--price", "false",
    ...(values.gateway
      ? ["--target", "api", "--api-url", site(PORTS.gateway), "--history", "true"]
      : ["--target", "node", "--api-url", NODE, "--history", "false"]),
  ]);
  start("reference-explorer", process.execPath, ["--experimental-strip-types", "server/main.ts"], {
    cwd: values["reference-explorer"],
    env: {
      ...process.env,
      PORT: String(PORTS.referenceExplorer),
      HOST: "127.0.0.1",
      TENRAI_URL: NODE,
      TENRAI_TARGET: "node",
      TENRAI_NETWORK: "development",
      TENRAI_RATE_LIMIT: "0",
    },
  });
  const links = [
    ["Web wallet", site(PORTS.wallet)],
    ["Reference wallet", site(PORTS.referenceWallet)],
    ["Explorer", site(PORTS.explorer)],
    ["Reference explorer", site(PORTS.referenceExplorer)],
  ];
  for (const [name, url] of links) await until(`${name} to answer`, 60, async () => (await fetch(`${url}/`)).ok);

  const notes = (read) => read.notes.map((note) => trai(note.valueAtoms)).join(", ");
  const summary = `Tenrai test network (development; test funds only, a new chain at every start)

Pages
${links.map(([name, url]) => `  ${name.padEnd(20)} ${url}/`).join("\n")}

Wallet A: transparent coins and private notes
  Recovery phrase   ${first.phrase}
  Address           ${first.addresses[0]}
  Private address   ${privateAddressA}
  Balance           ${trai(balanceA)} TRAI, private ${trai(finalA.balanceAtoms)} TRAI in notes of ${notes(finalA)}

Wallet B: received privately
  Recovery phrase   ${second.phrase}
  Address           ${second.addresses[0]}
  Private address   ${privateAddressB}
  Balance           ${trai(balanceB)} TRAI, private ${trai(finalB.balanceAtoms)} TRAI in notes of ${notes(finalB)}

To look up in the explorers: search for the id, or open a link
${[
    ["Transparent payment, A to B, 5 TRAI", "tx", sent.transactionId],
    ["A makes 100 TRAI private (ENTER): its transaction", "tx", enterTransaction],
    ["  and its private statement", "private", entered.digest],
    ["Private send, A to B, 25 TRAI", "private", transfer.digest],
    [`A makes ${trai(smallest.valueAtoms)} TRAI transparent again (EXIT), paid to B`, "private", exit.digest],
  ].map(([what, kind, id]) => `  ${what}
    ${id}
    ${site(PORTS.explorer)}/#/${kind}/${id}
    ${site(PORTS.referenceExplorer)}/${kind === "tx" ? "transactions" : "private"}/${id}`).join("\n")}

To try it: restore a wallet from its phrase with a password of your own, switch on Use private transfers under
Settings, General, and open the Private tab.
A private transaction shows what is public: its cut, fee, kind and spent-note markers, never amounts or owners.
One CPU thread mines all the time and a third wallet pays itself every few seconds, so the pages show a network in use;
below 5 GiB of free disk both stop. Proofs share the machine with them.
`;
  writeFileSync(join(data, "demo.txt"), summary);
  console.log(`\n${summary}`);
  if (values.open) {
    for (const [, url] of links) spawn("cmd", ["/c", "start", "", `${url}/`], { stdio: "ignore", detached: true });
  }
  payAlong(client, (await open(paying)).wallet, paying.addresses);
  log("running; Ctrl+C or closing this window stops the node and the pages");
  await new Promise(() => {});
} catch (error) {
  log(`the demo could not start: ${error?.message ?? String(error)}`);
  stopAll();
  process.exit(1);
}
