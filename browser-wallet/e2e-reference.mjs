// The reference web wallet in a real browser against a real node with private transfers active: the SDK's
// way of spending private notes, with its server relaying and hosting the prover, through the page's own
// forms. Two wallets from their recovery phrases switch the option on, publish their accounts, make coins
// private, send, pay out; a reload and a fresh browser find the same balances.
//
//   node e2e-reference.mjs --tenraid PATH\tenraid.exe --repo PATH\node-repo --wallet PATH\tenrai-reference-webwallet
//                          --data DIR [--gateway PATH\tenrai-api.exe] [--gallery] [--shots DIR] [--out results.json]
//
// Run `npm run build` in the wallet first.

import { spawn } from "node:child_process";
import { createWriteStream, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { browserExecutable } from "./browser-executable.mjs";
import { RunEnds, gatewayRefusals, sleep } from "./browser.mjs";
import { ReferencePage } from "./reference-page.mjs";

const { values } = parseArgs({
  options: {
    tenraid: { type: "string" },
    repo: { type: "string" },
    wallet: { type: "string" },
    data: { type: "string" },
    chrome: { type: "string", default: browserExecutable("chrome") },
    gallery: { type: "boolean", default: false },
    gateway: { type: "string" },
    shots: { type: "string" },
    out: { type: "string", default: "e2e-reference-results.json" },
  },
});
const NODE_PORT = 38721;
const MINING_PORT = 38722;
const PAGE_PORT = 38784;
const GATEWAY_PORT = 38723;
const ATOMS = 100_000_000n;
const PASSWORD = "correct horse battery staple";
const FUNDS = 420n * ATOMS;
const RECEIVER_COIN = 2n * ATOMS;
const FEE = 33_831_080n;
const trai = (atoms) => `${atoms / ATOMS}.${String(atoms % ATOMS).padStart(8, "0")}`;
/** What the node holds for these addresses together. */
const publicBalance = async (addresses) => {
  let total = 0n;
  for (const address of addresses) {
    total += BigInt((await (await fetch(`http://127.0.0.1:${NODE_PORT}/v1/address/${address}/balance`)).json()).balance);
  }
  return total;
};
const log = (line) => console.log(`${new Date().toISOString().slice(11, 19)} ${line}`);
const startPage = (name) =>
  ReferencePage.open(name, { chrome: values.chrome, url: `http://127.0.0.1:${PAGE_PORT}/`, shots: values.shots });

const results = [];
function report(name, passed, detail = "") {
  results.push({ name, passed, detail });
  log(`${passed ? "PASS" : "FAIL"} ${name}${detail ? `: ${detail}` : ""}`);
}

async function wallets() {
  const folder = resolve(values.wallet, "vendor", "tenrai-sdk", "wasm");
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
  return { sender: wallet(0x81), receiver: wallet(0x82), elsewhere: wallet(0x83) };
}

const started = Date.now();
const accounts = await wallets();
const dataDir = resolve(values.data);
rmSync(dataDir, { recursive: true, force: true });
mkdirSync(dataDir, { recursive: true });
const manifestPath = join(dataDir, "e2e.traidag.json");
const manifest = JSON.parse(readFileSync(resolve(values.repo, "config", "development.traidag.json"), "utf8"));
const coin = FUNDS / 3n;
manifest.allocations[1].amount -= Number(FUNDS + RECEIVER_COIN);
for (const address of accounts.sender.addresses) manifest.allocations.push({ address, amount: Number(coin) });
// Publishing a private account spends public coins; the receiver gets one for its fee.
manifest.allocations.push({ address: accounts.receiver.address, amount: Number(RECEIVER_COIN) });
manifest.upgrades = [{ height: 3, protocol_version: 3, params: "development-private" }];
writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);

const logged = [];
const node = spawn(values.tenraid, [
  "--no-color", "run", "--manifest", manifestPath, "--dev-validator", "--data-dir", join(dataDir, "node"),
  "--rpc-bind", `127.0.0.1:${NODE_PORT}`, "--mining-bind", `127.0.0.1:${MINING_PORT}`,
], { stdio: ["ignore", "pipe", "pipe"] });
node.stdout.on("data", (chunk) => logged.push(chunk));
node.stderr.on("data", (chunk) => logged.push(chunk));
const serverLog = [];
let server;
let gateway;

const open = [];
async function step(name, action) {
  try {
    report(name, true, (await action()) ?? "");
  } catch (error) {
    report(name, false, error?.message ?? String(error));
    for (const browser of open) await browser.screenshot(`failed-${name}-${browser.name}`).catch(() => {});
  }
}

try {
  for (let attempt = 0; ; attempt += 1) {
    try {
      if ((await (await fetch(`http://127.0.0.1:${NODE_PORT}/v1/private/status`)).json()).active) break;
    } catch {}
    if (attempt > 240) throw new Error(`private transfers never started: ${Buffer.concat(logged).toString("utf8").slice(-400)}`);
    await sleep(500);
  }
  if (values.gateway) {
    const { chain_id: chainId } = await (await fetch(`http://127.0.0.1:${NODE_PORT}/v1/node/info`)).json();
    gateway = spawn(values.gateway, [
      "serve", "--network", "development", "--chain-id", chainId, "--bind", `127.0.0.1:${GATEWAY_PORT}`,
      "--node-url", `http://127.0.0.1:${NODE_PORT}`, "--node-trust-file", join(dataDir, "gateway-trusted-node"),
      "--history-file", join(dataDir, "gateway-history.json"),
    ], { cwd: dataDir, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, RUST_LOG: "tenrai_api=debug" } });
    const gatewayLog = createWriteStream(join(dataDir, "gateway.log"));
    gateway.stdout.pipe(gatewayLog);
    gateway.stderr.pipe(gatewayLog);
    for (let attempt = 0; ; attempt += 1) {
      try {
        if ((await fetch(`http://127.0.0.1:${GATEWAY_PORT}/v1/private/status`)).ok) break;
      } catch {}
      if (attempt > 120) throw new Error("the gateway never answered");
      await sleep(500);
    }
    log("gateway up, the wallet's server talks to it");
  }
  server = spawn(process.execPath, ["--experimental-strip-types", "server/main.ts"], {
    cwd: values.wallet,
    stdio: ["ignore", "pipe", "pipe"],
    env: {
      ...process.env,
      PORT: String(PAGE_PORT),
      HOST: "127.0.0.1",
      TENRAI_API_URL: `http://127.0.0.1:${values.gateway ? GATEWAY_PORT : NODE_PORT}`,
      TENRAI_SEND_WHEN_NOT_SYNCED: "1",
      TENRAI_RATE_LIMIT: "0",
      TENRAI_TEST_GENESIS_MANIFEST: manifestPath,
    },
  });
  server.stdout.on("data", (chunk) => serverLog.push(chunk));
  server.stderr.on("data", (chunk) => serverLog.push(chunk));
  for (let attempt = 0; ; attempt += 1) {
    try {
      if ((await fetch(`http://127.0.0.1:${PAGE_PORT}/`)).ok) break;
    } catch {}
    if (attempt > 60) throw new Error(`the server did not start: ${Buffer.concat(serverLog).toString("utf8").slice(-600)}`);
    await sleep(500);
  }
  const sender = await startPage("sender");
  const receiver = await startPage("receiver");
  open.push(sender, receiver);
  let privateSender = 0n;
  let privateReceiver = 0n;
  let publicSender = FUNDS;
  let receiverAddress = "";

  await step("restore both wallets; nothing private shows until the option is on", async () => {
    await sender.restore(accounts.sender.phrase);
    await receiver.restore(accounts.receiver.phrase);
    await sender.until(`[...document.querySelectorAll('.tabs button')].length >= 3`, 30, "the tabs");
    if (await sender.eval(`[...document.querySelectorAll('.tabs button')].some((button) => button.textContent.includes('Private'))`)) throw new Error("the Private tab shows while its option is off");
    if (await sender.eval(sender.says("Timelocks"))) throw new Error("timelocks show while their option is off");
    await sender.screenshot("reference-option-off");
  });
  await step("both wallets publish their private accounts", async () => {
    await sender.enablePrivate();
    await receiver.enablePrivate();
    // The sender reads its review for longer than a cut takes: the wallet signs at the newer head with the same facts.
    await sender.publish("1", { review: 12_000 });
    await receiver.publish();
    await sender.until(`(() => { const button = ${sender.button("Send privately")}; return Boolean(button) && !button.disabled; })()`, 180, "the sender's registration");
    await receiver.until(`(() => { const button = ${receiver.button("Send privately")}; return Boolean(button) && !button.disabled; })()`, 180, "the receiver's registration");
    // The sender paid its fee from its largest coin and took the change back; the node says what is left.
    publicSender = await publicBalance(accounts.sender.addresses);
    const paid = FUNDS - publicSender;
    if (paid <= 0n || paid > ATOMS) throw new Error(`the registration cost ${trai(paid)}, not more than nothing and at most 1 TRAI`);
    await sender.screenshot("reference-registered");
    return `fee ${trai(paid)}`;
  });
  await step("an ENTER with a proof made in the browser", async () => {
    await sender.press("Make private");
    await sender.type("Amount (TRAI)", trai(100n * ATOMS));
    await sender.press("Review and sign");
    await sender.approve();
    await sender.noneOnWay();
    privateSender = coin - FEE;
    await sender.privateBalance(privateSender);
    await sender.screenshot("reference-after-enter");
    await sender.tab("Wallet");
    await sender.until(sender.says("Private balance"), 10, "the line under the balance");
    // The coin the ENTER spent, a whole one of 140 TRAI, leaves the balance at once, and the cut on the page goes on.
    const left = trai(publicSender - coin);
    await sender.until(`(document.querySelector('.hero__amount')?.textContent ?? '').startsWith(${JSON.stringify(left)})`, 30, `the balance to read ${left}`);
    const shownCut = () => sender.eval("Number((document.querySelector('.hero__sub:last-of-type')?.textContent ?? '').replace(/[^0-9]/g, ''))");
    const before = await shownCut();
    await sender.until(`Number((document.querySelector('.hero__sub:last-of-type')?.textContent ?? '').replace(/[^0-9]/g, '')) > ${before}`, 40, "the shown cut to go on");
    await sender.screenshot("reference-wallet-tab");
    await sender.tab("Private");
    return `private ${trai(privateSender)}, the page holds ${await sender.proofReleased()} MiB afterwards`;
  });
  await step("a private send to the second wallet", async () => {
    await receiver.press("Receive");
    await receiver.until("Boolean(document.querySelector('dialog[open] code.address-box'))", 10, "the private address");
    receiverAddress = await receiver.eval("document.querySelector('dialog[open] code.address-box').textContent");
    await receiver.screenshot("reference-receive");
    await receiver.eval("document.querySelector('dialog[open]').close()");
    await sender.press("Send privately");
    await sender.type("Private address of the recipient", receiverAddress);
    await sender.type("Amount (TRAI)", trai(10n * ATOMS));
    await sender.press("Review and sign");
    await sender.approve();
    await sender.noneOnWay();
    privateSender -= 10n * ATOMS + FEE;
    privateReceiver += 10n * ATOMS;
    await sender.privateBalance(privateSender);
    await receiver.privateBalance(privateReceiver);
    await receiver.screenshot("reference-after-send");
    return `sender ${trai(privateSender)}, receiver ${trai(privateReceiver)}`;
  });
  if (values.gallery) {
    await sender.gallery("after-send");
    await receiver.gallery("receiver");
    throw new RunEnds("the pictures are taken");
  }
  await step("an address without a private account is refused with a clear reason", async () => {
    await sender.tab("Private");
    await sender.press("Make private");
    await sender.type("Amount (TRAI)", trai(ATOMS));
    await sender.type("Private address", accounts.elsewhere.address);
    await sender.press("Review and sign");
    await sender.until(sender.says("no private account yet"), 60, "the refusal");
    await sender.eval("document.querySelector('dialog[open]').close()");
  });
  await step("an EXIT to a transparent address", async () => {
    await receiver.press("Make transparent");
    await receiver.until("document.querySelectorAll('dialog[open] label.check input').length > 0", 30, "the notes");
    await receiver.eval("document.querySelector('dialog[open] label.check input').click()");
    await receiver.type("Transparent address", accounts.elsewhere.address);
    await receiver.press("Review and sign");
    await receiver.approve();
    await receiver.noneOnWay();
    privateReceiver = 0n;
    await receiver.privateBalance(privateReceiver);
    const answer = await (await fetch(`http://127.0.0.1:${NODE_PORT}/v1/address/${accounts.elsewhere.address}/balance`)).json();
    if (BigInt(answer.balance) !== 10n * ATOMS - FEE) throw new Error(`the address holds ${answer.balance}`);
    return `paid ${trai(10n * ATOMS - FEE)}`;
  });
  await step("a cancel during the proof holds nothing", async () => {
    await sender.press("Make private");
    await sender.type("Amount (TRAI)", trai(5n * ATOMS));
    await sender.press("Review and sign");
    await sender.until(sender.says("The proof is made on this device"), 90, "the review");
    if (await sender.eval(`Boolean([...document.querySelectorAll('dialog[open] input[type=password]')].length)`)) await sender.type("Password", PASSWORD);
    await sender.press("Make the proof and send", 10);
    await sender.until(sender.says("Proof on this device"), 30, "the proof to start");
    await sleep(3000);
    await sender.press("Cancel the proof", 10);
    await sender.until(`!(${sender.says("Proof on this device")})`, 30, "the proof to end");
    await sender.privateBalance(privateSender);
    return `the page holds ${await sender.proofReleased()} MiB afterwards`;
  });
  await step("a reload restores every balance", async () => {
    await sender.goto();
    await sender.unlock();
    await sender.privateBalance(privateSender);
  });
  await step("a fresh browser restores the same balance from the phrase alone", async () => {
    const restored = await startPage("restored");
    open.push(restored);
    await restored.restore(accounts.sender.phrase);
    await restored.enablePrivate();
    await restored.privateBalance(privateSender);
    open.splice(open.indexOf(restored), 1);
    await restored.close();
  });
  await step("the option goes off, the tab goes with it, and on again the balance is back", async () => {
    await sender.disablePrivate();
    await sender.enablePrivate();
    await sender.privateBalance(privateSender);
  });
  await step("the notes move on during a proof, and the statement runs from its older anchor", async () => {
    await sender.press("Make private");
    await sender.type("Amount (TRAI)", trai(5n * ATOMS));
    await sender.press("Review and sign");
    await sender.approve();
    await sender.noneOnWay();
    privateSender += coin - FEE;
    await sender.privateBalance(privateSender);
    await sender.sendPrivately(receiverAddress, ATOMS);
    let checks = 0;
    while (await sender.eval(sender.says("Proof on this device"))) {
      await sender.eval(`${sender.button("Check again")}?.click()`);
      checks += 1;
      await sleep(2000);
    }
    await sender.noneOnWay();
    privateSender -= ATOMS + FEE;
    privateReceiver += ATOMS;
    await sender.privateBalance(privateSender);
    await receiver.privateBalance(privateReceiver);
    return `${checks} checks during the proof`;
  });
  await step("two copies of one wallet send the same notes at the same moment, and one statement runs", async () => {
    const twin = await startPage("twin");
    open.push(twin);
    await twin.restore(accounts.sender.phrase);
    await twin.enablePrivate();
    await twin.privateBalance(privateSender);
    // The same amount makes both take the same two notes. Each statement is held back, so both go out together.
    for (const page of [sender, twin]) {
      await page.holdBack(true);
      await page.tab("Private");
      await page.sendPrivately(receiverAddress, ATOMS);
      await page.until(`!(${page.says("Proof on this device")})`, 700, "the proof to end");
      await page.until("document.querySelectorAll('[data-on-way]').length === 1", 60, "the statement to be kept");
      await sleep(3000);
    }
    await Promise.all([sender.holdBack(false), twin.holdBack(false)]);
    // One statement runs. The node refuses the other, and its wallet keeps it for review: a refusal cannot rule out
    // that an earlier send of the same statement ran.
    const listed = (page) => page.eval("document.querySelectorAll('[data-on-way]').length");
    const kept = (page) => page.eval("[...document.querySelectorAll('[data-on-way]')].filter((row) => /spends a note of this one|already spent/i.test(row.textContent)).length");
    const settled = Date.now() + 300_000;
    for (;;) {
      const onWay = (await listed(sender)) + (await listed(twin));
      const forReview = (await kept(sender)) + (await kept(twin));
      if (onWay === 1 && forReview === 1) break;
      if (Date.now() > settled) throw new Error(`${onWay} statements listed on their way, ${forReview} kept for review; notices: ${await sender.notices()} / ${await twin.notices()}`);
      await sleep(1000);
    }
    privateSender -= ATOMS + FEE;
    privateReceiver += ATOMS;
    await sender.privateBalance(privateSender);
    await twin.privateBalance(privateSender);
    await receiver.privateBalance(privateReceiver);
    await sleep(15_000);
    await sender.privateBalance(privateSender, 20);
    await twin.privateBalance(privateSender, 20);
    await receiver.privateBalance(privateReceiver, 20);
    open.splice(open.indexOf(twin), 1);
    await twin.close();
    return `the receiver got ${trai(ATOMS)} once, the other statement was kept for review; both copies read ${trai(privateSender)}`;
  });
  if (values.gateway) {
    await step("the gateway never blocked the address", async () => {
      const { requests, refused, blocked } = gatewayRefusals(join(dataDir, "gateway.log"));
      if (blocked) throw new Error(`${blocked} requests blocked, ${refused} of ${requests} asked to wait`);
      return `${refused} of ${requests} requests asked to wait`;
    });
  }
  const problems = open.flatMap((browser) => browser.problems.map((problem) => `${browser.name}: ${problem}`));
  report("no script error or blocked resource in any page", problems.length === 0, problems.slice(0, 6).join(" | ").slice(0, 1200));
} catch (error) {
  if (error instanceof RunEnds) log(error.message);
  else report("the run", false, error?.message ?? String(error));
} finally {
  for (const browser of open) await browser.close().catch(() => {});
  server?.kill();
  gateway?.kill();
  node.kill();
  writeFileSync(join(dataDir, "node.log"), Buffer.concat(logged));
  writeFileSync(join(dataDir, "server.log"), Buffer.concat(serverLog));
  const passed = results.filter((result) => result.passed).length;
  log(`${passed} of ${results.length} passed in ${Math.round((Date.now() - started) / 1000)} s`);
  writeFileSync(values.out, `${JSON.stringify({ results }, null, 2)}\n`);
  process.exit(passed === results.length ? 0 : 1);
}
