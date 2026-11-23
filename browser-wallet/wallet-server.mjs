// The normal web wallet's files as its host serves them, with a configuration for one test network.

import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, resolve, sep } from "node:path";

const PROVER_POLICY = "default-src 'none'; script-src 'self' blob: 'wasm-unsafe-eval'; worker-src 'self' blob:; connect-src 'self'";
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".webmanifest": "application/manifest+json",
  ".wasm": "application/wasm",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
};

/**
 * Serves the wallet in `root` on `port`, with `config` as its config.json and `genesis` as the genesis of the test
 * network. `isolated` false leaves out the headers threads need, as a host that cannot send them does.
 */
export function serveWallet({ root, port, config, genesis, isolated = true }) {
  const settings = JSON.stringify(config);
  const prover = resolve(root, "vendor", "tenrai-prover") + sep;
  const headers = isolated
    ? { "Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Embedder-Policy": "require-corp", "Cross-Origin-Resource-Policy": "same-origin", "Cache-Control": "no-cache" }
    : { "Cache-Control": "no-cache" };
  return createServer(async (request, response) => {
    const { pathname } = new URL(request.url, "http://localhost");
    try {
      if (pathname === "/config.json") {
        response.writeHead(200, { ...headers, "Content-Type": TYPES[".json"] }).end(settings);
        return;
      }
      if (pathname === "/genesis.json") {
        response.writeHead(200, { ...headers, "Content-Type": TYPES[".json"] }).end(genesis);
        return;
      }
      const file = resolve(root, `.${pathname.endsWith("/") ? `${pathname}index.html` : pathname}`);
      if (!file.startsWith(resolve(root) + sep)) throw new Error("outside");
      const body = await readFile(file);
      const answer = { ...headers, "Content-Type": TYPES[extname(file)] ?? "application/octet-stream" };
      if (file.startsWith(prover)) answer["Content-Security-Policy"] = PROVER_POLICY;
      response.writeHead(200, answer).end(body);
    } catch {
      response.writeHead(404, headers).end();
    }
  }).listen(port, "127.0.0.1");
}
