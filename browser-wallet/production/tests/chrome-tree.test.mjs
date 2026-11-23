import assert from "node:assert/strict";
import test from "node:test";
import { mkdir, mkdtemp, readFile, realpath, rm, stat, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { basename, dirname, join, sep } from "node:path";
import { tmpdir } from "node:os";
import { Browser } from "../../browser.mjs";
import { verifyBrowserAssets } from "../browser-assets.mjs";

test("real Chrome closes its worker/renderer tree before profile removal", {
  skip: !process.env.TENRAI_E2E_CHROME, timeout: 60_000,
}, async (t) => {
  const parent = await realpath(tmpdir()), root = await mkdtemp(join(parent, "tenrai-owned-chrome-"));
  let page, server;
  t.after(async () => {
    await page?.close();
    if (server?.listening) await new Promise((done, fail) => server.close((error) => error ? fail(error) : done()));
    const actual = await realpath(root); assert.equal(dirname(actual), parent);
    assert(actual.startsWith(parent + sep) && basename(actual).startsWith("tenrai-owned-chrome-"));
    await rm(actual, { recursive: true, force: true });
  });
  page = await Browser.open("physical-chrome", { chrome: process.env.TENRAI_E2E_CHROME, url: "about:blank", profileRoot: root });
  await page.eval("globalThis.ownedFixtureWorker = new Worker(URL.createObjectURL(new Blob(['setInterval(()=>{},1000)'], {type:'text/javascript'})))");
  await page.until("Boolean(globalThis.ownedFixtureWorker)", 5, "the actual worker instance");
  const end = Date.now() + 5000;
  while ((await page.workers()) === 0) { if (Date.now() > end) throw new Error("Chrome did not report an actual worker target"); await new Promise((done) => setTimeout(done, 20)); }
  const profile = page.profile;
  await page.close();
  assert.equal((await page.process.ownedExit).cleanupFailed, false);
  await assert.rejects(stat(profile), (error) => error.code === "ENOENT");
});

test("actual Chrome response hashes reject an old protocol WASM even when the local release file is current", {
  skip: !process.env.TENRAI_E2E_CHROME, timeout: 60_000,
}, async (t) => {
  const parent = await realpath(tmpdir()), root = await mkdtemp(join(parent, "tenrai-owned-assets-"));
  let page, server;
  t.after(async () => {
    await page?.close();
    if (server?.listening) await new Promise((done, fail) => server.close((error) => error ? fail(error) : done()));
    const actual = await realpath(root); assert.equal(dirname(actual), parent);
    assert(actual.startsWith(parent + sep) && basename(actual).startsWith("tenrai-owned-assets-"));
    await rm(actual, { recursive: true, force: true });
  });
  const current = Buffer.from([0, 97, 115, 109, 1, 0, 0, 0]), old = Buffer.concat([current, Buffer.from([0, 0])]);
  const html = '<!doctype html><script src="/app.js"></script>';
  const app = "fetch('/tenrai_wasm_bg.wasm').then(r=>r.arrayBuffer()).then(()=>globalThis.fixtureAssetLoaded=true)";
  await mkdir(join(root, "wasm"));
  await writeFile(join(root, "wasm/tenrai_wasm_bg.wasm"), current);
  await writeFile(join(root, "index.html"), html); await writeFile(join(root, "app.js"), app);
  server = createServer((request, response) => {
    const body = request.url === "/tenrai_wasm_bg.wasm" ? old : request.url === "/app.js" ? app : html;
    response.writeHead(200, { "Content-Type": request.url.endsWith(".wasm") ? "application/wasm" : request.url.endsWith(".js") ? "text/javascript" : "text/html" });
    response.end(body);
  });
  await new Promise((done, fail) => { server.once("error", fail); server.listen(0, "127.0.0.1", done); });
  page = await Browser.open("actual-asset-response", { chrome: process.env.TENRAI_E2E_CHROME,
    url: `http://127.0.0.1:${server.address().port}/`, profileRoot: root, captureAssets: true });
  await page.until("globalThis.fixtureAssetLoaded === true", 10, "the actual old WASM response");
  const evidence = join(root, "loaded.json");
  await assert.rejects(verifyBrowserAssets(page, { wallet: root, sdkDirectory: root, classic: true, evidence }), /stale\/different release bytes/);
  const captured = JSON.parse(await readFile(evidence, "utf8"));
  const wasm = captured.assets.find((asset) => asset.url.endsWith("tenrai_wasm_bg.wasm"));
  assert(wasm); assert.notEqual(wasm.sha256, wasm.expected_sha256);
  assert.equal(wasm.bytes, old.length);
  await page.close();
});
