import assert from "node:assert/strict";
import { DatabaseSync } from "./sqlite.mjs";
import { createHash } from "node:crypto";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";

const LIMIT = 32 * 1024 * 1024;
const hash = (text) => createHash("sha384").update(text).digest("hex");
const equal = (left, right) => left instanceof Uint8Array && right instanceof Uint8Array && Buffer.from(left).equals(Buffer.from(right));

function disk(file) {
  const db = new DatabaseSync(file);
  try {
    if (typeof db.isTransaction !== "boolean") throw new Error("A supported native SQLite with isTransaction is required");
    db.exec("PRAGMA busy_timeout=0;PRAGMA journal_mode=DELETE;PRAGMA synchronous=EXTRA;PRAGMA cache_size=-2048;PRAGMA mmap_size=0;PRAGMA max_page_count=65536");
    assert.equal(db.prepare("PRAGMA journal_mode").get().journal_mode, "delete");
    assert.equal(Number(db.prepare("PRAGMA synchronous").get().synchronous), 3);
  } catch (error) { db.close(); throw error; }
  return db;
}

/** An actual disk journal under a lifetime OS SQLite lease, not a mocked monetary memoryStore. */
export function journalStore(directory, namespace, chainId) {
  let data, marks, lease;
  try {
    data = disk(join(directory, "operations.sqlite")); marks = disk(join(directory, "presence.sqlite"));
    lease = disk(join(directory, "session-lease.sqlite"));
    data.exec("CREATE TABLE IF NOT EXISTS records(name TEXT PRIMARY KEY,sealed BLOB NOT NULL) WITHOUT ROWID");
    marks.exec("CREATE TABLE IF NOT EXISTS present(name TEXT PRIMARY KEY) WITHOUT ROWID");
  } catch (error) { lease?.close(); marks?.close(); data?.close(); throw error; }
  let owned = false, closed = false, uncertain = false;
  function open() { if (!owned || closed || uncertain) throw new Error("The durable fixture journal is not safely owned"); }
  function transaction(db, action) {
    open(); db.exec("BEGIN IMMEDIATE"); assert.equal(db.isTransaction, true);
    try { const result = action(); db.exec("COMMIT"); assert.equal(db.isTransaction, false); return result; }
    catch (error) { uncertain = true; try { if (db.isTransaction) db.exec("ROLLBACK"); } catch {} throw error; }
  }
  function read(name) {
    open(); const row = data.prepare("SELECT sealed FROM records WHERE name=?").get(name);
    if (row === undefined) {
      if (marks.prepare("SELECT 1 FROM present WHERE name=?").get(name)) throw new Error("A previously durable fixture record disappeared");
      return null;
    }
    if (!(row.sealed instanceof Uint8Array) || row.sealed.byteLength > LIMIT) throw new Error("A fixture journal record is damaged");
    return row.sealed.slice();
  }
  function mark(name) { transaction(marks, () => marks.prepare("INSERT OR IGNORE INTO present VALUES(?)").run(name)); }
  function write(name, value) {
    open(); if (!(value instanceof Uint8Array) || value.byteLength > LIMIT) throw new Error("A fixture journal record is oversized");
    const exact = value.slice();
    transaction(data, () => data.prepare("INSERT INTO records VALUES(?,?) ON CONFLICT(name) DO UPDATE SET sealed=excluded.sealed").run(name, exact));
    if (!equal(read(name), exact)) { uncertain = true; throw new Error("A fixture COMMIT changed the exact original bytes"); }
    mark(name);
  }
  const store = {
    id: namespace,
    async acquireSendsLease() {
      if (owned || closed || uncertain) throw new Error("The fixture account journal is already owned or closed");
      lease.exec("BEGIN EXCLUSIVE"); assert.equal(lease.isTransaction, true); owned = true;
      return { release() { if (owned) { lease.exec("ROLLBACK"); assert.equal(lease.isTransaction, false); owned = false; } } };
    },
    async read(name) { if (!["state", "mark", "sends"].includes(name)) throw new Error("Unexpected fixture record"); return read(name); },
    async write(name, bytes) { if (!["state", "mark", "sends"].includes(name)) throw new Error("Unexpected fixture record"); write(name, bytes); },
  };
  const publicTransfers = {
    chainId,
    async read() { return read("public"); },
    async write(bytes) { write("public", bytes); },
    async checkAbsent() { open(); if (marks.prepare("SELECT 1 FROM present WHERE name='public'").get()) throw new Error("The retained public original disappeared"); },
    async markPresent() { mark("public"); },
    async exportRecovery() { const bytes = read("public"); if (!bytes) throw new Error("No public original to export"); return bytes; },
  };
  return { store, publicTransfers, close() {
    if (closed) return;
    if (owned) { lease.exec("ROLLBACK"); owned = false; }
    lease.close(); marks.close(); data.close(); closed = true;
  } };
}

/** All application-owned stores are separate disk files; cache pragmas never touch money journals. */
export async function openWallet({ sdk, client, phrase, directory, network }) {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const stores = new Map(), connections = [];
  const connect = (name) => { const db = disk(join(directory, name)); connections.push(db); return db; };
  const finalityStore = sdk.sqliteFinalityGuardStore(connect("finality.sqlite"));
  let wallet;
  try {
    wallet = await sdk.Wallet.fromMnemonic(phrase, { network: "development", genesisManifest: network.manifestText, finalityStore,
      publicCoinStore: async (scope) => sdk.sqlitePersistentPublicCoinStore(new DatabaseSync(join(directory, `coins-${hash(scope).slice(0, 32)}.sqlite`)), scope, { maxCoins: 100_000 }),
      privateStore: async (owner) => {
        if (!stores.has(owner.account)) {
          const path = join(directory, `account-${owner.account}`); await mkdir(path, { recursive: true, mode: 0o700 });
          const id = `e2e:${hash(`${network.manifestText}:${owner.fingerprint}:${owner.account}`)}`;
          const money = journalStore(path, id, network.manifest.chain_id);
          const inventoryDb = disk(join(path, "inventory.sqlite")), localDb = disk(join(path, "scratch.sqlite")), outcomeDb = disk(join(path, "outcomes.sqlite"));
          connections.push(inventoryDb, localDb, outcomeDb);
          money.store.inventoryPages = sdk.sqlitePrivateInventoryStore(inventoryDb, id);
          money.store.localPages = sdk.sqlitePrivateLocalScanStore(localDb, id);
          money.store.outcomes = sdk.sqliteOutcomeStore(outcomeDb, id);
          stores.set(owner.account, { money, options: { store: money.store, publicTransfers: money.publicTransfers, finalityStore, genesisManifest: network.manifestText } });
        }
        return stores.get(owner.account).options;
      } });
    const notes = await wallet.privateNotes(client);
    return { wallet, notes, directory, async close() {
      await wallet.close();
      for (const { money } of stores.values()) money.close();
      await finalityStore.close();
      // Explicit finality providers are closed by their application owner.
      for (const db of connections.slice(1)) { try { db.close(); } catch (error) { if (!/closed/i.test(String(error))) throw error; } }
    } };
  } catch (error) {
    await wallet?.close().catch(() => {}); await finalityStore.close();
    for (const { money } of stores.values()) money.close();
    for (const db of connections.slice(1)) { try { db.close(); } catch {} }
    throw error;
  }
}
