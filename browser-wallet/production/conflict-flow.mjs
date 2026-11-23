import assert from "node:assert/strict";
import { AssertionError } from "node:assert";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { openWallet } from "./sqlite-wallet.mjs";
import { json, until } from "./processes.mjs";

const ATOMS = 100_000_000n;
const point = (row) => `${row.transaction_id}:${Number(row.output_index)}`;
const order = (left, right) => left < right ? -1 : left > right ? 1 : 0;
const sorted = (rows) => rows.sort((left, right) => order(point(left.outpoint), point(right.outpoint)));
const total = (owners) => owners.flatMap((owner) => owner.utxos).reduce((sum, row) => sum + BigInt(row.amount), 0n);
const statusMap = (batch) => new Map(batch.statuses.map((row) => [row.transaction_id, row]));

/** Actual OS-process conflict: admission can refuse the loser before work inclusion. */
export async function conflictFlow({ sdk, wasm, client, network, directory, step }) {
  const peers = network.urls.map((url) => new client.constructor({ url, target: "node", timeoutMillis: 30_000 }));
  const expectedBonds = [400_000n, 300_000n, 300_000n].map((coins) => (coins * ATOMS).toString());
  const actualBonds = network.manifest.validators.map((member) => String(network.manifest.allocations.find(
    (allocation) => allocation.address === member.self_bond_address)?.amount));
  assert.deepEqual(actualBonds, expectedBonds, "conflict fixture must use the public Mainnet bootstrap self-bond weights");
  const report = { format: 1, transport: "QUIC", validators: 3, synthetic_test_keys_only: true, passed: false,
    expected_bootstrap_weight_ratio: [40, 30, 30], expected_validator_self_bond_atoms: expectedBonds, validator_self_bond_atoms: actualBonds,
    both_candidates_in_work_required: false, canonical_winner_predicted_from_http_order: false };
  let first, second, coin, candidates, owners, initial, expected, winnerId, loserId, winnerBefore, cutBefore;
  let snapshotHeight = 0, setupReference, safetyFailure, signalSafetyFailure;
  let observedWinner;
  const observedCuts = new Map();
  const observedSafetyFailure = new Promise((resolve) => { signalSafetyFailure = resolve; });
  const checked = async (action) => {
    if (safetyFailure) throw safetyFailure;
    try {
      const answer = await action();
      if (safetyFailure) throw safetyFailure;
      return answer;
    } catch (error) {
      if (error instanceof AssertionError) {
        safetyFailure ??= error; signalSafetyFailure(safetyFailure);
      }
      throw error;
    }
  };
  // The shared helper retries check exceptions. Safety failures instead latch for this whole flow.
  const wait = async (label, milliseconds, check) => {
    const polling = until(label, milliseconds, () => checked(check), () => {
      if (safetyFailure) throw safetyFailure;
      network.healthy();
    });
    const outcome = await Promise.race([
      polling.then((value) => ({ value })), observedSafetyFailure.then((error) => ({ error })),
    ]);
    if (safetyFailure) throw safetyFailure;
    return outcome.value;
  };
  // Drain every reply before a retry, including assertions arriving after another RPC failed.
  const collect = async (pending) => {
    const settled = await Promise.allSettled(pending);
    if (safetyFailure) throw safetyFailure;
    for (const reply of settled) if (reply.status === "rejected") throw reply.reason;
    return settled.map((reply) => reply.value);
  };
  const views = () => {
    return collect(peers.map((peer) => checked(async () => {
      const batch = await peer.transactions.statuses(candidates.map((entry) => entry.transactionId));
      const applied = batch.statuses.filter((state) => state.lifecycle === "finalized");
      assert(applied.length <= 1, "both competing signed transactions applied");
      if (applied.length) {
        assert(applied[0].finalized_cut); assert.equal(applied[0].failure, null);
        const winner = { transaction_id: applied[0].transaction_id, finalized_cut: structuredClone(applied[0].finalized_cut) };
        if (observedWinner) assert.deepEqual(winner, observedWinner, "observed finalized winner changed");
        else observedWinner = winner;
      }
      return batch;
    })));
  };
  const snapshot = (peer) => checked(async () => {
    const answer = await peer.address.utxoBatch(owners);
    assert(Number.isSafeInteger(answer.indexed_height) && answer.indexed_height >= 0, "invalid UTXO index height");
    if (answer.indexed_height < snapshotHeight) return false;
    assert.equal(answer.addresses.length, owners.length);
    assert.equal(new Set(answer.addresses.map((row) => row.address)).size, owners.length);
    const rows = answer.addresses.map((owner) => {
      assert(owners.includes(owner.address)); assert.equal(owner.truncated, false);
      assert(owner.utxos.length <= 32, "fresh conflict fixture must remain small and complete");
      return { address: owner.address, utxos: sorted(owner.utxos.map((row) => ({
        outpoint: { transaction_id: row.outpoint.transaction_id, output_index: Number(row.outpoint.output_index) },
        amount: BigInt(row.amount).toString(), created_finalized_height: row.created_finalized_height, source: row.source,
      }))) };
    });
    rows.sort((left, right) => order(left.address, right.address));
    if (!expected) {
      assert(rows.find((owner) => owner.address === coin.address).utxos.some((row) =>
        point(row.outpoint) === `${coin.transactionId}:${coin.outputIndex}` && BigInt(row.amount) === coin.amount),
      "an aligned setup index must contain the exact selected TEST input");
      setupReference ??= rows;
    }
    assert.deepEqual(rows, expected ?? initial ?? setupReference, "aligned tracked UTXO effects disagree");
    return rows;
  });
  const snapshots = async () => {
    const replies = await collect(peers.map(snapshot));
    return replies.some((reply) => reply === false) ? false : replies;
  };
  const commonCut = async (minimum = 0) => wait("one common canonical cut on three validators", 180_000, async () => {
    const heads = await collect(peers.map((peer) => peer.chain.head()));
    const height = Math.min(...heads.map((head) => head.height));
    if (height < minimum) return false;
    let reference;
    const cuts = await collect(network.urls.map((url) => checked(async () => {
      const cut = (await json(`${url}/v1/cuts/${height}`)).cut;
      assert.equal(cut.height, height);
      assert.match(cut.cut_id, /^[0-9a-f]{96}$/); assert.match(cut.state_root, /^[0-9a-f]{96}$/);
      const observed = observedCuts.get(height);
      if (observed) assert.deepEqual(cut, observed, "observed canonical cut changed at this height");
      else observedCuts.set(height, structuredClone(cut));
      if (reference) assert.deepEqual(cut, reference, "same-height canonical cut/state roots disagree");
      reference = cut;
      return cut;
    })));
    return cuts[0];
  });
  const submit = async (peer, candidate) => {
    try {
      const answer = await peer.transactions.submit(candidate.wireHex);
      assert.equal(answer.transaction_id, candidate.transactionId);
      return { transaction_id: candidate.transactionId, accepted: true, lifecycle: answer.lifecycle };
    } catch (error) {
      assert.equal(error.status, 409, "only an explicit input/duplicate refusal is an expected conflict response");
      assert(["already_pooled", "input_pooled", "input_missing"].includes(error.reason), "unexpected refusal reason");
      return { transaction_id: candidate.transactionId, accepted: false, status: error.status, reason: error.reason };
    }
  };
  const submitOrders = (orders) => Promise.all(peers.map(async (peer, index) => {
    const replies = [];
    for (const candidate of orders[index]) replies.push(await submit(peer, candidate));
    return { node: index, attempted_http_order: orders[index].map((row) => row.transactionId), replies };
  }));
  const unchangedWinner = async () => {
    let reference;
    const fingerprints = await collect(peers.map((peer) => checked(async () => {
      const view = await peer.transactions.get(winnerId);
      assert.equal(view.status.lifecycle, "finalized"); assert.equal(view.status.failure, null);
      const fingerprint = { transaction_id: view.transaction_id, wire_hex: view.wire_hex, inputs: view.inputs, outputs: view.outputs,
        fee: view.fee, block_id: view.block_id, finalized_cut: view.status.finalized_cut };
      if (reference) assert.deepEqual(fingerprint, reference, "canonical winner receipts disagree");
      if (winnerBefore) assert.deepEqual(fingerprint, winnerBefore, "canonical winner receipt changed");
      reference = fingerprint;
      return fingerprint;
    })));
    return fingerprints[0];
  };
  const assertOneWinner = async () => {
    const batches = await views();
    for (const batch of batches) {
      const states = statusMap(batch), applied = batch.statuses.filter((state) => state.lifecycle === "finalized");
      assert.equal(applied.length, 1, "exactly one real payment must commit on every node");
      assert.equal(applied[0].transaction_id, winnerId);
      assert.equal(applied[0].failure, null); assert(applied[0].finalized_cut);
      const loser = states.get(loserId);
      assert(!loser || loser.lifecycle !== "finalized", "the other signed spend must never apply");
      if (loser?.finalized_cut) {
        assert.equal(loser.lifecycle, "failed");
        assert(["conflict", "no_effect", "expired"].includes(loser.failure));
      }
    }
    return batches;
  };
  const classify = async (batches, attempts) => Promise.all(batches.map(async (batch, index) => {
    const loser = statusMap(batch).get(loserId);
    const refused = attempts[index].replies.filter((reply) => reply.transaction_id === loserId && !reply.accepted);
    let canonical = null;
    if (loser?.finalized_cut) {
      const page = await json(`${network.urls[index]}/v1/cuts/${loser.finalized_cut.height}/transactions?limit=64`);
      assert.equal(page.height, loser.finalized_cut.height);
      assert.equal(page.next_cursor, null, "fresh conflict cut must fit one complete bounded transaction page");
      const row = page.transactions.find((entry) => entry.transaction_id === loserId);
      if (row) {
        assert.equal(row.wire_hex, candidates.find((entry) => entry.transactionId === loserId).wireHex);
        assert.equal(row.status.lifecycle, "failed"); assert(row.block_id);
        assert.deepEqual(row.status.finalized_cut, loser.finalized_cut);
        canonical = { transaction_id: loserId, block_id: row.block_id, finalized_cut: row.status.finalized_cut, failure: row.status.failure };
      }
    }
    return { node: index,
      loser_outcome: canonical ? "finalized_no_effect"
        : refused.some((reply) => ["input_pooled", "input_missing"].includes(reply.reason)) ? "admission_refused"
          : loser?.finalized_cut ? "locally_inferred_conflict" : "no_finalized_loser_receipt",
      canonical_loser_receipt: canonical, loser_status: loser ?? null, loser_admission_refusals: refused };
  }));
  try {
    await step("two genuinely distinct canonical wires verify signatures for the same TEST input", async () => {
      await network.stopMining();
      first = await openWallet({ sdk, client: peers[0], phrase: network.accounts.sender.phrase,
        directory: join(directory, "conflict-sender-a"), network });
      second = await openWallet({ sdk, client: peers[1], phrase: network.accounts.sender.phrase,
        directory: join(directory, "conflict-sender-b"), network });
      const coins = await first.wallet.outputs(peers[0], { address: network.accounts.sender.address, limit: 2 });
      assert.equal(coins.length, 1, "the synthetic sender's first address must have one genesis coin");
      coin = coins[0];
      assert.equal(coin.address, network.accounts.sender.address); assert(coin.amount > 3n * ATOMS);
      const include = [{ transactionId: coin.transactionId, outputIndex: coin.outputIndex }];
      candidates = [];
      for (const [session, peer, to, amount] of [
        [first, peers[0], network.accounts.receiver.address, ATOMS],
        [second, peers[1], network.accounts.external.address, 2n * ATOMS],
      ]) {
        const review = await session.wallet.prepare(peer, { to, amount, include });
        assert.equal(review.inputs, 1);
        const signed = await session.wallet.exportSigned(review);
        const wire = Uint8Array.from(Buffer.from(signed.wireHex, "hex"));
        const inspected = JSON.parse(wasm.inspectTransaction(wire, signed.manifest));
        assert.equal(inspected.transaction_id, signed.transactionId);
        assert.equal(inspected.body.kind, "payment"); assert.equal(inspected.body.chain_id, network.manifest.chain_id);
        assert.deepEqual(inspected.body.inputs.map(point), [`${coin.transactionId}:${coin.outputIndex}`]);
        assert.equal(inspected.witnesses.length, 1);
        assert.equal(inspected.witnesses[0].profile, "ml-dsa-65-inline", "fresh test signer must supply its actual public key");
        wasm.verifyTransaction(wire, JSON.stringify([{ address: coin.address, amount: coin.amount.toString() }]), undefined, signed.manifest);
        const outputAtoms = inspected.body.outputs.reduce((sum, output) => sum + BigInt(output.amount), 0n);
        assert.equal(coin.amount - outputAtoms, signed.fee); assert(signed.fee >= BigInt(inspected.minimum_fee) && signed.fee > 0n);
        candidates.push({ transactionId: signed.transactionId, wireHex: signed.wireHex,
          fee: signed.fee.toString(), validUntil: signed.validUntil.toString(), body: inspected.body });
      }
      assert.notEqual(candidates[0].transactionId, candidates[1].transactionId);
      assert.notEqual(candidates[0].wireHex, candidates[1].wireHex);
      owners = [...new Set([coin.address, ...candidates.flatMap((entry) => entry.body.outputs.map((output) => output.address))])];
      assert(owners.length <= 8);
      report.setup_cut = await commonCut(); snapshotHeight = report.setup_cut.height;
      const all = await wait("all setup indexes reach the fixed agreed cut", 120_000, snapshots); initial = all[0];
      for (const actual of all) assert.deepEqual(actual, initial, "all nodes must begin with exactly the same tracked coins");
      assert(initial.find((row) => row.address === coin.address).utxos.some((row) =>
        point(row.outpoint) === `${coin.transactionId}:${coin.outputIndex}` && BigInt(row.amount) === coin.amount));
      report.shared_input = { transaction_id: coin.transactionId, output_index: coin.outputIndex, address: coin.address, amount: coin.amount.toString() };
      report.candidates = candidates; report.initial_utxos = initial; report.canonical_signatures_verified = 2;
      await first.close(); first = undefined; await second.close(); second = undefined;
    });
    await step("opposite HTTP orders yield exactly one committed spend and no losing output on three nodes", async () => {
      report.initial_submissions = await submitOrders([[candidates[0], candidates[1]], [candidates[1], candidates[0]], [candidates[1], candidates[0]]]);
      assert(report.initial_submissions.some((entry) => entry.replies.some((reply) => reply.accepted)), "the fixture must actually admit a candidate");
      network.startMining(0);
      const terminal = await wait("a real winning payment finalizes across all three validators", 180_000, async () => {
        const batches = await views();
        const applied = batches.map((batch) => batch.statuses.filter((state) => state.lifecycle === "finalized"));
        assert(applied.every((rows) => rows.length <= 1), "both competing signed transactions applied");
        if (applied.some((rows) => rows.length !== 1)) return false;
        assert(applied.every((rows) => rows[0].transaction_id === applied[0][0].transaction_id));
        assert(applied.every((rows) => rows[0].finalized_cut && rows[0].failure === null));
        return applied[0][0];
      });
      winnerId = terminal.transaction_id; loserId = candidates.find((entry) => entry.transactionId !== winnerId).transactionId;
      await network.stopMining();
      const winner = candidates.find((entry) => entry.transactionId === winnerId);
      snapshotHeight = terminal.finalized_cut.height;
      expected = structuredClone(initial);
      const sender = expected.find((owner) => owner.address === coin.address);
      sender.utxos = sender.utxos.filter((row) => point(row.outpoint) !== `${coin.transactionId}:${coin.outputIndex}`);
      for (const output of winner.body.outputs) expected.find((owner) => owner.address === output.address).utxos.push({
        outpoint: { transaction_id: winnerId, output_index: Number(output.index) }, amount: BigInt(output.amount).toString(),
        created_finalized_height: terminal.finalized_cut.height, source: "regular",
      });
      expected.forEach((owner) => sorted(owner.utxos));
      assert.equal(total(initial) - total(expected), BigInt(winner.fee), "exactly one fee leaves tracked value");
      await wait("all three exact UTXO sets contain only the winning effects", 120_000, async () => {
        const actuals = await snapshots(); if (!actuals) return false;
        for (const actual of actuals) assert.deepEqual(actual, expected);
        return true;
      });
      const batches = await assertOneWinner(); winnerBefore = await unchangedWinner();
      assert.equal(winnerBefore.wire_hex, winner.wireHex); assert.equal(BigInt(winnerBefore.fee), BigInt(winner.fee));
      cutBefore = await commonCut(terminal.finalized_cut.height);
      report.winner = winnerBefore; report.loser_id = loserId; report.expected_utxos = expected;
      report.before_restart_cut = cutBefore; report.initial_outcomes = await classify(batches, report.initial_submissions);
    });
    await step("cold owned-node restart and both original replays preserve the committed monetary outcome", async () => {
      await network.restartValidators();
      await wait("all reopened indexes retain the exact winning effects", 120_000, async () => {
        const actuals = await snapshots(); if (!actuals) return false;
        for (const actual of actuals) assert.deepEqual(actual, expected);
        return true;
      });
      assert.deepEqual(await unchangedWinner(), winnerBefore); await assertOneWinner();
      report.replay_submissions = await submitOrders([[candidates[1], candidates[0]], [candidates[0], candidates[1]], [candidates[0], candidates[1]]]);
      const heads = await Promise.all(peers.map((peer) => peer.chain.head()));
      const minimum = Math.max(...heads.map((head) => head.height)) + 4;
      network.startMining(1);
      report.after_replay_cut = await commonCut(minimum);
      await network.stopMining();
      snapshotHeight = report.after_replay_cut.height;
      await wait("replay indexes reach the new agreed cut with no second effect", 120_000, async () => {
        const actuals = await snapshots(); if (!actuals) return false;
        for (const actual of actuals) {
          assert.deepEqual(actual, expected, "restart/replay must not recreate the input or any losing output");
          assert(actual.flatMap((owner) => owner.utxos).every((row) => row.outpoint.transaction_id !== loserId), "no losing output exists");
        }
        return true;
      });
      assert.deepEqual(await unchangedWinner(), winnerBefore);
      const batches = await assertOneWinner();
      for (const url of network.urls) assert.deepEqual((await json(`${url}/v1/cuts/${cutBefore.height}`)).cut, cutBefore,
        "restart/replay cannot replace the historical committed cut or its state root");
      report.replay_outcomes = await classify(batches, report.replay_submissions);
      report.restart_replay_unchanged = true; report.passed = true;
    });
  } finally {
    await first?.close(); await second?.close();
    await writeFile(join(directory, "conflict-public-result.json"), `${JSON.stringify(report, null, 2)}\n`, { flag: "wx" });
  }
}
