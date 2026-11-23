// The outer owner contains the complete run, including direct SDK prover descendants.
import { parseArgs } from "node:util";
import { fileURLToPath } from "node:url";
import { configureOwnedPython, spawnOwned } from "./production/owned-process.mjs";

const { values } = parseArgs({ strict: false, allowPositionals: true, options: { python: { type: "string" } } });
configureOwnedPython(values.python);
const child = spawnOwned(process.execPath, [...process.execArgv,
  fileURLToPath(new URL("./production/runner-main.mjs", import.meta.url)), ...process.argv.slice(2)],
  { stdio: ["ignore", "inherit", "inherit"] });
const stop = () => { child.kill(); };
process.once("SIGINT", stop); process.once("SIGTERM", stop);
const ended = await child.ownedExit;
if (ended.error || ended.cleanupFailed) {
  console.error("The owned E2E run failed physical process-tree cleanup"); process.exitCode = 1;
} else process.exitCode = ended.code ?? 1;
