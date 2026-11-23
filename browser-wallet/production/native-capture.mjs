import assert from "node:assert/strict";
import { DatabaseSync } from "./sqlite.mjs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

/** Captures synthetic PUBLIC proof vectors. No validator/operational key or user store is exported. */
export async function captureUtxoCase({ sdk, wasm, sdkDirectory, network, directory, client, owner, name, coins }) {
  assert(Array.isArray(coins) && coins.length >= 1 && coins.length <= 8, "capture one to eight actual selected inputs");
  const expected = [...coins].sort((a, b) => a.outpoint.transaction_id === b.outpoint.transaction_id
    ? a.outpoint.output_index - b.outpoint.output_index : a.outpoint.transaction_id < b.outpoint.transaction_id ? -1 : 1);
  assert(new Set(expected.map((coin) => coin.outpoint.transaction_id + ":" + coin.outpoint.output_index)).size === expected.length);
  const { attachFinalityGuard, withFinalityProof, closeFinalityGuard } = await import(pathToFileURL(join(sdkDirectory, "dist/wallet/finality-guard.js")));
  const { registrationHead } = await import(pathToFileURL(join(sdkDirectory, "dist/wallet/private-registration-context.js")));
  const entropy = wasm.mnemonicEntropy(owner.phrase);
  const root = new wasm.WalletRoot(entropy, "");
  const store = sdk.sqliteFinalityGuardStore(new DatabaseSync(join(directory, `capture-${name}-finality.sqlite`)));
  attachFinalityGuard(root, store);
  let scan;
  try {
    const exchanges = [];
    const capture = new client.constructor({ url: network.url, target: "node", fetch: async (url, init) => {
      const answer = await fetch(url, init);
      if (answer.ok && /\/v1\/(private\/anchor|finality\/(bridge|cuts\/))/.test(String(url))) {
        assert(exchanges.length < 128, "fixture finality proof capture exceeded its finite budget");
        exchanges.push({ url: String(url), answer: await answer.clone().json() });
      }
      return answer;
    } });
    scan = new wasm.PrivateScan(root, "development", network.manifestText, 0, undefined, undefined);
    await registrationHead(wasm, root, "development", 0, network.manifestText, capture, { signal: AbortSignal.timeout(120_000) }, scan);
    const verified = JSON.parse(scan.registrationVerifiedHead());
    assert.equal(verified.chain_id, network.manifest.chain_id);
    const manifest = await client.transactions.manifest();
    manifest.finalized = verified.finalized;
    const outpoints = expected.map((coin) => coin.outpoint);
    const presence = await client.proofs.utxoPresence(verified.finalized, outpoints);
    const canonical = await withFinalityProof(root, "development", network.manifestText, () => wasm.readFinalizedUtxoSelection(
      JSON.stringify({ api_version: 1, anchor: verified.finalized, outpoints }), JSON.stringify(presence),
      JSON.stringify({ chain_id: verified.chain_id, network: "development", coins: expected })));
    const anchor = exchanges.findLast((entry) => /\/private\/anchor$/.test(entry.url))?.answer;
    assert(anchor && anchor.height === verified.finalized.height, "capture must use the actual WASM-verified anchor");
    const cut = await client.privateTransfers.finalizedCutProof(verified.finalized.height);
    assert.equal(cut.height, verified.finalized.height);
    return { name, manifest, anchor_response: anchor, cut_proof_response: cut,
      bridge_responses: exchanges.filter((entry) => /\/finality\/bridge/.test(entry.url)).map((entry) => entry.answer),
      presence_response: presence, verified_head: verified, canonical_coins: JSON.parse(canonical),
      authority: { account: 0, branch: "receive", index: 0 }, wallet_entropy_hex: Buffer.from(entropy).toString("hex") };
  } finally { scan?.free(); await closeFinalityGuard(root); await store.close(); root.free(); entropy.fill(0); }
}


export async function captureNativeCase(options) {
  const { descriptor, request } = options;
  const captured = await captureUtxoCase({ ...options, coins: [{ outpoint: request.outpoint, address: descriptor.address }] });
  const fields = descriptor.type === "timelock" ? ["owner_address", "unlock_height"]
    : descriptor.type === "hashlock" ? ["owner_address", "hash_hex"] : ["claim_address", "refund_address", "hash_hex", "refund_height"];
  const descriptor_request = Object.fromEntries([["type", descriptor.type], ...fields.map((field) => [field, descriptor[field]])]);
  assert(typeof request.maxFee === "bigint" && request.maxFee > 0n, "capture requires the original explicit fee cap");
  return { ...captured, descriptor_request, descriptor, request: { ...request, maxFee: request.maxFee.toString() } };
}
