import assert from "node:assert/strict";
import { join, resolve } from "node:path";
import { serveWallet } from "../wallet-server.mjs";
import { ProductionWalletPage, ProductionReferencePage } from "./pages.mjs";
import { openWallet } from "./sqlite-wallet.mjs";
import { privateSettled } from "./sdk-flows.mjs";
import { until, json } from "./processes.mjs";
import { verifyBrowserAssets } from "./browser-assets.mjs";

const ATOMS = 100_000_000n;

/** The browser signs/proves/persists. A separate genuine WASM recovery verifies monetary equations. */
export async function browserFlow({ sdk, wasm, client, network, directory, options, pagePort, step }) {
  const classic = options.mode === "classic";
  const Page = classic ? ProductionWalletPage : ProductionReferencePage;
  const pages = [];
  const recorded = new Set();
  let served, senderOracle, receiverOracle;
  const releaseEvidence = async (page) => {
    recorded.add(page);
    const evidence = join(directory, `${page.name}-loaded-assets.json`);
    const entry = { browser: page.name, evidence, verified: false };
    (network.browserAssets ??= []).push(entry);
    Object.assign(entry, await verifyBrowserAssets(page, { wallet: resolve(options.wallet), sdkDirectory: options.sdk,
      classic, evidence }), { verified: true });
  };
  const interrupted = () => { for (const page of pages) void page.close().catch(() => {}); };
  network.run.signal.addEventListener("abort", interrupted, { once: true });
  let phraseRoot, decoder;
  try {
    if (classic) {
      served = serveWallet({ root: resolve(options.wallet), port: pagePort, config: {
        api_url: options.apiUrl ?? network.url, node_url: network.url, price: false,
        send_when_not_synced: true, test_genesis_manifest: "genesis.json" }, genesis: network.manifestText });
    } else {
      network.run.start("reference-server", process.execPath, ["--experimental-strip-types", "server/main.ts"], {
        cwd: resolve(options.wallet), env: { PORT: String(pagePort), HOST: "127.0.0.1", TENRAI_API_URL: options.apiUrl ?? network.url,
          TENRAI_SEND_WHEN_NOT_SYNCED: "1", TENRAI_RATE_LIMIT: "0", TENRAI_TEST_GENESIS_MANIFEST: network.manifestPath } });
    }
    await until("real browser wallet server", 90_000, async () => {
      const answer = await fetch(`http://127.0.0.1:${pagePort}/`); await answer.body?.cancel(); return answer.ok;
    }, network.healthy);
    const open = async (name) => { const page = await Page.open(name, { chrome: options.chrome,
      url: `http://127.0.0.1:${pagePort}/`, shots: options.shots, profileRoot: directory, captureAssets: true }); pages.push(page); return page; };
    const sender = await open(`${options.mode}-sender`), receiver = await open(`${options.mode}-receiver`);
    senderOracle = await openWallet({ sdk, client, phrase: network.accounts.sender.phrase, directory: join(directory, "sender-observer"), network });
    receiverOracle = await openWallet({ sdk, client, phrase: network.accounts.receiver.phrase, directory: join(directory, "receiver-observer"), network });
    // The actual Wallet runtime has now checked and initialized its reviewed WASM binary.
    phraseRoot = new wasm.WalletRoot(wasm.mnemonicEntropy(network.accounts.sender.phrase), "");
    decoder = new wasm.PrivateScan(phraseRoot, "development", network.manifestText, 0, undefined, undefined);
    await step("both real browser wallets explicitly approve paid registration", async () => {
      if (classic) { await sender.restore(network.accounts.sender.phrase); await receiver.restore(network.accounts.receiver.phrase); }
      else { await sender.restorePrivate(network.accounts.sender.phrase); await receiver.restorePrivate(network.accounts.receiver.phrase); }
      await sender.publishPaid(); await receiver.publishPaid();
      assert.equal(sender.requests.filter((row) => /\/private\/registrations$/.test(row.url)).length, 1);
      assert.equal(receiver.requests.filter((row) => /\/private\/registrations$/.test(row.url)).length, 1);
    });
    await step("cancelling a real browser proof produces no executable POST or retained spend", async () => {
      const before = sender.requests.filter((entry) => /\/private\/(enters|transfers|exits)$/.test(entry.url)).length;
      await sender.cancelActualProof(ATOMS); await sender.proofReleased();
      assert.equal(sender.requests.filter((entry) => /\/private\/(enters|transfers|exits)$/.test(entry.url)).length, before);
      await privateSettled(senderOracle.notes, 0n, network.healthy); await sender.privateBalance(0n);
    });
    let held = 0n;
    for (const amount of [5n * ATOMS, 6n * ATOMS]) await step("browser ENTER uses real proof and complete public input reservation", async () => {
      const available = new Map((await senderOracle.wallet.outputs(client, { limit: 256 })).map((coin) => [`${coin.transactionId}:${coin.outputIndex}`, coin.amount]));
      const before = sender.requests.length; await sender.makePrivate(amount);
      const executed = await statement(sender, "enters", before);
      const spent = executed.info.inputs.reduce((sum, coin) => { const point = `${coin.transaction_id}:${coin.output_index}`;
        assert(available.has(point), "browser ENTER spent an unexpected public source"); return sum + available.get(point); }, 0n);
      const fee = BigInt(executed.view.details.fee); assert(fee > 0n); held += spent - fee;
      await privateSettled(senderOracle.notes, held, network.healthy); await sender.privateBalance(held);
    });
    const receiverAddress = await receiver.privateAddress();
    await step("browser private transfer and exact replay are checked by real validators", async () => {
      const before = sender.requests.length; await sender.payPrivate(ATOMS, receiverAddress);
      const executed = await statement(sender, "transfers", before); held -= ATOMS + BigInt(executed.view.details.fee);
      await privateSettled(senderOracle.notes, held, network.healthy); await privateSettled(receiverOracle.notes, ATOMS, network.healthy);
      await sender.privateBalance(held); await receiver.privateBalance(ATOMS);
      for (const url of network.urls) {
        const answer = await fetch(`${url}/v1/private/transfers`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ bytes: executed.frame }) });
        const problem = await answer.json(); assert.equal(answer.status, 409); assert.equal(problem.reason, "note_spent");
      }
      await privateSettled(receiverOracle.notes, ATOMS, network.healthy);
    });
    await step("browser reload restores disk journals and a fresh profile recovers from seed", async () => {
      await sender.goto(); await sender.unlock(); await sender.privateBalance(held);
      const fresh = await open(`${options.mode}-fresh-seed`);
      if (classic) await fresh.restore(network.accounts.sender.phrase); else await fresh.restorePrivate(network.accounts.sender.phrase);
      await fresh.privateBalance(held); await releaseEvidence(fresh); await fresh.close(); pages.splice(pages.indexOf(fresh), 1);
    });
    await step("browser EXIT pays out exactly its original private note less fee", async () => {
      const before = receiver.requests.length; await receiver.makeTransparent(network.accounts.receiver.address);
      const executed = await statement(receiver, "exits", before);
      assert.equal(BigInt(executed.view.details.exited) + BigInt(executed.view.details.fee), ATOMS);
      await privateSettled(receiverOracle.notes, 0n, network.healthy); await receiver.privateBalance(0n);
    });
    await step("no browser script/resource error and all proof workers physically end", async () => {
      for (const page of pages) { assert.deepEqual(page.problems, []); await page.proofReleased(); await releaseEvidence(page); }
    });
  } finally {
    network.run.signal.removeEventListener("abort", interrupted);
    for (const page of pages) {
      if (!recorded.has(page)) await releaseEvidence(page).catch(() => {}); // Preserve available response hashes on a failed flow too.
      await page.close();
    }
    if (served) await new Promise((done, fail) => served.close((error) => error ? fail(error) : done()));
    await receiverOracle?.close(); await senderOracle?.close(); decoder?.free(); phraseRoot?.free();
  }
  async function statement(page, route, after) {
    const found = page.requests.slice(after).find((entry) => entry.url.endsWith(`/v1/private/${route}`));
    assert(found?.body, "actual browser did not submit the complete private frame");
    const frame = JSON.parse(found.body).bytes; assert.match(frame, /^(?:[0-9a-f]{2})+$/);
    const info = JSON.parse(decoder.statementFrameInfo(frame));
    const view = await until("finalized original browser statement", 180_000, async () => {
      const answer = await json(`${network.url}/v1/private/statements/${info.digest}`);
      return answer.height !== null ? answer : false;
    }, network.healthy);
    assert(view.details); return { frame, info, view };
  }
}
