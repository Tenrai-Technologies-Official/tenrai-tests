// The wallet page in one headless Firefox with its own profile, driven over WebDriver BiDi. It answers as the
// Chrome of browser.mjs does, except for what BiDi cannot do here: going offline and limiting a page's memory.

import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { freePort, sleep } from "./browser.mjs";

let nextProfile = 0;
import { WalletPage } from "./wallet-page.mjs";

export class FirefoxWalletPage extends WalletPage {
  /** Starts a Firefox on `url`. */
  static async open(name, { firefox, url, shots, loaded = "document.readyState === 'complete'" }) {
    const port = await freePort();
    // A closed page frees its port at once while its Firefox may still hold the profile: the serial keeps profiles apart.
    const profile = join(tmpdir(), `tenrai-e2e-firefox-${process.pid}-${port}-${nextProfile++}`);
    rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });
    mkdirSync(profile, { recursive: true });
    writeFileSync(join(profile, "user.js"), 'user_pref("intl.accept_languages", "en-US");\nuser_pref("browser.shell.checkDefaultBrowser", false);\n');
    const child = spawn(firefox, ["--headless", "--no-remote", "--profile", profile, `--remote-debugging-port=${port}`, "--width=1400", "--height=1400", "about:blank"], { stdio: "ignore" });
    let socket;
    for (let attempt = 0; attempt < 120 && !socket; attempt += 1) {
      await sleep(500);
      try {
        const candidate = new WebSocket(`ws://127.0.0.1:${port}/session`);
        await new Promise((done, failed) => {
          candidate.addEventListener("open", done);
          candidate.addEventListener("error", failed);
        });
        socket = candidate;
      } catch {}
    }
    if (!socket) throw new Error(`${name}: Firefox did not open its debugging port`);
    const browser = new this();
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
    browser.socket = socket;
    socket.addEventListener("message", (event) => browser.heard(JSON.parse(event.data)));
    await browser.send("session.new", { capabilities: {} });
    await browser.send("session.subscribe", { events: ["log.entryAdded", "network.beforeRequestSent"] });
    browser.context = (await browser.send("browsingContext.getTree", {})).contexts[0].context;
    await browser.goto();
    return browser;
  }

  heard(message) {
    if (message.id) {
      this.pending.get(message.id)?.(message.result ?? { error: message.message ?? message.error });
      this.pending.delete(message.id);
    } else if (message.method === "log.entryAdded" && message.params.level === "error") {
      const text = message.params.text ?? "";
      if (!/favicon|status of 404|status of 409|status of 503|NetworkError/.test(text)) this.problems.push(text);
    } else if (message.method === "network.beforeRequestSent" && message.params.request.method === "POST") {
      this.requests.push({ url: message.params.request.url, id: message.params.request.request, body: null });
    }
  }

  send(method, params = {}) {
    return new Promise((done) => {
      this.next += 1;
      this.pending.set(this.next, done);
      this.socket.send(JSON.stringify({ id: this.next, method, params }));
    });
  }

  async eval(expression) {
    const answer = await this.send("script.evaluate", { expression, target: { context: this.context }, awaitPromise: true });
    if (answer.error) throw new Error(`${this.name}: ${answer.error}`);
    if (answer.type === "exception") throw new Error(`${expression}: ${answer.exceptionDetails?.text}`);
    return answer.result?.value;
  }

  async goto() {
    await this.send("browsingContext.navigate", { context: this.context, url: this.url, wait: "complete" });
    await this.until(this.loaded, 30, "the page to load");
  }

  async screenshot(name) {
    if (!this.shots) return;
    mkdirSync(this.shots, { recursive: true });
    const shot = await this.send("browsingContext.captureScreenshot", { context: this.context, origin: "document" });
    if (shot.data) writeFileSync(join(this.shots, `${name}.png`), Buffer.from(shot.data, "base64"));
  }

  async workers() {
    const answer = await this.send("script.getRealms", { type: "dedicated-worker" });
    return answer.realms?.length ?? 0;
  }

  renderersMiB() {
    return null;
  }

  async close() {
    await this.send("session.end", {}).catch(() => {});
    this.socket.close();
    if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(this.process.pid), "/T", "/F"], { stdio: "ignore" });
    else this.process.kill();
    await sleep(1500);
    rmSync(this.profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });
  }
}
