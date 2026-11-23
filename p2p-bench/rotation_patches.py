"""The rotation-b variant: the quorum base of a committee bounded by the committee's own stake over the
share it has to hold (option B of the committee rotation analysis), with the tests that belong to it.
A test build only; the node's tree stays as it is."""

from pathlib import Path

SNAPSHOT_COMMITTEE = Path("crates/traidag/src/pos/snapshot/committee.rs")
STAKE_WEIGHTS = Path("crates/node/src/p2p/tests/stake_weights.rs")

BOUNDED_BLOCK_OLD = """    active.truncate(
        usize::try_from(committee_limits.max_active_validators)
            .map_err(|_| EpochSnapshotError::Limits)?,
    );
    ActiveCommittee::new(
        chain_id,
        protocol_version,
        total_eligible_weight,
        active,
        committee_limits,
    )
    .map_err(EpochSnapshotError::from)
"""

BOUNDED_BLOCK_NEW = """    active.truncate(
        usize::try_from(committee_limits.max_active_validators)
            .map_err(|_| EpochSnapshotError::Limits)?,
    );
    let committee_weight = active.iter().try_fold(0_u128, |total, member| {
        total
            .checked_add(member.weight)
            .ok_or(EpochSnapshotError::WeightOverflow)
    })?;
    ActiveCommittee::new(
        chain_id,
        protocol_version,
        quorum_base(
            total_eligible_weight,
            committee_weight,
            committee_limits.required_coverage_bps,
        ),
        active,
        committee_limits,
    )
    .map_err(EpochSnapshotError::from)
"""

QUORUM_BASE = '''
/// The stake a committee's quorum is taken of: all eligible stake, but no more than the committee's own
/// stake over the share of the base it has to hold. A committee then always covers its base, and stake
/// outside it raises the quorum no further than to five sixths of the committee's stake.
pub(in crate::pos) fn quorum_base(
    total_eligible_weight: u128,
    committee_weight: u128,
    coverage_bps: u16,
) -> u128 {
    committee_weight
        .checked_mul(10_000)
        .map(|scaled| scaled / u128::from(coverage_bps))
        .map_or(total_eligible_weight, |bound| {
            bound.min(total_eligible_weight)
        })
}

#[cfg(test)]
mod bounded_base_tests {
    use super::quorum_base;
    use crate::cut::{quorum_weight, required_active_coverage};

    const COVERAGE_BPS: u16 = 8_000;

    struct Mix(u64);

    impl Mix {
        fn next(&mut self) -> u64 {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            self.0
        }
    }

    /// What the bound must give for one distribution: a base of at least the committee's stake and at
    /// most all eligible stake, covered by the committee, with a quorum the committee can reach and two
    /// quorums that share more than a third of the committee's stake.
    fn holds(committee_weight: u128, outside: u128) {
        let total = committee_weight + outside;
        let base = quorum_base(total, committee_weight, COVERAGE_BPS);
        let quorum = quorum_weight(base).unwrap();
        assert!(base >= committee_weight && base <= total, "base {base} of {committee_weight} and {total}");
        assert!(
            required_active_coverage(base, COVERAGE_BPS).unwrap() <= committee_weight,
            "the committee covers its base {base}"
        );
        assert!(quorum <= committee_weight, "quorum {quorum} of {committee_weight}");
        // Two quorums overlap in at least 2q - S; it must exceed a third of S.
        assert!(
            3 * (2 * quorum - committee_weight) > committee_weight,
            "quorum {quorum} of {committee_weight} overlaps too little"
        );
    }

    #[test]
    fn the_bound_holds_for_random_and_extreme_distributions() {
        let mut mix = Mix(0x9e37_79b9_7f4a_7c15);
        for _ in 0..200_000 {
            let members = 1 + mix.next() % 64;
            let committee: u128 = (0..members).map(|_| 1 + u128::from(mix.next() % 1_000_000)).sum();
            let outside: u128 = (0..mix.next() % 10_000).map(|_| u128::from(mix.next() % 1_000_000)).sum();
            holds(committee, outside);
        }
        for committee in [1, 2, 3, 4, 5, 6, 7, 64, 1 << 60, u128::MAX / 20_000] {
            for outside in [0, 1, committee / 4, committee / 4 + 1, committee, 100 * committee] {
                holds(committee, outside);
            }
        }
    }

    /// Every pair of signer sets of a small committee that each reach the quorum share more than a
    /// third of its stake, for every split of the stake and every amount outside.
    #[test]
    fn no_two_quorums_of_a_small_committee_share_a_third_or_less() {
        let mut mix = Mix(0x2545_f491_4f6c_dd1d);
        for _ in 0..400 {
            let members = 1 + (mix.next() % 8) as usize;
            let weights: Vec<u128> = (0..members).map(|_| 1 + u128::from(mix.next() % 50)).collect();
            let committee: u128 = weights.iter().sum();
            for outside in [0, committee / 8, committee / 4, committee, 10 * committee] {
                let base = quorum_base(committee + outside, committee, COVERAGE_BPS);
                let quorum = quorum_weight(base).unwrap();
                let weight = |set: usize| -> u128 {
                    (0..members).filter(|bit| set & (1 << bit) != 0).map(|bit| weights[bit]).sum()
                };
                for first in 0..1_usize << members {
                    if weight(first) < quorum {
                        continue;
                    }
                    for second in 0..1_usize << members {
                        if weight(second) >= quorum {
                            assert!(3 * weight(first & second) > committee, "{weights:?} {outside}");
                        }
                    }
                }
            }
        }
    }
}
'''

BOUNDED_TEST = '''use super::*;
use tenrai_traidag::{StakePositionKind, StakePositionLifecycle};

/// With the quorum base bounded by the committee's stake over the share it has to hold, 81 live
/// validators of equal stake rotate like 64 do: the newcomer that holds more than the last seat sits
/// from the next epoch, the base is the committee's stake over four fifths, and the new committee
/// carries the chain.
#[test]
#[ignore = "acceptance: two short days with 64 signers; minutes"]
fn eighty_one_validators_of_equal_stake_rotate_with_a_bounded_quorum_base() {
    let mut joined = with_validators_joining("stake-bounded-base", 17);
    let in_office = joined.node.successor_committee().unwrap();
    let last_joined = joined.ids.len() - 1;
    let newcomer = joined.ids[SEATS as usize];

    joined.advance_to(DAY_CUTS + 8);
    joined.heartbeats(None, 1_000);
    joined.delegate(last_joined, newcomer);
    let committee = loop {
        let next = joined.node.successor_committee().unwrap();
        if next.validator_set_id() != in_office.validator_set_id()
            && next.active().iter().any(|member| member.validator_id == newcomer)
        {
            break next;
        }
        let height = joined.node.chain().finalized_head().unwrap().height();
        assert!(height < 3 * DAY_CUTS, "rotation never went on");
        joined.node.advance_seated_for_test().unwrap();
    };
    let height = joined.node.chain().finalized_head().unwrap().height();
    let committee_weight: u128 = committee.active().iter().map(|member| member.weight).sum();
    println!(
        "from cut {height}: {} members, base {}, quorum {}, first {}",
        committee.active().len(),
        committee.total_eligible_weight(),
        committee.quorum_weight(),
        committee.active()[0].weight
    );
    assert_eq!(committee.active()[0].validator_id, newcomer);
    assert_eq!(committee.active().len(), SEATS as usize);
    assert_eq!(committee.total_eligible_weight(), committee_weight * 10_000 / 8_000);

    let Joined {
        path,
        fixture,
        keys,
        node,
        ..
    } = joined;
    drop(node);
    let chain = ChainStore::open(&path, &fixture.manifest).unwrap();
    let seats = seats_in(&committee, &keys, SEATS);
    let mut node = CutEngine::new(chain, seats).unwrap();
    for _ in 0..8 {
        node.advance_seated_for_test().unwrap();
    }
    drop(node);
    let _ = std::fs::remove_dir_all(&path);
}

/// With the bounded base the committee of 81 equal validators rotates, and an exit from it is let go:
/// a member that asks to leave keeps its seat and its stake bound while the committee that counts it
/// certifies, the next committee forms without it, and once that one certifies the self-bond unbonds.
/// Today the same exit stays requested for as long as the committee in office stays.
#[test]
#[ignore = "acceptance: three short days with 64 signers; minutes"]
fn an_exit_from_a_committee_of_eighty_one_is_let_go_at_the_next_committee() {
    let mut joined = with_validators_joining("stake-bounded-exit", 17);
    let leaving = 5;
    let validator = joined.ids[leaving];
    joined.advance_to(DAY_CUTS + 8);
    joined.heartbeats(None, 1_000);

    let profile = joined.node.chain().rules().genesis();
    let from = joined.node.chain().finalized_head().unwrap().height() + 1;
    let nonce = joined
        .node
        .chain()
        .validator_heartbeat_snapshot(
            validator,
            &joined.keys[leaving].public_key().to_bytes(),
            &[],
        )
        .unwrap()
        .validator()
        .owner_nonce();
    let (spender, coin, amount) = joined.coins[leaving];
    let exit = by_owner(
        profile,
        from,
        &joined.fixture.spenders[spender].seed,
        (coin, amount),
        0,
        (&joined.keys[leaving], 0),
        NativeOperation::Exit {
            validator_id: validator,
            expected_owner_nonce: nonce,
        },
    );
    mine_body(&mut joined.node, from, vec![exit], 4_000);
    for _ in 0..3 {
        joined.node.advance_seated_for_test().unwrap();
    }
    let self_bond = positions(joined.node.chain())
        .into_iter()
        .find(|position| {
            position.validator_id() == validator && position.kind() == StakePositionKind::SelfBond
        })
        .expect("the self-bond")
        .position_id();
    let lifecycle = |node: &CutEngine| {
        positions(node.chain())
            .into_iter()
            .find(|position| position.position_id() == self_bond)
            .map(|position| position.lifecycle())
    };

    let in_office = joined.node.successor_committee().unwrap();
    let mut changed_at = None;
    let released_at = loop {
        let height = joined.node.chain().finalized_head().unwrap().height();
        let next = joined.node.successor_committee().unwrap();
        if next.validator_set_id() == in_office.validator_set_id() {
            assert!(
                next.active()
                    .iter()
                    .any(|member| member.validator_id == validator),
                "it sits while the committee that counts it certifies"
            );
            assert!(
                matches!(
                    lifecycle(&joined.node),
                    Some(StakePositionLifecycle::ExitRequested { .. })
                ),
                "its stake is bound at cut {height}"
            );
        } else {
            if changed_at.is_none() {
                changed_at = Some(height);
            }
            assert!(
                next.active()
                    .iter()
                    .all(|member| member.validator_id != validator),
                "the next committee forms without it"
            );
        }
        if let Some(StakePositionLifecycle::Unbonding(_)) = lifecycle(&joined.node) {
            break height;
        }
        assert!(height < 3 * DAY_CUTS, "the exit was never let go");
        joined.node.advance_seated_for_test().unwrap();
        let next = joined.node.successor_committee().unwrap();
        joined
            .node
            .replace_seats_for_test(seats_in(&next, &joined.keys, SEATS));
    };
    println!(
        "exit asked at cut {from}, the next committee from cut {changed_at:?}, let go at cut {released_at}: {:?}",
        lifecycle(&joined.node)
    );
    assert!(changed_at.is_some_and(|changed| changed <= released_at));
    let Joined { path, node, .. } = joined;
    drop(node);
    let _ = std::fs::remove_dir_all(&path);
}
'''

PATCHES = [
    (SNAPSHOT_COMMITTEE, BOUNDED_BLOCK_OLD, BOUNDED_BLOCK_NEW),
    (SNAPSHOT_COMMITTEE, lambda text: text + QUORUM_BASE, None),
    (STAKE_WEIGHTS, '''#[path = "stake_weights/restarts.rs"]
''', '''#[path = "stake_weights/bounded_base.rs"]
mod bounded_base;
#[path = "stake_weights/restarts.rs"]
'''),
    (Path("crates/node/src/p2p/tests/stake_weights/bounded_base.rs"), None, BOUNDED_TEST),
]
