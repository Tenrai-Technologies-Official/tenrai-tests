import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseArgs } from "node:util";

import { browserExecutable } from "../browser-wallet/browser-executable.mjs";

const { values, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    minutes: { type: "string", default: "10" },
    every: { type: "string", default: "30" },
    port: { type: "string", default: "9491" },
    firefox: { type: "string", default: browserExecutable("firefox") },
  },
});
const url = positionals[0];
if (!url) {
  console.error("usage: node memwatch-firefox.mjs <url> [--minutes 10] [--every 30] [--port 9491]");
  process.exit(2);
}
const port = Number(values.port);
const profile = join(tmpdir(), `tenrai-memwatch-firefox-${port}`);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const mb = (bytes) => (bytes / 1048576).toFixed(1);

rmSync(profile, { recursive: true, force: true });
mkdirSync(profile, { recursive: true });
const browser = spawn(values.firefox, [
  "--headless", "--no-remote", "--profile", profile, `--remote-debugging-port=${port}`, "--width=1400", "--height=900", "about:blank",
], { stdio: "ignore" });

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

// Children are found by their parent, since the launcher hands the profile only to the main process.
function ours() {
  const rows = process.platform === "win32"
    ? execFileSync("powershell", ["-NoProfile", "-Command", "Get-CimInstance Win32_Process -Filter \"Name='firefox.exe'\" | ForEach-Object { '{0}|{1}|{2}|{3}' -f $_.ProcessId, $_.ParentProcessId, $_.PrivatePageCount, $_.CommandLine }"], { encoding: "utf8", maxBuffer: 1 << 24 })
      .split(/\r?\n/)
      .filter(Boolean)
      .map((line) => {
        const [id, parent, bytes, ...command] = line.split("|");
        return { id, parent, bytes: Number(bytes), command: command.join("|") };
      })
    : execFileSync("ps", ["-A", "-o", "pid=,ppid=,rss=,args="], { encoding: "utf8" })
      .split("\n")
      .filter((line) => line.includes("firefox"))
      .map((line) => {
        const [id, parent, rss, ...command] = line.trim().split(/\s+/);
        return { id, parent, bytes: Number(rss) * 1024, command: command.join(" ") };
      });
  const mains = new Set(rows.filter((row) => row.command.includes(profile) || row.command.includes(profile.replaceAll("/", "\\"))).map((row) => row.id));
  return rows
    .filter((row) => mains.has(row.id) || mains.has(row.parent))
    .map((row) => ({ ...row, kind: mains.has(row.id) ? "main" : row.command.trim().split(/\s+/).pop() }));
}

function processMemory() {
  const totals = {};
  for (const row of ours()) totals[row.kind] = (totals[row.kind] ?? 0) + row.bytes;
  return totals;
}

await send("session.new", { capabilities: {} });
const context = (await send("browsingContext.getTree", {})).contexts[0].context;
await send("browsingContext.navigate", { context, url, wait: "complete" });
await sleep(10_000);
const nodes = async () => (await send("script.evaluate", { expression: "document.getElementsByTagName('*').length", target: { context }, awaitPromise: false }))?.result?.value;
console.log(["minute", "elements", "main MB", "tab MB", "gpu MB", "other MB"].join("\t"));
const started = Date.now();
const sample = async (label) => {
  const memory = processMemory();
  const other = Object.entries(memory).filter(([kind]) => !["main", "tab", "gpu"].includes(kind)).reduce((sum, [, bytes]) => sum + bytes, 0);
  console.log([label, await nodes(), mb(memory.main ?? 0), mb(memory.tab ?? 0), mb(memory.gpu ?? 0), mb(other)].join("\t"));
};
while (Date.now() - started < Number(values.minutes) * 60_000) {
  await sample(((Date.now() - started) / 60_000).toFixed(1));
  await sleep(Number(values.every) * 1000);
}
await sample("end");
try {
  await send("session.end", {});
} catch {}
socket.close();
const left = ours();
browser.kill();
for (const row of left) {
  try {
    process.kill(Number(row.id));
  } catch {}
}
await sleep(2000);
rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });
