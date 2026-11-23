"""The decentralized finality test with the mainnet split 400,000 / 300,000 / 300,000 TRAI, as exact edits of the node
tree: py -3.14 split_test_patches.py check|apply TREE

check: every old text occurs exactly once; apply: writes the edits (LF)."""

import sys
from pathlib import Path

FILE = Path("crates/node/src/p2p/validator_overlay/tests/decentralized_finality.rs")

TEST = '''
/// The three genesis validators of mainnet with its split of the bonded million: 400,000, 300,000
/// and 300,000 TRAI. The first is in every quorum. Without the second or without the third the
/// other two finalize, and the one away catches up from its own store; without the first the two
/// others hold less than the quorum and finalize nothing new until it is back.
#[test]
#[ignore = "acceptance: three independent validators with the mainnet split over authenticated TCP; a few minutes"]
fn three_validators_with_the_mainnet_split_wait_only_for_the_anchor() {
    let _exclusive = crate::p2p::swarm_socket_tests::exclusive();
    crate::p2p::swarm_socket_tests::trace();
    let runtime = crate::p2p::swarm_socket_tests::runtime();
    runtime.block_on(async {
        let fixture = Fixture::with_weights(&[4, 3, 3]);
        let committee = &fixture.committee;
        let weights = committee
            .active()
            .iter()
            .map(|member| member.weight)
            .collect::<Vec<_>>();
        let quorum = committee.quorum_weight();
        assert_eq!(weights[0] * 10, committee.total_eligible_weight() * 4);
        assert!(weights[0] + weights[1] >= quorum && weights[0] + weights[2] >= quorum);
        assert!(weights[1] + weights[2] < quorum);
        let mut nodes = (0..3)
            .map(|index| Some(fixture.start(index)))
            .collect::<Vec<_>>();
        let mut evidence = vec![checkpoint(&nodes, &[0, 1, 2], 4, committee).await];

        for away in [2, 1] {
            nodes[away].take().unwrap().stop().await;
            let present = (0..3).filter(|index| *index != away).collect::<Vec<_>>();
            let next = max_height(&nodes) + 4;
            evidence.push(checkpoint(&nodes, &present, next, committee).await);
            nodes[away] = Some(fixture.start(away));
            let next = max_height(&nodes) + 4;
            evidence.push(checkpoint(&nodes, &[0, 1, 2], next, committee).await);
        }

        let last_anchor_height = nodes[0].take().unwrap().stop().await;
        // A cut the anchor voted for before it stopped may still become final.
        sleep(Duration::from_secs(5)).await;
        let stalled_height = max_height(&nodes);
        let last_in_flight_height = stalled_height.max(last_anchor_height + 1);
        for _ in 0..100 {
            sleep(Duration::from_millis(100)).await;
            for running in nodes.iter().flatten() {
                running.healthy();
            }
            assert!(
                max_height(&nodes) <= last_in_flight_height,
                "the two smaller validators finalized without the anchor"
            );
        }
        println!(
            "three-validators without the anchor: before={stalled_height}, after={}, last possible in-flight height={last_in_flight_height}; 10 second guard",
            max_height(&nodes)
        );
        nodes[0] = Some(fixture.start(0));
        evidence.push(checkpoint(&nodes, &[0, 1, 2], last_in_flight_height + 4, committee).await);

        let shown = weights.iter().map(u128::to_string).collect::<Vec<_>>();
        println!(
            "three-validators PASS {}",
            json!({ "weights": shown, "quorum": quorum.to_string(), "phases": evidence, "data_dirs": fixture.paths })
        );
        for running in nodes.iter_mut().filter_map(Option::take) {
            running.stop().await;
        }
    });
}
'''

PATCHES = [
    ("""    fn with_members(members: usize) -> Self {
        let paths = (0..members)""", """    fn with_members(members: usize) -> Self {
        Self::with_weights(&vec![1; members])
    }

    /// Members bonded in these proportions of the manifest's bonded allocation.
    fn with_weights(weights: &[u128]) -> Self {
        let members = weights.len();
        let paths = (0..members)"""),
    ("""        // What does not divide stays with the treasury.
        let each = bonded_atoms / members as u128;
        if let Some(treasury) = manifest
            .allocations
            .iter_mut()
            .max_by_key(|entry| entry.amount)
        {
            treasury.amount += bonded_atoms - each * members as u128;
        }""", """        // What does not divide stays with the treasury.
        let part = bonded_atoms / weights.iter().sum::<u128>();
        let bonds = weights
            .iter()
            .map(|weight| part * weight)
            .collect::<Vec<_>>();
        if let Some(treasury) = manifest
            .allocations
            .iter_mut()
            .max_by_key(|entry| entry.amount)
        {
            treasury.amount += bonded_atoms - bonds.iter().sum::<u128>();
        }"""),
    ("""            manifest.allocations.push(ManifestAllocation {
                address: address.clone(),
                amount: each,
            });""", """            manifest.allocations.push(ManifestAllocation {
                address: address.clone(),
                amount: bonds[index],
            });"""),
    ("""        assert_eq!(committee.active().len(), members);
        assert_eq!(
            committee.active()[0].weight * members as u128,
            committee.total_eligible_weight()
        );""", """        assert_eq!(committee.active().len(), members);
        for (member, weight) in committee.active().iter().zip(weights) {
            assert_eq!(
                member.weight * weights.iter().sum::<u128>(),
                committee.total_eligible_weight() * weight
            );
        }"""),
    ("""        cut.certificate.verify(committee).unwrap();
        cut.availability_receipt.verify(committee).unwrap();
        assert!(cut.certificate.signatures().len() >= 3);""", """        cut.certificate.verify(committee).unwrap();
        cut.availability_receipt.verify(committee).unwrap();
        let signed = cut
            .certificate
            .signatures()
            .iter()
            .map(|signature| committee.validator(signature.signer_index).unwrap().weight)
            .sum::<u128>();
        assert!(signed >= committee.quorum_weight());"""),
    ("""
/// A new validator's node starts from a package like any new node, with a key the chain has never""", TEST + """
/// A new validator's node starts from a package like any new node, with a key the chain has never"""),
]


def main():
    action, tree = sys.argv[1], Path(sys.argv[2])
    path = tree / FILE
    text = path.read_text(encoding="utf-8")
    for old, new in PATCHES:
        if text.count(old) != 1:
            raise SystemExit(f"{FILE}: an old text occurs {text.count(old)} times: {old[:70]!r}")
        text = text.replace(old, new)
    if action == "apply":
        path.write_text(text, encoding="utf-8", newline="\n")
    print(f"{action}: {len(PATCHES)} edits {'written' if action == 'apply' else 'apply once each'} to {FILE}")


if __name__ == "__main__":
    main()
