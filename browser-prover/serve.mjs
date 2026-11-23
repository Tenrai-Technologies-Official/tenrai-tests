// Serves the browser prover page, the prover package and the prover inputs on 127.0.0.1, with the headers
// a wallet's host sends: the whole page cross-origin isolated, as threaded WebAssembly needs, and the
// package under the policy that keeps its worker from reaching anything but its own folder.
//
//   node serve.mjs --package DIR --inputs DIR [--port 28790]

import { createServer } from "node:http";
import { readFile, readdir } from "node:fs/promises";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

const PACKAGE_POLICY = "default-src 'none'; script-src 'self' blob: 'wasm-unsafe-eval'; worker-src 'self' blob:; connect-src 'self'";
const here = fileURLToPath(new URL(".", import.meta.url));
const { values } = parseArgs({
  options: {
    package: { type: "string" },
    inputs: { type: "string" },
    port: { type: "string", default: "28790" },
  },
});
if (!values.package || !values.inputs) {
  console.error("usage: node serve.mjs --package DIR --inputs DIR [--port 28790]");
  process.exit(2);
}
const roots = { package: resolve(values.package), inputs: resolve(values.inputs), page: resolve(here) };
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".wasm": "application/wasm",
  ".json": "application/json",
  ".bin": "application/octet-stream",
};

/** The file a path names inside one of the roots, or nothing for a path that leaves it. */
function located(pathname) {
  const [, first = "", ...rest] = pathname.split("/");
  const mounted = first !== "page" && Object.hasOwn(roots, first);
  const root = mounted ? roots[first] : roots.page;
  const parts = mounted ? rest : [first, ...rest];
  const file = normalize(join(root, ...parts.map(decodeURIComponent)));
  return file === root || file.startsWith(root + sep) ? file : null;
}

createServer(async (request, response) => {
  const { pathname } = new URL(request.url, "http://localhost");
  response.setHeader("Cross-Origin-Opener-Policy", "same-origin");
  response.setHeader("Cross-Origin-Embedder-Policy", "require-corp");
  response.setHeader("Cache-Control", "no-store");
  if (pathname.startsWith("/package/")) response.setHeader("Content-Security-Policy", PACKAGE_POLICY);
  try {
    if (pathname === "/inputs/") {
      const names = (await readdir(roots.inputs)).filter((name) => name.endsWith(".bin")).sort();
      response.setHeader("Content-Type", TYPES[".json"]);
      response.end(JSON.stringify(names));
      return;
    }
    const file = located(pathname === "/" ? "/index.html" : pathname);
    if (!file) throw new Error("outside the served folders");
    const body = await readFile(file);
    response.setHeader("Content-Type", TYPES[extname(file)] ?? "application/octet-stream");
    response.end(body);
  } catch {
    response.statusCode = 404;
    response.end("not found");
  }
}).listen(Number(values.port), "127.0.0.1", () => {
  console.log(`browser prover page on http://127.0.0.1:${values.port}/`);
});
