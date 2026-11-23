import assert from "node:assert/strict";
import test from "node:test";
import { spawn } from "node:child_process";
import { DatabaseSync } from "../sqlite.mjs";
import { mkdtemp, realpath, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve, sep } from "node:path";
import { journalStore } from "../sqlite-wallet.mjs";

async function scratch(t) {
  const parent = await realpath(tmpdir()), path = await mkdtemp(join(parent, "tenrai-browser-journal-"));
  t.after(async () => {
    const actual = await realpath(path);
    assert.equal(dirname(actual), parent); assert(basename(actual).startsWith("tenrai-browser-journal-"));
    assert(resolve(actual).startsWith(parent + sep)); await rm(actual, { recursive: true, force: true });
  }); return path;
}
const identity = "fixture:operations:one", chain = "ab".repeat(48);

test("actual EXTRA disk commits preserve exact bytes after physical close/reopen", async (t) => {
  const path = await scratch(t), first = journalStore(path, identity, chain);
  const lease = await first.store.acquireSendsLease(); const value = Uint8Array.of(1, 2, 3, 4);
  await first.store.write("sends", value); await first.publicTransfers.write(value); value.fill(9);
  assert.deepEqual(await first.store.read("sends"), Uint8Array.of(1, 2, 3, 4));
  await lease.release(); first.close();
  const second = journalStore(path, identity, chain), reopened = await second.store.acquireSendsLease();
  assert.deepEqual(await second.publicTransfers.read(), Uint8Array.of(1, 2, 3, 4));
  await reopened.release(); second.close();
});

test("an actual second process cannot own the same fixture journal before release", async (t) => {
  const path = await scratch(t), owner = journalStore(path, identity, chain), lease = await owner.store.acquireSendsLease();
  const module = new URL("../sqlite-wallet.mjs", import.meta.url).href;
  const child = async () => {
    const code = `import {journalStore} from ${JSON.stringify(module)};let store;try{store=journalStore(${JSON.stringify(path)},${JSON.stringify(identity)},${JSON.stringify(chain)});const lease=await store.store.acquireSendsLease();await lease.release();store.close();console.log("owned");}catch(error){store?.close();if(!/locked|busy/i.test(String(error)))throw error;console.log("busy");}`;
    const process = spawn(globalThis.process.execPath, ["--input-type=module", "-e", code], { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    const output = [], errors = []; let bytes = 0;
    const capture = (target) => (chunk) => { bytes += chunk.length; if (bytes > 8192) process.kill(); else target.push(chunk); };
    process.stdout.on("data", capture(output)); process.stderr.on("data", capture(errors));
    const status = await new Promise((done, fail) => { process.once("error", fail); process.once("close", done); });
    assert.equal(status, 0, Buffer.concat(errors).toString("utf8")); return Buffer.concat(output).toString("utf8").trim();
  };
  try { assert.equal(await child(), "busy"); await lease.release(); owner.close(); assert.equal(await child(), "owned"); }
  finally { owner.close(); }
});

test("a missing previously durable original refuses absence after restart", async (t) => {
  const path = await scratch(t), owner = journalStore(path, identity, chain), lease = await owner.store.acquireSendsLease();
  await owner.publicTransfers.write(Uint8Array.of(1, 2)); await lease.release(); owner.close();
  const damage = new DatabaseSync(join(path, "operations.sqlite")); damage.exec("DELETE FROM records WHERE name='public'"); damage.close();
  const reopened = journalStore(path, identity, chain), held = await reopened.store.acquireSendsLease();
  try { await assert.rejects(reopened.publicTransfers.read(), /disappeared/); }
  finally { await held.release(); reopened.close(); }
});

test("uncertain physical write latches the fixture instead of returning fresh absence", async (t) => {
  const path = await scratch(t), owner = journalStore(path, identity, chain), lease = await owner.store.acquireSendsLease();
  const faults = new DatabaseSync(join(path, "operations.sqlite"));
  faults.exec("CREATE TRIGGER failed_write BEFORE INSERT ON records BEGIN SELECT RAISE(FAIL,'fixture disk failure'); END"); faults.close();
  await assert.rejects(owner.store.write("sends", Uint8Array.of(7)), /fixture disk failure/);
  await assert.rejects(owner.store.read("sends"), /not safely owned/);
  await lease.release(); owner.close();
  const reopened = journalStore(path, identity, chain), held = await reopened.store.acquireSendsLease();
  try { assert.equal(await reopened.store.read("sends"), null); }
  finally { await held.release(); reopened.close(); }
});
