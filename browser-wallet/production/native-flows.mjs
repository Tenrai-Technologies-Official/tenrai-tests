import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { openWallet } from "./sqlite-wallet.mjs";
import { until } from "./processes.mjs";
import { captureNativeCase, captureUtxoCase } from "./native-capture.mjs";

const ATOMS = 100_000_000n;
function changedPresence(answer, mode) {
  const changed = structuredClone(answer);
  if (mode === "low") {
    const row = changed.proofs[0];
    assert.match(row.record, /^[0-9a-f]{236}$/); // UTXO=outpoint52+u12816+lock41+height8+source1.
    row.record = row.record.slice(0, 104) + "1".padStart(32, "0") + row.record.slice(136);
  } else {
    const field = Object.keys(changed.anchor).find((key) => /root/.test(key) && /^[0-9a-f]{96}$/.test(changed.anchor[key]));
    assert(field, "fixture must mutate an actual unsigned anchor root");
    changed.anchor[field] = "00".repeat(48);
  }
  return changed;
}

export async function nativeFlow({ sdk, wasm, sdkDirectory, client, network, directory, captureFile, step }) {
  let funder, claimant;
  const vectors = [], publicCases = [];
  let mode = "honest", signatures = 0, submissions = 0, modifiedReplies = 0, preimagePosts = 0;
  const toxic = new client.constructor({ url: network.url, target: "node", fetch: async (url, init) => {
    if (String(url).endsWith("/v1/transactions") && init?.method === "POST") {
      submissions++;
      // The actual SDK HTTP transport sends encoded JSON bytes, not necessarily a string.
      const body = init.body;
      assert(typeof body === "string" || body instanceof Uint8Array, "observe the actual SDK JSON body");
      assert((typeof body === "string" ? body.length : body.byteLength) <= 1024 * 1024, "bound fixture POST observation");
      const text = typeof body === "string" ? body : new TextDecoder("utf-8", { fatal: true }).decode(body);
      const document = JSON.parse(text);
      assert(document && typeof document.wire_hex === "string" && /^(?:[0-9a-f]{2})+$/.test(document.wire_hex), "observe actual wire bytes");
      if (document.wire_hex.includes("83".repeat(32))) preimagePosts++;
    }
    const answer = await fetch(url, init);
    if (mode !== "honest" && answer.ok && String(url).endsWith("/v1/proofs/utxo-presence")) {
      modifiedReplies++;
      return new Response(JSON.stringify(changedPresence(await answer.json(), mode)), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return answer;
  } });
  try {
    funder = await openWallet({ sdk, client, phrase: network.accounts.sender.phrase, directory: join(directory, "native-funder"), network });
    claimant = await openWallet({ sdk, client: toxic, phrase: network.accounts.receiver.phrase, directory: join(directory, "native-claimant"), network });
    if (captureFile) {
      const selected = await funder.wallet.outputs(client, { limit: 1, address: network.accounts.sender.address });
      assert.equal(selected.length, 1, "capture needs an actual ordinary sender input");
      assert.equal(selected[0].address, network.accounts.sender.address);
      publicCases.push(await captureUtxoCase({ sdk, wasm, sdkDirectory, client, network, directory,
        owner: network.accounts.sender, name: "ordinary-single",
        coins: selected.map((coin) => ({ outpoint: { transaction_id: coin.transactionId, output_index: coin.outputIndex }, address: coin.address })) }));
    }
    const transactionManifest = await client.transactions.manifest();
    const locks = await sdk.NativeLockSession.fromManifest(transactionManifest);
    const height = (await client.chain.head()).height;
    const preimage = "83".repeat(32);
    const cases = [
      { name: "hash-32", descriptor: locks.create({ type: "hashlock", owner_address: network.accounts.receiver.address, hash_hex: locks.hash(preimage) }), branch: "claim", preimageHex: preimage },
      { name: "htlc-claim", descriptor: locks.create({ type: "htlc", claim_address: network.accounts.receiver.address, refund_address: network.accounts.sender.address, hash_hex: locks.hash(preimage), refund_height: String(height + 180) }), branch: "claim", preimageHex: preimage },
      { name: "timelock", descriptor: locks.create({ type: "timelock", owner_address: network.accounts.receiver.address, unlock_height: String(height + 40) }), branch: "claim" },
      { name: "htlc-refund", descriptor: locks.create({ type: "htlc", claim_address: network.accounts.sender.address, refund_address: network.accounts.receiver.address, hash_hex: locks.hash(preimage), refund_height: String(height + 48) }), branch: "refund" },
    ];
    if (captureFile) for (const bytes of [1, 31, 33, 64]) {
      const secret = "91".repeat(bytes);
      cases.push({ name: `hash-${bytes}`, descriptor: locks.create({ type: "hashlock", owner_address: network.accounts.receiver.address, hash_hex: locks.hash(secret) }), branch: "claim", preimageHex: secret });
    }
    await step("real ordinary work funds all native descriptors", async () => {
      const maximum = transactionManifest.transaction_limits.max_outputs - 1;
      assert(Number.isSafeInteger(maximum) && maximum >= 1 && maximum <= 256);
      for (let start = 0; start < cases.length; start += maximum) {
        const sent = await funder.wallet.send(client, { payments: cases.slice(start, start + maximum).map((entry) => ({ to: entry.descriptor.address, amount: 2n * ATOMS })) });
        await network.finalized(sent.transactionId);
      }
      for (const entry of cases) {
        const records = await until("actual native funding output", 120_000, async () => {
          const rows = await locks.watch(client, entry.descriptor.descriptor_hex); return rows.length === 1 ? rows : false;
        }, network.healthy);
        assert.equal(BigInt(records[0].amount), 2n * ATOMS); entry.outpoint = records[0].outpoint;
      }
    });
    await step("timelock and refund cannot be prepared before their certified boundaries", async () => {
      for (const entry of cases.filter((value) => value.name === "timelock" || value.name === "htlc-refund")) {
        assert((await client.chain.head()).height + 1 < Number(entry.descriptor.unlock_height ?? entry.descriptor.refund_height), "slow funding crossed the pre-boundary fixture; refuse a vacuous pass");
        if (captureFile) {
          const before = await captureNativeCase({ sdk, wasm, sdkDirectory, client, network, directory,
            owner: network.accounts.receiver, name: entry.name + "-before", descriptor: entry.descriptor, request: request(entry) });
          assert(before.verified_head.finalized.height + 1 < Number(entry.descriptor.unlock_height ?? entry.descriptor.refund_height),
            "capture crossed the pre-boundary certificate; refuse a vacuous fixture");
          vectors.push(before);
        }
        await assert.rejects(claimant.wallet.reviewNativeLockSpend(locks, toxic, request(entry)), /not open|not opened/i);
      }
    });
    await step("fake LOW canonical record and unsigned root cause zero signatures, wires or preimage POST", async () => {
      const entry = cases.find((entry) => entry.name === "htlc-claim");
      const review = await claimant.wallet.reviewNativeLockSpend(locks, toxic, request(entry));
      const start = submissions, secretsBefore = preimagePosts;
      for (const mutation of ["low", "root"]) {
        mode = mutation;
        await assert.rejects(locks.signSpend(review, claimant.wallet.multisigMemberPublicKey(), () => { signatures++; throw new Error("an invalid custody proof reached signing"); }));
        assert.equal(signatures, 0); assert.equal(submissions, start); assert.equal(preimagePosts, secretsBefore);
      }
      assert.equal(modifiedReplies, 2, "both attacks must reach the live presence-proof path"); mode = "honest";
    });
    for (const entry of cases) await step(`genuine ${entry.name} native spend verifies and executes once`, async () => {
      const boundary = entry.descriptor.unlock_height ?? (entry.branch === "refund" ? entry.descriptor.refund_height : undefined);
      if (boundary !== undefined) await until("certified native boundary", 180_000, async () => (await client.chain.head()).height + 1 >= Number(boundary), network.healthy);
      if (captureFile) vectors.push(await captureNativeCase({ sdk, wasm, sdkDirectory, client, network, directory,
        owner: network.accounts.receiver, name: entry.name, descriptor: entry.descriptor, request: request(entry) }));
      const before = (await claimant.wallet.balance(client)).atoms;
      const review = await claimant.wallet.reviewNativeLockSpend(locks, toxic, request(entry));
      assert.equal(review.inputAmount, 2n * ATOMS); assert(review.fee > 0n && review.fee < review.inputAmount);
      const signed = await claimant.wallet.prepareNativeLockSpend(locks, review);
      assert.equal(signed.amount + signed.fee, review.inputAmount);
      const sent = await claimant.wallet.submit(toxic, signed); await network.finalized(sent.transactionId);
      await until("exact native output is gone", 120_000, async () => (await locks.watch(client, entry.descriptor.descriptor_hex)).length === 0, network.healthy);
      await until("exact native credit", 120_000, async () => (await claimant.wallet.balance(client)).atoms === before + signed.amount, network.healthy);
      const again = await fetch(`${network.url}/v1/transactions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ api_version: 1, wire_hex: sent.wireHex }) });
      await again.body?.cancel(); assert(again.status === 409 || again.status === 202, "replay must be rejected or acknowledged idempotently");
      assert.equal((await claimant.wallet.balance(client)).atoms, before + signed.amount);
    });
    assert(preimagePosts >= 2, "the capture counter must observe both genuine successful preimage submissions");
    if (captureFile) await writeFile(captureFile, `${JSON.stringify({ format: 1, network: "development", genesis_manifest: network.manifestText,
      test_only: "Generated synthetic fixture wallets; never use these public fixture seeds for funds.",
      wallet_entropy_hex: vectors[0]?.wallet_entropy_hex, public_cases: publicCases, cases: vectors.map(({ wallet_entropy_hex, ...entry }) => entry) }, null, 2)}\n`, { flag: "wx" });
  } finally { await claimant?.close(); await funder?.close(); }
  function request(entry) { return { descriptorHex: entry.descriptor.descriptor_hex, branch: entry.branch,
    ...(entry.preimageHex === undefined ? {} : { preimageHex: entry.preimageHex }), outpoint: entry.outpoint, destination: network.accounts.receiver.address, maxFee: ATOMS }; }
}
