import assert from "node:assert/strict";
import test from "node:test";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, realpath, rm } from "node:fs/promises";
import { basename, dirname, join, sep } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { ownedRun, sleep } from "../processes.mjs";
import { spawnOwned } from "../owned-process.mjs";

const fixture = fileURLToPath(new URL("./fixtures/child-tree.mjs", import.meta.url));
const supported = process.platform === "win32" || process.platform === "linux";
const cleanup = new WeakMap();
const defer = (test, action) => cleanup.get(test).push(action);

async function scratch(t) {
  const parent = await realpath(tmpdir()), directory = await mkdtemp(join(parent, "tenrai-owned-tree-"));
  cleanup.set(t, []);
  t.after(async () => {
    for (const action of cleanup.get(t).reverse()) await action();
    const actual = await realpath(directory);
    assert.equal(dirname(actual), parent);
    assert(actual.startsWith(parent + sep) && basename(actual).startsWith("tenrai-owned-tree-"));
    await rm(actual, { recursive: true, force: true });
  });
  return directory;
}

async function ready(path) {
  const end = Date.now() + 8000;
  for (;;) {
    try {
      const row = JSON.parse(await readFile(path, "utf8"));
      assert(Number.isSafeInteger(row.parent) && row.parent > 0);
      assert(Number.isSafeInteger(row.grandchild) && row.grandchild > 0);
      assert.notEqual(row.parent, row.grandchild);
      return row;
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      if (Date.now() > end) throw new Error("Actual parent/grandchild did not become ready");
      await sleep(20);
    }
  }
}

function alive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { if (error.code === "ESRCH") return false; throw error; }
}

async function gone(row) {
  const end = Date.now() + 15_000;
  while (alive(row.parent) || alive(row.grandchild)) {
    if (Date.now() > end) throw new Error("Owned parent/grandchild remained alive after physical drain");
    await sleep(20);
  }
}

test("stop drains a real parent and real grandchild before resolving", { skip: !supported, timeout: 30_000 }, async (t) => {
  const root = await scratch(t), run = await ownedRun(join(root, "run")), info = join(root, "tree.json");
  await run.initialize(); defer(t, () => run.stop());
  const active = run.start("tree", process.execPath, [fixture, "waiting", info]);
  const row = await ready(info);
  assert(alive(row.parent) && alive(row.grandchild));
  await run.stop(); await active.ended;
  assert.equal(alive(row.parent), false); assert.equal(alive(row.grandchild), false);
});

for (const mode of ["deadline", "overflow"]) test(`capture ${mode} drains its actual grandchild`, { skip: !supported, timeout: 30_000 }, async (t) => {
  const root = await scratch(t), run = await ownedRun(join(root, "run")), info = join(root, "tree.json");
  await run.initialize(); defer(t, () => run.stop());
  const outcome = run.capture(process.execPath, [fixture, mode === "deadline" ? "waiting" : "overflow", info],
    { timeout: 10_000, maximum: mode === "overflow" ? 1024 : 8 * 1024 * 1024 }).then(
    () => ({ succeeded: true }), (error) => ({ error }));
  const row = await ready(info); // No vacuous timeout test before a grandchild actually existed.
  const result = await outcome;
  assert.match(result.error?.message ?? "unexpected success", mode === "deadline" ? /CLI deadline/ : /bounded CLI output/);
  assert.equal(alive(row.parent), false); assert.equal(alive(row.grandchild), false);
});

test("natural parent exit drains its still-live grandchild before reporting success", { skip: !supported, timeout: 30_000 }, async (t) => {
  const root = await scratch(t), run = await ownedRun(join(root, "run")), info = join(root, "tree.json");
  await run.initialize(); defer(t, () => run.stop());
  assert.equal(await run.capture(process.execPath, [fixture, "natural", info]), "TREE_READY\n");
  const row = await ready(info);
  assert.equal(alive(row.parent), false); assert.equal(alive(row.grandchild), false);
});

for (const detached of ["same-group", "detached"]) test(`host abort drains its real ${detached} grandchild via control-pipe EOF`,
  { skip: !supported, timeout: 35_000 }, async (t) => {
    const root = await scratch(t), info = join(root, "tree.json");
    const host = spawn(process.execPath, [fixture, "aborting-host", info, detached], { windowsHide: true, stdio: "ignore" });
    const ended = new Promise((done, fail) => { host.once("error", fail); host.once("close", (code) => done(code)); });
    let row;
    defer(t, async () => { if (host.exitCode === null && host.signalCode === null) host.kill(); await ended; if (row) await gone(row); });
    row = await ready(info);
    assert.equal(await ended, 17);
    await gone(row);
  });

test("an outer owned runner drains nested supervisors and detached targets", { skip: !supported, timeout: 35_000 }, async (t) => {
  const root = await scratch(t), info = join(root, "tree.json");
  const outer = spawnOwned(process.execPath, [fixture, "aborting-host", info, "detached"], { stdio: "ignore" });
  defer(t, async () => { outer.kill(); await outer.waitOwnedDrain(); });
  const row = await ready(info);
  assert.equal((await outer.waitOwnedDrain()).code, 17);
  assert.equal(alive(row.parent), false); assert.equal(alive(row.grandchild), false);
});

test("physical host termination closes the ownership pipe and drains a detached real grandchild", { skip: !supported, timeout: 35_000 }, async (t) => {
  const root = await scratch(t), info = join(root, "tree.json");
  const host = spawn(process.execPath, [fixture, "externally-terminated-host", info, "detached"], { windowsHide: true, stdio: "ignore" });
  const ended = new Promise((done, fail) => { host.once("error", fail); host.once("close", done); });
  let row;
  defer(t, async () => { if (host.exitCode === null && host.signalCode === null) host.kill(); await ended; if (row) await gone(row); });
  row = await ready(info);
  assert(alive(row.parent) && alive(row.grandchild));
  assert.equal(host.kill(), true); // Only this test's actual ChildProcess handle, never an inferred PID.
  await ended; await gone(row);
});
