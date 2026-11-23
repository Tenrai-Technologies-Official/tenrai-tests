import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { openWallet } from "./sqlite-wallet.mjs";
import { privateSettled } from "./sdk-flows.mjs";
import { freePort } from "./network.mjs";
import { json, until } from "./processes.mjs";

function atoms(text) {
  assert.match(text, /^(0|[1-9][0-9]*)(?:\.[0-9]{1,8})?$/);
  const [whole, fraction = ""] = text.split("."); return BigInt(whole) * 100_000_000n + BigInt(fraction.padEnd(8, "0"));
}

/** A real wallet-daemon owns the native sealed journal across process death/reopen. */
export async function daemonFlow({ sdk, client, network, directory, proverPath, endpoint, step }) {
  let process, observer, serial = 0, token;
  const port = await freePort(), tokenFile = join(network.run.data, "sender-walletd.token");
  const original = "browser-production-enter-original";
  let authority, sent, expected;
  const rpc = async (method, params = {}) => {
    const answer = await json(`http://127.0.0.1:${port}/v1/rpc`, { method: "POST", headers: {
      "Content-Type": "application/json", Authorization: `Bearer ${token}` }, body: JSON.stringify({ jsonrpc: "2.0", id: ++serial, method, params }),
      signal: AbortSignal.any([AbortSignal.timeout(900_000), network.run.signal]) });
    if (answer.error) { const error = new Error(answer.error.message); error.code = answer.error.code; throw error; }
    assert(answer.result); return answer.result;
  };
  // The release configuration alone: no override of the private transfers' qualification.
  const start = async () => {
    process = network.run.start(`wallet-daemon-${serial++}`, network.cli, ["--no-color", "daemon", ...network.accounts.sender.vault,
      "--service", endpoint, "--fallback", "none", "--genesis-manifest", network.manifestPath,
      "--listen", `127.0.0.1:${port}`, "--token-file", tokenFile, "--prover", resolve(proverPath)]);
    await until("physical native wallet daemon", 90_000, async () => {
      token = (await readFile(tokenFile, "utf8")).trim(); return rpc("status");
    }, () => { network.healthy(); process.healthy(); });
  };
  const stop = async () => {
    if (!process) return;
    const active = process; process = undefined; active.child.kill();
    await active.child.waitOwnedDrain(); await active.ended;
  };
  try {
    observer = await openWallet({ sdk, client, network, phrase: network.accounts.sender.phrase, directory: join(directory, "daemon-observer") });
    await step("the node reports private transfers active and internally qualified", async () => {
      const status = await client.privateTransfers.status();
      assert.equal(status.active, true);
      assert.equal(status.protocol.security_qualification, "internally_qualified");
    });
    await start();
    await step("walletd pays registration and pins its actual archive/account/chain identity", async () => {
      const registered = await rpc("private_register", { max_fee: "2", wait: true });
      assert.equal(registered.registered, true);
      const status = await rpc("status");
      assert.equal(status.account, 0); assert.equal(status.chain_id, network.manifest.chain_id); assert.equal(status.durable_request_ids, true);
      assert.match(status.request_archive_id, /^(?:[0-9a-f]{64}|[0-9a-f]{96})$/);
      authority = { expected_request_archive_id: status.request_archive_id, expected_account: 0, expected_chain_id: status.chain_id };
    });
    await step("walletd creates one real ENTER and permanent original request receipt", async () => {
      const before = new Map((await observer.wallet.outputs(client, { limit: 256 })).map((coin) => [`${coin.transactionId}:${coin.outputIndex}`, coin.amount]));
      sent = await rpc("private_enter", { amount: "5", request_id: original, ...authority, wait: true });
      assert.equal(sent.state, "finalized"); assert.equal(sent.request_id, original); assert.match(sent.statement, /^[0-9a-f]{96}$/);
      const view = await client.privateTransfers.statement(sent.statement); assert(view.details);
      const spent = view.details.spent.reduce((sum, coin) => { const key = `${coin.transaction_id}:${coin.output_index}`; assert(before.has(key)); return sum + before.get(key); }, 0n);
      expected = spent - atoms(sent.fee); assert(expected > 0n);
      await privateSettled(observer.notes, expected, network.healthy);
      const local = await rpc("private_balance"); assert.equal(atoms(local.private_balance), expected);
    });
    await step("walletd cold restart retries the exact closed request without a second payment", async () => {
      await stop(); await start();
      const current = await rpc("status"); assert.equal(current.request_archive_id, authority.expected_request_archive_id);
      const retry = await rpc("private_retry", { request_id: original, ...authority, recovery_only: true, wait: true });
      assert.equal(retry.statement, sent.statement); assert.equal(retry.state, "finalized"); assert.equal(retry.cut_id, sent.cut_id);
      await assert.rejects(rpc("private_enter", { amount: "6", request_id: original, ...authority, wait: true }));
      await assert.rejects(rpc("private_retry", { request_id: "never-used", ...authority, recovery_only: true, wait: true }));
      await assert.rejects(rpc("private_enter", { amount: "5", request_id: "wrong-store", ...authority, expected_request_archive_id: "00".repeat(32), wait: true }));
      await privateSettled(observer.notes, expected, network.healthy);
      assert.equal(atoms((await rpc("private_balance")).private_balance), expected);
    });
  } finally { await stop(); await observer?.close(); }
}
