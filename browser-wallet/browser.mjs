// One Chrome with its own profile, driven over the DevTools protocol, for the wallet tests in this folder.

import { execFileSync } from "node:child_process";
import { spawnOwned } from "./production/owned-process.mjs";
import { BrowserAssets } from "./production/browser-assets.mjs";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, sep } from "node:path";

export const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
/** Ends a run early on purpose: what it set out to do is done. */
export class RunEnds extends Error {}
/** What a page may hold once a proof's worker ended; the proof itself needs about 3 GiB. */
const PROOF_LEFT_MIB = 900;
/** A person turning the wheel over what they use next, which a wallet's idle lock counts as someone there; unlike a
 * synthetic pointer press it starts no sound, which a page may only start after a real gesture. */
export const PRESS = (node) => `${node}.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: 0 }));`;

/** A port nothing listens on now, so that tests can run beside each other. */
export function freePort() {
  return new Promise((done, failed) => {
    const probe = createServer();
    probe.once("error", failed);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => done(port));
    });
  });
}

let nextProfile = 0;

/** Chrome's helper processes can hold a profile for some seconds after the browser ended; a profile still held
 * after half a minute is a process that did not end. */
async function removeProfile(profile) {
  for (let attempt = 0; ; attempt += 1) {
    try { rmSync(profile, { recursive: true, force: true }); return; }
    catch (error) {
      if (!["EPERM", "EBUSY", "ENOTEMPTY"].includes(error.code) || attempt >= 60) throw error;
      await sleep(500);
    }
  }
}

export class Browser {
  /** Starts a browser on `url`; `flags` go on its command line. */
  static async open(name, { chrome, url, shots, profileRoot, captureAssets = false, flags = [], loaded = "document.readyState === 'complete'" }) {
    const port = await freePort();
    // A closed page frees its port at once while its browser may still hold the profile: the serial keeps profiles apart.
    const profile = mkdtempSync(join(profileRoot ?? tmpdir(), `tenrai-e2e-${process.pid}-${port}-${nextProfile++}-`));
    const child = spawnOwned(chrome, [
      "--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "--no-first-run",
      "--no-default-browser-check", "--window-size=1400,1400", "--lang=en-US", "--accept-lang=en-US", ...flags, "about:blank",
    ], { stdio: "ignore", windowsHide: true });
    let browser;
    try {
      let address;
      for (let attempt = 0; attempt < 150 && !address; attempt += 1) {
        await sleep(200);
        try {
          address = (await (await fetch(`http://127.0.0.1:${port}/json`, { signal: AbortSignal.timeout(2000) })).json()).find((target) => target.type === "page")?.webSocketDebuggerUrl;
        } catch {}
      }
      if (!address) { child.kill(); await child.waitOwnedDrain(); throw new Error(`${name}: Chrome DevTools did not start`); }
      browser = new this();
      browser.name = name;
      browser.url = url;
      browser.loaded = loaded;
      browser.shots = shots;
      browser.process = child;
      browser.profile = profile;
      browser.next = 0;
      browser.pending = new Map();
      browser.problems = [];
      browser.busy = [];
      browser.requests = [];
      if (captureAssets) browser.assets = new BrowserAssets(browser);
      await browser.connect(address);
      browser.socket.addEventListener("message", (event) => browser.heard(JSON.parse(event.data)));
      browser.socket.addEventListener("close", () => browser.endPending());
      await browser.send("Page.enable");
      await browser.send("Runtime.enable");
      await browser.send("Log.enable");
      await browser.send("Inspector.enable");
      await browser.send("Network.enable", { maxPostDataSize: 8 << 20, maxTotalBufferSize: 64 << 20 });
      if (captureAssets) {
        await browser.send("Network.setCacheDisabled", { cacheDisabled: true });
        await browser.send("Network.setBypassServiceWorker", { bypass: true });
      }
      await browser.goto();
      return browser;
    } catch (error) {
      browser?.socket?.close(); browser?.endPending();
      child.kill(); await child.waitOwnedDrain();
      // A profile that stays locked must not hide why the browser did not open.
      await removeProfile(profile).catch((cleanup) => { error.message += `; its profile stayed: ${cleanup.message}`; });
      throw error;
    }
  }

  /** Takes over a browser or web view that already listens on `port`; `stop` ends it. */
  static async attach(name, { port, shots, stop, loaded = "document.readyState === 'complete'" }) {
    let address;
    let url;
    for (let attempt = 0; attempt < 300 && !address; attempt += 1) {
      await sleep(200);
      try {
        const page = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((target) => target.type === "page");
        address = page?.webSocketDebuggerUrl;
        url = page?.url;
      } catch {}
    }
    const browser = new this();
    browser.name = name;
    browser.url = url;
    browser.loaded = loaded;
    browser.shots = shots;
    browser.process = { kill: stop };
    browser.profile = null;
    browser.next = 0;
    browser.pending = new Map();
    browser.problems = [];
    browser.busy = [];
    browser.requests = [];
    browser.socket = new WebSocket(address);
    await new Promise((done) => browser.socket.addEventListener("open", done));
    browser.socket.addEventListener("message", (event) => browser.heard(JSON.parse(event.data)));
    browser.socket.addEventListener("close", () => browser.endPending());
    await browser.send("Page.enable");
    await browser.send("Runtime.enable");
    await browser.send("Log.enable");
    await browser.send("Inspector.enable");
    await browser.until(loaded, 60, "the page to load");
    return browser;
  }

  heard(message) {
    this.assets?.heard(message);
    if (message.id) {
      this.pending.get(message.id)?.(message);
      this.pending.delete(message.id);
    } else if (message.method === "Inspector.targetCrashed") {
      // Nothing answers after this: whoever waits, and whoever asks later, hears that the page is gone.
      this.crashed = true;
      for (const answer of this.pending.values()) answer({ error: { message: "the page crashed" } });
      this.pending.clear();
    } else if (message.method === "Runtime.exceptionThrown") {
      this.problems.push(message.params.exceptionDetails.exception?.description ?? message.params.exceptionDetails.text);
    } else if (message.method === "Log.entryAdded" && message.params.entry.level === "error" && !/favicon|status of 404|status of 409|ERR_INTERNET_DISCONNECTED|ERR_BLOCKED_BY_CLIENT/.test(message.params.entry.text)) {
      const { text, url } = message.params.entry;
      // A busy node or a client over its budget says so, and the wallet asks again; anything else is a problem.
      if (/status of (429|503)/.test(text)) this.busy.push(url);
      else this.problems.push(url ? `${text} (${url})` : text);
    } else if (message.method === "Log.entryAdded" && message.params.entry.level === "warning") {
      // A page for the mainnet shows no warning either, its workers' included.
      const { text, url } = message.params.entry;
      this.problems.push(`warning: ${url ? `${text} (${url})` : text}`);
    } else if (message.method === "Runtime.consoleAPICalled" && ["error", "warning", "assert"].includes(message.params.type)) {
      const text = message.params.args.map((argument) => argument.value ?? argument.description ?? "").join(" ");
      this.problems.push(`console.${message.params.type}: ${text.slice(0, 500)}`);
    } else if (message.method === "Network.requestWillBeSent" && message.params.request.method === "POST"
      && /\/v1\/private\//.test(message.params.request.url)) {
      // Only the private submissions are kept, which the steps look at; a wallet's routine reads would fill the bound.
      const bytes = Buffer.byteLength(message.params.request.postData ?? "");
      this.requestBytes = (this.requestBytes ?? 0) + bytes;
      if (this.requests.length >= 256 || this.requestBytes > 128 * 1024 * 1024) {
        if (!this.captureOverflow) this.problems.push("bounded POST capture exceeded its fixture budget");
        this.captureOverflow = true;
      } else this.requests.push({ url: message.params.request.url, id: message.params.requestId, body: message.params.request.postData ?? null });
    }
  }

  /** Keeps a page's private statements from leaving, whatever else it reads meanwhile. */
  holdBack(on) {
    return this.send("Network.setBlockedURLs", { urls: on ? ["*/v1/private/enters", "*/v1/private/transfers", "*/v1/private/exits"] : [] });
  }

  endPending() {
    for (const answer of this.pending.values()) answer({ error: { message: "The owned browser connection closed" } });
    this.pending.clear();
  }

  async connect(address) {
    this.socket = new WebSocket(address);
    let timer;
    try {
      await new Promise((done, fail) => {
        this.socket.addEventListener("open", done, { once: true });
        this.socket.addEventListener("error", () => fail(new Error("Owned browser DevTools connection failed")), { once: true });
        timer = setTimeout(() => fail(new Error("Owned browser DevTools connection did not open")), 10_000);
      });
    } finally { clearTimeout(timer); }
  }

  send(method, params = {}) {
    if (this.crashed) return Promise.resolve({ error: { message: "the page crashed" } });
    return new Promise((done) => {
      this.next += 1;
      if (this.pending.size >= 64) { done({ error: { message: "DevTools fixture request budget exceeded" } }); return; }
      const id = this.next;
      const timer = setTimeout(() => { this.pending.delete(id); done({ error: { message: "DevTools request timed out" } }); }, 30_000);
      this.pending.set(id, (answer) => { clearTimeout(timer); done(answer); });
      try { this.socket.send(JSON.stringify({ id, method, params })); }
      catch (error) { this.pending.get(id)?.({ error: { message: String(error.message ?? error) } }); this.pending.delete(id); }
    });
  }

  async eval(expression) {
    const answer = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (answer.error) throw new Error(`${this.name}: ${answer.error.message}`);
    if (answer.result?.exceptionDetails) throw new Error(`${expression}: ${answer.result.exceptionDetails.exception?.description ?? answer.result.exceptionDetails.text}`);
    return answer.result?.result?.value;
  }

  async goto() {
    await this.send("Page.navigate", { url: this.url });
    await this.until(this.loaded, 30, "the page to load");
  }

  async until(expression, seconds, what) {
    const end = Date.now() + seconds * 1000;
    for (;;) {
      let value;
      try {
        value = await this.eval(expression);
      } catch {
        value = false;
      }
      if (value) return value;
      if (Date.now() > end) throw new Error(`${this.name}: gave up waiting for ${what} after ${seconds} s; notices: ${await this.notices()}`);
      await sleep(250);
    }
  }

  async notices() {
    return this.eval("[...document.querySelectorAll('#notices *, .notice, .toast')].map((n) => n.textContent).join(' | ')").catch(() => "");
  }

  /** A field as a person fills it, pressing into it first. */
  async fill(id, value) {
    await this.eval(`(() => { const field = document.getElementById(${JSON.stringify(id)}); ${PRESS("field")} field.value = ${JSON.stringify(value)}; field.dispatchEvent(new Event("input", { bubbles: true })); field.dispatchEvent(new Event("change", { bubbles: true })); })()`);
  }

  async click(id) {
    await this.eval(`(() => { const node = document.getElementById(${JSON.stringify(id)}); ${PRESS("node")} node.click(); })()`);
  }

  text(id) {
    return this.eval(`document.getElementById(${JSON.stringify(id)})?.textContent ?? ""`);
  }

  visible(id) {
    return `(() => { const node = document.getElementById(${JSON.stringify(id)}); return Boolean(node) && !node.hidden && node.getClientRects().length > 0; })()`;
  }

  async screenshot(name) {
    if (!this.shots) return;
    mkdirSync(this.shots, { recursive: true });
    const shot = await this.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    if (shot.result?.data) writeFileSync(join(this.shots, `${name}.png`), Buffer.from(shot.result.data, "base64"));
  }

  async workers() {
    const targets = await this.send("Target.getTargets");
    if (targets.error || !Array.isArray(targets.result?.targetInfos)) throw new Error("DevTools did not report actual worker targets");
    return targets.result.targetInfos.filter((target) => target.type === "worker").length;
  }

  /** A proof's workers end with it, and with them its memory. */
  async proofReleased() {
    const end = Date.now() + 30_000;
    while ((await this.workers()) > 0) {
      if (Date.now() > end) throw new Error(`${await this.workers()} workers stay after the proof`);
      await sleep(500);
    }
    const held = this.renderersMiB();
    if (held !== null && held > PROOF_LEFT_MIB) throw new Error(`the page holds ${held} MiB after the proof`);
    return held;
  }

  /** What the fattest renderer of this browser holds, on Windows. */
  renderersMiB() {
    if (process.platform !== "win32") return null;
    if (!this.profile) return null;
    const tag = this.profile.split(sep).pop();
    const script = `Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object { $_.CommandLine -like '*${tag}*' -and $_.CommandLine -like '*--type=renderer*' } | ForEach-Object { $_.PrivatePageCount }`;
    const rows = execFileSync("powershell", ["-NoProfile", "-Command", script], { encoding: "utf8", windowsHide: true, timeout: 10_000 }).split(/\r?\n/).filter(Boolean).map(Number);
    return rows.length ? Math.round(Math.max(...rows) / 1048576) : 0;
  }

  close() { this.closing ??= this.closeOwned(); return this.closing; }

  async closeOwned() {
    const child = this.process;
    try {
      if (child.ownedExit) {
        // Let Chrome end renderers/workers itself while CDP is still connected. A crashed or
        // unresponsive browser then falls back to its exact private Job/group, never a PID sweep.
        if (this.socket.readyState === WebSocket.OPEN) void this.send("Browser.close").catch(() => {});
        let timer;
        try { await Promise.race([child.ownedExit, new Promise((done) => { timer = setTimeout(done, 5000); })]); }
        finally { clearTimeout(timer); }
        if (child.exitCode === null && child.signalCode === null) child.kill();
        await child.waitOwnedDrain();
      } else {
        // Attached app drivers supply their own explicit owner-stop hook; no foreign process is inferred.
        await child.kill(); await sleep(1500);
      }
      if (this.profile) await removeProfile(this.profile);
    } finally { this.socket.close(); this.endPending(); }
  }
}

/** How many of a gateway's requests its debug log shows asked to wait, and how many it refused for a blocked address. */
export function gatewayRefusals(path) {
  const text = readFileSync(path, "utf8");
  return {
    requests: text.match(/request detail /g)?.length ?? 0,
    refused: text.match(/request detail .* status=429 /g)?.length ?? 0,
    blocked: text.match(/reason="blocked"/g)?.length ?? 0,
  };
}
