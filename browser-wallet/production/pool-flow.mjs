import assert from "node:assert/strict";
import { mkdir, readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { account, freePort } from "./network.mjs";
import { json, until } from "./processes.mjs";

function atoms(text) {
  assert.match(text, /^(0|[1-9][0-9]*)(?:\.[0-9]{1,8})?$/, `an amount in TRAI: ${text}`);
  const [whole, fraction = ""] = text.split(".");
  return BigInt(whole) * 100_000_000n + BigInt(fraction.padEnd(8, "0"));
}

/** A real pool between the three validators and a miner: its own wallet daemon, PPLNS work, rewards booked from the
 * cuts that pay them, payouts the nodes finalize, and a cold pool restart that neither loses nor repeats money. */
export async function poolFlow({ network, directory, step, poolPath }) {
  const run = network.run, member = network.members[1];
  const nodeUrl = `http://127.0.0.1:${member.ports.rpc}`;
  const owner = await account(run, network.cli, "pool");
  const miner = await account(run, network.cli, "pool-miner");
  const ports = { wallet: await freePort(), pplns: await freePort(), solo: await freePort(), web: await freePort() };
  const tokenFile = join(run.data, "pool-walletd.token"), data = join(directory, "pool");
  await mkdir(data, { mode: 0o700 });
  const web = `http://127.0.0.1:${ports.web}`;
  // The pool answers a client about five requests a second and then asks it to come back in 10 s, as it
  // asks any website visitor; this flow keeps to that.
  const api = async (path) => {
    for (let tries = 0; ; tries++) {
      try { return await json(`${web}${path}`); }
      catch (error) {
        if (!error.message.startsWith("HTTP 429") || tries === 30) throw error;
        await sleep(10_000);
      }
    }
  };
  // A long wait asks once a second.
  const slowly = (check) => async () => { await sleep(1_000); return check(); };
  let daemon, pool, mining, starts = 0, serial = 0;
  const healthy = () => { network.healthy(); daemon?.healthy(); pool?.healthy(); mining?.healthy(); };
  const startPool = async () => {
    pool = run.start(`pool-${++starts}`, resolve(poolPath), ["--no-color", "--network", "development",
      "--chain-id", network.manifest.chain_id, "--node", `127.0.0.1:${member.ports.mining}`, "--node-api", `127.0.0.1:${member.ports.rpc}`,
      "--wallet", `127.0.0.1:${ports.wallet}`, "--wallet-token-file", tokenFile, "--pplns-listen", `127.0.0.1:${ports.pplns}`,
      "--solo-listen", `127.0.0.1:${ports.solo}`, "--web-bind", `127.0.0.1:${ports.web}`, "--db", join(data, "pool.sqlite"),
      "--tenq-mode", "light", "--payout-interval", "1", "--payout-threshold", "1", "--payout-threshold-min", "1", "--explorer-url", ""]);
    await until("the pool's API", 120_000, () => api("/api/v1/pool"), healthy);
  };
  const stopPool = async () => {
    const active = pool; pool = undefined; active.child.kill();
    await active.child.waitOwnedDrain(); await active.ended;
  };
  const stopMiner = async () => {
    const active = mining; mining = undefined; active.child.kill();
    await active.child.waitOwnedDrain(); await active.ended;
  };
  // Every block the pool lists, page by page along its cursor.
  const blocks = async () => {
    const all = [];
    for (let before = null; ;) {
      const page = await api(`/api/v1/blocks${before === null ? "" : `?before=${before}`}`);
      all.push(...page.blocks);
      if (page.next == null) return all;
      assert(page.next !== before && all.length <= 20_000, "the pool's block list ends");
      before = page.next;
    }
  };
  const view = () => api(`/api/v1/miners/${miner.address}`);
  // A booked block's credits never change, so each is read once.
  const details = new Map();
  const detail = async (hash) => {
    if (!details.has(hash)) details.set(hash, await api(`/api/v1/blocks/${hash}`));
    return details.get(hash);
  };
  // The miner's earnings must be the sum of its credits in the blocks whose rewards the pool booked. The pool keeps
  // each page of its lists for two seconds, so one walk can read a page older than the miner's view: the books are
  // compared once two walks, further apart than that, find the same booked blocks and the view did not change.
  const finalized = async () => (await blocks()).filter((block) => block.stage === "finalized").map((block) => block.hash);
  const books = async () => {
    for (let tries = 0; tries < 40; tries++) {
      const before = await view();
      const first = await finalized();
      await sleep(2_500);
      const booked = await finalized();
      const after = await view();
      if (before.earned !== after.earned || first.join() !== booked.join()) continue;
      let credited = 0n;
      for (const hash of booked) {
        credited += (await detail(hash)).credits.filter((credit) => credit.address === miner.address).reduce((sum, credit) => sum + atoms(credit.amount), 0n);
      }
      return { earned: atoms(after.earned), credited, booked, view: after };
    }
    throw new Error("the pool's books did not hold still long enough to compare");
  };

  await step("the pool's wallet daemon answers on its own vault", async () => {
    daemon = run.start("pool-walletd", network.cli, ["--no-color", "daemon", ...owner.vault, "--service", nodeUrl, "--fallback", "none",
      "--genesis-manifest", network.manifestPath, "--listen", `127.0.0.1:${ports.wallet}`, "--token-file", tokenFile]);
    await until("the pool's wallet daemon", 90_000, async () => {
      const token = (await readFile(tokenFile, "utf8")).trim();
      const answer = await json(`http://127.0.0.1:${ports.wallet}/v1/rpc`, { method: "POST", headers: { "Content-Type": "application/json",
        Authorization: `Bearer ${token}` }, body: JSON.stringify({ jsonrpc: "2.0", id: ++serial, method: "status", params: {} }) });
      return answer.result;
    }, healthy);
  });
  await step("the pool starts on its node and daemon", startPool);
  await step("a pool miner finds blocks the nodes register", async () => {
    mining = run.start("pool-miner", network.miner, ["--no-color", "--no-dashboard", "--no-api", "--cpu-only", "-t", "1", "--cpu-batch", "2",
      "--tenq-mode", "light", "-a", miner.address, "-w", "rig1", "--chain-id", network.manifest.chain_id, "--stratum", `stratum+tcp://127.0.0.1:${ports.pplns}`, "--pool"]);
    // At this network's difficulty a share can be a block, dozens a second: the miner stops once the pool found
    // some, so every block it found is followed to its end and the books cover all of them.
    await until("a pool block", 900_000, async () => (await api("/api/v1/blocks")).blocks.length > 0, healthy);
    await stopMiner();
    await until("a registered pool block", 900_000, slowly(async () =>
      (await blocks()).some((block) => ["registered", "executed", "finalized"].includes(block.stage))), healthy);
  });
  await step("each booked pool block holds exactly the reward the node paid it", async () => {
    const booked = await until("a pool block whose reward is booked", 1_800_000, slowly(async () => {
      const found = (await blocks()).filter((block) => block.stage === "finalized"); return found.length ? found : false;
    }), healthy);
    for (const block of booked) {
      const booking = await detail(block.hash);
      const onChain = await json(`${nodeUrl}/v1/blocks/${block.hash}`);
      assert.equal(onChain.reward?.state, "paid", `the node paid ${block.hash}`);
      assert.equal(BigInt(onChain.reward.subsidy ?? "0") + BigInt(onChain.reward.fees), atoms(booking.reward), "booked reward equals the node's");
      const credits = booking.credits.reduce((sum, credit) => sum + atoms(credit.amount), 0n);
      assert.equal(credits + atoms(booking.fee), atoms(booking.reward), "credits and pool fee add up to the reward");
    }
    const { earned, credited } = await books();
    assert(earned > 0n); assert.equal(earned, credited, "the miner earned exactly its credits");
  });
  await step("a cold pool restart keeps every booked credit", async () => {
    const before = await books();
    await stopPool(); await startPool();
    const after = await books();
    for (const hash of before.booked) assert(after.booked.includes(hash), `${hash} stays booked`);
    assert(after.earned >= before.earned); assert.equal(after.earned, after.credited, "no credit lost or doubled by the restart");
  });
  await step("the pool pays the miner and the three nodes finalize each payout once", async () => {
    const paid = await until("a finalized pool payout", 1_800_000, slowly(async () => {
      const done = (await view()).payouts.filter((payout) => payout.status === "paid" && payout.transaction); return done.length ? done : false;
    }), healthy);
    for (const payout of paid) await network.finalized(payout.transaction);
    // The miner's address receives nothing but pool payouts, so every node must hold exactly their sum.
    await until("the miner's balance on every node equals its paid payouts", 600_000, slowly(async () => {
      const current = await view();
      const settled = current.payouts.filter((payout) => payout.status === "paid");
      if (current.payouts.some((payout) => payout.status !== "paid" && payout.status !== "failed")) return false;
      const ids = settled.map((payout) => payout.transaction);
      assert.equal(new Set(ids).size, ids.length, "no payout transaction repeats");
      const sum = settled.reduce((total, payout) => total + atoms(payout.amount), 0n);
      assert.equal(atoms(current.paid), sum, "the pool's paid total is the sum of its payouts");
      const balances = await Promise.all(network.urls.map((url) => json(`${url}/v1/address/${miner.address}/balance`)));
      return balances.every((balance) => BigInt(balance.balance) === sum);
    }), healthy);
  });
}
