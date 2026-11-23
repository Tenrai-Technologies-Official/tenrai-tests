// A real process tree for lifecycle tests, not a monetary/finality fixture.
import { spawn } from "node:child_process";
import { readFile, rename, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const [mode, information, detached] = process.argv.slice(2);
const self = fileURLToPath(import.meta.url);
// Finite safety net even if a cleanup regression leaves this fixture alive.
setTimeout(() => process.exit(93), 45_000).unref();
if (mode === "grandchild") {
  process.stdout.write("GRANDCHILD_READY\n");
  setInterval(() => {}, 1000);
} else if (mode === "aborting-host" || mode === "externally-terminated-host") {
  const { spawnOwned } = await import("../../owned-process.mjs");
  const child = spawnOwned(process.execPath, [self, "waiting", information, detached], { stdio: "ignore" });
  const end = Date.now() + 15_000;
  for (;;) {
    try { JSON.parse(await readFile(information, "utf8")); break; }
    catch { if (Date.now() > end) throw new Error("Real grandchild did not become ready"); }
    await new Promise((done) => setTimeout(done, 20));
  }
  // Deliberately no finally, stop(), kill() or waitOwnedDrain(): the owned stdin EOF must drain.
  if (child.exitCode !== null || child.signalCode !== null) throw new Error("Tree exited before host abort");
  if (mode === "externally-terminated-host") await new Promise(() => {});
  process.exit(17);
} else {
  const child = spawn(process.execPath, [self, "grandchild"], {
    windowsHide: true, detached: detached === "detached", stdio: ["ignore", "pipe", "ignore"],
  });
  await new Promise((done, fail) => {
    child.once("error", fail);
    child.once("exit", () => fail(new Error("Grandchild exited before ready")));
    child.stdout.once("data", (data) => data.toString("utf8").includes("GRANDCHILD_READY") ? done() : fail(new Error("Invalid grandchild readiness")));
  });
  await writeFile(information + ".partial", JSON.stringify({ parent: process.pid, grandchild: child.pid }), { flag: "wx" });
  await rename(information + ".partial", information);
  process.stdout.write("TREE_READY\n");
  if (mode === "natural") process.exit(0);
  if (mode === "overflow") process.stdout.write("x".repeat(1024 * 1024));
  setInterval(() => {}, 1000);
}
