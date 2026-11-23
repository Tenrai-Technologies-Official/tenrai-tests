// The measurement of measure.mjs in headless Firefox, over WebDriver BiDi: one fresh browser per input,
// the memory of its processes sampled every second while the proof runs.
//
//   node measure-firefox.mjs [--url http://127.0.0.1:28790/] [--threads 0] [--only NAME] [--minutes 30] [--out results.json]

import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseArgs } from "node:util";

import { browserExecutable } from "../browser-wallet/browser-executable.mjs";

const { values } = parseArgs({
  options: {
    url: { type: "string", default: "http://127.0.0.1:28790/" },
    binary: { type: "string", default: browserExecutable("firefox") },
    threads: { type: "string", default: "0" },
    only: { type: "string" },
    port: { type: "string", default: "9492" },
    minutes: { type: "string", default: "30" },
    out: { type: "string", default: "results.json" },
  },
});
const port = Number(values.port);
const profile = join(tmpdir(), `tenrai-browser-prover-firefox-${port}`);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const mib = (bytes) => Math.round(bytes / 1048576);

/** Firefox's processes of this run: the one started with the profile and its children. */
function processes() {
  const rows = process.platform === "win32"
    ? execFileSync("powershell", ["-NoProfile", "-Command", "Get-CimInstance Win32_Process -Filter \"Name='firefox.exe'\" | ForEach-Object { '{0}|{1}|{2}|{3}|{4}' -f $_.ProcessId, $_.ParentProcessId, $_.PrivatePageCount, $_.WorkingSetSize, $_.CommandLine }"], { encoding: "utf8", maxBuffer: 1 << 24 })
      .split(/\r?\n/)
      .filter(Boolean)
      .map((line) => {
        const [id, parent, committed, resident, ...command] = line.split("|");
        return { id, parent, committed: Number(committed), resident: Number(resident), command: command.join("|") };
      })
    : execFileSync("ps", ["-A", "-o", "pid=,ppid=,rss=,args="], { encoding: "utf8" })
      .split("\n")
      .filter((line) => line.includes("firefox"))
      .map((line) => {
        const [id, parent, rss, ...command] = line.trim().split(/\s+/);
        return { id, parent, committed: Number(rss) * 1024, resident: Number(rss) * 1024, command: command.join(" ") };
      });
  const mains = new Set(rows.filter((row) => row.command.includes(profile) || row.command.includes(profile.replaceAll("/", "\\"))).map((row) => row.id));
  return rows.filter((row) => mains.has(row.id) || mains.has(row.parent));
}

async function measure(name) {
  rmSync(profile, { recursive: true, force: true });
  mkdirSync(profile, { recursive: true });
  const browser = spawn(values.binary, ["--headless", "--no-remote", "--profile", profile, `--remote-debugging-port=${port}`, "about:blank"], { stdio: "ignore" });
  let socket;
  for (let attempt = 0; attempt < 60 && !socket; attempt += 1) {
    await sleep(500);
    try {
      const candidate = new WebSocket(`ws://127.0.0.1:${port}/session`);
      await new Promise((resolve, reject) => {
        candidate.addEventListener("open", resolve);
        candidate.addEventListener("error", reject);
      });
      socket = candidate;
    } catch {}
  }
  let next = 0;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message.result ?? message.error ?? message);
      pending.delete(message.id);
    }
  });
  const send = (method, params = {}) => new Promise((resolve) => {
    next += 1;
    pending.set(next, resolve);
    socket.send(JSON.stringify({ id: next, method, params }));
  });
  const session = await send("session.new", { capabilities: {} });
  const context = (await send("browsingContext.getTree", {})).contexts[0].context;
  await send("browsingContext.navigate", { context, url: `${values.url}?input=${encodeURIComponent(name)}&threads=${values.threads}`, wait: "complete" });
  const evaluate = async (expression) => (await send("script.evaluate", { expression, target: { context }, awaitPromise: false }))?.result?.value;
  const started = Date.now();
  let peak = 0;
  let peakResident = 0;
  const timeline = [];
  while (Date.now() - started < Number(values.minutes) * 60_000) {
    const rows = processes();
    const committed = rows.reduce((sum, row) => sum + row.committed, 0);
    const resident = rows.reduce((sum, row) => sum + row.resident, 0);
    peak = Math.max(peak, committed);
    peakResident = Math.max(peakResident, resident);
    timeline.push([Math.round((Date.now() - started) / 1000), mib(committed), mib(resident)]);
    if ((await evaluate("document.title")) === "done") break;
    await sleep(1000);
  }
  const [entry] = JSON.parse((await evaluate("JSON.stringify(window.results ?? [])")) ?? "[]");
  const isolated = await evaluate("self.crossOriginIsolated");
  const left = processes();
  try {
    await send("session.end", {});
  } catch {}
  socket.close();
  browser.kill();
  for (const row of left) {
    try {
      process.kill(Number(row.id));
    } catch {}
  }
  await sleep(2000);
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });
  const version = session?.capabilities ? `Firefox/${session.capabilities.browserVersion}` : "Firefox";
  return { ...(entry ?? { name, ok: false, error: "the page did not finish" }), browser: version, isolated, peak_private_bytes: peak, peak_resident_bytes: peakResident, timeline };
}

const names = (await (await fetch(`${values.url}inputs/`)).json()).filter((name) => !values.only || name === values.only);
const results = [];
for (const name of names) {
  const entry = await measure(name);
  results.push(entry);
  console.log(`${entry.name}\t${entry.ok ? "proof" : "refused"}\t${Math.round(entry.prove_ms ?? 0)} ms\twasm ${mib(entry.memory_bytes ?? 0)} MiB\tbrowser peak ${mib(entry.peak_private_bytes)} MiB committed, ${mib(entry.peak_resident_bytes)} MiB in RAM\t${entry.proof_bytes ?? ""}\t${entry.error ?? ""} ${(entry.notes ?? []).join(" / ").slice(0, 300)}`);
}
writeFileSync(values.out, `${JSON.stringify({ browser: results[0]?.browser, threads: Number(values.threads), results }, null, 2)}\n`);
