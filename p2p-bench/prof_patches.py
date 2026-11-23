"""The prof variant: where the node spends its time, its CPU and its disk writes.

Adds a probe to a copy of the node. Every 10 s each node writes to its log (stderr, so node.log):

  PROF <unix ms> <site> <file:line or -> n=.. wall_ms=.. max_ms=.. cpu_ms=.. bytes=.. reads=.. misses=.. miss_ms=..
  BLK <unix ms> <accepted|heard|asked|got|try> <start of the block ID>
  PROFDB <unix ms> <rocksdb ticker> <increase>
  PROFHIST <unix ms> <rocksdb histogram> n=.. sum=.. p50=.. p99=.. max=..

A site is a code path: wall time from start to end, CPU time of the thread meanwhile (Windows
QueryThreadCycleTime), the longest single run, the bytes it handled, and the state nodes it read: how
many, how many missed the node cache and how long those misses took. BLK lines follow each block: when
a node took it into its pool (the earliest one is where it was mined), heard of it from a peer, asked
for it, received it, and each time it tried to check and take it. Disk writes carry the file
and line of the code that asked for them, so every synced write and every WAL flush can be traced
to its caller; db.sync also counts the logged bytes it put on disk. PROFDB and PROFHIST are the
RocksDB statistics: user, WAL, flush and compaction bytes, WAL syncs and their durations.

Each thread adds up on its own and hands its sums over once a second, so threads do not wait for one
another to be measured. Frequent paths and waits take wall time only; PROFINFO says what one CPU
reading costs. TENRAI_PROF_NO_PREFIX_AHEAD=1 in the node's environment turns off running the next
cut's prefix ahead, for a comparison on the same build. Reading the thread's cycles is a Windows call,
so this build lets the primitives crate deny unsafe code instead of forbidding it and allows that one
call. The node repository has none of this; it is a measuring build only.
"""

from pathlib import Path

PROBE = Path("crates/primitives/src/probe.rs")
LIB = Path("crates/primitives/src/lib.rs")
PRODUCER = Path("crates/traidag/src/cut/producer/mod.rs")
PREFIX_CORE = Path("crates/traidag/src/cut/producer/prefix.rs")
WORK = Path("crates/traidag/src/state/work_execution.rs")
PAYMENT = Path("crates/traidag/src/state/payment.rs")
KEYS = Path("crates/traidag/src/state/key_registry_authorization.rs")
EXECUTE = Path("crates/traidag/src/state/cut_batch/execute.rs")
READER = Path("crates/traidag-storage/src/rocks/finalized_state.rs")
STORE = Path("crates/traidag-storage/src/rocks/mod.rs")
VALIDATED = Path("crates/traidag-storage/src/rocks/install/validated.rs")
AUTH = Path("crates/auth/src/mldsa65.rs")
APPLY = Path("crates/traidag-storage/src/rocks/install/apply.rs")
SCHEMA = Path("crates/traidag-storage/src/rocks/schema.rs")
LOCKS = Path("crates/traidag-storage/src/rocks/lock_index.rs")
ENGINE = Path("crates/node/src/chain/engine.rs")
PREFIX = Path("crates/node/src/chain/prefix.rs")
WORKER = Path("crates/node/src/chain/finality/prefix_worker.rs")
ADMISSION = Path("crates/node/src/chain/transaction_admission.rs")
FINALITY = Path("crates/node/src/chain/finality.rs")
AVAILABILITY = Path("crates/node/src/chain/finality/availability.rs")
SERVICE = Path("crates/node/src/chain/finality/service.rs")
HOST = Path("crates/node/src/p2p/host.rs")
MINING_ROUTES = Path("crates/node/src/api/pool_routes.rs")
RUNNER = Path("crates/node/src/p2p/swarm/runner.rs")
RELAY_LANE = Path("crates/node/src/p2p/swarm/relay_lane.rs")
DISPATCH = Path("crates/node/src/p2p/swarm/runner/dispatch.rs")

P = "tenrai_primitives::probe"

PROBE_SOURCE = r'''use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::future::Future;
use std::panic::Location;
use std::pin::Pin;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::task::{Context, Poll};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

type Key = (&'static str, &'static str, u32);
type DbStats = Box<dyn Fn() -> Option<String> + Send>;
/// Unix milliseconds, what happened, the first bytes of the block ID.
type BlockEvent = (u64, &'static str, [u8; 8]);
/// State node reads, those that missed the cache, nanoseconds spent in the misses.
type Reads = [u64; 3];

#[derive(Default, Clone, Copy)]
struct Stat {
    count: u64,
    wall: u64,
    max: u64,
    cpu: u64,
    bytes: u64,
    reads: Reads,
}

impl Stat {
    fn add(&mut self, other: Stat) {
        self.count += other.count;
        self.wall += other.wall;
        self.max = self.max.max(other.max);
        self.cpu += other.cpu;
        self.bytes += other.bytes;
        for (total, value) in self.reads.iter_mut().zip(other.reads) {
            *total += value;
        }
    }
}

/// Each thread adds up on its own and hands its sums over at most once a second, so the
/// measuring does not make threads wait for each other.
#[derive(Default)]
struct Local {
    stats: HashMap<Key, Stat>,
    blocks: Vec<BlockEvent>,
    reads: [u64; 4],
    flushed: Option<Instant>,
}

impl Local {
    fn flush(&mut self) {
        merge(std::mem::take(&mut self.stats), std::mem::take(&mut self.blocks));
        for (total, value) in [&READS, &READ_MISSES, &READ_NS, &MISS_NS].iter().zip(self.reads) {
            total.fetch_add(value, Ordering::Relaxed);
        }
        self.reads = [0; 4];
    }

    fn flush_due(&mut self, now: Instant) {
        if self.flushed.is_none_or(|at| now.duration_since(at) >= Duration::from_secs(1)) {
            self.flushed = Some(now);
            self.flush();
        }
    }
}

impl Drop for Local {
    fn drop(&mut self) {
        self.flush();
    }
}

thread_local! {
    static LOCAL: RefCell<Local> = RefCell::new(Local::default());
    /// This thread's reads since it started, for what a span or a step read meanwhile.
    static READ_TOTALS: Cell<Reads> = const { Cell::new([0; 3]) };
}

static STATS: Mutex<Option<HashMap<Key, Stat>>> = Mutex::new(None);
static BLOCKS: Mutex<Vec<BlockEvent>> = Mutex::new(Vec::new());
static DB_STATS: Mutex<Option<DbStats>> = Mutex::new(None);
pub static LOGGED: AtomicU64 = AtomicU64::new(0);
static READS: AtomicU64 = AtomicU64::new(0);
static READ_MISSES: AtomicU64 = AtomicU64::new(0);
static READ_NS: AtomicU64 = AtomicU64::new(0);
static MISS_NS: AtomicU64 = AtomicU64::new(0);

fn merge(stats: HashMap<Key, Stat>, blocks: Vec<BlockEvent>) {
    if !stats.is_empty() {
        let mut global = STATS.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        let global = global.get_or_insert_with(HashMap::new);
        for (key, stat) in stats {
            global.entry(key).or_default().add(stat);
        }
    }
    if !blocks.is_empty() {
        BLOCKS
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .extend(blocks);
    }
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn cycles() -> u64 {
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetCurrentThread() -> isize;
        fn QueryThreadCycleTime(thread: isize, cycles: *mut u64) -> i32;
    }
    let mut value = 0_u64;
    unsafe { QueryThreadCycleTime(GetCurrentThread(), &mut value) };
    value
}

#[cfg(not(windows))]
fn cycles() -> u64 {
    0
}

fn cycles_per_ns() -> f64 {
    static RATE: OnceLock<f64> = OnceLock::new();
    *RATE.get_or_init(|| {
        let mut best = 0.0_f64;
        for _ in 0..5 {
            let wall = Instant::now();
            let start = cycles();
            while wall.elapsed() < Duration::from_millis(10) {
                std::hint::spin_loop();
            }
            let rate = (cycles() - start) as f64 / wall.elapsed().as_nanos() as f64;
            best = best.max(rate);
        }
        let probe = Instant::now();
        for _ in 0..10_000 {
            std::hint::black_box(cycles());
        }
        eprintln!(
            "PROFINFO cycles_per_ns={best:.3} cycle_read_ns={:.0}",
            probe.elapsed().as_nanos() as f64 / 10_000.0
        );
        if best > 0.0 { best } else { 1.0 }
    })
}

fn reads_since(start: Reads) -> Reads {
    let now = READ_TOTALS.with(Cell::get);
    [now[0] - start[0], now[1] - start[1], now[2] - start[2]]
}

fn record(key: Key, stat: Stat, now: Instant) {
    let _ = LOCAL.try_with(|local| {
        let mut local = local.borrow_mut();
        local.stats.entry(key).or_default().add(stat);
        local.flush_due(now);
    });
}

pub struct Span {
    key: Key,
    wall: Instant,
    cycles: Option<u64>,
    bytes: u64,
    reads: Reads,
}

impl Span {
    pub fn bytes(&mut self, bytes: u64) {
        self.bytes += bytes;
    }
}

impl Drop for Span {
    fn drop(&mut self) {
        let now = Instant::now();
        let cpu = self.cycles.map_or(0, |since| {
            (cycles().saturating_sub(since) as f64 / cycles_per_ns()) as u64
        });
        let wall = (now - self.wall).as_nanos() as u64;
        let stat = Stat {
            count: 1,
            wall,
            max: wall,
            cpu,
            bytes: self.bytes,
            reads: reads_since(self.reads),
        };
        record(self.key, stat, now);
    }
}

fn open(key: Key, cpu: bool) -> Span {
    start();
    let cycles = cpu.then(|| {
        let _ = cycles_per_ns();
        cycles()
    });
    Span { key, wall: Instant::now(), cycles, bytes: 0, reads: READ_TOTALS.with(Cell::get) }
}

/// Wall and CPU time of a code path, and the state nodes it read.
pub fn span(name: &'static str) -> Span {
    open((name, "", 0), true)
}

pub fn span_at(name: &'static str, at: &'static Location<'static>) -> Span {
    open((name, at.file(), at.line()), true)
}

/// Wall time only, for frequent paths and for waits.
pub fn span_wall(name: &'static str) -> Span {
    open((name, "", 0), false)
}

pub fn span_wall_at(name: &'static str, at: &'static Location<'static>) -> Span {
    open((name, at.file(), at.line()), false)
}

pub fn bytes(name: &'static str, bytes: u64) {
    start();
    record((name, "", 0), Stat { count: 1, bytes, ..Stat::default() }, Instant::now());
}

/// Counts one event of a kind, such as each answer a route gives.
pub fn count(prefix: &'static str, label: &'static str) {
    start();
    record((prefix, label, 0), Stat { count: 1, ..Stat::default() }, Instant::now());
}

/// One step in the life of a block on this node, for following it across the nodes.
pub fn block(kind: &'static str, id: &[u8]) {
    start();
    let at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as u64)
        .unwrap_or(0);
    let mut short = [0_u8; 8];
    let length = id.len().min(8);
    short[..length].copy_from_slice(&id[..length]);
    let _ = LOCAL.try_with(|local| {
        let mut local = local.borrow_mut();
        local.blocks.push((at, kind, short));
        local.flush_due(Instant::now());
    });
}

thread_local! {
    /// What this thread is doing, for counting signature checks by where they run.
    static PLACE: Cell<&'static str> = const { Cell::new("other") };
}

pub struct Place(&'static str);

/// Names what the thread does until the returned value is dropped.
pub fn place(name: &'static str) -> Place {
    Place(PLACE.with(|place| place.replace(name)))
}

impl Drop for Place {
    fn drop(&mut self) {
        PLACE.with(|place| place.set(self.0));
    }
}

/// Counts one event under the name of what the thread is doing.
pub fn at_place(prefix: &'static str) {
    count(prefix, PLACE.with(Cell::get));
}

pub fn node_read(nanos: u64, miss: bool) {
    READ_TOTALS.with(|totals| {
        let mut reads = totals.get();
        reads[0] += 1;
        if miss {
            reads[1] += 1;
            reads[2] += nanos;
        }
        totals.set(reads);
    });
    let _ = LOCAL.try_with(|local| {
        let mut local = local.borrow_mut();
        local.reads[0] += 1;
        local.reads[2] += nanos;
        if miss {
            local.reads[1] += 1;
            local.reads[3] += nanos;
        }
        if local.reads[0] % 4096 == 0 {
            local.flush_due(Instant::now());
        }
    });
}

/// The steps of one function, each under its own name.
pub struct Phases {
    prefix: &'static str,
    at: Instant,
    cycles: u64,
    reads: Reads,
}

pub fn phases(prefix: &'static str) -> Phases {
    start();
    let _ = cycles_per_ns();
    Phases { prefix, at: Instant::now(), cycles: cycles(), reads: READ_TOTALS.with(Cell::get) }
}

impl Phases {
    pub fn step(&mut self, name: &'static str) {
        let now = Instant::now();
        let current = cycles();
        let wall = (now - self.at).as_nanos() as u64;
        let stat = Stat {
            count: 1,
            wall,
            max: wall,
            cpu: (current.saturating_sub(self.cycles) as f64 / cycles_per_ns()) as u64,
            bytes: 0,
            reads: reads_since(self.reads),
        };
        record((self.prefix, name, 0), stat, now);
        self.at = now;
        self.cycles = current;
        self.reads = READ_TOTALS.with(Cell::get);
    }
}

pub struct Timed<F> {
    name: &'static str,
    inner: F,
}

pub fn timed<F: Future + Unpin>(name: &'static str, inner: F) -> Timed<F> {
    Timed { name, inner }
}

impl<F: Future + Unpin> Future for Timed<F> {
    type Output = F::Output;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<F::Output> {
        let _span = span_wall(self.name);
        Pin::new(&mut self.inner).poll(cx)
    }
}

pub fn register_db(stats: DbStats) {
    *DB_STATS.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(stats);
    start();
}

fn start() {
    static STARTED: OnceLock<()> = OnceLock::new();
    STARTED.get_or_init(|| {
        let _ = std::thread::Builder::new()
            .name("probe-report".into())
            .spawn(report);
    });
}

fn ms(nanos: u64) -> f64 {
    nanos as f64 / 1_000_000.0
}

fn report() {
    let mut previous: HashMap<String, f64> = HashMap::new();
    loop {
        std::thread::sleep(Duration::from_secs(10));
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|elapsed| elapsed.as_millis())
            .unwrap_or(0);
        let stats = STATS
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take()
            .unwrap_or_default();
        let mut sorted: Vec<_> = stats.into_iter().collect();
        sorted.sort_by(|left, right| right.1.wall.cmp(&left.1.wall));
        let mut lines = Vec::new();
        for ((name, file, line), stat) in sorted {
            let site = match (file.is_empty(), line) {
                (true, _) => format!("{name} -"),
                (false, 0) => format!("{name}.{} -", file.replace(char::is_whitespace, "_")),
                (false, line) => format!("{name} {file}:{line}"),
            };
            lines.push(format!(
                "PROF {now} {site} n={} wall_ms={:.1} max_ms={:.1} cpu_ms={:.1} bytes={} reads={} \
                 misses={} miss_ms={:.1}",
                stat.count,
                ms(stat.wall),
                ms(stat.max),
                ms(stat.cpu),
                stat.bytes,
                stat.reads[0],
                stat.reads[1],
                ms(stat.reads[2])
            ));
        }
        lines.push(format!(
            "PROF {now} db.node_read - n={} misses={} read_ms={:.1} miss_ms={:.1}",
            READS.swap(0, Ordering::Relaxed),
            READ_MISSES.swap(0, Ordering::Relaxed),
            ms(READ_NS.swap(0, Ordering::Relaxed)),
            ms(MISS_NS.swap(0, Ordering::Relaxed))
        ));
        let blocks = std::mem::take(
            &mut *BLOCKS.lock().unwrap_or_else(|poisoned| poisoned.into_inner()),
        );
        for (at, kind, id) in blocks {
            let id: String = id.iter().map(|byte| format!("{byte:02x}")).collect();
            lines.push(format!("BLK {at} {kind} {id}"));
        }
        let text = DB_STATS
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .as_ref()
            .and_then(|stats| stats());
        if let Some(text) = text {
            for line in text.lines() {
                let fields: Vec<&str> = line.split_whitespace().collect();
                let Some(name) = fields.first() else { continue };
                let value = |label: &str| {
                    fields
                        .windows(3)
                        .find(|window| window[0] == label && window[1] == ":")
                        .and_then(|window| window[2].parse::<f64>().ok())
                };
                let Some(count) = value("COUNT") else { continue };
                let before = previous.insert(format!("{name}#count"), count).unwrap_or(0.0);
                if count <= before {
                    continue;
                }
                match value("P50") {
                    None => lines.push(format!("PROFDB {now} {name} {}", count - before)),
                    Some(p50) => {
                        let sum = value("SUM").unwrap_or(0.0);
                        let sum_before = previous.insert(format!("{name}#sum"), sum).unwrap_or(0.0);
                        lines.push(format!(
                            "PROFHIST {now} {name} n={} sum={} p50={p50} p99={} max={}",
                            count - before,
                            sum - sum_before,
                            value("P99").unwrap_or(0.0),
                            value("P100").unwrap_or(0.0)
                        ));
                    }
                }
            }
        }
        eprintln!("{}", lines.join("\n"));
    }
}
'''


def before(path, anchor, lines):
    """Puts lines in front of an anchor that occurs once."""
    return (path, anchor, "".join(f"{line}\n" for line in lines) + anchor)


def after(path, anchor, lines):
    """Puts lines behind an anchor that occurs once."""
    return (path, anchor, anchor + "".join(f"{line}\n" for line in lines))


def spanned(path, signature, name, indent="        "):
    """Measures a whole function from its first line to its return. The anchor may run on into the
    body to be unique; the probe goes right behind the brace that opens the body."""
    opened = signature.index("{\n") + 2
    return (path, signature, signature[:opened] + f'{indent}let _probe = {P}::span("{name}");\n'
            + signature[opened:])


def step(path, anchor, name, indent="    "):
    """Ends the running step of a function's phases under a name."""
    return before(path, anchor, [f'{indent}phases.step("{name}");'])



def payment_steps(text):
    """Times the steps of applying one payment to the cut's state."""
    start = text.index("    pub fn execute_payment(")
    end = text.index("    pub fn execute_stake(", start)
    body = text[start:end]
    steps = [
        ("        let transaction = registration.transaction();", None),
        ("        let outputs = regular_output_records(", "inputs_match"),
        ("        let spent = self", "outputs"),
        ("        let prepared_keys = self.prepare_keys(", "spend"),
        ("        let receipt = PaymentReceipt::applied(transaction_id, registration.fee());", "keys"),
    ]
    for anchor, name in steps:
        assert body.count(anchor) == 1, anchor
        line = f'        phases.step("{name}");' if name else f'        let mut phases = {P}::phases("exec");'
        body = body.replace(anchor, line + chr(10) + anchor)
    return text[:start] + body + text[end:]


def every(old, new):
    """Replaces every occurrence, for code that repeats the same line."""
    def apply(text):
        assert old in text, old
        return text.replace(old, new)
    return apply


SWARM_CALLS = [
    "handle_swarm_event(", "handle_completion(", "dispatch_sync_cut_plans(", "dispatch_work_sync_requests(",
    "dispatch_pending_sync_validations(", "dispatch_ready_cuts(", "dispatch_live_work(", "dispatch_relay(",
    "state.relay.mark_known(", "state.relay.queue_local_transaction(", "flush_work_announcements(",
    "flush_transaction_announcements(", "super::inventory_check::recheck(", "sample_pressure(",
    "dispatch::resume_deferred_peers(", "dispatch_cut_requests(", "dispatch::want_work(", "announce_cut(",
    "state.live_work.cut_installed(", "state.addresses.refresh_committee(",
]


def time_swarm_loop(text):
    """Wraps every handler and dispatcher call of the runner's select loop in a span."""
    start = text.index("    loop {\n        tokio::select! {\n            event = swarm.select_next_some() => {\n")
    end = text.index("\nfn handle_swarm_event(")
    lines = text[start:end].split("\n")
    out, open_call = [], None
    for line in lines:
        stripped = line.strip()
        indent = line[: len(line) - len(line.lstrip())]
        if open_call is None:
            call = next((call for call in SWARM_CALLS if stripped.startswith(call)), None)
            if call is not None:
                name = call.rstrip("(").replace("state.", "").replace("super::", "").replace("dispatch::", "")
                out.append(f'{indent}let probe = {P}::span_wall("swarm.{name}");')
                open_call = indent
        out.append(line)
        if open_call is not None and line.startswith(open_call) and stripped.endswith(");") and (
                line[len(open_call)] != " "):
            out.append(f"{open_call}drop(probe);")
            open_call = None
    region = "\n".join(out)
    region = region.replace("            event = swarm.select_next_some() => {\n",
                            f'            event = {P}::timed("swarm.poll", swarm.select_next_some()) => {{\n', 1)
    assert open_call is None and "swarm.poll" in region
    return text[:start] + region + text[end:]


def live_raw(text):
    """Counts how each block taken from a peer ended, and at which lock a busy one gave up."""
    signature = "    pub(crate) fn try_submit_live_raw(&self, raw: RawWorkPayload) -> LiveWorkAdmission {\n"
    assert text.count(signature) == 1
    text = text.replace(signature, (
        signature
        + f'        let _probe = {P}::span("live.raw");\n'
        + f'        {P}::block("try", raw.header.block_id().as_bytes());\n'
        + "        let result = self.try_submit_live_raw_inner(raw);\n"
        + f'        {P}::count("live.raw", match &result {{\n'
        + '            LiveWorkAdmission::Accepted(Acceptance::Accepted) => "accepted",\n'
        + '            LiveWorkAdmission::Accepted(Acceptance::Known) => "known",\n'
        + '            LiveWorkAdmission::Busy(_) => "busy",\n'
        + '            LiveWorkAdmission::Early(_) => "early",\n'
        + '            LiveWorkAdmission::Incomplete(_) => "incomplete",\n'
        + '            LiveWorkAdmission::Rejected(_) => "rejected",\n'
        + "        });\n        result\n    }\n\n"
        + "    fn try_submit_live_raw_inner(&self, raw: RawWorkPayload) -> LiveWorkAdmission {\n"))
    start = text.index("    fn try_submit_live_raw_inner(")
    end = text.index("    fn authorize_live_work(", start)
    body = text[start:end]
    busy = "            Err(HostError::Busy) => return LiveWorkAdmission::Busy(raw.into()),\n"
    assert body.count(busy) == 3, body.count(busy)
    for step in ("precheck", "prepare", "authorize"):
        body = body.replace(busy, (
            "            Err(HostError::Busy) => {\n"
            f'                {P}::count("live.busy", "{step}");\n'
            "                return LiveWorkAdmission::Busy(raw.into());\n            }\n"), 1)
    commit = "Ok(Err(CutEngineError::LiveWorkAdmissionStale)) | Err(HostError::Busy) => {\n"
    assert body.count(commit) == 1
    body = body.replace(commit, commit + f'{P}::count("live.busy", "commit");\n')
    return text[:start] + body + text[end:]


def lock_helpers(text):
    """Times how long each engine lock was waited for and held, by the code that asked."""
    for kind in ("read", "write"):
        mutable = "mut " if kind == "write" else ""
        lines = [f"    fn {kind}<T>(&self, with: impl FnOnce(&{mutable}CutEngine) -> T) -> Result<T, HostError> {{",
                 f"        let {mutable}engine = self.engine.{kind}().map_err(|_| HostError::Poisoned)?;",
                 f"        Ok(with(&{mutable}engine))"]
        old = "\n".join(lines) + "\n"
        assert text.count(old) == 1, old
        text = text.replace(old, "\n".join([
            "    #[track_caller]",
            lines[0],
            "        let at = std::panic::Location::caller();",
            f'        let wait = {P}::span_wall_at("lock.{kind}.wait", at);',
            lines[1],
            "        drop(wait);",
            f'        let _hold = {P}::span_at("lock.{kind}.hold", at);',
            lines[2]]) + "\n")
    for kind, call in (("read_by", "try_read"), ("write_by", "try_write")):
        mutable = "mut " if kind == "write_by" else ""
        old = (f"        loop {{\n            match self.engine.{call}() {{\n"
               f"                Ok({mutable}engine) => return Ok(with(&{mutable}engine)),\n")
        assert text.count(old) == 1, old
        text = text.replace(old, (
            "        let at = std::panic::Location::caller();\n"
            f'        let wait = {P}::span_wall_at("lock.{kind}.wait", at);\n'
            f"        loop {{\n            match self.engine.{call}() {{\n"
            f"                Ok({mutable}engine) => {{\n"
            "                    drop(wait);\n"
            f'                    let _hold = {P}::span_at("lock.{kind}.hold", at);\n'
            f"                    return Ok(with(&{mutable}engine));\n"
            "                }\n"))
        signature = f"    fn {kind}<T>(\n"
        assert text.count(signature) == 1, signature
        text = text.replace(signature, "    #[track_caller]\n" + signature)
    signature = "    fn optional_write_by<T>(\n"
    assert text.count(signature) == 1
    text = text.replace(signature, "    #[track_caller]\n" + signature)
    old = ("    fn optional_write<T>(&self, with: impl FnOnce(&mut CutEngine) -> T) -> Result<T, HostError> {\n"
           "        let mut engine = self.engine.write().map_err(|_| HostError::Poisoned)?;\n")
    assert text.count(old) == 1
    return text.replace(old, (
        "    #[track_caller]\n"
        "    fn optional_write<T>(&self, with: impl FnOnce(&mut CutEngine) -> T) -> Result<T, HostError> {\n"
        "        let at = std::panic::Location::caller();\n"
        f'        let wait = {P}::span_wall_at("lock.optional_write.wait", at);\n'
        "        let mut engine = self.engine.write().map_err(|_| HostError::Poisoned)?;\n"
        "        drop(wait);\n"
        f'        let _hold = {P}::span_at("lock.optional_write.hold", at);\n'))


PATCHES = [
    (PROBE, None, PROBE_SOURCE),
    before(LIB, "pub mod recipient_payloads;\n", ["pub mod probe;"]),
    (LIB, "#![forbid(unsafe_code)]\n", "#![deny(unsafe_code)]\n"),

    # Cut execution in the consensus core: the prefix and the rest of the cut, step by step.
    after(PREFIX_CORE, "    let profile = input.profile;\n    let rules = profile.execution_rules();\n",
          [f'    let mut phases = {P}::phases("cut.prefix");']),
    step(PREFIX_CORE, "    let anchors = prefix\n        .entries\n", "prepare"),
    step(PREFIX_CORE, "    let receipts = execution.execute_with_evidence(&mut batch, work.evidence)?;\n", "setup"),
    step(PREFIX_CORE, "    let update = batch.finish()?;\n", "execute"),
    step(PREFIX_CORE, "    let prepared = update.prepare(\n", "hash"),
    step(PREFIX_CORE, "    Ok(ExecutedPrefix {\n", "finish"),
    before(PRODUCER, "    let execution_set = executed.execution_set.clone();\n",
           [f'    let mut phases = {P}::phases("cut.rest");']),
    step(PRODUCER, "    let registered_work: Vec<_> = input\n        .registrations\n", "take_prefix"),
    step(PRODUCER, "    let (block_status, block_status_nodes) = advance_block_status(\n", "registration_emission_fees"),
    step(PRODUCER, "    let anchor_registrations = anchor_registrations(profile, &registration)?;\n", "block_status"),
    step(PRODUCER, "    let mut staged: std::collections::BTreeMap<Hash384, Vec<u8>> = prepared\n", "daa_stake"),
    step(PRODUCER, "    let utxo_commitment = {\n", "settle"),
    step(PRODUCER, "    let epoch_state_update = input.predecessor_epoch_state.advance(\n", "utxo_commitment"),
    step(PRODUCER, "    let post_rotation_reader = StagedNodeReader::new(input.reader, &staged);\n",
         "epoch_priority_candidate"),
    step(PRODUCER, "    let emission_transition = PreparedEmissionTransition::prepare(\n", "pos_finalization"),
    step(PRODUCER, "    let state_nodes = compact_final_state_nodes(\n", "emission_epoch_nodes"),
    step(PRODUCER, "    let state_values = [\n", "compact"),
    step(PRODUCER, "    Ok(ProducedCut {\n        candidate,\n", "values"),
    before(WORK, "            let rechecked = crate::RegisteredWork::admit(\n",
           [f'            let tenq = {P}::span("work.tenq_recheck");']),
    before(WORK, "            if rechecked != registered {\n", ["            drop(tenq);"]),
    (PAYMENT, "    let (anchor_inputs, fee) = validate_anchored_transaction(&request)?;\n",
     f'    let inputs = {P}::span("tx.inputs");\n'
     "    let (anchor_inputs, fee) = validate_anchored_transaction(&request)?;\n    drop(inputs);\n"),
    before(KEYS, "    let mut resolver = AnchoredKeyResolver::default();\n    let mut pending = BTreeMap::new();\n",
           [f'    let keys = {P}::span("tx.keys");']),
    (KEYS, "    verify_input_authorizations_with_resolver(transaction, anchor_inputs, &resolver)?;\n"
           "    Ok(pending.into_values().collect())\n",
     f'    drop(keys);\n    let signatures = {P}::span("tx.signatures");\n'
     "    verify_input_authorizations_with_resolver(transaction, anchor_inputs, &resolver)?;\n"
     "    drop(signatures);\n    Ok(pending.into_values().collect())\n"),

    (EXECUTE, payment_steps, None),

    # Storage: node reads, every write and sync by caller, RocksDB statistics.
    (READER, """        if let Some(cached) = self.store.nodes().get(id) {
            return Ok(Some(cached.as_ref().clone()));
        }
        let column = self.store.cf(CF_STATE_NODE)?;
        let node = self.store.db().get_cf(&column, id.as_bytes())?;
""", f"""        let probe = std::time::Instant::now();
        if let Some(cached) = self.store.nodes().get(id) {{
            let node = cached.as_ref().clone();
            {P}::node_read(probe.elapsed().as_nanos() as u64, false);
            return Ok(Some(node));
        }}
        let column = self.store.cf(CF_STATE_NODE)?;
        let node = self.store.db().get_cf(&column, id.as_bytes())?;
        {P}::node_read(probe.elapsed().as_nanos() as u64, true);
"""),
    (STORE, """    pub(crate) fn write_logged(&self, batch: WriteBatch) -> Result<(), StoreError> {
        self.db.write(batch)?;
""", f"""    #[track_caller]
    pub(crate) fn write_logged(&self, batch: WriteBatch) -> Result<(), StoreError> {{
        let mut probe = {P}::span_at("db.logged", std::panic::Location::caller());
        let size = batch.size_in_bytes() as u64;
        probe.bytes(size);
        {P}::LOGGED.fetch_add(size, Ordering::Relaxed);
        self.db.write(batch)?;
"""),
    (STORE, """        let started = std::time::Instant::now();
        if let Err(error) = self.db.flush_wal(true) {
""", f"""        let started = std::time::Instant::now();
        let mut probe = {P}::span_at("db.sync", std::panic::Location::caller());
        probe.bytes({P}::LOGGED.swap(0, Ordering::Relaxed));
        if let Err(error) = self.db.flush_wal(true) {{
"""),
    (STORE, """        let mut options = WriteOptions::default();
        options.set_sync(true);
        // A synced write puts everything logged before it on disk as well.
""", f"""        let mut options = WriteOptions::default();
        options.set_sync(true);
        let mut probe = {P}::span_at("db.write_sync", std::panic::Location::caller());
        probe.bytes(batch.size_in_bytes() as u64);
        {P}::bytes("db.write_sync.covered_logged", {P}::LOGGED.swap(0, Ordering::Relaxed));
        // A synced write puts everything logged before it on disk as well.
"""),
    (STORE, "    fn from_open_database(db: Arc<DB>, identity: StoreIdentity) -> Self {\n",
     "    fn from_open_database(db: Arc<DB>, identity: StoreIdentity) -> Self {\n"
     "        let weak = Arc::downgrade(&db);\n"
     f"        {P}::register_db(Box::new(move || {{\n"
     "            weak.upgrade()\n"
     '                .and_then(|db| db.property_value("rocksdb.options-statistics").ok().flatten())\n'
     "        }));\n"),
    after(SCHEMA, "    options.set_bytes_per_sync(1024 * 1024);\n", ["    options.enable_statistics();"]),
    (LOCKS, every("        self.db.write(batch)?;\n",
                  f'        {P}::bytes("db.lock_index", batch.size_in_bytes() as u64);\n'
                  "        self.db.write(batch)?;\n"), None),

    # Cut work in the node: copies, builds, checks, installs, the prefix run ahead.
    spanned(ENGINE, "    pub(crate) fn builder(&self) -> Result<CutBuilder, CutEngineError> {\n", "engine.copy"),
    spanned(ENGINE, "        transition_proof: CutTransitionProof,\n        receipt: AvailabilityReceipt,\n"
                    "    ) -> Result<CutCandidateBuild, CutEngineError> {\n", "cut.build"),
    spanned(ENGINE, "    ) -> Result<PreparedProposalCandidate, CutEngineError> {\n        #[cfg(test)]\n"
                    "        PROPOSAL_RUNS.with(|runs| runs.set(runs.get() + 1));\n", "cut.check"),
    spanned(ENGINE, "        parent_precommit_receipt: ParentPrecommitReceipt,\n"
                    "    ) -> Result<ProducedCut, CutEngineError> {\n", "cut.build_cut"),
    spanned(ENGINE, "        availability: AvailabilityPlan,\n    ) -> Result<PreparedCut, CutEngineError> {\n"
                    "        let predecessor_head = self.chain.finalized_head()?;\n", "cut.install.check"),
    spanned(ENGINE, "        prepared: PreparedCut,\n    ) -> Result<CutOutcome, CutEngineError> {\n"
                    "        let started = std::time::Instant::now();\n", "cut.install.commit"),
    before(ENGINE, "        let transition_proof = produced.transition_proof.clone();\n",
           [f'        let mut phases = {P}::phases("install.node");']),
    step(ENGINE, "        let head = certified.head();\n"
                 "        let parent_precommit_receipt = produced.parent_precommit_receipt.clone();\n",
         "certificate", "        "),
    step(ENGINE, "        let payload = self.payload(\n", "transaction_ids", "        "),
    step(ENGINE, "        let execution = CutExecutionRecord::from_finalized_cut(\n", "payload", "        "),
    step(ENGINE, "        let registered_transactions = registrations\n", "records", "        "),
    step(ENGINE, "        let retained = registrations\n", "registered_ids", "        "),
    step(ENGINE, "        let availability_admitted = availability.admission_ids();\n", "retained", "        "),
    step(ENGINE, "        let queue_bytes = produced.queue.encode();\n", "availability_records", "        "),
    step(ENGINE, "        let install = store.check_validated_cut(\n", "request", "        "),
    step(ENGINE, "        Ok(PreparedCut {\n            predecessor: predecessor_head,\n", "store_check",
         "        "),
    spanned(ENGINE, "    pub(crate) fn authorize(self) -> Result<AuthorizedRawWorkAdmission, RawWorkAuthorizationError> {\n",
            "work.authorize"),

    # Every ML-DSA check by what the thread was doing, kept or run in full.
    (AUTH, "            .is_some_and(|(passed, key)| passed.contains(key))\n"
           "        {\n"
           "            return Ok(());\n"
           "        }\n",
     "            .is_some_and(|(passed, key)| passed.contains(key))\n"
     "        {\n"
     f'            {P}::at_place("sig.kept");\n'
     "            return Ok(());\n"
     "        }\n"
     f'        {P}::at_place("sig.full");\n'),
    after(ADMISSION, "    pub(crate) fn validate(mut self) -> Result<Self, CutEngineError> {\n",
          [f'        let _place = {P}::place("mempool");']),
    after(ENGINE, "    pub(crate) fn authorize(self) -> Result<AuthorizedRawWorkAdmission, RawWorkAuthorizationError> {\n",
          [f'        let _place = {P}::place("block");']),
    after(ENGINE, "        PROPOSAL_RUNS.with(|runs| runs.set(runs.get() + 1));\n",
          [f'        let _place = {P}::place("check");']),
    after(ENGINE, "        let predecessor_head = self.chain.finalized_head()?;\n",
          [f'        let _place = {P}::place("install");']),
    after(PREFIX, "    fn execute(&self) -> Result<ExecutedPrefix, CutEngineError> {\n",
          [f'        let _place = {P}::place("prefix");']),

    # What checking a cut's availability artifacts at install is made of.
    before(VALIDATED, "    let transition = artifacts.transition();\n"
                      "    transition.verify_predecessor_queue(predecessor_queue)?;\n",
           [f'    let mut phases = {P}::phases("install.artifacts");']),
    step(VALIDATED, "    let admitted = validate_admitted_availability_work(profile, artifacts)?;\n", "transition"),
    step(VALIDATED, "    reject_registered_availability_work(predecessor, artifacts)?;\n", "admitted"),
    step(VALIDATED, "    let mut available = require_referenced_available_work(\n", "replay"),
    (VALIDATED, "    validate_selected_retained_work(profile, registration, &available, retained_work)?;\n"
                "    Ok(())\n}\n",
     '    phases.step("referenced");\n'
     "    validate_selected_retained_work(profile, registration, &available, retained_work)?;\n"
     '    phases.step("retained");\n'
     "    Ok(())\n}\n"),

    # The store's side of an install: what the check reads and what the write costs.
    (VALIDATED, "        let install = self.certified_install(genesis, profile, request)?;\n"
                "        self.check_certified_cut(&install, limits)?;\n",
     f'        let certified = {P}::span("install.store.certified_install");\n'
     "        let install = self.certified_install(genesis, profile, request)?;\n"
     "        drop(certified);\n"
     f'        let _probe = {P}::span("install.store.check_certified");\n'
     "        self.check_certified_cut(&install, limits)?;\n"),
    before(VALIDATED, "        let predecessor = self.finalized_state(genesis, profile)?;\n"
                      "        let head = predecessor.head();\n",
           [f'        let mut phases = {P}::phases("install.certified");']),
    step(VALIDATED, "        let certified = ConsensusCutVerifier::new(profile, genesis)?.verify(\n", "predecessor",
         "        "),
    step(VALIDATED, "        validate_availability_artifacts(\n            self,\n", "certificate", "        "),
    step(VALIDATED, "        let accounting = request\n            .emission_accounting\n",
         "availability_artifacts", "        "),
    step(VALIDATED, "        let install = CertifiedCutInstall::from_verified(\n", "emission_records", "        "),
    (APPLY, "        let (prepared, _) = self.prepare_on_head(request, limits)?;\n"
            "        self.assert_new_records(&prepared)\n",
     f'        let on_head = {P}::span("install.check.prepare_on_head");\n'
     "        let (prepared, _) = self.prepare_on_head(request, limits)?;\n"
     "        drop(on_head);\n"
     "        self.assert_new_records(&prepared)\n"),
    (APPLY, "        self.assert_new_finality_records(prepared)?;\n"
            "        self.assert_immutable_state_records(prepared)?;\n"
            "        self.assert_availability_artifact_records(prepared)?;\n"
            "        self.assert_new_application_records(prepared)\n",
     f'        let mut phases = {P}::phases("install.check");\n'
     "        self.assert_new_finality_records(prepared)?;\n"
     '        phases.step("new_finality");\n'
     "        self.assert_immutable_state_records(prepared)?;\n"
     '        phases.step("immutable_state");\n'
     "        self.assert_availability_artifact_records(prepared)?;\n"
     '        phases.step("availability_artifacts");\n'
     "        let result = self.assert_new_application_records(prepared);\n"
     '        phases.step("new_application");\n'
     "        result\n"),
    (APPLY, "        let (prepared, current) = self.prepare_on_head(request, limits)?;\n"
            "        self.write_install(prepared, current)\n",
     f'        let on_head = {P}::span("install.write.prepare_on_head");\n'
     "        let (prepared, current) = self.prepare_on_head(request, limits)?;\n"
     "        drop(on_head);\n"
     "        self.write_install(prepared, current)\n"),
    (APPLY, "        self.write_sync(self.install_batch(&prepared, archive_checkpoint)?)?;\n"
            "        self.nodes().put_all(\n",
     f'        let batching = {P}::span("install.write.batch");\n'
     "        let batch = self.install_batch(&prepared, archive_checkpoint)?;\n"
     "        drop(batching);\n"
     "        self.write_sync(batch)?;\n"
     f'        let _caching = {P}::span("install.write.cache");\n'
     "        self.nodes().put_all(\n"),
    spanned(ENGINE, "        body: tenrai_traidag::WorkBody,\n"
                    "    ) -> Result<PreparedRawWorkAdmission, CutEngineError> {\n"
                    "        self.pool().preflight_live_header(&header)?;\n", "work.prepare"),
    spanned(ENGINE, "        admission: AuthorizedRawWorkAdmission,\n    ) -> Result<(), CutEngineError> {\n"
                    "        let (verified, _cache_lease) = self.take_current_authorized_work(admission)?;\n",
            "work.commit"),
    spanned(ADMISSION, "    pub(crate) fn validate(mut self) -> Result<Self, CutEngineError> {\n", "tx.validate"),
    spanned(ADMISSION, "        candidate: TransactionCandidate,\n"
                       "    ) -> Result<PreparedTransactionAdmission, CutEngineError> {\n", "tx.prepare"),
    spanned(ADMISSION, "        prepared: PreparedTransactionAdmission,\n    ) -> Result<Hash384, CutEngineError> {\n",
            "tx.commit"),
    spanned(PREFIX, "        stake: &PosState,\n    ) -> Result<Arc<ExecutedPrefix>, CutEngineError> {\n",
            "cut.prefix.get"),
    spanned(PREFIX, "    fn execute(&self) -> Result<ExecutedPrefix, CutEngineError> {\n", "cut.prefix.run"),
    spanned(PREFIX, "    pub(crate) fn prefix_run(&self) -> Result<PrefixRun, CutEngineError> {\n",
            "cut.prefix.snapshot"),
    before(PREFIX, "                    slot = self\n                        .changed\n",
           [f'                    let _probe = {P}::span_wall("cut.prefix.wait");']),
    before(WORKER, "            let started = Instant::now();\n            let height = run.head()",
           [f'            let _probe = {P}::span("cut.prefix.ahead");']),

    # Finality and availability: signing, journals, the engine lock, the service's own time.
    spanned(FINALITY, "    fn sign_vote(&mut self, signer_index: u32, body: VoteBody) -> Result<(), FinalityError> {\n",
            "vote.sign_vote"),
    (FINALITY, every("let vote = Vote::sign(body, self.seat.signer_index, &self.seat.seed, randomness)?;",
                     f'let vote = {{ let _probe = {P}::span("vote.mldsa_sign"); '
                     "Vote::sign(body, self.seat.signer_index, &self.seat.seed, randomness)? };"), None),
    (FINALITY, """        let builder = self
            .engine
            .read()
            .map_err(|_| FinalityError::LockPoisoned)?
            .builder()?;
        Ok(builder.build(operation)?)
""", f"""        let copy = {P}::span("finality.copy");
        let builder = self
            .engine
            .read()
            .map_err(|_| FinalityError::LockPoisoned)?
            .builder()?;
        drop(copy);
        Ok(builder.build(operation)?)
"""),
    (FINALITY, """        let install = {
            let mut engine = self
                .engine
                .write()
                .map_err(|_| FinalityError::LockPoisoned)?;
            engine.install_candidate(candidate)
        };
""", f"""        let install = {{
            let wait = {P}::span_wall("lock.install.wait");
            let mut engine = self
                .engine
                .write()
                .map_err(|_| FinalityError::LockPoisoned)?;
            drop(wait);
            let _hold = {P}::span("lock.install.hold");
            engine.install_candidate(candidate)
        }};
"""),
    spanned(AVAILABILITY, "        outcome: &AvailabilityOutcome,\n    ) -> Result<(), FinalityError> {\n"
                          "        let missing = outcome\n", "avail.keep"),
    spanned(AVAILABILITY, "        outcome: &AvailabilityOutcome,\n"
                          "    ) -> Result<Option<AvailabilityVote>, FinalityError> {\n", "avail.sign_precommit"),
    spanned(SERVICE, "        event: ConsensusEvent,\n        pacemaker: &mut Pacemaker,\n"
                     "        storage_paused: &mut bool,\n    ) -> Result<(), FinalityError> {\n",
            "service.consensus"),
    spanned(SERVICE, "        event: AvailabilityEvent,\n    ) -> Result<(), FinalityError> {\n",
            "service.availability"),
    spanned(SERVICE, "        pacemaker: &mut Pacemaker,\n        storage_paused: &mut bool,\n"
                     "    ) -> Result<bool, FinalityError> {\n        if matches!(\n", "service.drive"),
    after(SERVICE, "    fn run_prefix_ahead(&mut self) {\n", [
        f'        let _probe = {P}::span("service.prefix_request");',
        '        if std::env::var_os("TENRAI_PROF_NO_PREFIX_AHEAD").is_some() {',
        "            return;",
        "        }"]),

    # The P2P side: engine lock waits by caller, transaction submission, the swarm loop.
    (HOST, lock_helpers, None),
    (HOST, live_raw, None),
    after(HOST, '                tracing::debug!(block = %announcement.header.block_id().to_hex(), '
                '"work block accepted");\n',
          [f'                {P}::block("accepted", announcement.header.block_id().as_bytes());']),

    # Where each block stands in the cuts: admitted to availability, registered, run.
    after(ENGINE, "        let stored = started.elapsed();\n", [
        "        for block_id in &prepared.availability_admitted {",
        f'            {P}::block("admitted", block_id.as_bytes());',
        "        }",
        "        for registration in &prepared.registrations {",
        f'            {P}::block("registered", registration.registered.block_id().as_bytes());',
        "        }",
        "        for block_id in &prepared.work_ids {",
        f'            {P}::block("executed", block_id.as_bytes());',
        "        }"]),

    # A block's way to this node: heard of, asked for, received, queued.
    after(RELAY_LANE, "        PeerMessage::WorkInventory(ids) => {\n"
                      "            send_response(swarm, route, PeerMessage::Ack);\n",
          ["            for id in &ids {", f'                {P}::block("heard", id.as_bytes());', "            }"]),
    (RELAY_LANE, """    let (peer, expected_id) = match state.live_work.enqueue(peer, expected_id, work) {
        Enqueued::Queued => return,
        Enqueued::Displaced { peer, expected_id } => (peer, expected_id),
        Enqueued::Refused => (peer, expected_id),
    };
""", f"""    {P}::block("got", expected_id.as_bytes());
    let (peer, expected_id) = match state.live_work.enqueue(peer, expected_id, work) {{
        Enqueued::Queued => {{
            {P}::count("live.enqueue", "queued");
            return;
        }}
        Enqueued::Displaced {{ peer, expected_id }} => {{
            {P}::count("live.enqueue", "displaced");
            (peer, expected_id)
        }}
        Enqueued::Refused => {{
            {P}::count("live.enqueue", "refused");
            (peer, expected_id)
        }}
    }};
"""),
    (DISPATCH, """                (PeerMessage::GetCompactWork { ids: ids.clone() }, pending)
""", f"""                for id in ids.iter() {{
                    {P}::block("asked", id.as_bytes());
                }}
                (PeerMessage::GetCompactWork {{ ids: ids.clone() }}, pending)
"""),
    (DISPATCH, """        LiveWorkTake::Ready(queued) => queued,
        LiveWorkTake::Deferred => {
""", f"""        LiveWorkTake::Ready(queued) => {{
            {P}::bytes("live.queue_behind", state.live_work.len() as u64);
            queued
        }}
        LiveWorkTake::Deferred => {{
            {P}::count("live.take", "deferred");
"""),
    spanned(HOST, "        candidate: TransactionCandidate,\n    ) -> Result<Hash384, HostError> {\n"
                  "        self.require_optional_ingress()?;\n        for attempt in 0..2 {\n", "tx.submit"),
    (RUNNER, time_swarm_loop, None),

    # Mining: how long templates and candidates take, and every answer a candidate gets.
    (MINING_ROUTES, """    let result = admit_candidate(&state, request).await;
""", f"""    let probe = {P}::span_wall("mining.candidate");
    let result = admit_candidate(&state, request).await;
    drop(probe);
    match &result {{
        Ok(response) => {P}::count("mining.answer", response.status),
        Err(failure) => {P}::count("mining.answer", failure.message()),
    }}
"""),
    (MINING_ROUTES, """) -> Result<Json<crate::chain::mining::WorkTemplate>, Failure> {
""", f""") -> Result<Json<crate::chain::mining::WorkTemplate>, Failure> {{
    let _probe = {P}::span_wall("mining.template");
"""),
]
