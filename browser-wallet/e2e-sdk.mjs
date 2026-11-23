// The SDK under Node with the native prover against a real node with private transfers active: two wallets from
// their phrases publish their accounts, one makes coins private, sends privately and pays out; a coin on its way stays
// out of an ordinary send, a statement sent twice runs once, and a third copy of the wallet finds the same balance.
//
//   node e2e-sdk.mjs --tenraid PATH\tenraid.exe --repo PATH\node-repo --sdk PATH\tenrai-sdk
//                    --prover PATH\tenrai-tsp2-prove.exe --data DIR [--out results.json]

import { spawn } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { openWallet } from "./production/sqlite-wallet.mjs";

const { values } = parseArgs({
  options: {
    tenraid: { type: "string" },
    repo: { type: "string" },
    sdk: { type: "string" },
    prover: { type: "string" },
    data: { type: "string" },
    out: { type: "string", default: "e2e-sdk-results.json" },
  },
});
const NODE_PORT = 38741;
const MINING_PORT = 38742;
const ATOMS = 100_000_000n;
const FUNDS = 420n * ATOMS;
const RECEIVER_COIN = 2n * ATOMS;
const FEE = 33_831_080n;
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const log = (line) => console.log(`${new Date().toISOString().slice(11, 19)} ${line}`);
const trai = (atoms) => `${atoms / ATOMS}.${String(atoms % ATOMS).padStart(8, "0")}`;

const sdk = resolve(values.sdk);
const { TenraiClient } = await import(pathToFileURL(join(sdk, "dist", "index.js")).href);
const walletSdk = await import(pathToFileURL(join(sdk, "dist", "wallet", "index.js")).href);
const { localProver, nativeProver, ProofFailure } = walletSdk;
const wasm = await import(pathToFileURL(join(sdk, "wasm", "tenrai_wasm.js")).href);
wasm.initSync({ module: readFileSync(join(sdk, "wasm", "tenrai_wasm_bg.wasm")) });

function account(marker) {
  const entropy = new Uint8Array(32).fill(marker);
  const root = new wasm.WalletRoot(entropy, "");
  const addresses = ["0", "1", "2"].map((index) => wasm.addressFromPublicKey(root.derivePublicKey("0", "receive", index), "development"));
  const phrase = wasm.mnemonicFromEntropy(entropy);
  root.free();
  return { phrase, addresses };
}

const sender = account(0x81);
const receiver = account(0x82);
const elsewhere = account(0x83);
const data = resolve(values.data);
rmSync(data, { recursive: true, force: true });
mkdirSync(data, { recursive: true });
const manifest = JSON.parse(readFileSync(resolve(values.repo, "config", "development.traidag.json"), "utf8"));
manifest.allocations[1].amount -= Number(FUNDS + RECEIVER_COIN);
for (const address of sender.addresses) manifest.allocations.push({ address, amount: Number(FUNDS / 3n) });
// Publishing a private account spends public coins; the receiver gets one for its fee.
manifest.allocations.push({ address: receiver.addresses[0], amount: Number(RECEIVER_COIN) });
manifest.upgrades = [{ height: 3, protocol_version: 3, params: "development-private" }];
const manifestText = `${JSON.stringify(manifest, null, 2)}\n`;
const manifestPath = join(data, "e2e.traidag.json");
writeFileSync(manifestPath, manifestText);
const logged = [];
const node = spawn(values.tenraid, [
  "--no-color", "run", "--manifest", manifestPath, "--dev-validator", "--data-dir", join(data, "node"),
  "--rpc-bind", `127.0.0.1:${NODE_PORT}`, "--mining-bind", `127.0.0.1:${MINING_PORT}`,
], { stdio: ["ignore", "pipe", "pipe"] });
node.stdout.on("data", (chunk) => logged.push(chunk));
node.stderr.on("data", (chunk) => logged.push(chunk));

const results = [];
const step = async (name, action) => {
  try {
    const detail = (await action()) ?? "";
    results.push({ name, passed: true, detail });
    log(`PASS ${name}${detail ? `: ${detail}` : ""}`);
  } catch (error) {
    results.push({ name, passed: false, detail: error?.message ?? String(error) });
    log(`FAIL ${name}: ${error?.message ?? String(error)}`);
  }
};

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
    await sleep(1500);
  }
}

/** Refreshes until what is on its way became final and the balance reads `atoms`. */
async function settled(notes, atoms) {
  return until(`the balance to read ${trai(atoms)}`, 180, async () => {
    const read = await notes.refresh();
    const outcomes = notes.onTheWay().length ? await notes.check(read.height) : [];
    const ended = outcomes.find((outcome) => outcome.outcome !== "final");
    if (ended) throw new Error(`the statement ended ${ended.outcome}: ${ended.reason ?? ""}`);
    return notes.onTheWay().length === 0 && read.balanceAtoms === atoms ? read : null;
  });
}

const prover = nativeProver({ path: values.prover });
const proving = { prover };

try {
  const client = new TenraiClient({ url: `http://127.0.0.1:${NODE_PORT}` });
  await until("private transfers to start", 120, async () => {
    try {
      return (await client.privateTransfers.status()).active;
    } catch {
      return false;
    }
  });
  // Each wallet keeps its state in a folder of its own, as an application on disk does; one folder holds one session.
  const network = { manifestText, manifest };
  const open = (who, folder) => openWallet({ sdk: walletSdk, client, phrase: who.phrase, directory: join(data, "wallets", folder), network });
  let first = await open(sender, "sender");
  const second = await open(receiver, "receiver");
  let firstFee = 0n;
  // The rules are active before the first private cut is final, which the notes are read from.
  await until("the first private cut", 120, async () => {
    await first.notes.refresh();
    await second.notes.refresh();
    return true;
  });
  let held = 0n;

  await step("the wallet says what stands in the way before any proof", async () => {
    if (await first.notes.usability() !== "ready") throw new Error(`usability on a test network: ${await first.notes.usability()}`);
    await first.notes.refresh();
    if (await first.notes.registered()) throw new Error("registered before the registration");
    await first.notes.transfer({ to: first.notes.address, amount: ATOMS }, proving).then(
      () => { throw new Error("a transfer went out unregistered"); },
      (error) => { if (!/regist|account/i.test(error.message)) throw error; },
    );
  });
  await step("both accounts register", async () => {
    // A registration pays its fee from public coins up to a reviewed maximum.
    for (const who of [first, second]) {
      const quote = await who.notes.planRegistration({ maximumFeeAtoms: ATOMS, change: { branch: "receive", index: 0 } });
      if (quote.feeAtoms <= 0n || quote.feeAtoms > ATOMS) throw new Error(`a registration fee of ${trai(quote.feeAtoms)}`);
      if (who === first) {
        // The wallet verifies a newer head before it signs, as when a person reads the review while the chain moves on;
        // the registration is signed at that head with the reviewed facts.
        await until("a head two cuts past the quote", 60, async () =>
          (await (await fetch(`http://127.0.0.1:${NODE_PORT}/v1/chain/head`)).json()).height >= quote.anchorHeight + 2);
        await who.notes.refresh();
      }
      const sent = await who.notes.register(quote);
      if (sent.feeAtoms !== String(quote.feeAtoms)) throw new Error(`signed a fee of ${sent.feeAtoms}, reviewed ${quote.feeAtoms}`);
      if (who === first) firstFee = quote.feeAtoms;
    }
    await until("both registrations", 120, async () => {
      await first.notes.refresh();
      await second.notes.refresh();
      return (await first.notes.registered()) && (await second.notes.registered());
    });
    // A paid registration is a statement on its way until a check finds it final.
    await settled(first.notes, 0n);
    await settled(second.notes, 0n);
  });
  await step("localProver takes the native program under Node", async () => {
    const chosen = localProver({ path: values.prover });
    if (typeof chosen.prove !== "function") throw new Error("no prover");
  });
  await step("a cancelled proof holds nothing", async () => {
    const stop = new AbortController();
    // Cancelled once the proof runs; before it, the wallet first reads its coins and its notes up to the newest head.
    const cancelled = { prove: (input, options) => { setTimeout(() => stop.abort(), 1500); return prover.prove(input, options); } };
    const running = first.notes.enter({ amount: 5n * ATOMS }, { prover: cancelled, signal: stop.signal });
    await running.then(
      () => { throw new Error("the proof ran on"); },
      (error) => { if (!(error instanceof ProofFailure) || error.kind !== "cancelled") throw error; },
    );
    if (first.notes.onTheWay().length) throw new Error("a statement stays on its way");
  });
  await step("an ENTER with a native proof, its coin held meanwhile", async () => {
    const started = Date.now();
    const entry = await first.notes.enter({ amount: 100n * ATOMS }, proving);
    const coin = entry.inputs[0];
    if (!first.notes.holdsCoin(coin)) throw new Error("the coin is not held");
    const free = await first.wallet.outputs(client);
    if (free.some((output) => `${output.transactionId}:${output.outputIndex}` === coin)) throw new Error("an ordinary send could take the coin");
    held = FUNDS / 3n - FEE;
    const read = await settled(first.notes, held);
    // The ENTER took a whole coin of 140 TRAI, the largest; the registration's fee left before it.
    await until("the transparent balance", 60, async () => (await first.wallet.balance(client)).atoms === FUNDS - firstFee - FUNDS / 3n);
    // No block holds an ENTER's transaction, and the node still answers for it.
    const id = (await first.wallet.activity(client, { limit: 1 }))[0]?.transactionId;
    const answer = await until("the ENTER's transaction", 30, async () => {
      const reply = await fetch(`http://127.0.0.1:${NODE_PORT}/v1/transactions/${id}`);
      return reply.ok ? reply.json() : null;
    });
    if (answer.kind !== "private_enter" || answer.operation?.data?.statement_digest !== entry.digest) throw new Error(`the node says ${JSON.stringify(answer).slice(0, 300)}`);
    if (BigInt(answer.fee) !== FEE || !answer.inputs[0]?.spends) throw new Error(`fee ${answer.fee}, inputs ${JSON.stringify(answer.inputs).slice(0, 200)}`);
    const status = await (await fetch(`http://127.0.0.1:${NODE_PORT}/v1/transactions/${id}/status`)).json();
    if (status.lifecycle !== "finalized" || !status.finalized_cut || status.finalized_cut.height !== answer.status.finalized_cut?.height) throw new Error(`status ${JSON.stringify(status).slice(0, 300)}`);
    return `${Math.round((Date.now() - started) / 1000)} s, ${read.notes.length} notes, private ${trai(held)}`;
  });
  let frame;
  await step("a private send reaches the second wallet", async () => {
    const started = Date.now();
    const entry = await first.notes.transfer({ to: second.notes.address, amount: 10n * ATOMS }, proving);
    frame = entry.frameHex;
    held -= 10n * ATOMS + FEE;
    await settled(first.notes, held);
    const got = await until("the payment", 120, async () => {
      const seen = await second.notes.refresh();
      return seen.balanceAtoms === 10n * ATOMS ? seen : null;
    });
    if (got.received.length !== 1 || BigInt(got.received[0].amountAtoms) !== 10n * ATOMS) throw new Error("no payment told");
    return `${Math.round((Date.now() - started) / 1000)} s, sender ${trai(held)}`;
  });
  await step("the same statement sent again is known as spent", async () => {
    if (!frame) throw new Error("no statement was sent before");
    const answer = await fetch(`http://127.0.0.1:${NODE_PORT}/v1/private/transfers`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ bytes: frame }),
    });
    const body = await answer.json();
    if (answer.status !== 409 || body.reason !== "note_spent") throw new Error(`${answer.status} ${JSON.stringify(body)}`);
    return `409 ${body.reason}`;
  });
  await step("an EXIT pays a note out", async () => {
    const read = await second.notes.refresh();
    if (!read.notes.length) throw new Error("the second wallet holds no note");
    await second.notes.exit({ to: elsewhere.addresses[0], notes: [read.notes[0].note] }, proving);
    await settled(second.notes, 0n);
    const paid = await (await fetch(`http://127.0.0.1:${NODE_PORT}/v1/address/${elsewhere.addresses[0]}/balance`)).json();
    if (BigInt(paid.balance) !== 10n * ATOMS - FEE) throw new Error(`the address holds ${paid.balance}`);
    return `paid ${trai(10n * ATOMS - FEE)}`;
  });
  await step("the sealed state and a new copy from the phrase find the same balance", async () => {
    // The sender's session ends and opens again from its folder; a new copy starts from the phrase alone.
    await first.close();
    first = await open(sender, "sender");
    const saved = await first.notes.refresh();
    if (saved.balanceAtoms !== held) throw new Error(`the saved state reads ${trai(saved.balanceAtoms)}`);
    const fresh = await open(sender, "sender-copy");
    const read = await fresh.notes.refresh();
    if (read.balanceAtoms !== held) throw new Error(`a new copy reads ${trai(read.balanceAtoms)}`);
    const rescanned = await fresh.notes.refresh({ rescan: true });
    if (rescanned.balanceAtoms !== held) throw new Error(`a rescan reads ${trai(rescanned.balanceAtoms)}`);
    await fresh.close();
    return trai(held);
  });
  await step("two copies of one wallet send the same notes at once, while reads run, and one statement runs", async () => {
    // A new ENTER makes two notes; the note from before leaves, so both copies must take these two.
    const older = (await first.notes.refresh()).notes;
    await first.notes.enter({ amount: 5n * ATOMS }, proving);
    held += FUNDS / 3n - FEE;
    await settled(first.notes, held);
    await first.notes.exit({ to: elsewhere.addresses[0], notes: older.map((note) => note.note) }, proving);
    held -= older.reduce((sum, note) => sum + note.valueAtoms, 0n);
    const read = await settled(first.notes, held);
    if (read.notes.length !== 2) throw new Error(`${read.notes.length} notes`);
    const twin = await open(sender, "sender-twin");
    await twin.notes.refresh();
    let proving_ = true;
    const reads = (async () => {
      let count = 0;
      while (proving_) {
        await first.notes.refresh();
        await first.notes.registered(second.notes.address);
        count += 1;
        await sleep(300);
      }
      return count;
    })();
    const outcomes = await Promise.allSettled([
      first.notes.transfer({ to: second.notes.address, amount: ATOMS }, proving),
      twin.notes.transfer({ to: second.notes.address, amount: 2n * ATOMS }, proving),
    ]);
    proving_ = false;
    const count = await reads;
    const taken = outcomes.filter((outcome) => outcome.status === "fulfilled");
    const refused = outcomes.filter((outcome) => outcome.status === "rejected");
    if (taken.length !== 1 || refused.length !== 1) throw new Error(`taken ${taken.length}, refused ${refused.length}: ${refused.map((outcome) => outcome.reason?.message).join("; ")}`);
    if (!["note_pooled", "note_spent"].includes(refused[0].reason?.reason)) throw new Error(`refused for ${refused[0].reason?.reason}: ${refused[0].reason?.message}`);
    const winner = outcomes[0].status === "fulfilled" ? first : twin;
    const loser = winner === first ? twin : first;
    const paid = winner === first ? ATOMS : 2n * ATOMS;
    held -= paid + FEE;
    await settled(winner.notes, held);
    // The loser's wallet keeps the refused statement for review: a refusal cannot rule out that an earlier send of
    // the same statement ran. Its notes are spent by the winner, so nothing of the balance stays held.
    await until(`the refused copy's balance to read ${trai(held)}`, 180, async () => (await loser.notes.refresh()).balanceAtoms === held);
    const kept = loser.notes.onTheWay();
    if (kept.length !== 1 || kept[0].stage !== "needs_review") throw new Error(`the refused statement is ${kept.map((send) => send.stage).join(", ") || "gone"}`);
    await until("the one payment", 120, async () => (await second.notes.refresh()).balanceAtoms === paid);
    if (winner.notes.onTheWay().length) throw new Error("the statement that ran stays on its way");
    await twin.close();
    return `${count} reads during the proofs; refused with ${refused[0].reason.reason} and kept for review; the receiver got ${trai(paid)} once`;
  });
  await first.close();
  await second.close();
} catch (error) {
  results.push({ name: "the run", passed: false, detail: error?.message ?? String(error) });
  log(`FAIL the run: ${error?.message ?? String(error)}`);
} finally {
  node.kill();
  writeFileSync(join(data, "node.log"), Buffer.concat(logged));
  const passed = results.filter((result) => result.passed).length;
  log(`${passed} of ${results.length} passed`);
  writeFileSync(values.out, `${JSON.stringify({ results }, null, 2)}\n`);
  process.exit(passed === results.length ? 0 : 1);
}
