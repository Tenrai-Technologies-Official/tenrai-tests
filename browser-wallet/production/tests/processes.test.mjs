import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, readFile, realpath, rm, stat } from "node:fs/promises";
import { basename, dirname, join, sep } from "node:path";
import { tmpdir } from "node:os";
import { ownedRun } from "../processes.mjs";

async function scratch(t) {
  const parent = await realpath(tmpdir()), root = await mkdtemp(join(parent, "tenrai-browser-process-"));
  t.after(async () => { const path = await realpath(root); assert.equal(dirname(path), parent);
    assert(path.startsWith(parent + sep) && basename(path).startsWith("tenrai-browser-process-")); await rm(path, { recursive: true, force: true }); });
  return root;
}

test("an existing directory and its sentinel are never reset", async (t) => {
  const root = await scratch(t), existing = join(root, "existing"); await mkdir(existing);
  await assert.rejects(ownedRun(existing), /exist/i);
  const fresh = await ownedRun(join(root, "new"));
  assert.equal((await readFile(join(fresh.data, ".tenrai-browser-production-owner"), "utf8")).trim(), fresh.runId);
  await assert.rejects(ownedRun(fresh.data), /exist/i);
});

test("actual owned subprocess closure drains before stop resolves and refuses new work", async (t) => {
  const root = await scratch(t), fixture = await ownedRun(join(root, "new")); await fixture.initialize();
  const child = fixture.start("waiting", process.execPath, ["-e", "process.stdout.write('ready');setInterval(()=>{},1000)"]);
  assert.equal(await fixture.capture(process.execPath, ["-e", "process.stdout.write('captured')"]), "captured");
  await fixture.stop(); await child.ended;
  assert(child.child.exitCode !== null || child.child.signalCode !== null); assert(fixture.signal.aborted);
  await assert.rejects(fixture.capture(process.execPath, ["-e", "process.stdout.write('new-process')"]), /stopping/);
  await fixture.stop();
});

test("capture output budget stops the actual writer instead of growing a fixture array", async (t) => {
  const root = await scratch(t), fixture = await ownedRun(join(root, "new"));
  try { await assert.rejects(fixture.capture(process.execPath, ["-e", "process.stdout.write('x'.repeat(1000000));setInterval(()=>{},1000)"], { maximum: 1024 }), /bounded CLI output/); }
  finally { await fixture.stop(); }
});


test("inherited Tenrai config paths cannot select an existing user store or key", async (t) => {
  const root = await scratch(t), previous = process.env.TENRAI_API_CONFIG;
  process.env.TENRAI_API_CONFIG = "forbidden-existing-user-config";
  try {
    const fixture = await ownedRun(join(root, "new"));
    assert.equal(fixture.env.TENRAI_API_CONFIG, undefined);
    assert(fixture.env.TENRAI_WALLET_COIN_CACHE_DIRECTORY.startsWith(fixture.data + sep));
    assert.equal(fixture.env.HOME, process.env.HOME, "never repurpose HOME");
    await fixture.stop();
  } finally {
    if (previous === undefined) delete process.env.TENRAI_API_CONFIG; else process.env.TENRAI_API_CONFIG = previous;
  }
});


test("initialize creates the configured retained-cache parent inside the owned run", async (t) => {
  const root = await scratch(t), fixture = await ownedRun(join(root, "new"));
  const configured = fixture.env.TENRAI_WALLET_COIN_CACHE_DIRECTORY;
  await assert.rejects(stat(configured), (error) => error.code === "ENOENT");
  await fixture.initialize();
  const actual = await realpath(configured);
  assert.equal(dirname(actual), await realpath(fixture.data));
  assert.equal(basename(actual), "coin-cache");
  assert((await stat(actual)).isDirectory());
  await fixture.initialize(); // Idempotent setup keeps this same owned parent.
  assert.equal(await realpath(configured), actual);
  await fixture.stop();
});
