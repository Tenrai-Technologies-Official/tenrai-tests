//! UDP and TCP proxies that put intercontinental paths between local nodes.
//!
//!   wan-proxy CONFIG.json NODE
//!
//! Runs the proxies in front of NODE: each direction gets half the RTT, a drifting jitter, spikes and
//! loss, and one flow stays in order. A UDP sender is known by its source port, a TCP sender by the
//! process owning the socket (`lsof` outside Windows); TCP loss costs time, see tcp_model.rs. Logs one
//! line per direction every 10 s; a `switch_file` holding `off` passes everything until it says `on`.
//! A `cut_file` names nodes, separated by white space, whose UDP links lose every packet, as when
//! their internet is down, and groups such as `v1,v2|v3,v4`, between which every UDP packet is lost
//! while each group stays whole; it is read every 200 ms. A `slow_file` holds lines `NODE DELAY_MS LOSS`:
//! every link of that node gets DELAY_MS more in each direction and loses LOSS more of its
//! packets, as a congested or failing uplink would; it is read every 200 ms as well.
//!
//! A UDP direction can also be a link of limited speed: `rate_mbps` per pair, a queue that holds
//! `queue_ms` of waiting before it drops the newest packet, and `cross_share`, the share of the
//! link other traffic takes, as on a link that is busy with more than these nodes. That share
//! drifts around its mean, as flows that give way to each other do; with `cross_bursts` other
//! traffic instead holds the whole link in bursts, a storm rather than a busy line.

mod platform;
mod tcp_model;

use platform::{fine_timer, owner_of};
use serde_json::Value;
use std::collections::{HashMap, HashSet, hash_map::Entry};
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;
use tcp_model::{MSS, Sender};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream, UdpSocket};
use tokio::sync::mpsc::{UnboundedSender, unbounded_channel};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::time::{Instant, sleep_until};

/// Bytes a TCP direction may hold before the node's writes wait, about a socket send buffer.
const TCP_QUEUE_BYTES: usize = 4 * 1024 * 1024;
/// Mean length of one burst of other traffic on a busy link.
const CROSS_BURST_S: f64 = 0.2;
/// Waiting a limited link's queue holds when the config names none.
const DEFAULT_QUEUE_MS: f64 = 200.0;

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }

    fn unit(&mut self) -> f64 {
        (self.next() >> 11) as f64 / (1_u64 << 53) as f64
    }

    fn uniform(&mut self, low: f64, high: f64) -> f64 {
        low + (high - low) * self.unit()
    }

    fn gauss(&mut self) -> f64 {
        let a = self.unit().max(1e-12);
        let b = self.unit();
        (-2.0 * a.ln()).sqrt() * (std::f64::consts::TAU * b).cos()
    }

    fn exponential(&mut self, mean: f64) -> f64 {
        -mean * (1.0 - self.unit()).max(1e-12).ln()
    }
}

#[derive(Default)]
struct Stats {
    packets: u64,
    bytes: u64,
    dropped: u64,
    delay_sum: f64,
    delay_max: f64,
    late_max: f64,
    queue_max: f64,
    overflow: u64,
}

/// One direction between two nodes, shared by every flow on it.
struct Path {
    base: f64,
    jitter: f64,
    loss: f64,
    drift: f64,
    last: Instant,
    spike_until: Instant,
    spike_extra: f64,
    next_spike: Instant,
    /// Bytes a second the link carries, 0 for no limit, and the waiting its queue holds.
    rate: f64,
    queue_limit: f64,
    cross_share: f64,
    cross_bursts: bool,
    /// The share other traffic holds now, drifting around `cross_share`.
    cross_now: f64,
    cross_last: Instant,
    busy_until: Instant,
    next_cross: Instant,
    rng: Rng,
    stats: Stats,
}

struct Spikes {
    every: f64,
    extra: (f64, f64),
    length: (f64, f64),
}

impl Path {
    fn delay(&mut self, now: Instant, spikes: &Spikes) -> f64 {
        if self.jitter > 0.0 {
            let dt = now.saturating_duration_since(self.last).as_secs_f64();
            let (theta, mean, sigma) = (2.0, self.jitter / 2.0, self.jitter / 4.0);
            self.drift += theta * (mean - self.drift) * dt
                + sigma * (2.0 * theta * dt).sqrt() * self.rng.gauss();
            self.drift = self.drift.clamp(0.0, self.jitter);
        }
        self.last = now;
        if spikes.every > 0.0 && now >= self.next_spike {
            self.spike_extra = self.rng.uniform(spikes.extra.0, spikes.extra.1);
            self.spike_until =
                now + Duration::from_secs_f64(self.rng.uniform(spikes.length.0, spikes.length.1));
            self.next_spike = now + Duration::from_secs_f64(self.rng.exponential(spikes.every));
        }
        let spike = if now < self.spike_until {
            self.spike_extra
        } else {
            0.0
        };
        self.base + self.drift + spike
    }

    /// When a packet has left a limited link, after what waits before it, or None when the
    /// queue is full and drops it.
    fn transmit(&mut self, now: Instant, bytes: usize) -> Option<Instant> {
        if self.rate <= 0.0 {
            return Some(now);
        }
        let available = if self.cross_bursts {
            self.advance_cross(now);
            self.rate
        } else {
            self.rate * (1.0 - self.drift_cross(now))
        };
        let start = self.busy_until.max(now);
        let waited = start.saturating_duration_since(now).as_secs_f64();
        if waited > self.queue_limit {
            return None;
        }
        self.stats.queue_max = self.stats.queue_max.max(waited);
        self.busy_until = start + Duration::from_secs_f64(bytes as f64 / available);
        Some(self.busy_until)
    }

    /// The share other traffic holds, reverting to its mean within about a second.
    fn drift_cross(&mut self, now: Instant) -> f64 {
        if self.cross_share <= 0.0 {
            return 0.0;
        }
        let dt = now
            .saturating_duration_since(self.cross_last)
            .as_secs_f64()
            .min(1.0);
        self.cross_last = now;
        let (theta, sigma) = (1.0, self.cross_share / 3.0);
        self.cross_now += theta * (self.cross_share - self.cross_now) * dt
            + sigma * (2.0 * theta * dt).sqrt() * self.rng.gauss();
        self.cross_now = self.cross_now.clamp(0.0, 0.9);
        self.cross_now
    }

    /// Other traffic holds the whole link in bursts, idle long enough between them to take its
    /// share.
    fn advance_cross(&mut self, now: Instant) {
        if self.cross_share <= 0.0 {
            return;
        }
        while self.next_cross <= now {
            let burst = self.rng.exponential(CROSS_BURST_S);
            self.busy_until = self.busy_until.max(self.next_cross) + Duration::from_secs_f64(burst);
            let idle = self
                .rng
                .exponential(burst * (1.0 - self.cross_share) / self.cross_share);
            self.next_cross += Duration::from_secs_f64(burst + idle);
        }
    }
}

struct World {
    config: Value,
    spikes: Spikes,
    sources: HashMap<u16, String>,
    pids_file: Option<String>,
    pids: Mutex<HashMap<u32, String>>,
    paths: Mutex<HashMap<(String, String), Path>>,
    wan: AtomicBool,
    cut: RwLock<HashSet<String>>,
    /// Groups of nodes no packet passes between.
    parted: RwLock<Vec<HashSet<String>>>,
    /// Extra one-way delay in seconds and extra loss per node, from the `slow_file`.
    slow: RwLock<HashMap<String, (f64, f64)>>,
    tcp_bbr: bool,
}

fn link_value(table: &Value, a: &str, b: &str) -> f64 {
    match table {
        Value::Object(map) => map
            .get(&format!("{a}-{b}"))
            .or_else(|| map.get(&format!("{b}-{a}")))
            .and_then(Value::as_f64)
            .unwrap_or(0.0),
        other => other.as_f64().unwrap_or(0.0),
    }
}

fn pair(value: &Value, default: (f64, f64)) -> (f64, f64) {
    match value.as_array() {
        Some(items) if items.len() == 2 => (
            items[0].as_f64().unwrap() / 1000.0,
            items[1].as_f64().unwrap() / 1000.0,
        ),
        _ => default,
    }
}

impl World {
    fn new(config: Value) -> Self {
        let spikes = &config["spikes"];
        let sources = config["sources"]
            .as_object()
            .map(|map| {
                map.iter()
                    .map(|(port, node)| (port.parse().unwrap(), node.as_str().unwrap().to_owned()))
                    .collect()
            })
            .unwrap_or_default();
        Self {
            spikes: Spikes {
                every: spikes["every_s"].as_f64().unwrap_or(0.0),
                extra: pair(&spikes["extra_ms"], (0.08, 0.2)),
                length: pair(&spikes["length_ms"], (0.2, 0.6)),
            },
            sources,
            pids_file: config["pids_file"].as_str().map(str::to_owned),
            pids: Mutex::new(HashMap::new()),
            paths: Mutex::new(HashMap::new()),
            wan: AtomicBool::new(true),
            cut: RwLock::new(HashSet::new()),
            parted: RwLock::new(Vec::new()),
            slow: RwLock::new(HashMap::new()),
            tcp_bbr: config["tcp_model"].as_str() == Some("bbr"),
            config,
        }
    }

    fn with_path<T>(&self, from: &str, to: &str, work: impl FnOnce(&mut Path, &Spikes) -> T) -> T {
        let mut paths = self.paths.lock().unwrap();
        let path = paths
            .entry((from.to_owned(), to.to_owned()))
            .or_insert_with(|| {
                let now = Instant::now();
                let mut rng = Rng(0x9E37_79B9_7F4A_7C15
                    ^ (from.len() as u64 * 131 + to.len() as u64)
                    ^ seed_of(from, to));
                let next_spike = if self.spikes.every > 0.0 {
                    now + Duration::from_secs_f64(rng.exponential(self.spikes.every))
                } else {
                    now + Duration::from_secs(1 << 30)
                };
                let jitter = link_value(&self.config["jitter_ms"], from, to) / 1000.0;
                let cross_share =
                    link_value(&self.config["cross_share"], from, to).clamp(0.0, 0.95);
                let queue_ms = match link_value(&self.config["queue_ms"], from, to) {
                    ms if ms > 0.0 => ms,
                    _ => DEFAULT_QUEUE_MS,
                };
                let next_cross = if cross_share > 0.0 {
                    now + Duration::from_secs_f64(
                        rng.exponential(CROSS_BURST_S * (1.0 - cross_share) / cross_share),
                    )
                } else {
                    now
                };
                Path {
                    base: link_value(&self.config["rtt_ms"], from, to) / 2000.0,
                    jitter,
                    loss: link_value(&self.config["loss"], from, to),
                    drift: jitter / 2.0,
                    last: now,
                    spike_until: now,
                    spike_extra: 0.0,
                    next_spike,
                    rate: link_value(&self.config["rate_mbps"], from, to) * 1e6 / 8.0,
                    queue_limit: queue_ms / 1000.0,
                    cross_share,
                    cross_bursts: self.config["cross_bursts"].as_bool().unwrap_or(false),
                    cross_now: cross_share,
                    cross_last: now,
                    busy_until: now,
                    next_cross,
                    rng,
                    stats: Stats::default(),
                }
            });
        work(path, &self.spikes)
    }

    /// When a datagram should leave, or None when it is lost.
    fn datagram(
        &self,
        from: &str,
        to: &str,
        bytes: usize,
        last_due: &mut Instant,
    ) -> Option<Instant> {
        if self.is_cut(from) || self.is_cut(to) || self.is_parted(from, to) {
            self.with_path(from, to, |path, _| path.stats.dropped += 1);
            return None;
        }
        let wan = self.wan.load(Ordering::Relaxed);
        let (slower, lossier) = self.slowdown(from, to);
        self.with_path(from, to, |path, spikes| {
            let now = Instant::now();
            if !wan {
                *last_due = (*last_due).max(now);
                note(path, now, *last_due, bytes);
                return Some(*last_due);
            }
            if path.rng.unit() < 1.0 - (1.0 - path.loss) * (1.0 - lossier) {
                path.stats.dropped += 1;
                return None;
            }
            let Some(sent) = path.transmit(now, bytes) else {
                path.stats.dropped += 1;
                path.stats.overflow += 1;
                return None;
            };
            let delay = path.delay(now, spikes) + slower;
            let due = (*last_due).max(sent + Duration::from_secs_f64(delay));
            *last_due = due;
            note(path, now, due, bytes);
            Some(due)
        })
    }

    /// When a chunk of a TCP stream arrives; a lost segment in it delays the chunk and all after it.
    fn stream(&self, from: &str, to: &str, bytes: usize, sender: &mut Sender) -> Instant {
        let wan = self.wan.load(Ordering::Relaxed);
        let (slower, lossier) = self.slowdown(from, to);
        self.with_path(from, to, |path, spikes| {
            let now = Instant::now();
            if !wan {
                sender.last_due = sender.last_due.max(now);
                note(path, now, sender.last_due, bytes);
                return sender.last_due;
            }
            let mut delay = path.delay(now, spikes) + slower;
            let rtt = (2.0 * (path.base + slower) + path.drift).max(0.001);
            let segments = (bytes as f64 / MSS).ceil() as i32;
            let kept = (1.0 - path.loss) * (1.0 - lossier);
            let lost = path.rng.unit() < 1.0 - kept.powi(segments);
            if lost {
                path.stats.dropped += 1;
                delay += if path.rng.unit() < 0.8 {
                    rtt
                } else {
                    Sender::rto(rtt)
                };
            }
            let start = sender.send(now, bytes, rtt, lost);
            let due = sender.last_due.max(start + Duration::from_secs_f64(delay));
            sender.last_due = due;
            note(path, now, due, bytes);
            due
        })
    }

    fn is_parted(&self, from: &str, to: &str) -> bool {
        let groups = self.parted.read().unwrap();
        let group_of = |node: &str| groups.iter().position(|group| group.contains(node));
        matches!((group_of(from), group_of(to)), (Some(one), Some(other)) if one != other)
    }

    fn is_cut(&self, node: &str) -> bool {
        self.cut.read().unwrap().contains(node)
    }

    /// The extra one-way delay and loss of a link, from both its ends in the `slow_file`.
    fn slowdown(&self, from: &str, to: &str) -> (f64, f64) {
        let slow = self.slow.read().unwrap();
        let (a, b) = (
            slow.get(from).copied().unwrap_or_default(),
            slow.get(to).copied().unwrap_or_default(),
        );
        (a.0 + b.0, 1.0 - (1.0 - a.1) * (1.0 - b.1))
    }

    fn late(&self, from: &str, to: &str, due: Instant) {
        let late = Instant::now().saturating_duration_since(due).as_secs_f64();
        if let Some(path) = self
            .paths
            .lock()
            .unwrap()
            .get_mut(&(from.to_owned(), to.to_owned()))
        {
            path.stats.late_max = path.stats.late_max.max(late);
        }
    }

    fn tcp_source(&self, client_port: u16, listen_port: u16) -> Option<String> {
        let pid = owner_of(client_port, listen_port)?;
        if let Some(node) = self.pids.lock().unwrap().get(&pid) {
            return Some(node.clone());
        }
        let text = std::fs::read_to_string(self.pids_file.as_ref()?).ok()?;
        let fresh: HashMap<String, String> = serde_json::from_str(&text).ok()?;
        let mut pids = self.pids.lock().unwrap();
        pids.clear();
        for (pid, node) in fresh {
            if let Ok(pid) = pid.parse() {
                pids.insert(pid, node);
            }
        }
        pids.get(&pid).cloned()
    }
}

fn seed_of(from: &str, to: &str) -> u64 {
    from.bytes()
        .chain(to.bytes())
        .fold(0xCBF2_9CE4_8422_2325, |hash, byte| {
            (hash ^ byte as u64).wrapping_mul(0x100_0000_01B3)
        })
}

fn note(path: &mut Path, now: Instant, due: Instant, bytes: usize) {
    let delay = due.saturating_duration_since(now).as_secs_f64();
    path.stats.packets += 1;
    path.stats.bytes += bytes as u64;
    path.stats.delay_sum += delay;
    path.stats.delay_max = path.stats.delay_max.max(delay);
}

type Queue = UnboundedSender<(Instant, Vec<u8>)>;

fn udp_sender(
    world: Arc<World>,
    socket: Arc<UdpSocket>,
    to: Option<SocketAddr>,
    from: String,
    target: String,
) -> Queue {
    let (queue, mut items) = unbounded_channel::<(Instant, Vec<u8>)>();
    tokio::spawn(async move {
        while let Some((due, data)) = items.recv().await {
            sleep_until(due).await;
            world.late(&from, &target, due);
            let _ = match to {
                Some(address) => socket.send_to(&data, address).await,
                None => socket.send(&data).await,
            };
        }
    });
    queue
}

struct Flow {
    source: Option<String>,
    upward: Queue,
    last_due: Instant,
}

async fn udp_proxy(world: Arc<World>, node: String, listen: u16, target: u16) {
    let socket = Arc::new(
        UdpSocket::bind(("127.0.0.1", listen))
            .await
            .expect("the UDP port is free"),
    );
    let mut flows: HashMap<SocketAddr, Flow> = HashMap::new();
    let mut buffer = vec![0_u8; 65_536];
    loop {
        let Ok((length, client)) = socket.recv_from(&mut buffer).await else {
            continue;
        };
        let flow = match flows.entry(client) {
            Entry::Occupied(flow) => flow.into_mut(),
            Entry::Vacant(slot) => {
                let source = world.sources.get(&client.port()).cloned();
                if source.is_none() {
                    println!(
                        "{} unknown UDP sender port {} passes undelayed",
                        clock(),
                        client.port()
                    );
                }
                let upstream = Arc::new(
                    UdpSocket::bind("127.0.0.1:0")
                        .await
                        .expect("a local UDP port"),
                );
                upstream
                    .connect(("127.0.0.1", target))
                    .await
                    .expect("the node's port");
                let name = source.clone().unwrap_or_default();
                let upward = udp_sender(
                    world.clone(),
                    upstream.clone(),
                    None,
                    name.clone(),
                    node.clone(),
                );
                let downward = udp_sender(
                    world.clone(),
                    socket.clone(),
                    Some(client),
                    node.clone(),
                    name.clone(),
                );
                let (task_world, task_node, task_source) =
                    (world.clone(), node.clone(), source.clone());
                tokio::spawn(async move {
                    let mut buffer = vec![0_u8; 65_536];
                    let mut last_due = Instant::now();
                    loop {
                        // Windows reports a closed target port as a read error; the flow outlives a node restart.
                        let Ok(length) = upstream.recv(&mut buffer).await else {
                            tokio::time::sleep(Duration::from_millis(10)).await;
                            continue;
                        };
                        let due = match &task_source {
                            Some(source) => {
                                task_world.datagram(&task_node, source, length, &mut last_due)
                            }
                            None => Some(Instant::now()),
                        };
                        if let Some(due) = due {
                            let _ = downward.send((due, buffer[..length].to_vec()));
                        }
                    }
                });
                slot.insert(Flow {
                    source,
                    upward,
                    last_due: Instant::now(),
                })
            }
        };
        let due = match &flow.source {
            Some(source) => world.datagram(source, &node, length, &mut flow.last_due),
            None => Some(Instant::now()),
        };
        if let Some(due) = due {
            let _ = flow.upward.send((due, buffer[..length].to_vec()));
        }
    }
}

async fn pump(
    world: Arc<World>,
    mut reader: tokio::net::tcp::OwnedReadHalf,
    mut writer: tokio::net::tcp::OwnedWriteHalf,
    from: Option<String>,
    to: Option<String>,
) {
    let (queue, mut items) =
        unbounded_channel::<(Instant, Option<(Vec<u8>, OwnedSemaphorePermit)>)>();
    let names = (
        from.clone().unwrap_or_default(),
        to.clone().unwrap_or_default(),
    );
    let late_world = world.clone();
    let writing = tokio::spawn(async move {
        while let Some((due, data)) = items.recv().await {
            sleep_until(due).await;
            late_world.late(&names.0, &names.1, due);
            match data {
                Some((data, _held)) => {
                    if writer.write_all(&data).await.is_err() {
                        break;
                    }
                }
                None => {
                    let _ = writer.shutdown().await;
                    break;
                }
            }
        }
    });
    let room = Arc::new(Semaphore::new(TCP_QUEUE_BYTES));
    let mut buffer = vec![0_u8; 65_536];
    let mut sender = Sender::new(Instant::now(), world.tcp_bbr);
    loop {
        let length = reader.read(&mut buffer).await.unwrap_or(0);
        if length == 0 {
            let _ = queue.send((sender.last_due, None));
            break;
        }
        let Ok(held) = room.clone().acquire_many_owned(length as u32).await else {
            break;
        };
        let due = match (&from, &to) {
            (Some(from), Some(to)) => world.stream(from, to, length, &mut sender),
            _ => Instant::now().max(sender.last_due),
        };
        sender.last_due = due;
        let _ = queue.send((due, Some((buffer[..length].to_vec(), held))));
    }
    let _ = writing.await;
}

async fn tcp_proxy(world: Arc<World>, node: String, listen: u16, target: u16) {
    let listener = TcpListener::bind(("127.0.0.1", listen))
        .await
        .expect("the TCP port is free");
    loop {
        let Ok((client, address)) = listener.accept().await else {
            continue;
        };
        let world = world.clone();
        let node = node.clone();
        tokio::spawn(async move {
            let source = world.tcp_source(address.port(), listen);
            if source.is_none() {
                println!(
                    "{} unknown TCP sender port {} passes undelayed",
                    clock(),
                    address.port()
                );
            }
            let Ok(upstream) = TcpStream::connect(("127.0.0.1", target)).await else {
                return;
            };
            let _ = client.set_nodelay(true);
            let _ = upstream.set_nodelay(true);
            let (client_read, client_write) = client.into_split();
            let (upstream_read, upstream_write) = upstream.into_split();
            let up = pump(
                world.clone(),
                client_read,
                upstream_write,
                source.clone(),
                Some(node.clone()),
            );
            let down = pump(world, upstream_read, client_write, Some(node), source);
            tokio::join!(up, down);
        });
    }
}

fn clock() -> String {
    let seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    format!(
        "{:02}:{:02}:{:02}Z",
        seconds / 3600 % 24,
        seconds / 60 % 60,
        seconds % 60
    )
}

async fn report(world: Arc<World>) {
    loop {
        tokio::time::sleep(Duration::from_secs(10)).await;
        let mut paths = world.paths.lock().unwrap();
        let mut names: Vec<_> = paths.keys().cloned().collect();
        names.sort();
        for name in names {
            let stats = std::mem::take(&mut paths.get_mut(&name).unwrap().stats);
            let mean = if stats.packets > 0 {
                stats.delay_sum / stats.packets as f64
            } else {
                0.0
            };
            println!(
                "{} {}->{} packets {} bytes {} dropped {} mean {:.1} ms max {:.1} ms late max {:.1} ms queue max {:.1} ms overflow {}",
                clock(),
                name.0,
                name.1,
                stats.packets,
                stats.bytes,
                stats.dropped,
                mean * 1000.0,
                stats.delay_max * 1000.0,
                stats.late_max * 1000.0,
                stats.queue_max * 1000.0,
                stats.overflow
            );
        }
    }
}

async fn watch_switch(world: Arc<World>, file: String) {
    loop {
        let on = std::fs::read_to_string(&file).map_or(true, |text| text.trim() != "off");
        if world.wan.swap(on, Ordering::Relaxed) != on {
            println!(
                "{} wide area links {}",
                clock(),
                if on { "on" } else { "off" }
            );
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
}

async fn watch_cut(world: Arc<World>, file: String) {
    loop {
        let text = std::fs::read_to_string(&file).unwrap_or_default();
        let names = text
            .split_whitespace()
            .filter(|token| !token.contains('|'))
            .map(str::to_owned)
            .collect::<HashSet<_>>();
        let groups = text
            .split_whitespace()
            .filter(|token| token.contains('|'))
            .flat_map(|token| token.split('|'))
            .map(|group| group.split(',').map(str::to_owned).collect::<HashSet<_>>())
            .collect::<Vec<_>>();
        let changed = {
            let mut cut = world.cut.write().unwrap();
            let mut parted = world.parted.write().unwrap();
            let changed = *cut != names || *parted != groups;
            *cut = names.clone();
            *parted = groups;
            changed
        };
        if changed {
            let mut listed = names.into_iter().collect::<Vec<_>>();
            listed.sort();
            println!(
                "{} links cut for [{}], parted by [{}]",
                clock(),
                listed.join(" "),
                text.trim()
            );
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
}

async fn watch_slow(world: Arc<World>, file: String) {
    loop {
        let slow = std::fs::read_to_string(&file)
            .unwrap_or_default()
            .lines()
            .filter_map(|line| {
                let mut parts = line.split_whitespace();
                let node = parts.next()?.to_owned();
                let delay_ms = parts.next()?.parse::<f64>().ok()?;
                let loss = parts
                    .next()
                    .map_or(Some(0.0), |loss| loss.parse::<f64>().ok())?;
                Some((node, (delay_ms.max(0.0) / 1000.0, loss.clamp(0.0, 1.0))))
            })
            .collect::<HashMap<_, _>>();
        let changed = {
            let mut current = world.slow.write().unwrap();
            let changed = *current != slow;
            *current = slow.clone();
            changed
        };
        if changed {
            let mut listed = slow
                .iter()
                .map(|(node, (delay, loss))| {
                    format!("{node} +{:.0} ms {:.1} %", delay * 1000.0, loss * 100.0)
                })
                .collect::<Vec<_>>();
            listed.sort();
            println!("{} links slowed for [{}]", clock(), listed.join(", "));
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    let config: Value = serde_json::from_str(&std::fs::read_to_string(&args[1]).unwrap()).unwrap();
    let node = args[2].clone();
    fine_timer();
    let world = Arc::new(World::new(config));
    if let Some(file) = world.config["switch_file"].as_str() {
        let on = std::fs::read_to_string(file).map_or(true, |text| text.trim() != "off");
        world.wan.store(on, Ordering::Relaxed);
        println!(
            "{} wide area links {}",
            clock(),
            if on { "on" } else { "off" }
        );
        tokio::spawn(watch_switch(world.clone(), file.to_owned()));
    }
    if let Some(file) = world.config["cut_file"].as_str() {
        tokio::spawn(watch_cut(world.clone(), file.to_owned()));
    }
    if let Some(file) = world.config["slow_file"].as_str() {
        tokio::spawn(watch_slow(world.clone(), file.to_owned()));
    }
    for proxy in world.config["proxies"].as_array().unwrap().clone() {
        if proxy["node"].as_str() != Some(node.as_str()) {
            continue;
        }
        let listen = proxy["listen"].as_u64().unwrap() as u16;
        let target = proxy["target"].as_u64().unwrap() as u16;
        let protocol = proxy["protocol"].as_str().unwrap_or("udp").to_owned();
        println!(
            "{} {protocol} proxy {listen} -> {target} for {node}",
            clock()
        );
        if protocol == "tcp" {
            tokio::spawn(tcp_proxy(world.clone(), node.clone(), listen, target));
        } else {
            tokio::spawn(udp_proxy(world.clone(), node.clone(), listen, target));
        }
    }
    report(world).await;
}
