import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { createSocket } from "node:dgram";
import { join, resolve } from "node:path";
import { randomBytes } from "node:crypto";
import { json, until } from "./processes.mjs";

const ATOMS = 100_000_000n;
const BOND = 1_000_000n * ATOMS;
const allocatedPorts = new Set();
function udpPort() {
  return new Promise((done, fail) => {
    const probe = createSocket("udp4");
    let port, failure, settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      if (error) fail(error); else done(port);
    };
    const close = () => {
      try { probe.close(); }
      catch (error) { if (error.code !== "ERR_SOCKET_DGRAM_NOT_RUNNING") failure ??= error; }
    };
    const stop = (error) => { failure ??= error; close(); finish(failure); };
    const timer = setTimeout(() => stop(new Error("UDP port probe exceeded 5 seconds")), 5_000);
    probe.unref();
    probe.on("error", stop);
    probe.on("listening", () => {
      // A bind may complete after failure; it still has to release its socket.
      if (!settled) {
        try { port = probe.address().port; }
        catch (error) { stop(error); return; }
      }
      close();
      if (failure) finish(failure);
    });
    probe.once("close", () => finish(failure ?? (port === undefined ? new Error("UDP port probe closed before binding") : undefined)));
    try { probe.bind({ port: 0, address: "127.0.0.1", exclusive: true }); }
    catch (error) { stop(error); }
  });
}
export async function freePort(transport = "tcp") {
  assert(transport === "tcp" || transport === "udp", "Unknown port transport");
  for (;;) {
    let port;
    if (transport === "udp") port = await udpPort();
    else {
      const probe = createServer();
      port = await new Promise((done, fail) => { probe.once("error", fail); probe.listen(0, "127.0.0.1", () => done(probe.address().port)); });
      await new Promise((done, fail) => probe.close((error) => error ? fail(error) : done()));
    }
    if (!allocatedPorts.has(port)) { allocatedPorts.add(port); return port; }
  }
}
// Validators run QUIC only.
const multiaddr = (port) => `/ip4/127.0.0.1/udp/${port}/quic-v1`;
function integer(value) { if (!Number.isSafeInteger(value) || value < 0) throw new Error("Unsafe genesis amount"); return BigInt(value); }
function amount(value) { if (value < 0n || value > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("Test genesis exceeds exact JSON integer range"); return Number(value); }
function document(output) {
  try { return JSON.parse(output.trim()); }
  catch { const lines = output.trim().split(/\r?\n/).filter((line) => line.startsWith("{")); return JSON.parse(lines.at(-1) ?? output); }
}

/** Generated accounts belong only to this fresh test. No existing seed, vault or node store is read. */
export async function account(run, cli, name) {
  const file = join(run.data, `${name}.vault`);
  const made = document(await run.capture(cli, ["--no-color", "init", "--file", file, "--generate-password", "--network", "development", "--output", "json"]));
  assert.equal(typeof made.recovery_phrase, "string");
  assert.equal(typeof made.password_file, "string");
  assert.equal(typeof made.first_address?.address, "string");
  const vault = ["--file", file, "--password-file", made.password_file, "--network", "development"];
  const addresses = [made.first_address.address];
  for (const index of [1, 2]) {
    const row = document(await run.capture(cli, ["--no-color", "address", ...vault, "--index", String(index), "--output", "json"]));
    assert.equal(typeof row.address, "string"); addresses.push(row.address);
  }
  return { file, vault, phrase: made.recovery_phrase, address: addresses[0], addresses };
}

export async function prepareNetwork(run, options) {
  await run.initialize();
  const binary = (name) => resolve(options[name]);
  for (const name of ["tenraid", "cli", "miner"]) if (!options[name]) throw new Error(`--${name} is required`);
  const node = binary("tenraid"), cli = binary("cli"), miner = binary("miner");
  const template = JSON.parse(await readFile(join(resolve(options.repo), "config/development.traidag.json"), "utf8"));
  const total = template.allocations.reduce((sum, row) => sum + integer(row.amount), 0n);
  const treasury = template.allocations.at(-1).address;
  const accounts = {};
  for (const name of ["sender", "receiver", "external", "mining"]) accounts[name] = await account(run, cli, name);
  const members = [];
  for (let index = 0; index < 3; index++) {
    const owner = await account(run, cli, `validator-${index}`);
    const data = join(run.data, `node-${index}`); await mkdir(data, { mode: 0o700 });
    const keyFile = join(run.data, `validator-${index}.key`);
    const password = owner.vault[owner.vault.indexOf("--password-file") + 1];
    const key = document(await run.capture(cli, ["--no-color", "validator-key-export", ...owner.vault,
      "--validator-key-file", keyFile, "--validator-key-password-file", password, "--output", "json"]));
    assert.match(key.consensus_public_key, /^[0-9a-f]{3904}$/);
    const heartbeatFile = join(run.data, `validator-${index}-heartbeat.key`);
    const heartbeat = document(await run.capture(cli, ["--no-color", "validator-heartbeat-fee-key-export", ...owner.vault,
      "--heartbeat-fee-key-file", heartbeatFile, "--heartbeat-fee-key-password-file", password, "--output", "json"]));
    assert.equal(typeof heartbeat.heartbeat_fee_address, "string");
    const ports = { rpc: await freePort(), mining: await freePort(), p2p: await freePort("udp"), overlay: await freePort("udp") };
    const endpoint = (await run.capture(node, ["--no-color", "validator-endpoint", "--network", "development", "--data-dir", data, "--route", multiaddr(ports.overlay)])).trim();
    assert.match(endpoint, /^(?:[0-9a-f]{2})+$/);
    members.push({ data, owner, keyFile, password, heartbeatFile, heartbeatAddress: heartbeat.heartbeat_fee_address, key: key.consensus_public_key, endpoint, ports, validatorId: (index + 1).toString(16).padStart(2, "0").repeat(48) });
  }
  assert.equal(new Set(members.map((member) => member.key)).size, 3, "validator signers must actually be independent");
  const selfBonds = options.mode === "conflict"
    ? [400_000n, 300_000n, 300_000n].map((coins) => coins * ATOMS) : members.map(() => BOND);
  const allocations = members.map((member, index) => ({ address: member.owner.address, amount: amount(selfBonds[index]) }));
  for (const member of members) allocations.push({ address: member.heartbeatAddress, amount: amount(100n * ATOMS) });
  for (const address of accounts.sender.addresses) allocations.push({ address, amount: amount(140n * ATOMS) });
  allocations.push({ address: accounts.receiver.address, amount: amount(30n * ATOMS) });
  const spent = allocations.reduce((sum, row) => sum + integer(row.amount), 0n);
  allocations.push({ address: treasury, amount: amount(total - spent) });
  const manifest = { ...template, params: "development-private", upgrades: [], chain_id: randomBytes(48).toString("hex"), allocations,
    validators: members.map((member) => ({ validator_id: member.validatorId,
      owner_address: member.owner.address, reward_address: member.owner.address, consensus_public_key: member.key,
      overlay_endpoint_hex: member.endpoint, self_bond_address: member.owner.address, commission_bps: 100 })) };
  assert.equal(manifest.validators.length, 3);
  assert.equal(allocations.reduce((sum, row) => sum + integer(row.amount), 0n), total);
  const manifestText = `${JSON.stringify(manifest, null, 2)}\n`;
  const manifestPath = join(run.data, "genesis.json"); await writeFile(manifestPath, manifestText, { flag: "wx" });
  const genesisOutput = await run.capture(node, ["--no-color", "genesis", "--manifest", manifestPath]);
  await writeFile(join(run.data, "public-genesis-identities.txt"), genesisOutput, { flag: "wx" });
  const nodeTemplate = JSON.parse(await readFile(join(resolve(options.repo), "config/node.example.json"), "utf8"));
  for (const member of members) {
    const config = { ...nodeTemplate, rpc_bind: `127.0.0.1:${member.ports.rpc}`, mining_bind: `127.0.0.1:${member.ports.mining}`,
      log: { ...nodeTemplate.log, debug: true, file: join(run.data, `validator-${members.indexOf(member)}-runtime.log`) },
      browser_origins: options.browserOrigins ?? [], api_landing_page: false, storage: { mode: options.pruned ? "pruned" : "full" },
      public_p2p: { listen_addrs: [multiaddr(member.ports.p2p)], bootstrap_peers: members.filter((other) => other !== member).map((other) => multiaddr(other.ports.p2p)),
        enable_upnp: false, allow_private_discovery: true, relay_service: false, connections_per_ip: 32 },
      validator_overlay: { listen_addrs: [multiaddr(member.ports.overlay)] },
      validator: { consensus_key_file: member.keyFile, password_file: member.password, heartbeat: {
        validator_id: member.validatorId, fee_key_file: member.heartbeatFile, fee_key_password_file: member.password,
        max_fee_atoms: amount(ATOMS) } }, tenq: { light: true } };
    member.config = join(member.data, "node.json"); await writeFile(member.config, `${JSON.stringify(config, null, 2)}\n`, { flag: "wx" });
    await run.capture(node, ["--no-color", "prepare-genesis-validator-signer", "--manifest", manifestPath,
      "--data-dir", member.data, "--consensus-public-key", member.key, "--confirm"]);
  }
  const startValidator = (member, index, suffix = "") => run.start(`validator-${index}${suffix}`, node, ["--no-color", "run", "--manifest", manifestPath,
    "--data-dir", member.data, "--config", member.config],
    { env: { RUST_LOG: "warn,tenraid=debug,tenrai_node=debug,tenrai_traidag_storage=debug" } });
  const running = members.map((member, index) => startValidator(member, index));
  let restartSerial = 0;
  const healthy = () => running.forEach((process) => process.healthy());
  const urls = members.map((member) => `http://127.0.0.1:${member.ports.rpc}`);
  await until("three independently signing private-genesis nodes", 180_000, async () => {
    const status = await Promise.all(urls.map((url) => json(`${url}/v1/private/status`)));
    const heads = await Promise.all(urls.map((url) => json(`${url}/v1/chain/head`)));
    return status.every((row) => row.active) && heads.every((row) => row.height >= 3);
  }, healthy);
  let mining, minerSerial = 0;
  const startMining = (index = 0) => {
    assert(!mining, "stop the owned miner before starting another");
    assert(Number.isInteger(index) && index >= 0 && index < members.length);
    const name = minerSerial++ === 0 ? "miner" : `miner-${minerSerial}`;
    mining = run.start(name, miner, ["--no-color", "--no-dashboard", "--no-api", "--cpu-only", "-t", "1", "--cpu-batch", "2",
      "--tenq-mode", "light", "-a", accounts.mining.address, "-s", `127.0.0.1:${members[index].ports.mining}`]);
  };
  const stopMining = async () => {
    if (!mining) return;
    const active = mining; active.child.kill();
    await active.child.waitOwnedDrain(); await active.ended; mining = undefined;
  };
  startMining();
  return { run, cli, node, miner, members, urls, url: urls[0], accounts, manifest, manifestText, manifestPath,
    healthy() { healthy(); mining?.healthy(); },
    startMining, stopMining,
    async restartValidators() {
      await stopMining();
      const before = await Promise.all(urls.map((url) => json(`${url}/v1/chain/head`)));
      const minimum = Math.min(...before.map((head) => head.height));
      const active = [...running];
      for (const process of active) process.child.kill();
      await Promise.all(active.map(async (process) => { await process.child.waitOwnedDrain(); await process.ended; }));
      const suffix = `-restart-${++restartSerial}`;
      members.forEach((member, index) => { running[index] = startValidator(member, index, suffix); });
      await until("three owned validators reopened their existing test stores", 180_000, async () => {
        const heads = await Promise.all(urls.map((url) => json(`${url}/v1/chain/head`)));
        return heads.every((head) => head.height >= minimum);
      }, healthy);
    },
    async finalized(transactionId) {
      const states = await Promise.all(urls.map((url) => until(`finalized original ${transactionId}`, 180_000, async () => {
        const state = await json(`${url}/v1/transactions/${transactionId}/status`); return state.lifecycle === "finalized" ? state : false;
      }, () => { healthy(); mining?.healthy(); })));
      assert(states.every((state) => state.finalized_cut && state.transaction_id === transactionId));
      assert(states.every((state) => state.finalized_cut.cut_id === states[0].finalized_cut.cut_id));
      return states[0];
    } };
}
