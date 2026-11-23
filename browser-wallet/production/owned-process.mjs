import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

let python = process.platform === "win32" ? "python" : "python3";
export function configureOwnedPython(executable) {
  if (executable !== undefined) {
    if (typeof executable !== "string" || !executable || executable.length > 4096) throw new TypeError("Invalid fixture Python executable");
    python = executable;
  }
}

/** Fixture-only ownership: the supervisor never releases its private Job/group before physical drain. */
export function spawnOwned(executable, arguments_, options = {}) {
  const output = options.stdio === "ignore" ? ["ignore", "ignore"] : [options.stdio?.[1] ?? "pipe", options.stdio?.[2] ?? "pipe"];
  const child = spawn(python, ["-I", "-u", fileURLToPath(new URL("./owned_process.py", import.meta.url)), executable, ...arguments_], {
    ...options, windowsHide: true, stdio: ["pipe", ...output] });
  let asked = false, failure;
  child.once("error", (error) => { failure = error; });
  child.stdin.on("error", () => { /* A target that already drained can close the stop pipe first. */ });
  child.ownedExit = new Promise((done) => child.once("close", (code, signal) => done({ code, signal, error: failure,
    cleanupFailed: code === 249 || signal !== null })));
  child.requestOwnedStop = () => {
    if (asked || child.exitCode !== null || child.signalCode !== null) return false;
    asked = true; child.stdin.end("STOP\n"); return true;
  };
  // Existing fixture callers use kill(), but it now stops the exact owned Job/group, not only Python.
  child.kill = () => child.requestOwnedStop();
  child.waitOwnedDrain = async (milliseconds = 15_000) => {
    let timer;
    try {
      const ended = await Promise.race([child.ownedExit, new Promise((_, fail) => {
        timer = setTimeout(() => fail(new Error("Owned fixture tree cleanup did not acknowledge physical drain")), milliseconds);
      })]);
      if (ended.error || ended.cleanupFailed) throw ended.error ?? new Error("Owned fixture tree cleanup failed");
      return ended;
    } finally { clearTimeout(timer); }
  };
  return child;
}
