"""The relay variant: how each block travels to the other validators and what holds it up.

It keeps the BLK lines of the prof variant (accepted, heard, asked, got, try, admitted, registered,
executed) and adds:

  BLK named     a received availability proposal names the block (first time per proposal)
  BLK wanted    the validator asks its peers for it because a proposal waits for it
  BLK partial   its compact form arrived without some of its transactions
  BLK txasked   those transactions were asked for
  BLK txgot     they arrived

and, every 10 s, PROF counts:

  relay.rtt.<request>.<time>  answers to each kind of relay request, by the time they took
  relay.answer.<how>          ok, busy, reject or failed
  relay.full.<what>           a peer's relay slots were all taken when this wanted one
  relay.open                  per dispatch and peer the open relay requests (bytes = sum, n = samples)
  rx.<class>, tx.<class>      frames and bytes of each message class

`py -3.14 relay_trace.py results/<run>` reads them.
"""

from pathlib import Path

import prof_patches
from prof_patches import P, after, before

AVAILABILITY = Path("crates/node/src/chain/finality/availability.rs")
RUNNER = Path("crates/node/src/p2p/swarm/runner.rs")
RELAY_LANE = Path("crates/node/src/p2p/swarm/relay_lane.rs")
DISPATCH = Path("crates/node/src/p2p/swarm/runner/dispatch.rs")
TRACKER = Path("crates/node/src/p2p/swarm/request_tracker.rs")
METRICS = Path("crates/node/src/p2p/metrics.rs")

PROBE_EXTRA = r'''
/// Bytes under a name and a label, such as the frames of each message class.
pub fn labelled_bytes(prefix: &'static str, label: &'static str, bytes: u64) {
    start();
    record((prefix, label, 0), Stat { count: 1, bytes, ..Stat::default() }, Instant::now());
}
'''

RTT_HELPERS = '''
type RelayRequestId = libp2p::request_response::OutboundRequestId;
use super::request_tracker::PendingKind;

static RELAY_SENT: std::sync::Mutex<
    Option<std::collections::HashMap<RelayRequestId, (std::time::Instant, &'static str)>>,
> = std::sync::Mutex::new(None);

fn relay_kind(kind: PendingKind) -> &'static str {
    match kind {
        PendingKind::CutInventory => "relay.rtt.cut_inventory",
        PendingKind::WorkInventory => "relay.rtt.work_inventory",
        PendingKind::TransactionInventory => "relay.rtt.transaction_inventory",
        PendingKind::LiveWorkFetch => "relay.rtt.compact_work",
        PendingKind::WorkTransactionsFetch => "relay.rtt.work_transactions",
        PendingKind::TransactionFetch => "relay.rtt.transactions",
        _ => "relay.rtt.other",
    }
}

fn relay_probe_sent(id: RelayRequestId, kind: PendingKind) {
    let mut sent = RELAY_SENT.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    sent.get_or_insert_with(std::collections::HashMap::new)
        .insert(id, (std::time::Instant::now(), relay_kind(kind)));
}

pub(super) fn relay_probe_answered(id: RelayRequestId, how: &'static str) {
    let taken = RELAY_SENT
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .as_mut()
        .and_then(|sent| sent.remove(&id));
    PROBE::count("relay.answer", how);
    let Some((at, kind)) = taken else {
        return;
    };
    let millis = at.elapsed().as_millis();
    let bucket = match millis {
        0..100 => "<100ms",
        100..250 => "<250ms",
        250..500 => "<500ms",
        500..1_000 => "<1s",
        1_000..2_000 => "<2s",
        2_000..4_000 => "<4s",
        _ => ">=4s",
    };
    PROBE::count(kind, if how == "failed" { "failed" } else { bucket });
    PROBE::labelled_bytes(kind, "ms", millis as u64);
}
'''.replace("PROBE", P)


def full_slot(label):
    return (f"            if !state.requests.has_relay_slot(RelaySlot::Announcement, peer) {{\n"
            f"                {P}::count(\"relay.full\", \"{label}\");\n"
            "                state.metrics.record_suppressed();\n                retry = true;\n"
            "                continue;\n            }\n")


SUPPRESSED = ("            if !state.requests.has_relay_slot(RelaySlot::Announcement, peer) {\n"
              "                state.metrics.record_suppressed();\n                retry = true;\n"
              "                continue;\n            }\n")

FETCH_DEFERRED = ("    } else if free {\n        FetchReadiness::Ready\n    } else {\n"
                  "        FetchReadiness::Deferred\n    }\n")


def announcements(text):
    """The work and the transaction announcements each count their own full slots."""
    assert text.count(SUPPRESSED) == 2, text.count(SUPPRESSED)
    text = text.replace(SUPPRESSED, full_slot("work_announce"), 1)
    return text.replace(SUPPRESSED, full_slot("transaction_announce"), 1)


def send_request(text):
    old = """    let id = swarm
        .behaviour_mut()
        .relay
        .send_request(&peer, WireRequest(message));
    assert!(state.requests.insert(ProtocolLane::Relay, id, pending));
    Ok(())
}
"""
    new = """    let kind = pending.kind;
    let id = swarm
        .behaviour_mut()
        .relay
        .send_request(&peer, WireRequest(message));
    relay_probe_sent(id, kind);
    assert!(state.requests.insert(ProtocolLane::Relay, id, pending));
    Ok(())
}
""" + RTT_HELPERS
    assert text.count(old) == 1
    return text.replace(old, new)


PATCHES = [(prof_patches.PROBE, None, prof_patches.PROBE_SOURCE + PROBE_EXTRA)] + prof_patches.PATCHES[1:3] + [
    patch for patch in prof_patches.PATCHES[93:99]] + [
    # What a proposal names, the first time this node sees it.
    after(AVAILABILITY, '                    tracing::debug!(%error, "availability proposal waits for local data");\n',
          ["                    for descriptor in proposal.outcome().descriptors() {",
           f'                        {P}::block("named", descriptor.block_id().as_bytes());',
           "                    }"]),
    after(AVAILABILITY, '                    "availability proposal accepted"\n                );\n',
          ["                if self.deferred_proposal.is_none() {",
           "                    for descriptor in proposal.outcome().descriptors() {",
           f'                        {P}::block("named", descriptor.block_id().as_bytes());',
           "                    }",
           "                }"]),
    after(DISPATCH, "    for id in ids.into_iter().take(MAX_WANTED_WORK) {\n",
          [f'        {P}::block("wanted", id.as_bytes());']),
    (DISPATCH, announcements, None),
    after(DISPATCH, "    super::request_work_transactions(swarm, state);\n", [
        "    for peer in authenticated_peers(&state.sessions) {",
        f'        {P}::bytes("relay.open", state.requests.pending_relay(peer) as u64);',
        "    }"]),
    after(TRACKER, """    pub(super) fn can_insert(&self, lane: ProtocolLane, peer: PeerId) -> bool {
        self.capacity.can_reserve(lane.default_capacity(), peer)
    }
""", ["", "    pub(super) fn pending_relay(&self, peer: PeerId) -> usize {",
      "        [PendingCapacityClass::Announcements, PendingCapacityClass::WorkFetches,",
      "         PendingCapacityClass::TransactionFetches, PendingCapacityClass::RelayOther]",
      "            .into_iter()",
      "            .map(|class| self.capacity.pending(class, peer))",
      "            .sum()", "    }"]),
    # Fetches whose provider had no free slot, and batches waiting for admission.
    (DISPATCH, FETCH_DEFERRED,
     "    } else if free {\n        FetchReadiness::Ready\n    } else {\n"
     f'        {P}::count("relay.full", if slot == RelaySlot::Work {{ "work_fetch" }} else {{ "transaction_fetch" }});\n'
     "        FetchReadiness::Deferred\n    }\n"),
    after(RUNNER, "    while !state.waiting_transactions.is_empty() {\n",
          [f'        {P}::bytes("relay.waiting_batches", state.waiting_transactions.len() as u64);']),
    after(RUNNER, "                crate::p2p::host::LiveWorkAdmission::Incomplete(partial) => {\n"
                  "                    let now = std::time::Instant::now();\n",
          [f'                    {P}::block("partial", partial.block_id().as_bytes());']),
    (RUNNER, "            if count == 0 || !state.requests.can_fetch_work_transactions(peer, count) {\n",
     "            if count != 0 && !state.requests.can_fetch_work_transactions(peer, count) {\n"
     f'                {P}::count("relay.full", "work_transactions");\n'
     "            }\n"
     "            if count == 0 || !state.requests.can_fetch_work_transactions(peer, count) {\n"),
    after(RUNNER, "            let partials = state.partial_work.take(peer, count);\n",
          ["            for partial in &partials {",
           f'                {P}::block("txasked", partial.block_id().as_bytes());',
           "            }"]),
    (RUNNER, send_request, None),
    after(RELAY_LANE, "                    for work in works {\n"
                      "                        let expected_id = work.header.block_id();\n",
          [f'                        {P}::block("txgot", expected_id.as_bytes());']),
    after(RELAY_LANE, "    response: PeerMessage,\n) {\n", [
        "    super::runner::relay_probe_answered(request_id, match &response {",
        '        PeerMessage::Reject(REJECT_BUSY) => "busy",',
        '        PeerMessage::Reject(_) => "reject",',
        '        _ => "ok",',
        "    });"]),
    after(RELAY_LANE, "            request_id,\n            error,\n        } => {\n",
          ['            super::runner::relay_probe_answered(request_id, "failed");']),
    after(METRICS, "    pub(super) fn record_rx(&self, class: MessageClass, bytes: usize) {\n",
          [f'        {P}::labelled_bytes("rx", class.name(), bytes as u64);']),
    after(METRICS, "    pub(super) fn record_tx(&self, class: MessageClass, bytes: usize) {\n",
          [f'        {P}::labelled_bytes("tx", class.name(), bytes as u64);']),
]
