import assert from "node:assert/strict";
import test from "node:test";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, realpath, rm } from "node:fs/promises";
import { basename, dirname, join, sep } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

test("inherited SIGCHLD ignore cannot auto-reap the owned WNOWAIT leader", { skip: process.platform !== "linux", timeout: 30_000 }, async (t) => {
  const parent = await realpath(tmpdir()), root = await mkdtemp(join(parent, "tenrai-owned-sigchld-"));
  const info = join(root, "actual-tree.json");
  const launcher = fileURLToPath(new URL("./fixtures/ignored_sigchld.py", import.meta.url));
  const tree = fileURLToPath(new URL("./fixtures/child-tree.mjs", import.meta.url));
  const child = spawn("python3", ["-I", launcher, process.execPath, tree, "natural", info, "detached"],
    { windowsHide: true, stdio: ["pipe", "ignore", "pipe"] });
  child.stdin.on("error", () => {});
  let diagnostics = "", row;
  child.stderr.on("data", (data) => { if (diagnostics.length < 4096) diagnostics += data.toString("utf8").slice(0, 4096 - diagnostics.length); });
  const ended = new Promise((done, fail) => { child.once("error", fail); child.once("close", (code) => done(code)); });
  const alive = (pid) => {
    try { process.kill(pid, 0); return true; }
    catch (error) { if (error.code === "ESRCH") return false; throw error; }
  };
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) child.stdin.end("STOP\n");
    await ended;
    if (row) { assert.equal(alive(row.parent), false); assert.equal(alive(row.grandchild), false); }
    const actual = await realpath(root); assert.equal(dirname(actual), parent);
    assert(actual.startsWith(parent + sep) && basename(actual).startsWith("tenrai-owned-sigchld-"));
    await rm(actual, { recursive: true, force: true });
  });
  const end = Date.now() + 10_000;
  for (;;) {
    try { row = JSON.parse(await readFile(info, "utf8")); break; }
    catch (error) { if (error.code !== "ENOENT") throw error; }
    if (Date.now() > end || child.exitCode !== null) throw new Error(`Actual grandchild was not started: ${diagnostics}`);
    await new Promise((done) => setTimeout(done, 20));
  }
  assert.equal(await ended, 0, diagnostics);
  assert(Number.isSafeInteger(row.parent) && row.parent > 0);
  assert(Number.isSafeInteger(row.grandchild) && row.grandchild > 0);
  assert.equal(alive(row.parent), false); assert.equal(alive(row.grandchild), false);
});
