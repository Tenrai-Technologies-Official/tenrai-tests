import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseArgs } from "node:util";

import { browserExecutable } from "../browser-wallet/browser-executable.mjs";

const KINDS = ["renderer", "gpu-process", "browser", "utility"];

const { values, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    minutes: { type: "string", default: "10" },
    every: { type: "string", default: "30" },
    port: { type: "string", default: "9471" },
    chrome: { type: "string", default: browserExecutable("chrome") },
    run: { type: "string" },
  },
});
const url = positionals[0];
if (!url) {
  console.error("usage: node memwatch.mjs <url> [--minutes 10] [--every 30] [--port 9471] [--run <expression>]");
  process.exit(2);
}
const port = Number(values.port);
const profile = join(tmpdir(), `tenrai-memwatch-${port}`);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const mb = (bytes) => (bytes / 1048576).toFixed(1);

rmSync(profile, { recursive: true, force: true });
mkdirSync(profile, { recursive: true });
const browser = spawn(values.chrome, [
  "--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--window-size=1400,900", "about:blank",
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
const errors = [];
socket.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id) {
    pending.get(message.id)?.(message.result ?? {});
    pending.delete(message.id);
  } else if (message.method === "Runtime.exceptionThrown") {
    errors.push(message.params.exceptionDetails.exception?.description ?? message.params.exceptionDetails.text);
  }
});
const send = (method, params = {}) => new Promise((resolve) => {
  next += 1;
  pending.set(next, resolve);
  socket.send(JSON.stringify({ id: next, method, params }));
});

function processMemory() {
  const totals = {};
  const add = (kind, bytes) => {
    totals[kind] = (totals[kind] ?? 0) + bytes;
  };
  if (process.platform === "win32") {
    const script = `Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object { $_.CommandLine -like '*tenrai-memwatch-${port}*' } | ForEach-Object { $kind = if ($_.CommandLine -match '--type=([a-z-]+)') { $matches[1] } else { 'browser' }; "$kind $($_.PrivatePageCount)" }`;
    for (const line of execFileSync("powershell", ["-NoProfile", "-Command", script], { encoding: "utf8" }).split(/\r?\n/)) {
      const [kind, bytes] = line.trim().split(" ");
      if (kind) add(kind, Number(bytes));
    }
  } else {
    for (const line of execFileSync("ps", ["-A", "-o", "rss=,args="], { encoding: "utf8" }).split("\n")) {
      if (!line.includes(`tenrai-memwatch-${port}`)) continue;
      const kind = /--type=([a-z-]+)/.exec(line)?.[1] ?? "browser";
      add(kind, Number(line.trim().split(/\s+/)[0]) * 1024);
    }
  }
  return totals;
}

async function sample(label) {
  await send("HeapProfiler.collectGarbage");
  await sleep(500);
  const { metrics = [] } = await send("Performance.getMetrics");
  const metric = Object.fromEntries(metrics.map(({ name, value }) => [name, value]));
  const memory = processMemory();
  console.log([
    label, mb(metric.JSHeapUsedSize), mb(metric.JSHeapTotalSize), metric.Nodes, metric.JSEventListeners,
    metric.LayoutObjects, ...KINDS.map((kind) => mb(memory[kind] ?? 0)),
  ].join("\t"));
}

await send("Runtime.enable");
await send("Performance.enable");
await send("Page.enable");
await send("Page.navigate", { url });
await sleep(10_000);
if (values.run) await send("Runtime.evaluate", { expression: values.run, awaitPromise: true, userGesture: true });
console.log(["minute", "heap MB", "heap total", "nodes", "listeners", "layout", ...KINDS.map((kind) => `${kind} MB`)].join("\t"));
const started = Date.now();
while (Date.now() - started < Number(values.minutes) * 60_000) {
  await sample(((Date.now() - started) / 60_000).toFixed(1));
  await sleep(Number(values.every) * 1000);
}
await sample("end");
if (errors.length) console.log("exceptions:", errors.join(" | "));
socket.close();
browser.kill();
await sleep(1500);
rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });
