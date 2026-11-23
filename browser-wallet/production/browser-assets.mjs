import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, realpath, stat, writeFile } from "node:fs/promises";
import { resolve, sep } from "node:path";

const MAX_ASSETS = 512, MAX_BYTES = 64 * 1024 * 1024, MAX_TOTAL = 256 * 1024 * 1024;
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const protocolWasm = (url) => /\/tenrai_wasm_bg(?:-[A-Za-z0-9_-]+)?\.wasm$/.test(new URL(url).pathname);

/** Reads the bodies Chrome actually received, without another fetch or access to private API bodies. */
export class BrowserAssets {
  constructor(browser) { this.browser = browser; this.responses = new Map(); this.records = []; this.work = Promise.resolve(); this.total = 0; this.accepted = 0; }
  heard(message) {
    const row = message.params;
    if (message.method === "Network.responseReceived") {
      const response = row.response, url = new URL(response.url);
      if (!/^https?:$/.test(url.protocol) || !["Document", "Script"].includes(row.type) &&
        !/\.wasm$|\/release\.json$/.test(url.pathname)) return;
      if (response.status < 200 || response.status >= 300) return;
      if (++this.accepted > MAX_ASSETS) { this.fail("Asset response budget exceeded"); return; }
      this.responses.set(row.requestId, { url: response.url, type: row.type, mime_type: response.mimeType,
        from_disk_cache: Boolean(response.fromDiskCache), from_service_worker: Boolean(response.fromServiceWorker) });
    } else if (message.method === "Network.loadingFailed") {
      // A deliberately cancelled prover fetch is not a response consumed by the running app.
      this.responses.delete(row.requestId);
    } else if (message.method === "Network.loadingFinished") {
      const response = this.responses.get(row.requestId);
      if (!response) return;
      this.responses.delete(row.requestId);
      this.work = this.work.then(async () => {
        if (row.encodedDataLength > MAX_BYTES) throw new Error("Oversized loaded browser asset");
        const body = await this.browser.send("Network.getResponseBody", { requestId: row.requestId });
        if (body.error || typeof body.result?.body !== "string") throw new Error("Chrome did not retain an actual release-asset response body");
        if (body.result.body.length > MAX_BYTES * 2) throw new Error("Oversized CDP asset body");
        const bytes = Buffer.from(body.result.body, body.result.base64Encoded ? "base64" : "utf8");
        this.total += bytes.length;
        if (bytes.length > MAX_BYTES || this.total > MAX_TOTAL) throw new Error("Loaded release-asset byte budget exceeded");
        this.records.push({ ...response, bytes: bytes.length, sha256: sha(bytes) });
      }).catch((error) => this.fail(error.message));
    }
  }
  fail(message) { this.error ??= new Error(message); }
  async settled() {
    const end = Date.now() + 30_000;
    for (;;) {
      const work = this.work; await work;
      if (this.error) throw this.error;
      if (this.responses.size === 0 && work === this.work) return [...this.records];
      if (Date.now() > end) throw new Error("Loaded browser asset responses did not finish");
      await new Promise((done) => setTimeout(done, 20));
    }
  }
}

async function actualFile(root, parts) {
  const parent = await realpath(root), file = await realpath(resolve(parent, ...parts));
  assert(file.startsWith(parent + sep), "Expected release asset escapes its supplied package");
  const metadata = await stat(file); assert(metadata.isFile() && metadata.size <= MAX_BYTES);
  return { expected_file: file, expected_sha256: sha(await readFile(file)) };
}

/** Each observed protocol WASM must equal --sdk; served app/SDK scripts must equal --wallet. */
export async function verifyBrowserAssets(page, { wallet, sdkDirectory, classic, evidence }) {
  const observed = await page.assets.settled();
  const expectedWasm = await actualFile(sdkDirectory, ["wasm", "tenrai_wasm_bg.wasm"]);
  let protocolCount = 0, scriptCount = 0;
  const records = [];
  for (const record of observed) {
    const url = new URL(record.url);
    assert.equal(url.origin, new URL(page.url).origin, "The isolated wallet loaded a release asset outside its own server");
    assert.equal(record.from_disk_cache, false, "The release-asset check must observe a current server response");
    assert.equal(record.from_service_worker, false, "A service worker hid the release asset used by the page");
    const parts = decodeURIComponent(url.pathname).split("/").filter(Boolean);
    assert(parts.every((part) => part !== "." && part !== ".." && !part.includes("\\") && !part.includes(":")));
    let expected;
    if (protocolWasm(record.url)) { expected = expectedWasm; protocolCount += 1; }
    else if (/\/tenrai_wasm\.js$/.test(url.pathname)) expected = await actualFile(sdkDirectory, ["wasm", "tenrai_wasm.js"]);
    else if (record.type === "Document") expected = await actualFile(wallet, classic ? ["index.html"] : ["dist", "index.html"]);
    else if (record.type === "Script" || /\.wasm$|\/release\.json$/.test(url.pathname)) {
      // The reference server exposes /prover from the *installed* SDK, not its vendor tree.
      expected = !classic && parts[0] === "prover" ? await actualFile(sdkDirectory, ["prover", ...parts.slice(1)]) :
        classic && parts[0] === "vendor" && parts[1] === "tenrai-prover" ? await actualFile(sdkDirectory, ["prover", ...parts.slice(2)]) :
          await actualFile(wallet, classic ? parts : ["dist", ...parts]);
      if (record.type === "Script") scriptCount += 1;
    }
    assert(expected, `No expected local release path for ${record.url}`);
    records.push({ ...record, ...expected });
  }
  await writeFile(evidence, `${JSON.stringify({ format: 1, browser: page.name, protocol_wasm: expectedWasm, assets: records }, null, 2)}\n`, { flag: "wx" });
  for (const record of records) assert.equal(record.sha256, record.expected_sha256, `Chrome used stale/different release bytes at ${record.url}`);
  assert(protocolCount > 0, "No actual protocol WASM response was captured from this wallet");
  assert(scriptCount > 0, "No actual app/SDK script response was captured from this wallet");
  return { browser: page.name, protocol_responses: protocolCount, scripts: scriptCount, evidence, protocol_sha256: expectedWasm.expected_sha256 };
}
