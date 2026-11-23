// The normal web wallet in a real browser against a real node with private transfers active: two wallets
// from their recovery phrases publish their accounts, one makes coins private with a proof made in the
// browser, sends to the other, pays out to a transparent address; reloads and a restore from the phrase
// find the same balances; a reload and a cancel during a proof, too little memory, a lost connection and
// a statement sent twice leave nothing held and no balance wrong.
//
//   node e2e.mjs --tenraid PATH\tenraid.exe --repo PATH\node-repo --wallet PATH\tenrai-webwallet --data DIR
//                [--long] [--plain] [--pruned] [--gallery] [--gateway PATH\tenrai-api.exe] [--firefox PATH\firefox.exe]
//                [--out results.json]
//
// --gallery takes pictures of the wallet in every size, design and language after its first send and ends there.
// --gateway puts the public gateway between the wallets and the node, as on the hosted API.
// --pruned runs the node with pruned storage.
// --firefox PATH runs the pages in Firefox and leaves out the steps that need Chrome's switches.
// --long adds the statement that expires unused, which waits 15 minutes of cuts. --plain serves the page
// without the isolation headers, as a host that cannot send them does: the proof then runs in the build
// without threads, which takes minutes, so the run ends after the first ENTER.

import { spawn } from "node:child_process";
import { createWriteStream, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { browserExecutable } from "./browser-executable.mjs";
import { RunEnds, gatewayRefusals, sleep } from "./browser.mjs";
import { FirefoxWalletPage } from "./firefox.mjs";
import { ATOMS, PASSWORD, READY, WalletPage, trai } from "./wallet-page.mjs";
import { serveWallet } from "./wallet-server.mjs";

const { values } = parseArgs({
  options: {
    tenraid: { type: "string" },
    repo: { type: "string" },
    wallet: { type: "string" },
    data: { type: "string" },
    chrome: { type: "string", default: browserExecutable("chrome") },
    long: { type: "boolean", default: false },
    plain: { type: "boolean", default: false },
    pruned: { type: "boolean", default: false },
    gallery: { type: "boolean", default: false },
    gateway: { type: "string" },
    firefox: { type: "string" },
    shots: { type: "string" },
    out: { type: "string", default: "e2e-results.json" },
  },
});
const NODE_PORT = 38711;
const MINING_PORT = 38712;
const PAGE_PORT = 38781;
const GATEWAY_PORT = 38713;
const ACTIVATION = 3;
const FUNDS = 420n * ATOMS;
const FIRST_COINS = 3;
const RECEIVER_COIN = 2n * ATOMS;
const FEE = 33_831_080n;
const startPage = (name, flags = []) => (values.firefox ? FirefoxWalletPage : WalletPage).open(name, {
  chrome: values.chrome,
  firefox: values.firefox,
  url: `http://127.0.0.1:${PAGE_PORT}/`,
  shots: values.shots,
  flags,
  loaded: "document.readyState === 'complete' && !document.documentElement.classList.contains('unchecked')",
});
const log = (line) => console.log(`${new Date().toISOString().slice(11, 19)} ${line}`);

const results = [];
function report(name, passed, detail = "") {
  results.push({ name, passed, detail });
  log(`${passed ? "PASS" : "FAIL"} ${name}${detail ? `: ${detail}` : ""}`);
}

/** Two fixed test wallets and a third address, from the protocol WASM of the wallet under test. */
async function wallets() {
  const folder = resolve(values.wallet, "vendor", "tenrai-wasm");
  const wasm = await import(pathToFileURL(join(folder, "tenrai_wasm.js")).href);
  wasm.initSync({ module: readFileSync(join(folder, "tenrai_wasm_bg.wasm")) });
  const wallet = (marker) => {
    const entropy = new Uint8Array(32).fill(marker);
    const root = new wasm.WalletRoot(entropy, "");
    const addresses = ["0", "1", "2"].map((index) => wasm.addressFromPublicKey(root.derivePublicKey("0", "receive", index), "development"));
    const phrase = wasm.mnemonicFromEntropy(entropy);
    root.free();
    return { phrase, address: addresses[0], addresses };
  };
  return { sender: wallet(0x71), receiver: wallet(0x72), elsewhere: wallet(0x73) };
}

function writeManifest(path, accounts) {
  const manifest = JSON.parse(readFileSync(resolve(values.repo, "config", "development.traidag.json"), "utf8"));
  const coin = FUNDS / BigInt(FIRST_COINS);
  manifest.allocations[1].amount -= Number(FUNDS + RECEIVER_COIN);
  for (const address of accounts.sender.addresses) manifest.allocations.push({ address, amount: Number(coin) });
  // Publishing a private account spends public coins; the receiver gets one for its fee.
  manifest.allocations.push({ address: accounts.receiver.address, amount: Number(RECEIVER_COIN) });
  manifest.upgrades = [{ height: ACTIVATION, protocol_version: 3, params: "development-private" }];
  writeFileSync(path, `${JSON.stringify(manifest, null, 2)}\n`);
  return manifest;
}

async function nodeStatus() {
  return (await fetch(`http://127.0.0.1:${NODE_PORT}/v1/private/status`)).json();
}

/** What the node holds for these addresses together. */
async function publicBalance(addresses) {
  let total = 0n;
  for (const address of addresses) {
    total += BigInt((await (await fetch(`http://127.0.0.1:${NODE_PORT}/v1/address/${address}/balance`)).json()).balance);
  }
  return total;
}

const started = Date.now();
const accounts = await wallets();
const dataDir = resolve(values.data);
rmSync(dataDir, { recursive: true, force: true });
mkdirSync(dataDir, { recursive: true });
const manifestPath = join(dataDir, "e2e.traidag.json");
writeManifest(manifestPath, accounts);
const nodeLog = join(dataDir, "node.log");
const node = spawn(values.tenraid, [
  "--no-color", "run", "--manifest", manifestPath, "--dev-validator", "--data-dir", join(dataDir, "node"),
  "--rpc-bind", `127.0.0.1:${NODE_PORT}`, "--mining-bind", `127.0.0.1:${MINING_PORT}`,
  "--browser-origin", `http://127.0.0.1:${PAGE_PORT}`,
  ...(values.pruned ? ["--storage-mode", "pruned"] : []),
], { stdio: ["ignore", "pipe", "pipe"] });
const logged = [];
node.stdout.on("data", (chunk) => logged.push(chunk));
node.stderr.on("data", (chunk) => logged.push(chunk));
let nodeEnded = false;
node.on("exit", () => {
  nodeEnded = true;
});
const page = serveWallet({
  root: values.wallet,
  port: PAGE_PORT,
  config: {
    api_url: `http://127.0.0.1:${values.gateway ? GATEWAY_PORT : NODE_PORT}`,
    node_url: `http://127.0.0.1:${NODE_PORT}`,
    price: false,
    send_when_not_synced: true,
    test_genesis_manifest: "genesis.json",
  },
  genesis: readFileSync(manifestPath),
  isolated: !values.plain,
});
const open = [];
let gateway;
const closeAll = async () => {
  for (const browser of open) await browser.close().catch(() => {});
  page.close();
  gateway?.kill();
  node.kill();
  writeFileSync(nodeLog, Buffer.concat(logged));
};
process.on("SIGINT", () => void closeAll().then(() => process.exit(130)));

async function step(name, action) {
  try {
    report(name, true, (await action()) ?? "");
  } catch (error) {
    report(name, false, error?.message ?? String(error));
    for (const browser of open) await browser.screenshot(`failed-${name}-${browser.name}`).catch(() => {});
  }
}

// Firefox has no switch here for a page's memory or for going offline.
const chromeStep = values.firefox ? async (name) => log(`NOT RUN in Firefox: ${name}`) : step;

try {
  for (let attempt = 0; ; attempt += 1) {
    try {
      if ((await nodeStatus()).active) break;
    } catch {}
    if (nodeEnded) throw new Error(`the node ended: ${Buffer.concat(logged).toString("utf8").slice(-600)}`);
    if (attempt > 240) throw new Error("private transfers never started");
    await sleep(500);
  }
  const status = await nodeStatus();
  log(`node up, private transfers active, fee ${status.fees.transfer_fee}`);
  if (values.gateway) {
    const { chain_id: chainId } = await (await fetch(`http://127.0.0.1:${NODE_PORT}/v1/node/info`)).json();
    gateway = spawn(values.gateway, [
      "serve", "--network", "development", "--chain-id", chainId, "--bind", `127.0.0.1:${GATEWAY_PORT}`,
      "--node-url", `http://127.0.0.1:${NODE_PORT}`, "--node-trust-file", join(dataDir, "gateway-trusted-node"),
      "--cors-origins", `http://127.0.0.1:${PAGE_PORT}`, "--history-file", join(dataDir, "gateway-history.json"),
    ], { cwd: dataDir, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, RUST_LOG: "tenrai_api=debug" } });
    const gatewayLog = createWriteStream(join(dataDir, "gateway.log"));
    gateway.stdout.pipe(gatewayLog);
    gateway.stderr.pipe(gatewayLog);
    for (let attempt = 0; ; attempt += 1) {
      try {
        if ((await fetch(`http://127.0.0.1:${GATEWAY_PORT}/v1/private/status`)).ok) break;
      } catch {}
      if (attempt > 120) throw new Error(`the gateway never answered: ${Buffer.concat(logged).toString("utf8").slice(-600)}`);
      await sleep(500);
    }
    log("gateway up, the wallets talk to it");
  }

  const sender = await startPage("sender");
  const receiver = await startPage("receiver");
  open.push(sender, receiver);
  let privateSender = 0n;
  let transparentSender = FUNDS;
  let privateReceiver = 0n;
  const released = [];

  await step("restore both wallets from their phrases", async () => {
    await sender.restore(accounts.sender.phrase);
    await receiver.restore(accounts.receiver.phrase);
    await sender.transparentBalance(FUNDS);
  });
  await step("the wallet shows nothing private until its option is on", async () => {
    const fresh = await startPage("fresh");
    open.push(fresh);
    await fresh.until(fresh.visible("setup-panel"), 30, "the setup");
    await fresh.connected();
    await fresh.eval(`document.querySelector('[data-setup="restore"]').click()`);
    await fresh.click("open-restore");
    await fresh.fill("restore-phrase", accounts.elsewhere.phrase);
    await fresh.fill("restore-password", PASSWORD);
    await fresh.fill("restore-password-confirm", PASSWORD);
    await fresh.until("!document.getElementById('restore-submit').disabled", 10, "the phrase to pass");
    await fresh.click("restore-submit");
    await fresh.until("!document.getElementById('private-enabled').disabled", 60, "the private switch");
    await sleep(1500);
    for (const id of ["private-tab", "balance-private-line", "open-native-locks", "open-multisig"]) {
      if (await fresh.eval(fresh.visible(id))) throw new Error(`${id} shows while its option is off`);
    }
    const asked = fresh.requests.filter((request) => /\/v1\/private\//.test(request.url)).length;
    await fresh.screenshot("option-off");
    open.splice(open.indexOf(fresh), 1);
    await fresh.close();
    if (asked) throw new Error("the wallet posted to the private routes");
  });
  await step("both wallets publish their private accounts", async () => {
    // The sender reads its review for longer than a cut takes: the wallet signs at the newer head with the same facts.
    await sender.publish("1", { review: 12_000 });
    await receiver.publish();
    await sender.until(READY, 180, "the sender's registration");
    await receiver.until(READY, 180, "the receiver's registration");
    // The sender paid its fee from its largest coin and took the change back; the node says what is left.
    transparentSender = await publicBalance(accounts.sender.addresses);
    const paid = FUNDS - transparentSender;
    if (paid <= 0n || paid > ATOMS) throw new Error(`the registration cost ${trai(paid)}, not more than nothing and at most 1 TRAI`);
    await sender.transparentBalance(transparentSender);
    await sender.screenshot("sender-registered");
    return `fee ${trai(paid)}`;
  });
  await step("an ENTER with a proof made in the browser", async () => {
    await sender.enter(100n * ATOMS);
    await sender.approve();
    const took = await sender.proofRuns();
    await sender.noneOnWay();
    // A whole coin of 140 TRAI, the largest, covers the amount and the fee; the rest is a second note.
    const coin = FUNDS / BigInt(FIRST_COINS);
    privateSender = coin - FEE;
    transparentSender -= coin;
    await sender.privateBalance(privateSender);
    await sender.transparentBalance(transparentSender);
    await sender.screenshot("sender-after-enter");
    await sender.eval("document.querySelector('[data-tab=\"wallet\"]').click()");
    if (!(await sender.text("balance-private")).startsWith(trai(privateSender))) throw new Error("the wallet tab shows another private balance");
    await sender.screenshot("wallet-tab");
    await sender.eval("document.getElementById('open-private-tab').click()");
    await sender.until(sender.visible("private-balance"), 5, "the line under the balance to lead to the tab");
    released.push(await sender.proofReleased());
    return `proof and send in ${took.toFixed(0)} s, private ${trai(privateSender)}`;
  });
  if (values.plain) {
    throw new RunEnds("the build without threads made its proof");
  }
  if (values.gallery) await sender.gallery("two-notes", { proof: true });
  await step("a private send to the second wallet", async () => {
    const amount = 10n * ATOMS;
    await sender.transfer(amount, await receiver.text("private-address"));
    await sender.approve();
    const took = await sender.proofRuns();
    await sender.noneOnWay();
    privateSender -= amount + FEE;
    privateReceiver += amount;
    await sender.privateBalance(privateSender);
    await receiver.privateBalance(privateReceiver);
    await receiver.screenshot("receiver-after-send");
    return `proof and send in ${took.toFixed(0)} s; sender ${trai(privateSender)}, receiver ${trai(privateReceiver)}`;
  });
  if (values.gallery) {
    await sender.gallery("after-send");
    await receiver.gallery("receiver");
    throw new RunEnds("the pictures are taken");
  }
  await step("the Send dialog's switch carries recipient and amount to the private dialog", async () => {
    await sender.eval("document.querySelector('[data-tab=\"wallet\"]').click()");
    await sender.click("open-send");
    await sender.until(sender.visible("send-mode"), 5, "the switch");
    await sender.fill("send-recipient", accounts.elsewhere.address);
    await sender.fill("send-amount", "1.5");
    await sender.screenshot("send-switch");
    await sender.click("send-mode-private");
    await sender.until("document.getElementById('private-dialog').open && !document.getElementById('send-dialog').open", 5, "the private dialog");
    const to = await sender.eval("document.getElementById('private-send-to').value");
    const amount = await sender.eval("document.getElementById('private-send-amount').value");
    await sender.closeDialog();
    if (to !== accounts.elsewhere.address || amount !== "1.5") throw new Error(`carried ${to} and ${amount}`);
  });
  await step("an EXIT to a transparent address", async () => {
    await receiver.tab("exit");
    const before = await receiver.eval("document.querySelectorAll('#private-exit-notes input').length");
    await receiver.exit(accounts.elsewhere.address);
    await receiver.approve();
    const took = await receiver.proofRuns();
    await receiver.noneOnWay();
    privateReceiver = 0n;
    await receiver.privateBalance(privateReceiver);
    const answer = await (await fetch(`http://127.0.0.1:${NODE_PORT}/v1/address/${accounts.elsewhere.address}/balance`)).json().catch(() => null);
    return `proof and send in ${took.toFixed(0)} s, notes before ${before}, paid ${trai(10n * ATOMS - FEE)}, node says ${JSON.stringify(answer)}`;
  });
  await step("the option goes off and on again without changing a balance", async () => {
    await receiver.eval("document.getElementById('private-enabled').click()");
    await receiver.until("document.getElementById('private-tab').hidden", 30, "the Private tab to go");
    if (await receiver.eval(receiver.visible("balance-private-line"))) throw new Error("the private line stays");
    await receiver.eval("document.getElementById('private-enabled').click()");
    await receiver.privateTab();
    await receiver.privateBalance(privateReceiver);
  });
  await step("a reload restores every balance from the sealed state", async () => {
    for (const browser of [sender, receiver]) {
      await browser.goto();
      await browser.unlock();
    }
    await sender.privateBalance(privateSender);
    await sender.transparentBalance(transparentSender);
    await receiver.privateBalance(privateReceiver);
  });
  await step("a fresh browser restores the same balances from the phrase alone", async () => {
    const restored = await startPage("restored");
    open.push(restored);
    await restored.restore(accounts.sender.phrase);
    await restored.privateBalance(privateSender);
    await restored.transparentBalance(transparentSender);
    await restored.screenshot("restored");
    open.splice(open.indexOf(restored), 1);
    await restored.close();
  });
  await step("a cancel during the proof holds nothing", async () => {
    await sender.enter(5n * ATOMS);
    await sender.approve();
    await sender.until(sender.visible("private-proof"), 90, "the proof to start");
    await sleep(3000);
    await sender.click("private-proof-cancel");
    await sender.until(`!${sender.visible("private-proof")}`, 30, "the proof to end");
    if (await sender.onWay()) throw new Error("a statement stays on its way");
    await sender.privateBalance(privateSender);
    await sender.transparentBalance(transparentSender);
    // The wallet draws its figures with the next frame.
    await sender.until("!/held|reserved/i.test(document.getElementById('balance-reserved').textContent)", 5, "the coins to come free");
    return `the page holds ${await sender.proofReleased()} MiB afterwards`;
  });
  await step("a proof started right after a cancel gets its memory", async () => {
    const started = async () => {
      await sender.enter(5n * ATOMS);
      await sender.approve();
      await sender.until(sender.visible("private-proof"), 90, "the proof to start");
    };
    await started();
    await sleep(3000);
    await sender.click("private-proof-cancel");
    await sender.until(`!${sender.visible("private-proof")}`, 30, "the proof to end");
    await started();
    // A browser hands the last worker's memory back seconds later; the worker asks again for 10 s before it gives up.
    const end = Date.now() + 13_000;
    while (Date.now() < end) {
      if (!(await sender.eval(sender.visible("private-proof")))) throw new Error(`the next proof ended: ${await sender.notices()}`);
      await sleep(500);
    }
    await sender.click("private-proof-cancel");
    await sender.until(`!${sender.visible("private-proof")}`, 30, "the proof to end");
    if (await sender.onWay()) throw new Error("a statement stays on its way");
    await sender.privateBalance(privateSender);
    await sender.transparentBalance(transparentSender);
  });
  await step("a reload during the proof holds nothing", async () => {
    await sender.enter(5n * ATOMS);
    await sender.approve();
    await sender.until(sender.visible("private-proof"), 90, "the proof to start");
    await sleep(3000);
    await sender.goto();
    await sender.unlock();
    await sender.privateBalance(privateSender);
    await sender.transparentBalance(transparentSender);
    if (await sender.onWay()) throw new Error("a statement stays on its way");
  });
  await chromeStep("too little memory is told before any work and holds nothing", async () => {
    const small = await startPage("small", ["--js-flags=--wasm-max-mem-pages=20000"]);
    open.push(small);
    await small.openedElsewhere(accounts.sender.phrase);
    await small.privateBalance(privateSender);
    await small.enter(5n * ATOMS);
    await small.approve();
    await small.until("[...document.querySelectorAll('body *')].some((node) => node.children.length === 0 && /too little free memory/.test(node.textContent))", 180, "the memory refusal");
    const onWay = await small.onWay();
    await small.screenshot("too-little-memory");
    open.splice(open.indexOf(small), 1);
    await small.close();
    if (onWay) throw new Error("a statement stays on its way");
  });
  await step("one note cannot be sent from, and is told so; another ENTER adds notes", async () => {
    await sender.tab("send");
    await sender.until("/spends two notes/.test(document.getElementById('private-send-hint').textContent) && document.getElementById('private-send-submit').disabled", 60, "the reason");
    await sender.screenshot("one-note");
    await sender.closeDialog();
    await sender.enter(5n * ATOMS);
    await sender.approve();
    const took = await sender.proofRuns();
    await sender.noneOnWay();
    const coin = FUNDS / BigInt(FIRST_COINS);
    privateSender += coin - FEE;
    transparentSender -= coin;
    await sender.privateBalance(privateSender);
    await sender.transparentBalance(transparentSender);
    released.push(await sender.proofReleased());
    return `proof and send in ${took.toFixed(0)} s, private ${trai(privateSender)}`;
  });
  await step("sending to an address without a private account is refused with a clear reason", async () => {
    await sender.transfer(ATOMS, accounts.elsewhere.address);
    await sender.until("[...document.querySelectorAll('body *')].some((node) => node.children.length === 0 && /no private account/.test(node.textContent))", 60, "the refusal");
    if (await sender.onWay()) throw new Error("a statement went out");
    await sender.closeDialog();
  });
  await chromeStep("a lost connection keeps the statement and it goes out when the line is back", async () => {
    sender.requests.length = 0;
    await sender.transfer(ATOMS, await receiver.text("private-address"));
    await sender.approve();
    await sender.until(sender.visible("private-proof"), 90, "the proof to start");
    await sender.send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await sender.until(`!${sender.visible("private-proof")}`, 700, "the proof to end");
    await sender.until("document.querySelectorAll('#private-activity [data-on-way]').length === 1", 60, "the statement to be kept");
    await sender.screenshot("offline-kept");
    await sleep(5000);
    await sender.send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await sender.noneOnWay(420);
    privateSender -= ATOMS + FEE;
    privateReceiver += ATOMS;
    await sender.privateBalance(privateSender);
    await receiver.privateBalance(privateReceiver);
  });
  await chromeStep("the same statement sent twice runs once", async () => {
    const sent = sender.requests.filter((request) => /\/v1\/private\/transfers$/.test(request.url)).at(-1);
    if (!sent) throw new Error("no statement was seen on its way");
    const body = sent.body ?? (await sender.send("Network.getRequestPostData", { requestId: sent.id })).result?.postData;
    if (!body) throw new Error("the statement's bytes are not at hand");
    const again = await fetch(`http://127.0.0.1:${NODE_PORT}/v1/private/transfers`, { method: "POST", headers: { "Content-Type": "application/json" }, body });
    const answer = await again.json();
    if (again.status !== 409 || answer.reason !== "note_spent") throw new Error(`the node answered ${again.status} ${JSON.stringify(answer)}`);
    await sender.privateBalance(privateSender);
    await receiver.privateBalance(privateReceiver);
    return `the node answered ${again.status} ${answer.reason}`;
  });
  await chromeStep("a statement whose notes another copy of the wallet spent is refused and kept for review", async () => {
    const copy = await startPage("copy");
    open.push(copy);
    await copy.openedElsewhere(accounts.sender.phrase);
    await copy.privateBalance(privateSender);
    // This copy proves offline, so its statement stays here while the other copy spends the same notes.
    await copy.transfer(ATOMS, await receiver.text("private-address"));
    await copy.approve();
    await copy.until(copy.visible("private-proof"), 90, "the proof to start");
    await copy.send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    await copy.until(`!${copy.visible("private-proof")}`, 700, "the proof to end");
    await copy.until("document.querySelectorAll('#private-activity [data-on-way]').length === 1", 60, "the statement to be kept");
    const heldNotes = await copy.onWayAmount();

    await sender.exit(accounts.elsewhere.address, 2);
    await sender.approve();
    await sender.proofRuns();
    await sender.noneOnWay();
    await copy.send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    // A refusal cannot rule out that an earlier send of the same statement ran, so the wallet keeps the statement
    // for review until its validity window closes, with the node's reason.
    await copy.until("[...document.querySelectorAll('#private-activity [data-on-way]')].some((row) => /already spent/.test(row.textContent))", 300, "the refusal kept for review");
    // The other copy spent the notes, so the next check finds nothing left and nothing of it held.
    const left = await sender.privateBalance(0n);
    await copy.privateBalance(0n);
    // The figure beside the balance is brief: "0.00 TRAI".
    if (!/^0[.,]0+ /.test(await copy.onWayAmount())) throw new Error(`spent notes stay held: ${await copy.onWayAmount()} on the way`);
    if ((await copy.onWay()) !== 1) throw new Error(`${await copy.onWay()} statements listed on their way`);
    open.splice(open.indexOf(copy), 1);
    await copy.close();
    return `held while offline: ${heldNotes}; refused as spent and kept for review; the balance now reads ${left}`;
  });
  if (values.long) {
    await chromeStep("a statement that expires unused lets its coins go", async () => {
      // The sender's last coin is the registration's change, the whole of it goes in.
      const coin = transparentSender;
      await sender.enter(coin - FEE);
      await sender.approve();
      await sender.until(sender.visible("private-proof"), 90, "the proof to start");
      await sender.send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
      await sender.until(`!${sender.visible("private-proof")}`, 700, "the proof to end");
      await sender.until("document.querySelectorAll('#private-activity [data-on-way]').length === 1", 60, "the statement to be kept");
      const held = await sender.text("balance-reserved");
      await sleep(16 * 60_000);
      await sender.send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
      // The wallet locked itself after 15 minutes without activity, with the statement sealed.
      await sender.unlock();
      // The receiver idled as long and locked itself too; locked, it opens no new notes, and the
      // steps after this one read its balance.
      await receiver.unlock();
      await sender.noneOnWay(120);
      await sender.transparentBalance(transparentSender);
      return `held while offline: ${held}`;
    });
  }
  /** Proves with the page's submissions held back, so the statement stays here until they go free. */
  const provedHeldBack = async (page) => {
    await page.until(page.visible("private-proof"), 90, "the proof to start");
    await page.until(`!${page.visible("private-proof")}`, 700, "the proof to end");
    await page.until("document.querySelectorAll('#private-activity [data-on-way]').length === 1", 60, "the statement to be kept");
    // A worker's memory comes back a moment after it ended.
    await sleep(3000);
  };
  let heldAfterMoves = 0n;
  await chromeStep("the wallet's state moves on during a proof, and the statement runs from its older anchor", async () => {
    // The sender's last coin is the registration's change; the ENTER spends it whole.
    const coin = transparentSender;
    await sender.enter(5n * ATOMS);
    await sender.approve();
    await sender.proofRuns();
    await sender.noneOnWay();
    transparentSender -= coin;
    await sender.privateBalance(coin - FEE);
    await sender.transparentBalance(transparentSender);
    // A third wallet pays this one while it proves, so its notes and the chain move on meanwhile.
    const third = await startPage("third");
    open.push(third);
    await third.restore(accounts.elsewhere.phrase);
    await third.publish();
    await third.until(READY, 180, "the third wallet's registration");
    await third.holdBack(true);
    await third.enter(ATOMS, await sender.text("private-address"));
    await third.approve();
    await provedHeldBack(third);
    await sender.transfer(ATOMS, await receiver.text("private-address"));
    await sender.approve();
    await sender.until(sender.visible("private-proof"), 90, "the proof to start");
    await third.holdBack(false);
    let checks = 0;
    while (await sender.eval(sender.visible("private-proof"))) {
      await sender.click("private-refresh");
      checks += 1;
      await sleep(2000);
    }
    await sender.noneOnWay();
    await third.noneOnWay();
    heldAfterMoves = coin - 2n * FEE;
    privateReceiver += ATOMS;
    await sender.privateBalance(heldAfterMoves);
    await receiver.privateBalance(privateReceiver);
    open.splice(open.indexOf(third), 1);
    await third.close();
    return `${checks} checks during the proof; a payment arrived meanwhile; ${trai(heldAfterMoves)} held`;
  });
  await chromeStep("two copies of one wallet send the same notes at the same moment, and one statement runs", async () => {
    const copy = await startPage("twin");
    open.push(copy);
    await copy.openedElsewhere(accounts.sender.phrase);
    await copy.privateBalance(heldAfterMoves);
    const kept = (page) => page.eval("[...document.querySelectorAll('#private-activity [data-on-way]')].filter((row) => /spends a note of this one|already spent/.test(row.textContent)).length");
    const notes = await sender.eval("document.querySelectorAll('#private-note-list li').length");
    const to = await receiver.text("private-address");
    await Promise.all([sender.holdBack(true), copy.holdBack(true)]);
    await sender.transfer(ATOMS, to);
    await sender.approve();
    await provedHeldBack(sender);
    await copy.transfer(2n * ATOMS, to);
    await copy.approve();
    await provedHeldBack(copy);
    await Promise.all([sender.holdBack(false), copy.holdBack(false)]);
    // One statement runs. The node refuses the other, and its wallet keeps it for review: a refusal cannot rule out
    // that an earlier send of the same statement ran.
    const settled = Date.now() + 300_000;
    for (;;) {
      const listed = (await sender.onWay()) + (await copy.onWay());
      const forReview = (await kept(sender)) + (await kept(copy));
      if (listed === 1 && forReview === 1) break;
      if (Date.now() > settled) throw new Error(`${listed} statements listed on their way, ${forReview} kept for review; notices: ${await sender.notices()} / ${await copy.notices()}`);
      await sleep(1000);
    }
    const end = Date.now() + 120_000;
    let paid = null;
    while (paid === null) {
      await receiver.awake();
      await receiver.click("private-refresh");
      const shown = await receiver.text("private-balance");
      if (shown.startsWith(trai(privateReceiver + ATOMS))) paid = ATOMS;
      else if (shown.startsWith(trai(privateReceiver + 2n * ATOMS))) paid = 2n * ATOMS;
      else if (Date.now() > end) throw new Error(`the receiver reads ${shown}`);
      else await sleep(1000);
    }
    privateReceiver += paid;
    const held = heldAfterMoves - paid - FEE;
    await sender.privateBalance(held);
    await copy.privateBalance(held);
    await receiver.privateBalance(privateReceiver);
    // Several cuts later nothing else ran.
    await sleep(15_000);
    await sender.privateBalance(held, 20);
    await copy.privateBalance(held, 20);
    await receiver.privateBalance(privateReceiver, 20);
    open.splice(open.indexOf(copy), 1);
    await copy.close();
    return `${notes} notes before; the receiver got ${trai(paid)} once, the other statement was kept for review; both copies read ${trai(held)}`;
  });
  if (values.gateway) {
    await step("every private statement went through the gateway", async () => {
      const sent = sender.requests.filter((request) => /\/v1\/private\//.test(request.url));
      if (!sent.length || sent.some((request) => !request.url.includes(`:${GATEWAY_PORT}/`))) throw new Error(`${sent.length} sent, some elsewhere`);
      return `${sent.length} requests; asked to wait: sender ${sender.busy.length}, receiver ${receiver.busy.length} times`;
    });
    await step("the gateway never blocked the address", async () => {
      const { requests, refused, blocked } = gatewayRefusals(join(dataDir, "gateway.log"));
      if (blocked) throw new Error(`${blocked} requests blocked, ${refused} of ${requests} asked to wait`);
      return `${refused} of ${requests} requests asked to wait`;
    });
  }
  // A gateway without a proxy in front refuses a large body unread when the address is over its budget; a browser
  // sees a broken connection, and the wallet sends again. Behind the hosted API's nginx it gets the 429.
  const unread = (problem) => values.gateway && /ERR_CONNECTION_(ABORTED|RESET).*\/v1\/private\//.test(problem);
  const problems = open.flatMap((browser) => browser.problems.filter((problem) => !unread(problem)).map((problem) => `${browser.name}: ${problem}`));
  report("no script error or blocked resource in any page", problems.length === 0, problems.slice(0, 8).join(" | ").slice(0, 1500));
} catch (error) {
  if (error instanceof RunEnds) log(error.message);
  else report("the run", false, error?.message ?? String(error));
} finally {
  await closeAll();
  const passed = results.filter((result) => result.passed).length;
  log(`${passed} of ${results.length} passed in ${Math.round((Date.now() - started) / 1000)} s`);
  writeFileSync(values.out, `${JSON.stringify({ results }, null, 2)}\n`);
  process.exit(passed === results.length ? 0 : 1);
}
