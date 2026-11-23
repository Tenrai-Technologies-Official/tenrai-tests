import { spawnOwned } from "./owned-process.mjs";
import { createWriteStream } from "node:fs";
import { mkdir, realpath, writeFile } from "node:fs/promises";
import { basename, dirname, isAbsolute, join, parse, resolve } from "node:path";
import { randomUUID } from "node:crypto";

export const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

export async function until(label, milliseconds, check, healthy = () => {}) {
  const end = Date.now() + milliseconds;
  let last = "";
  while (Date.now() < end) {
    healthy();
    try { const value = await check(); if (value) return value; }
    catch (error) { last = error instanceof Error ? error.message : String(error); }
    await sleep(250);
  }
  throw new Error(`${label} did not complete within ${milliseconds}ms${last ? `: ${last}` : ""}`);
}

/** This fixture creates one fresh child. It never removes a supplied or previous directory. */
export async function ownedRun(directory) {
  if (typeof directory !== "string" || !isAbsolute(directory)) throw new Error("--data must be a fresh absolute directory");
  const target = resolve(directory);
  if (target === parse(target).root) throw new Error("A filesystem root is not a fixture directory");
  const parent = await realpath(dirname(target));
  const data = join(parent, basename(target));
  await mkdir(data, { recursive: false, mode: 0o700 });
  const runId = randomUUID();
  await writeFile(join(data, ".tenrai-browser-production-owner"), `${runId}\n`, { flag: "wx", mode: 0o600 });
  return new ProcessSet(data, runId);
}

export class ProcessSet {
  #children = new Set();
  #cleanupFailure;
  constructor(data, runId) {
    this.stopped = new AbortController();
    this.signal = this.stopped.signal;
    this.data = data;
    this.runId = runId;
    const inherited = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.toUpperCase().startsWith("TENRAI_")));
    this.env = { ...inherited, LOCALAPPDATA: join(data, "profile"), APPDATA: join(data, "profile"),
      XDG_CONFIG_HOME: join(data, "profile/config"), XDG_DATA_HOME: join(data, "profile/data"), XDG_CACHE_HOME: join(data, "profile/cache"),
      TENRAI_WALLET_COIN_CACHE_DIRECTORY: join(data, "coin-cache") };
  }
  async initialize() {
    for (const name of ["profile", "profile/config", "profile/data", "profile/cache", "coin-cache"]) await mkdir(join(this.data, name), { recursive: true, mode: 0o700 });
  }

  /** Captured CLI output can contain a generated TEST recovery phrase; never log it on failure. */
  capture(executable, args, { timeout = 120_000, maximum = 8 * 1024 * 1024 } = {}) {
    if (this.signal.aborted) return Promise.reject(new Error("The owned fixture is stopping"));
    return new Promise((yes, no) => {
      const child = spawnOwned(executable, args, { env: this.env, cwd: this.data, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
      this.#children.add(child);
      this.#trackCleanup(child);
      let size = 0, failed;
      const stdout = [];
      const timer = setTimeout(() => { failed = new Error(`${basename(executable)} exceeded its CLI deadline`); child.kill(); }, timeout);
      child.stdout.on("data", (bytes) => {
        size += bytes.length;
        if (size > maximum) { failed = new Error(`${basename(executable)} exceeded its bounded CLI output`); child.kill(); }
        else stdout.push(bytes);
      });
      child.stderr.on("data", () => {});
      child.once("error", (error) => { failed = error; });
      child.once("close", (code) => {
        clearTimeout(timer); this.#children.delete(child);
        if (failed || code !== 0) no(failed ?? new Error(`${basename(executable)} ${args[0]} exited with ${code}; secret CLI output withheld`));
        else yes(Buffer.concat(stdout).toString("utf8"));
      });
    });
  }

  start(name, executable, args, options = {}) {
    if (this.signal.aborted) throw new Error("The owned fixture is stopping");
    if (!/^[a-z0-9-]{1,64}$/.test(name)) throw new Error("Invalid fixture process name");
    const child = spawnOwned(executable, args, { env: { ...this.env, ...options.env }, cwd: options.cwd ?? this.data,
      windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    this.#children.add(child);
    const output = createWriteStream(join(this.data, `${name}.log`), { flags: "wx" });
    let logged = 0, error;
    const copy = (bytes) => {
      const remaining = Math.max(0, 16 * 1024 * 1024 - logged);
      if (remaining) { const part = bytes.subarray(0, remaining); output.write(part); logged += part.length; }
    };
    child.stdout.on("data", copy); child.stderr.on("data", copy);
    child.once("error", (reason) => { error = reason; });
    const ended = new Promise((done) => child.once("close", () => { this.#children.delete(child); output.end(); done(); }));
    this.#trackCleanup(child);
    return { child, ended, healthy() { if (error || child.exitCode !== null || child.signalCode !== null) throw new Error(`${name} stopped unexpectedly${error ? `: ${error.message}` : ""}`); } };
  }

  #trackCleanup(child) {
    void child.ownedExit.then((ended) => {
      if (ended.error || ended.cleanupFailed) this.#cleanupFailure ??= ended.error ?? new Error("An owned fixture tree failed physical cleanup");
    });
  }

  async stop() {
    this.stopped.abort(new Error("The owned fixture is stopping"));
    const children = [...this.#children];
    for (const child of children) child.kill();
    await Promise.all(children.map((child) => child.waitOwnedDrain()));
    if (this.#cleanupFailure) throw this.#cleanupFailure;
  }
}

export async function json(url, options = {}) {
  const reply = await fetch(url, { ...options, signal: options.signal ?? AbortSignal.timeout(30_000) });
  if (!reply.ok) { await reply.body?.cancel(); throw new Error(`HTTP ${reply.status} ${new URL(url).pathname}`); }
  if (!reply.body) throw new Error("Missing fixture API reply body");
  const reader = reply.body.getReader();
  const chunks = []; let size = 0;
  try {
    for (;;) {
      const row = await reader.read(); if (row.done) break;
      size += row.value.byteLength;
      if (size > 16 * 1024 * 1024) { await reader.cancel(); throw new Error("Oversized fixture API reply"); }
      chunks.push(Buffer.from(row.value));
    }
  } finally { reader.releaseLock(); }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}
