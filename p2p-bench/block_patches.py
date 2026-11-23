"""The blocks variant: which blocks a node gives up on and why, for runs in which blocks stay with too
few validators. Adds debug lines with the block's ID, read with BENCH_RUST_LOG naming tenrai_node::p2p
and tenrai_node::chain=debug. Changes no behaviour; the node repository has none of this.

  BLOCKS fetch queue dropped     the fetch queue was full and gave up its oldest block
  BLOCKS announcement dropped    a mined block fell out of the queue of blocks to announce
  BLOCKS live work refused       a received block found no room in the queue of blocks to check
  BLOCKS no provider left        a queued block has nobody on record to ask any more (once per block)
  BLOCKS pool room               the work pool filled up or has room again, which stops fetches
  BLOCKS left the pool           a block left the pool unregistered, and why
"""

from pathlib import Path

FETCH = Path("crates/node/src/p2p/swarm/relay/fetch.rs")
RELAY = Path("crates/node/src/p2p/swarm/relay.rs")
RELAY_LANE = Path("crates/node/src/p2p/swarm/relay_lane.rs")
CANDIDATES = Path("crates/node/src/chain/engine/candidates.rs")

PATCHES = [
    (FETCH, """                    self.queued.remove(&InventoryId::Work(evicted));
""", """                    self.queued.remove(&InventoryId::Work(evicted));
                    tracing::debug!(block = %evicted.to_hex(), "BLOCKS fetch queue dropped");
"""),
    (FETCH, """            let Some(peer) = self.inventory.next_provider(object, now) else {
                continue;
            };
            match ready(peer, RelaySlot::Work) {
""", """            let Some(peer) = self.inventory.next_provider(object, now) else {
                if !self.inventory.has_provider(object, now) {
                    static TOLD: std::sync::Mutex<Option<HashSet<Hash384>>> = std::sync::Mutex::new(None);
                    let mut told = TOLD.lock().unwrap();
                    if told.get_or_insert_with(HashSet::new).insert(id) {
                        tracing::debug!(block = %id.to_hex(), "BLOCKS no provider left");
                    }
                }
                continue;
            };
            match ready(peer, RelaySlot::Work) {
"""),
    (RELAY, """    pub(super) fn set_pool_room(&mut self, room: bool) {
        self.pool_room = room;
""", """    pub(super) fn set_pool_room(&mut self, room: bool) {
        if self.pool_room != room {
            tracing::debug!(room, queued = self.work_queue.len(), "BLOCKS pool room");
        }
        self.pool_room = room;
"""),
    (RELAY, """    pub(super) fn queue_local_work(&mut self, id: Hash384) {
        queue_local(
            &mut self.local_work_announcements,
            &mut self.local_work_set,
            id,
            MAX_LOCAL_WORK_ANNOUNCEMENTS,
        );
""", """    pub(super) fn queue_local_work(&mut self, id: Hash384) {
        if queue_local(
            &mut self.local_work_announcements,
            &mut self.local_work_set,
            id,
            MAX_LOCAL_WORK_ANNOUNCEMENTS,
        ) {
            tracing::debug!("BLOCKS announcement dropped");
        }
"""),
    (RELAY_LANE, """        Enqueued::Refused => (peer, expected_id),
    };
    state.relay.fetch_deferred(
""", """        Enqueued::Refused => (peer, expected_id),
    };
    tracing::debug!(block = %expected_id.to_hex(), "BLOCKS live work refused");
    state.relay.fetch_deferred(
"""),
    (CANDIDATES, """        expired.extend(gave_way);
        if !expired.is_empty() {
""", """        expired.extend(gave_way);
        for (index, work) in expired.iter().enumerate() {
            let why = if index < past_window {
                "past its window"
            } else if index < past_window + full_anchor {
                "full anchor"
            } else {
                "full pool"
            };
            tracing::debug!(block = %work.block_id.to_hex(), why, "BLOCKS left the pool");
        }
        if !expired.is_empty() {
"""),
]
