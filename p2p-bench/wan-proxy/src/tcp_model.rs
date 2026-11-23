//! A TCP sender per connection direction, since the proxy's local sockets never lose a packet. Cubic
//! shrinks its window on loss; BBR keeps its measured rate, so a loss only costs the retransmission.

use std::time::Duration;
use tokio::time::Instant;

pub const MSS: f64 = 1_400.0;
const INITIAL_WINDOW: f64 = 10.0;
const BETA: f64 = 0.7;
const CUBIC_C: f64 = 0.4;
const MAX_WINDOW: f64 = 16.0 * 1024.0 * 1024.0 / MSS;
const MIN_RTO: f64 = 0.3;

pub struct Sender {
    bbr: bool,
    window: f64,
    threshold: f64,
    window_max: f64,
    reduced_at: Option<Instant>,
    next_free: Instant,
    last_send: Instant,
    pub last_due: Instant,
}

impl Sender {
    pub fn new(now: Instant, bbr: bool) -> Self {
        Self {
            bbr,
            window: INITIAL_WINDOW,
            threshold: f64::INFINITY,
            window_max: 0.0,
            reduced_at: None,
            next_free: now,
            last_send: now,
            last_due: now,
        }
    }

    pub fn rto(rtt: f64) -> f64 {
        MIN_RTO.max(2.0 * rtt)
    }

    /// When a chunk leaves at the window's pace; under Cubic an idle sender starts small again.
    pub fn send(&mut self, now: Instant, bytes: usize, rtt: f64, lost: bool) -> Instant {
        if !self.bbr && now.saturating_duration_since(self.last_send).as_secs_f64() > Self::rto(rtt)
        {
            self.window = self.window.min(INITIAL_WINDOW);
        }
        let since = self
            .reduced_at
            .map(|at| now.saturating_duration_since(at).as_secs_f64());
        if lost && !self.bbr {
            if since.is_none_or(|since| since >= rtt) {
                self.window_max = self.window;
                self.window = (self.window * BETA).max(2.0);
                self.threshold = self.window;
                self.reduced_at = Some(now);
            }
        } else if self.bbr || self.window < self.threshold {
            self.window = (self.window + bytes as f64 / MSS).min(MAX_WINDOW);
        } else {
            let since = since.unwrap_or(0.0);
            let k = (self.window_max * (1.0 - BETA) / CUBIC_C).cbrt();
            let cubic = CUBIC_C * (since - k).powi(3) + self.window_max;
            let friendly = self.window_max * BETA + 3.0 * (1.0 - BETA) / (1.0 + BETA) * since / rtt;
            self.window = cubic.max(friendly).clamp(2.0, MAX_WINDOW);
        }
        let rate = self.window * MSS / rtt;
        let start = now.max(self.next_free);
        self.next_free = start + Duration::from_secs_f64(bytes as f64 / rate);
        self.last_send = now;
        start
    }
}
