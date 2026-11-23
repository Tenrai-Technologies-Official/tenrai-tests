// Compare native and browser proofs for the same fixtures. Blinding makes their bytes differ.

import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { browserExecutable } from "../browser-wallet/browser-executable.mjs";
import { Browser, sleep } from "../browser-wallet/browser.mjs";

const { values } = parseArgs({
  options: {
    inputs: { type: "string" },
    prover: { type: "string" },
    package: { type: "string" },
    work: { type: "string", default: "compare-work" },
    threads: { type: "string", default: "6" },
    port: { type: "string", default: "28791" },
    chrome: { type: "string", default: browserExecutable("chrome") },
    out: { type: "string", default: "compare-results.json" },
  },
});
const here = fileURLToPath(new URL(".", import.meta.url));
const VERIFIER = resolve(here, "..", "p2p-bench", "bench-private", "target", "release", "bench-private.exe");
const log = (line) => console.log(`${new Date().toISOString().slice(11, 19)} ${line}`);

const flip = (text) => text.slice(0, -1) + (text.at(-1) === "0" ? "1" : "0");
const more = (text) => String(BigInt(text) + 1n);
const less = (text) => String(BigInt(text) - 1n);
const number = (value) => value + 1;

/** What a person or a broken wallet could change, by the input's own path. */
const CHANGES = {
  enter: [
    ["enter.outputs.0.value_atoms", more], ["enter.entered_atoms", more], ["enter.fee_atoms", less],
    ["enter.outputs.0.note_blinding", flip], ["enter.outputs.0.encapsulation_seed", flip], ["enter.outputs.0.nonce", flip],
    ["enter.outputs.0.note_commitment", flip], ["enter.outputs.0.note_id", flip], ["enter.outputs.0.capsule", flip],
    ["enter.outputs.0.capsule_digest", flip], ["enter.outputs.0.recipient_membership.siblings.0", flip],
    ["enter.outputs.0.recipient_membership.owner_spend_lock", flip], ["enter.outputs.0.recipient_membership.record_index", number],
    ["enter.statement", flip], ["enter.statement_digest", flip], ["enter.rules_hash", flip], ["enter.inputs_digest", flip],
    ["enter.intent_nonce", flip], ["enter.anchor.cut_id", flip], ["enter.valid_until_height", number],
    ["enter.protocol_version", number], ["chain_id", flip], ["expected_private_state_root", flip],
    ["anchor.note_tree_frontier.0", flip], ["note_tree_root", flip],
  ],
  transfer: [
    ["owner_authorization_secret", flip], ["sources.0.note_opening", flip], ["sources.1.note_siblings.0", flip],
    ["sources.0.leaf_index", number], ["sources.0.expected_nullifier", flip], ["sources.0.creation_lineage_commitment", flip],
    ["sources.0.creation_admission_anchor_digest", flip], ["sources.0.creation_admission_anchor_height", number],
    ["sources.0.capsule", flip], ["sources.0.capsule_digest", flip], ["sources.0.note_commitment", flip],
    ["recipient_membership.siblings.0", flip], ["transfer.outputs.0.value_atoms", more], ["transfer.outputs.1.note_blinding", flip],
    ["transfer.outputs.0.note_commitment", flip], ["transfer.outputs.1.capsule_digest", flip],
    ["transfer.fee_atoms", less], ["transfer.statement", flip], ["transfer.statement_digest", flip], ["transfer.intent_nonce", flip],
    ["expected_private_state_root", flip], ["chain_id", flip],
  ],
  exit: [
    ["owner_authorization_secret", flip], ["sources.0.note_opening", flip], ["sources.0.note_siblings.0", flip],
    ["sources.0.expected_nullifier", flip], ["sources.0.capsule", flip], ["recipient_membership.siblings.0", flip],
    ["exit.amount_atoms", more], ["exit.destination", flip], ["exit.fee_atoms", less], ["exit.statement", flip],
    ["exit.statement_digest", flip], ["exit.rules_hash", flip], ["exit.valid_until_height", number], ["chain_id", flip],
    ["expected_private_state_root", flip],
  ],
};

function changed(input, path, how) {
  const copy = structuredClone(input);
  const parts = path.split(".");
  const last = parts.pop();
  let place = copy;
  for (const part of parts) place = place[part];
  place[last] = how(place[last]);
  return copy;
}

const kindOf = (input) => ["enter", "exit", "transfer"].find((name) => input[name]);
const encode = (value) => Buffer.from(JSON.stringify(value, null, 2));

/** Every case, the name of a valid one marked as such. */
function cases(folder) {
  const all = [];
  let number = 0;
  const add = (label, valid, bytes, digest) => {
    number += 1;
    all.push({ file: `${String(number).padStart(3, "0")}.bin`, label, valid, bytes, digest });
  };
  const names = readdirSync(folder).filter((name) => name.endsWith(".bin")).sort();
  let first;
  for (const name of names) {
    const bytes = readFileSync(join(folder, name));
    const input = JSON.parse(bytes.toString("utf8"));
    const kind = kindOf(input);
    add(`valid ${name}`, true, bytes, input[kind].statement_digest);
    first ??= input;
    for (const [path, how] of CHANGES[kind]) {
      const digest = input[kind].statement_digest;
      if (path === `${kind}.outputs.1.note_blinding` && input[kind].outputs.length < 2) continue;
      if (path.startsWith("sources.1") && input.sources.length < 2) continue;
      add(`${name} ${path}`, false, encode(changed(input, path, how)), digest);
    }
    if (kind === "enter") {
      const three = structuredClone(input);
      three.enter.outputs = [...three.enter.outputs, three.enter.outputs[0], three.enter.outputs[0]].slice(0, 3);
      add(`${name} three outputs`, false, encode(three), input.enter.statement_digest);
    } else {
      const three = structuredClone(input);
      three.sources = [three.sources[0], three.sources[0], three.sources[0]];
      add(`${name} three sources`, false, encode(three), input[kind].statement_digest);
    }
    if (kind === "transfer") {
      const swapped = structuredClone(input);
      swapped.sources.reverse();
      add(`${name} sources swapped`, false, encode(swapped), input.transfer.statement_digest);
      const twice = structuredClone(input);
      twice.sources[1] = twice.sources[0];
      add(`${name} the same note twice`, false, encode(twice), input.transfer.statement_digest);
    }
  }
  const unknown = structuredClone(first);
  unknown.format = "tenrai-tsp2-unknown-1";
  add("an unknown format", false, encode(unknown), null);
  add("empty", false, Buffer.alloc(0), null);
  add("not JSON", false, Buffer.from("this is no prover input"), null);
  add("cut off in the middle", false, encode(first).subarray(0, 4000), null);
  add("no format at all", false, Buffer.from("{}"), null);
  return all;
}

function verified(digest, proof) {
  if (!digest) return null;
  const file = join(resolve(values.work), "proof.hex");
  writeFileSync(file, proof);
  const answer = spawnSync(VERIFIER, ["verify-proof", digest, file], { encoding: "utf8" });
  if (answer.status !== 0) return `the tool failed: ${answer.stderr.slice(-200)}`;
  return JSON.parse(answer.stdout).accepted_by;
}

function native(item) {
  const started = Date.now();
  const answer = spawnSync(values.prover, [], { input: item.bytes, maxBuffer: 8 << 20, timeout: 600_000 });
  const ms = Date.now() - started;
  const refusal = answer.stderr.toString("utf8").trim().split(/\r?\n/).filter(Boolean).at(-1) ?? "";
  if (answer.status !== 0) return { proved: false, message: refusal, code: answer.status, ms };
  return { proved: true, proof: answer.stdout.toString("hex"), ms };
}

const work = resolve(values.work);
rmSync(work, { recursive: true, force: true });
const folder = join(work, "inputs");
mkdirSync(folder, { recursive: true });
const all = cases(resolve(values.inputs));
for (const item of all) writeFileSync(join(folder, item.file), item.bytes);
log(`${all.length} cases, ${all.filter((item) => item.valid).length} of them valid`);

for (const item of all) {
  item.native = native(item);
  log(`native ${item.file} ${item.label}: ${item.native.proved ? "proof" : `refused (${item.native.code}) ${item.native.message.slice(0, 110)}`} in ${(item.native.ms / 1000).toFixed(1)} s`);
}

const server = spawn("node", [join(here, "serve.mjs"), "--package", resolve(values.package), "--inputs", folder, "--port", values.port], { stdio: "ignore" });
let page;
let failed = 0;
try {
  await sleep(800);
  page = await Browser.open("compare", { chrome: values.chrome, url: `http://127.0.0.1:${values.port}/?threads=${values.threads}&pause=3000` });
  let seen = 0;
  for (;;) {
    const done = await page.eval("document.title === 'done'");
    const count = await page.eval("window.results?.length ?? 0");
    while (seen < count) {
      const row = JSON.parse(await page.eval(`JSON.stringify(window.results[${seen}])`));
      const item = all[seen];
      if (row.name !== item.file) throw new Error(`the page answered for ${row.name}, expected ${item.file}`);
      item.browser = row.ok ? { proved: true, proof: row.proof_hex, ms: row.prove_ms } : { proved: false, message: row.error, ms: row.prove_ms };
      log(`browser ${item.file} ${item.label}: ${row.ok ? "proof" : `refused ${String(row.error).slice(0, 110)}`} in ${(row.prove_ms / 1000).toFixed(1)} s`);
      await page.eval(`delete window.results[${seen}].proof_hex`);
      seen += 1;
    }
    if (done && seen >= all.length) break;
    await sleep(500);
  }
  for (const item of all) {
    const problems = [];
    if (item.native.proved !== item.browser.proved) problems.push("the provers disagree");
    if (!item.native.proved && !item.browser.proved && item.native.message !== item.browser.message) {
      problems.push(`other words: ${item.native.message} / ${item.browser.message}`);
    }
    if (item.valid && !(item.native.proved && item.browser.proved)) problems.push("a valid input was refused");
    if (!item.valid && (item.native.proved || item.browser.proved)) problems.push("a changed input was proven");
    for (const side of ["native", "browser"]) {
      if (item[side].proved) {
        item[side].accepted_by = verified(item.digest, item[side].proof);
        delete item[side].proof;
        const accepted = Array.isArray(item[side].accepted_by) ? item[side].accepted_by.length === 1 : Boolean(item[side].accepted_by);
        if (item.valid !== accepted) problems.push(`${side}: accepted by ${JSON.stringify(item[side].accepted_by)}`);
      }
    }
    item.problems = problems;
    if (problems.length) {
      failed += 1;
      log(`PROBLEM ${item.file} ${item.label}: ${problems.join("; ")}`);
    }
  }
} finally {
  await page?.close().catch(() => {});
  server.kill();
}
for (const item of all) delete item.bytes;
writeFileSync(values.out, `${JSON.stringify({ threads: Number(values.threads), cases: all }, null, 2)}\n`);
rmSync(work, { recursive: true, force: true });
log(`${all.length - failed} of ${all.length} cases as they must be`);
process.exit(failed ? 1 : 0);
