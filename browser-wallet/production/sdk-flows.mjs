import assert from "node:assert/strict";
import { join } from "node:path";
import { openWallet } from "./sqlite-wallet.mjs";
import { until } from "./processes.mjs";

const ATOMS = 100_000_000n;
const key = (coin) => `${coin.transactionId}:${coin.outputIndex}`;

export async function privateSettled(notes, expected, healthy) {
  return until(`certified private balance ${expected}`, 180_000, async () => {
    const read = await notes.refresh();
    const outcomes = await notes.check(read.height);
    assert(outcomes.every((entry) => entry.outcome === "final"), "an original private send ended without certified execution");
    return notes.onTheWay().length === 0 && read.balanceAtoms === expected ? read : false;
  }, healthy);
}

export async function publicBalance(wallet, client, expected, healthy) {
  return until(`complete public balance ${expected}`, 180_000, async () => (await wallet.balance(client)).atoms === expected, healthy);
}

export async function paidRegistration(owner, client, network) {
  const before = (await owner.wallet.balance(client)).atoms;
  const cap = 2n * ATOMS; // Explicit approval policy for this funded synthetic fixture.
  const quote = await owner.notes.planRegistration({ maximumFeeAtoms: cap, change: { branch: "receive", index: 0 } });
  assert(quote.feeAtoms > 0n && quote.feeAtoms <= cap);
  assert.equal(quote.maximumFeeAtoms, cap);
  assert.equal(quote.inputs.reduce((sum, coin) => sum + coin.amountAtoms, 0n), quote.feeAtoms + quote.changeAtoms);
  const send = await owner.notes.register(quote);
  assert.equal(send.kind, "private_register"); assert.equal(send.digest, quote.transactionId);
  assert.match(send.registration.frameId, /^[0-9a-f]{96}$/);
  assert.notEqual(quote.authorizationId, send.registration.frameId);
  assert.notEqual(send.digest, send.registration.frameId, "funding TXID and complete frame ID have distinct domains");
  for (const coin of send.inputs) assert(owner.notes.holdsCoin(coin), "funding inputs remain reserved before certified execution");
  await until("genuine registration execution evidence", 180_000, async () => {
    const outcomes = await owner.notes.checkRegistrations();
    assert(outcomes.every((entry) => entry.outcome === "final"));
    return !owner.notes.onTheWay().some((entry) => entry.digest === send.digest) && await owner.notes.registrationStatus();
  }, network.healthy);
  await publicBalance(owner.wallet, client, before - quote.feeAtoms, network.healthy);
  await Promise.all(network.urls.map(async (url) => {
    const peer = new client.constructor({ url, target: "node" });
    const state = await until("registration funding on every validator", 180_000, async () => {
      const answer = await peer.transactions.status(send.digest); return answer.lifecycle === "finalized" ? answer : false;
    }, network.healthy);
    assert.equal(state.observed_work ?? null, null, "private funding must not invent a work block");
  }));
  return send;
}

export async function sdkPrivateFlow({ sdk, client, network, directory, proverPath, step }) {
  let sender, receiver, copy;
  const make = (who, name) => openWallet({ sdk, client, phrase: who.phrase, directory: join(directory, name), network });
  try {
    sender = await make(network.accounts.sender, "sender-sdk");
    receiver = await make(network.accounts.receiver, "receiver-sdk");
    await step("paid registration conserves funding and verifies execution on all three validators", async () => {
      await paidRegistration(sender, client, network); await paidRegistration(receiver, client, network);
    });
    const proving = { prover: sdk.nativeProver({ path: proverPath }), signal: network.run.signal };
    await step("cancelling an actual native proof frees only the unexported preparation", async () => {
      const before = (await sender.wallet.outputs(client, { limit: 256 })).map(key).sort();
      const stop = new AbortController(); let invoked = false, timer;
      const cancelling = { prove(input, options) { invoked = true; timer = setTimeout(() => stop.abort(), 500); return proving.prover.prove(input, options); } };
      try { await assert.rejects(sender.notes.enter({ amount: ATOMS }, { prover: cancelling, signal: AbortSignal.any([stop.signal, network.run.signal]) }),
        (error) => error instanceof sdk.ProofFailure && error.kind === "cancelled"); }
      finally { clearTimeout(timer); }
      assert(invoked, "the regression must start the actual native prover path");
      assert.equal(sender.notes.onTheWay().length, 0);
      assert.deepEqual((await sender.wallet.outputs(client, { limit: 256 })).map(key).sort(), before);
    });
    let privateAtoms = 0n;
    let publicAtoms = (await sender.wallet.balance(client)).atoms;
    for (const amount of [5n * ATOMS, 6n * ATOMS]) {
      await step("ENTER reserves public inputs and preserves the exact value equation", async () => {
        const available = new Map((await sender.wallet.outputs(client, { limit: 256 })).map((coin) => [key(coin), coin.amount]));
        const send = await sender.notes.enter({ amount }, proving);
        const input = send.inputs.reduce((sum, point) => { assert(available.has(point)); return sum + available.get(point); }, 0n);
        assert(input > BigInt(send.feeAtoms));
        for (const point of send.inputs) assert(sender.notes.holdsCoin(point));
        const remaining = await sender.wallet.outputs(client, { limit: 256 });
        assert(remaining.every((coin) => !send.inputs.includes(key(coin))));
        privateAtoms += input - BigInt(send.feeAtoms); publicAtoms -= input;
        await privateSettled(sender.notes, privateAtoms, network.healthy);
        await publicBalance(sender.wallet, client, publicAtoms, network.healthy);
      });
    }
    await step("private TRANSFER conserves value and a finalized replay cannot execute twice", async () => {
      assert.equal((await receiver.notes.refresh()).balanceAtoms, 0n);
      const send = await sender.notes.transfer({ to: receiver.notes.address, amount: ATOMS }, proving);
      privateAtoms -= ATOMS + BigInt(send.feeAtoms);
      await privateSettled(sender.notes, privateAtoms, network.healthy);
      await privateSettled(receiver.notes, ATOMS, network.healthy);
      for (const url of network.urls) {
        const peer = new client.constructor({ url, target: "node" });
        await assert.rejects(peer.privateTransfers.submitTransfer(send.frameHex), (error) => error.status === 409 && error.reason === "note_spent");
      }
      const next = await client.chain.head();
      await until("further genuine cuts after rejected replays", 60_000, async () => (await client.chain.head()).height >= next.height + 3, network.healthy);
      await privateSettled(sender.notes, privateAtoms, network.healthy); await privateSettled(receiver.notes, ATOMS, network.healthy);
    });
    await step("closed disk journals reopen and a fresh seed scan recovers identical private money", async () => {
      await sender.close(); sender = await make(network.accounts.sender, "sender-sdk");
      const read = await privateSettled(sender.notes, privateAtoms, network.healthy);
      assert(read.noteCount > 0n); assert.equal(sender.notes.onTheWay().length, 0);
      copy = await make(network.accounts.sender, "sender-fresh-seed-restore");
      const recovered = await privateSettled(copy.notes, privateAtoms, network.healthy);
      assert.equal(recovered.noteCount, read.noteCount);
      assert.deepEqual((await copy.notes.page()).notes.map((note) => note.noteCommitment), (await sender.notes.page()).notes.map((note) => note.noteCommitment));
      await copy.close(); copy = undefined;
    });
    await step("private EXIT pays its exact value less the actual fee once", async () => {
      const page = await receiver.notes.page(); assert.equal(page.notes.length, 1);
      const value = page.notes[0].valueAtoms, before = (await receiver.wallet.balance(client)).atoms;
      const exit = await receiver.notes.exit({ to: network.accounts.receiver.address, notes: [page.notes[0].note] }, proving);
      assert.equal(BigInt(exit.amountAtoms) + BigInt(exit.feeAtoms), value);
      await privateSettled(receiver.notes, 0n, network.healthy);
      await publicBalance(receiver.wallet, client, before + BigInt(exit.amountAtoms), network.healthy);
      await assert.rejects(client.privateTransfers.submitExit(exit.frameHex), (error) => error.status === 409 && error.reason === "note_spent");
    });
  } finally { await copy?.close(); await receiver?.close(); await sender?.close(); }
}
