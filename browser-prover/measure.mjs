// Opens the browser prover page in headless Chrome or Edge, one fresh browser per input, samples the private
// memory of the browser's processes every second while the proof runs, and writes what each proof took.
//
//   node measure.mjs [--url http://127.0.0.1:28790/] [--browser chrome|edge] [--threads 0] [--only NAME] [--minutes 30] [--out results.json]

import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseArgs } from "node:util";

import { browserExecutable } from "../browser-wallet/browser-executable.mjs";

const { values } = parseArgs({
  options: {
    url: { type: "string", default: "http://127.0.0.1:28790/" },
    browser: { type: "string", default: "chrome" },
    binary: { type: "string" },
    threads: { type: "string", default: "0" },
    only: { type: "string" },
    port: { type: "string", default: "9472" },
    minutes: { type: "string", default: "30" },
    out: { type: "string", default: "results.json" },
  },
});
const binary = values.binary ?? browserExecutable(values.browser);
const port = Number(values.port);
const tag = `tenrai-browser-prover-${port}`;
const profile = join(tmpdir(), tag);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const mib = (bytes) => Math.round(bytes / 1048576);

/** Private and resident bytes of every process of this browser run, summed and by process kind. */
function processMemory() {
  const totals = { private: 0, resident: 0, kinds: [] };
  if (process.platform === "win32") {
    const image = values.browser === "edge" ? "msedge.exe" : "chrome.exe";
    const script = `Get-CimInstance Win32_Process -Filter "Name='${image}'" | Where-Object { $_.CommandLine -like '*${tag}*' } | `
      + `ForEach-Object { $kind = if ($_.CommandLine -match '--type=([a-z-]+)') { $Matches[1] } else { 'browser' }; "$($_.PrivatePageCount) $($_.WorkingSetSize) $kind" }`;
    for (const line of execFileSync("powershell", ["-NoProfile", "-Command", script], { encoding: "utf8" }).split(/\r?\n/)) {
      const [committed, resident, kind] = line.trim().split(" ");
      if (committed) {
        totals.private += Number(committed);
        totals.resident += Number(resident);
        totals.kinds.push(`${kind} ${mib(Number(committed))}/${mib(Number(resident))}`);
      }
    }
  } else {
    for (const line of execFileSync("ps", ["-A", "-o", "rss=,args="], { encoding: "utf8" }).split("\n")) {
      if (!line.includes(tag)) continue;
      const resident = Number(line.trim().split(/\s+/)[0]) * 1024;
      totals.private += resident;
      totals.resident += resident;
      totals.kinds.push(`${/--type=([a-z-]+)/.exec(line)?.[1] ?? "browser"} ${mib(resident)}/${mib(resident)}`);
    }
  }
  return totals;
}

/** One proof in a browser of its own, so the memory peak belongs to that proof alone. */
async function measure(name) {
  rmSync(profile, { recursive: true, force: true });
  mkdirSync(profile, { recursive: true });
  const browser = spawn(binary, [
    "--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "about:blank",
  ], { stdio: "ignore" });
  let address;
  for (let attempt = 0; attempt < 100 && !address; attempt += 1) {
    await sleep(200);
    try {
      address = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((target) => target.type === "page")?.webSocketDebuggerUrl;
    } catch {}
  }
  const socket = new WebSocket(address);
  await new Promise((resolve) => socket.addEventListener("open", resolve));
  let next = 0;
  const pending = new Map();
  const consoleLines = [];
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id) {
      pending.get(message.id)?.(message.result ?? {});
      pending.delete(message.id);
    } else if (message.method === "Runtime.exceptionThrown") {
      consoleLines.push(`exception: ${message.params.exceptionDetails.exception?.description ?? message.params.exceptionDetails.text}`);
    }
  });
  const send = (method, params = {}) => new Promise((resolve) => {
    next += 1;
    pending.set(next, resolve);
    socket.send(JSON.stringify({ id: next, method, params }));
  });
  const evaluate = async (expression) => (await send("Runtime.evaluate", { expression, returnByValue: true })).result?.value;

  await send("Runtime.enable");
  await send("Page.enable");
  const version = (await (await fetch(`http://127.0.0.1:${port}/json/version`)).json()).Browser;
  await send("Page.navigate", { url: `${values.url}?input=${encodeURIComponent(name)}&threads=${values.threads}` });
  const started = Date.now();
  let peak = 0;
  let peakResident = 0;
  let peakKinds = [];
  const timeline = [];
  while (Date.now() - started < Number(values.minutes) * 60_000) {
    const memory = processMemory();
    if (memory.private > peak) peakKinds = memory.kinds;
    peak = Math.max(peak, memory.private);
    peakResident = Math.max(peakResident, memory.resident);
    timeline.push([Math.round((Date.now() - started) / 1000), mib(memory.private), mib(memory.resident)]);
    if ((await evaluate("document.title")) === "done") break;
    await sleep(1000);
  }
  const [entry] = JSON.parse((await evaluate("JSON.stringify(window.results ?? [])")) ?? "[]");
  const isolated = await evaluate("self.crossOriginIsolated");
  socket.close();
  browser.kill();
  await sleep(1500);
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });
  return { ...(entry ?? { name, ok: false, error: "the page did not finish" }), browser: version, isolated, peak_private_bytes: peak, peak_resident_bytes: peakResident, peak_processes: peakKinds, timeline, console: consoleLines.slice(-20) };
}

const names = (await (await fetch(`${values.url}inputs/`)).json()).filter((name) => !values.only || name === values.only);
const results = [];
for (const name of names) {
  const entry = await measure(name);
  results.push(entry);
  console.log(`${entry.name}\t${entry.ok ? "proof" : "refused"}\t${Math.round(entry.prove_ms ?? 0)} ms\twasm ${mib(entry.memory_bytes ?? 0)} MiB\tbrowser peak ${mib(entry.peak_private_bytes)} MiB committed, ${mib(entry.peak_resident_bytes)} MiB in RAM\t${entry.proof_bytes ?? ""}\t${entry.error ?? ""} ${(entry.notes ?? []).join(" / ").slice(0, 300)}
  processes at the peak, committed/in RAM MiB: ${entry.peak_processes.join(", ")}`);
}
writeFileSync(values.out, `${JSON.stringify({ browser: results[0]?.browser, threads: Number(values.threads), results }, null, 2)}\n`);
