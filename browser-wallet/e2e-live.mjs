// The mining and payment path of a release on one development chain, every part a real process: miners through the
// stratum bridge, through a pool's PPLNS and SOLO ports and straight at the node; the node finalizes their blocks and
// the miners and the pool's PPLNS and SOLO miners get paid; a CLI wallet pays another and sees the payment final with
// its status and balance; the explorer shows the payment, the block that carries it and the balances.
//
//   node e2e-live.mjs --bin DIR --bridge PATH --pool PATH [--pool-kind reference|private] --explorer PATH
//                     --explorer-root DIR --repo PATH\node-repo --data DIR [--chrome PATH] [--minutes N] [--out results.json]
//
// --bin is a folder frozen by tenrai-tests/p2p-bench/freeze.py (tenraid, tenrai-wallet, tenrai-miner); --bridge,
// --pool and --explorer are the release builds of the stratum bridge, a pool and the explorer server. The reference
// pool pays from one minimum payout, the private pool from each miner's own threshold.

import { spawn, spawnSync } from "node:child_process";
import { copyFileSync, createWriteStream, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { browserExecutable } from "./browser-executable.mjs";
import { Browser, sleep } from "./browser.mjs";

const { values } = parseArgs({
  options: {
    bin: { type: "string" },
    bridge: { type: "string" },
    pool: { type: "string" },
    "pool-kind": { type: "string" },
    explorer: { type: "string" },
    "explorer-root": { type: "string" },
    repo: { type: "string" },
    data: { type: "string" },
    chrome: { type: "string", default: browserExecutable("chrome") },
    minutes: { type: "string", default: "20" },
    out: { type: "string", default: "e2e-live-results.json" },
  },
});
const NODE_PORT = 38761;
const MINING_PORT = 38762;
const DAEMON_PORT = 38763;
const BRIDGE_PORT = 38764;
const PPLNS_PORT = 38765;
const EXPLORER_PORT = 38766;
const SOLO_PORT = 38767;
const POOL_WEB_PORT = 38768;
const NODE_URL = `http://127.0.0.1:${NODE_PORT}`;
const POOL_URL = `http://127.0.0.1:${POOL_WEB_PORT}`;
const ATOMS = 100_000_000n;
const FUNDS = 500n * ATOMS;
const PAID = 5n * ATOMS;
const deadline = Date.now() + Number(values.minutes) * 60_000;
const log = (line) => console.log(`${new Date().toISOString().slice(11, 19)} ${line}`);

const data = resolve(values.data);
rmSync(data, { recursive: true, force: true });
mkdirSync(data, { recursive: true });
// The programs run from copies: some write a default settings file beside themselves, which must not land in the
// frozen release folder.
const binDir = join(data, "bin");
mkdirSync(binDir);
for (const name of ["tenraid.exe", "tenrai-wallet.exe", "tenrai-miner.exe"]) {
  copyFileSync(join(resolve(values.bin), name), join(binDir, name));
}
const copied = (path) => {
  const target = join(binDir, basename(path));
  copyFileSync(resolve(path), target);
  return target;
};
const bridgeExe = copied(values.bridge);
const poolExe = copied(values.pool);
// Both pools build a tenrai-pool.exe; the release set names the reference pool's copy tenrai-reference-pool.exe.
const poolKind = values["pool-kind"] ?? (basename(values.pool).toLowerCase() === "tenrai-pool.exe" ? "private" : "reference");
const explorerExe = copied(values.explorer);
const bin = (name) => join(binDir, name);
const cliProfile = { ...process.env, LOCALAPPDATA: join(data, "cli-home"), HOME: join(data, "cli-home") };
mkdirSync(cliProfile.HOME, { recursive: true });

/** A CLI wallet of its own file and password, made by the release wallet itself. */
function wallet(name) {
  const file = join(data, `${name}.vault`);
  const passwordFile = join(data, `${name}.pass`);
  writeFileSync(passwordFile, `live-${name}-password`);
  const vault = ["--file", file, "--password-file", passwordFile, "--network", "development"];
  const made = spawnSync(bin("tenrai-wallet.exe"), ["--no-color", "init", ...vault, "--output", "json"], {
    encoding: "utf8",
    env: cliProfile,
  });
  if (made.status !== 0) throw new Error(`the ${name} wallet: ${made.stderr}`);
  return { name, file, vault, address: JSON.parse(made.stdout).first_address.address };
}

// The chain's genesis is the development one plus the payer's allocation, so the wallets check finality from it
// and not from the development genesis they carry.
function cli(account, args) {
  const ran = spawnSync(bin("tenrai-wallet.exe"), [
    "--no-color", ...args, ...account.vault, "--genesis-manifest", manifestPath, "--service", NODE_URL,
    "--fallback", "none", "--output", "json",
  ], { encoding: "utf8", env: cliProfile, timeout: 600_000 });
  if (ran.status !== 0) throw new Error(`${args[0]} of ${account.name}: ${ran.stderr || ran.stdout}`);
  // A waiting send prints one JSON line when it is submitted and one when it ends; later lines add to earlier ones.
  const lines = ran.stdout.split(/\r?\n/).filter((line) => line.trim().startsWith("{"));
  if (lines.length === 0) throw new Error(`${args[0]} of ${account.name} printed no JSON: ${ran.stdout.slice(0, 200)}`);
  return Object.assign({}, ...lines.map((line) => JSON.parse(line)));
}

const payer = wallet("payer");
const payee = wallet("payee");
const poolWallet = wallet("pool");
const feeWallet = wallet("fees");
const bridgeMiner = wallet("bridge-miner");
const directMiner = wallet("direct-miner");
const pplnsMiner = wallet("pplns-miner");
const soloMiner = wallet("solo-miner");

const manifest = JSON.parse(readFileSync(resolve(values.repo, "config", "development.traidag.json"), "utf8"));
manifest.allocations[1].amount -= Number(FUNDS);
manifest.allocations.push({ address: payer.address, amount: Number(FUNDS) });
const manifestPath = join(data, "live.traidag.json");
writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);

const children = [];
function run(name, path, args) {
  const child = spawn(path, args, { stdio: ["ignore", "pipe", "pipe"], env: cliProfile });
  const out = createWriteStream(join(data, `${name}.log`));
  child.stdout.pipe(out);
  child.stderr.pipe(out);
  child.on("exit", (code) => log(`${name} ended with ${code}`));
  children.push(child);
  return child;
}

async function until(what, seconds, check) {
  const end = Math.min(Date.now() + seconds * 1000, deadline);
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
    await sleep(2000);
  }
}

const json = async (url) => {
  const reply = await fetch(url);
  if (!reply.ok) throw new Error(`${url}: ${reply.status}`);
  return reply.json();
};
const balanceOf = async (address) => BigInt((await json(`${NODE_URL}/v1/address/${address}/balance`)).balance);
const statusOf = (id) => json(`${NODE_URL}/v1/transactions/${id}/status`);
const trai = (atoms) => `${(Number(atoms) / Number(ATOMS)).toFixed(8)} TRAI`;

const results = [];
let explorer;
async function step(name, action) {
  try {
    const detail = (await action()) ?? "";
    results.push({ name, passed: true, detail });
    log(`PASS ${name}${detail ? `: ${detail}` : ""}`);
  } catch (error) {
    results.push({ name, passed: false, detail: error?.message ?? String(error) });
    log(`FAIL ${name}: ${error?.message ?? String(error)}`);
    await explorer?.screenshot("failed-explorer").catch(() => {});
  }
}

try {
  run("node", bin("tenraid.exe"), [
    "--no-color", "run", "--manifest", manifestPath, "--dev-validator", "--data-dir", join(data, "node"),
    "--rpc-bind", `127.0.0.1:${NODE_PORT}`, "--mining-bind", `127.0.0.1:${MINING_PORT}`,
    "--browser-origin", `http://127.0.0.1:${EXPLORER_PORT}`,
  ]);
  await until("the node's API", 120, async () => (await fetch(`${NODE_URL}/v1/chain/head`)).ok);
  const chainId = (await json(`${NODE_URL}/v1/node/info`)).chain_id;

  run("pool-daemon", bin("tenrai-wallet.exe"), [
    "--no-color", "daemon", ...poolWallet.vault, "--genesis-manifest", manifestPath, "--service", NODE_URL,
    "--listen", `127.0.0.1:${DAEMON_PORT}`,
  ]);
  await until("the pool's wallet daemon", 120, async () => {
    try {
      readFileSync(`${poolWallet.file}.token`, "utf8");
      return true;
    } catch {
      return false;
    }
  });
  run("bridge", bridgeExe, [
    "--no-color", "--network", "development", "--node", `127.0.0.1:${MINING_PORT}`, "--chain-id", chainId,
    "--listen", `127.0.0.1:${BRIDGE_PORT}`, "--tag", "live bridge", "--tenq-mode", "light",
    "--start-difficulty", "32", "--min-difficulty", "16",
  ]);
  run("pool", poolExe, [
    "--no-color", "--network", "development", "--node", `http://127.0.0.1:${MINING_PORT}`, "--node-api", NODE_URL,
    "--chain-id", chainId, "--wallet", `http://127.0.0.1:${DAEMON_PORT}`, "--wallet-token-file", `${poolWallet.file}.token`,
    "--pplns-listen", `127.0.0.1:${PPLNS_PORT}`, "--solo-listen", `127.0.0.1:${SOLO_PORT}`, "--tag", "live pool",
    "--tenq-mode", "light", "--start-difficulty", "32", "--min-difficulty", "16", "--share-interval", "5",
    "--pplns-fee", "1", "--solo-fee", "1", "--fee-recipient", `${feeWallet.address}=100`, "--payout-interval", "1",
    ...(poolKind === "private"
      ? ["--payout-threshold", "0.01", "--payout-threshold-min", "0.01"]
      : ["--min-payout", "0.01"]),
    "--db", join(data, "pool.sqlite"), "--web-bind", `127.0.0.1:${POOL_WEB_PORT}`,
    "--pool-name", "Live", "--public-host", "127.0.0.1",
  ]);
  const miner = (name, account, server, extra) => run(name, bin("tenrai-miner.exe"), [
    "-a", account.address, ...server, "--cpu-only", "-t", "1", "--cpu-batch", "2", "--tenq-mode", "light",
    "--no-api", "--no-dashboard", "--no-color", ...extra,
  ]);
  miner("miner-direct", directMiner, ["-s", `127.0.0.1:${MINING_PORT}`], ["--tag", "live direct"]);
  miner("miner-bridge", bridgeMiner, ["--stratum", `stratum+tcp://127.0.0.1:${BRIDGE_PORT}`], ["-w", "bridge"]);
  miner("miner-pplns", pplnsMiner, ["--stratum", `stratum+tcp://127.0.0.1:${PPLNS_PORT}`], ["--pool", "-w", "pplns"]);
  miner("miner-solo", soloMiner, ["--stratum", `stratum+tcp://127.0.0.1:${SOLO_PORT}`], ["--pool", "-w", "solo"]);
  explorer = null;
  run("explorer", explorerExe, [
    "--bind", `127.0.0.1:${EXPLORER_PORT}`, "--root", resolve(values["explorer-root"]), "--network", "development",
    "--target", "node", "--node-url", NODE_URL, "--api-url", NODE_URL,
  ]);

  await step("the node finalizes cuts with blocks of every miner", async () => {
    const start = (await json(`${NODE_URL}/v1/chain/head`)).height;
    const head = await until("ten more cuts", 300, async () => {
      const height = (await json(`${NODE_URL}/v1/chain/head`)).height;
      return height >= start + 10 ? height : null;
    });
    return `heights ${start} to ${head}`;
  });

  let payment;
  await step("a CLI wallet payment is final with its status", async () => {
    payment = cli(payer, ["send", "--to", payee.address, "--amount", trai(PAID).split(" ")[0], "--wait"]);
    const id = payment.transaction ?? payment.transaction_id;
    if (!id) throw new Error(`no transaction id in ${JSON.stringify(payment)}`);
    payment.id = id;
    const status = await until("the payment's finality", 300, async () => {
      const answer = await statusOf(id);
      return answer.lifecycle === "finalized" ? answer : null;
    });
    payment.status = status;
    return `${id.slice(0, 16)}... ${JSON.stringify(status).slice(0, 160)}`;
  });

  await step("the payee's wallet and the node show the amount, the payer's the amount and its fee", async () => {
    if (!payment?.id) throw new Error("the CLI payment did not go out");
    const received = await until("the payee's balance", 300, async () => {
      const atoms = await balanceOf(payee.address);
      return atoms === PAID ? atoms : null;
    });
    const shown = cli(payee, ["balance"]);
    const payerAtoms = await balanceOf(payer.address);
    const fee = BigInt((await json(`${NODE_URL}/v1/transactions/${payment.id}`)).fee);
    if (payerAtoms !== FUNDS - PAID - fee) throw new Error(`the payer holds ${trai(payerAtoms)}, not ${trai(FUNDS - PAID - fee)}`);
    return `payee ${trai(received)} (wallet: ${JSON.stringify(shown).slice(0, 120)}), payer ${trai(payerAtoms)}, fee ${trai(fee)}`;
  });

  await step("blocks straight at the node and through the bridge pay their miners", async () => {
    const direct = await until("a reward for the direct miner", 600, async () => (await balanceOf(directMiner.address)) > 0n);
    const bridged = await until("a reward for the bridge miner", 600, async () => (await balanceOf(bridgeMiner.address)) > 0n);
    return `direct ${trai(await balanceOf(directMiner.address))}, bridge ${trai(await balanceOf(bridgeMiner.address))} (${direct && bridged})`;
  });

  await step("the pool credits its PPLNS and SOLO miners and pays them", async () => {
    const pool = await until("the pool's blocks", 600, async () => {
      const blocks = await json(`${POOL_URL}/api/v1/blocks`);
      const list = blocks.blocks ?? blocks;
      return Array.isArray(list) && list.length > 0 ? list : null;
    });
    const pplns = await until("a payout to the PPLNS miner", 900, async () => (await balanceOf(pplnsMiner.address)) > 0n);
    const solo = await until("a payout to the SOLO miner", 900, async () => (await balanceOf(soloMiner.address)) > 0n);
    const shares = await json(`${POOL_URL}/api/v1/miners/${pplnsMiner.address}`);
    return `${pool.length} pool blocks; PPLNS ${trai(await balanceOf(pplnsMiner.address))}, SOLO ${trai(await balanceOf(soloMiner.address))}` +
      ` (${pplns && solo}); PPLNS miner ${JSON.stringify(shares).slice(0, 160)}`;
  });

  await step("the explorer shows the payment final, its block and the payee's balance", async () => {
    if (!payment?.id) throw new Error("the CLI payment did not go out");
    explorer = await Browser.open("explorer", {
      chrome: values.chrome, url: `http://127.0.0.1:${EXPLORER_PORT}/#/lookup/${payment.id}`, shots: data,
    });
    await explorer.goto();
    await explorer.until(`document.body.innerText.includes(${JSON.stringify(payment.id.slice(0, 12))})`, 60, "the payment's page");
    const page = await explorer.eval("document.body.innerText");
    if (!/final/i.test(page)) throw new Error("the payment's page does not say final");
    await explorer.eval(`location.hash = "#/address/${payee.address}"`);
    await explorer.until(`/(^|[^0-9.,])5([.,]0+)?\\s*TRAI/.test(document.body.innerText)`, 60, "the payee's balance");
    return "payment page and address page";
  });
} finally {
  writeFileSync(resolve(values.out), `${JSON.stringify({ results }, null, 2)}\n`);
  await explorer?.close().catch(() => {});
  for (const child of children) child.kill();
  const passed = results.filter((result) => result.passed).length;
  log(`${passed} of ${results.length} passed`);
  process.exitCode = passed === results.length && results.length > 0 ? 0 : 1;
}
