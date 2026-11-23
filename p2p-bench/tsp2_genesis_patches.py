"""The tsp2-genesis variant: mainnet runs private transfers from its genesis instead of from an upgrade, with the tests
that belong to it. Testnet stays apart: it keeps the other mainnet values and runs no private transfers until that is
decided for testnet on its own. A development chain may name its genesis parameter set in its manifest, so the tests
run the same path on the test dataset; with TENRAI_PRIVATE_FROM_GENESIS set, the shared private test fixtures start
private transfers from genesis instead of by their upgrade. A test build; the node's tree stays as it is.

What changes besides the parameter sets: the genesis guard lets a public network run private transfers as its own
genesis set does, and the upgrade check refuses an upgrade that would start them on a public network, where it refused
every upgrade with private rules, which would have stopped every later mainnet upgrade.
"""

from pathlib import Path

CONSENSUS = Path("crates/primitives/src/params/consensus.rs")
CANONICAL = Path("crates/traidag/src/consensus_profile/canonical.rs")
PROFILE_ERRORS = Path("crates/traidag/src/consensus_profile/mod.rs")
SCHEDULE = Path("crates/traidag/src/consensus_profile/schedule.rs")
SCHEDULE_TESTS = Path("crates/traidag/src/consensus_profile/schedule_tests.rs")
MANIFEST = Path("crates/traidag/src/genesis/manifest.rs")
FLOWS = Path("crates/node/src/chain/tests/private/flows.rs")
PRIVATE_TESTS = Path("crates/node/src/chain/tests/private.rs")
PRIVATE_VALIDATORS = Path("crates/node/src/chain/tests/private_validators.rs")
PRIVATE_COPIES = Path("crates/node/src/chain/tests/private_validators/copies.rs")
PRIVATE_RELAY = Path("crates/node/src/p2p/tests/private_relay.rs")

# Every fixture below set the same upgrade; each now asks the one switch.
FIXTURE_UPGRADE = """    fixture.manifest.upgrades = vec![ManifestUpgrade {
        height: ACTIVATION,
        protocol_version: tenrai_traidag::GENESIS_PROTOCOL_VERSION + 1,
        params: "development-private".into(),
    }];
"""
FIXTURE_SWITCH = """    crate::chain::private_tests::start_private(&mut fixture.manifest);
"""

PATCHES = [
    (CONSENSUS, """    /// Run private transfers. Only an upgrade on a development chain can name such a set.
    pub private_transfers: bool,""", """    /// Run private transfers. A public network runs them from its genesis set; a development chain
    /// from an upgrade or from the genesis set its manifest names.
    pub private_transfers: bool,"""),
    (CONSENSUS, """/// 50 blocks/s and 2.5 s cuts: 125 blocks a cut, 500 TPS, 34,560 cuts a day; peaks up to 100 blocks/s.
pub const MAINNET: ConsensusParams = ConsensusParams {
    work_blocks_per_second: 50,
    cut_interval_millis: 2_500,
    transactions_per_work_block: 10,
    registration_block_cap: 250,
    execution_block_cap: 256,
    max_active_validators: 64,
    full_tenq_dataset: true,
    private_transfers: false,
};""", """/// 50 blocks/s and 2.5 s cuts: 125 blocks a cut, 500 TPS, 34,560 cuts a day; peaks up to 100 blocks/s.
/// Private transfers from genesis on.
pub const MAINNET: ConsensusParams = ConsensusParams {
    work_blocks_per_second: 50,
    cut_interval_millis: 2_500,
    transactions_per_work_block: 10,
    registration_block_cap: 250,
    execution_block_cap: 256,
    max_active_validators: 64,
    full_tenq_dataset: true,
    private_transfers: true,
};"""),
    (CONSENSUS, """/// Runs the mainnet values.
pub const TESTNET: ConsensusParams = MAINNET;""", """/// Runs the mainnet values but private transfers, which are decided for testnet on its own.
pub const TESTNET: ConsensusParams = ConsensusParams {
    private_transfers: false,
    ..MAINNET
};"""),
    (CONSENSUS, """/// Mainnet timing on the test dataset. Only development may run another committee size.
pub const DEVELOPMENT: ConsensusParams = ConsensusParams {
    full_tenq_dataset: false,
    ..MAINNET
};""", """/// Mainnet timing on the test dataset. Only development may run another committee size. Its private
/// transfers start by an upgrade or with the genesis set its manifest names.
pub const DEVELOPMENT: ConsensusParams = ConsensusParams {
    full_tenq_dataset: false,
    private_transfers: false,
    ..MAINNET
};"""),

    (CANONICAL, """        if params.private_transfers && network != Network::Development {
            return Err(ConsensusProfileError::Tsp2ActivationUnavailable);
        }""", """        // A public network runs private transfers as its genesis set does: they cannot start later.
        if params.private_transfers
            && network != Network::Development
            && !network.params().private_transfers
        {
            return Err(ConsensusProfileError::Tsp2ActivationUnavailable);
        }"""),
    (CANONICAL, """    /// Public networks pin their committee, so only development may differ.
    pub fn with_committee(
        network: Network,
        identity: NetworkIdentity,
        committee: CommitteeLimits,
    ) -> Result<Self, ConsensusProfileError> {
        Self::rule_set(
            network,
            identity,
            network.params(),
            network.rule_set_parameters(),
            GENESIS_PROTOCOL_VERSION,
            committee,
        )
    }""", """    /// Public networks pin their committee, so only development may differ.
    pub fn with_committee(
        network: Network,
        identity: NetworkIdentity,
        committee: CommitteeLimits,
    ) -> Result<Self, ConsensusProfileError> {
        Self::genesis_with(network, identity, committee, network.params())
    }

    /// The genesis rules of `network` under `params`: its own set, or one a development chain's
    /// manifest names.
    pub fn genesis_with(
        network: Network,
        identity: NetworkIdentity,
        committee: CommitteeLimits,
        params: ConsensusParams,
    ) -> Result<Self, ConsensusProfileError> {
        Self::rule_set(
            network,
            identity,
            params,
            network.rule_set_parameters(),
            GENESIS_PROTOCOL_VERSION,
            committee,
        )
    }"""),
    (CANONICAL, """    pub fn with_committee_and_day_for_test(
        network: Network,
        identity: NetworkIdentity,
        committee: CommitteeLimits,
        cuts_per_day: u64,
    ) -> Result<Self, ConsensusProfileError> {
        let params = network.params();
        Self::assemble(""", """    pub fn with_committee_and_day_for_test(
        network: Network,
        identity: NetworkIdentity,
        committee: CommitteeLimits,
        cuts_per_day: u64,
    ) -> Result<Self, ConsensusProfileError> {
        Self::genesis_with_day_for_test(
            network,
            identity,
            committee,
            network.params(),
            cuts_per_day,
        )
    }

    /// Test chains only: [`Self::genesis_with`] on days of `cuts_per_day` cuts.
    #[cfg(any(test, feature = "test-support"))]
    pub fn genesis_with_day_for_test(
        network: Network,
        identity: NetworkIdentity,
        committee: CommitteeLimits,
        params: ConsensusParams,
        cuts_per_day: u64,
    ) -> Result<Self, ConsensusProfileError> {
        Self::assemble("""),
    (PROFILE_ERRORS, """    #[error("private transfers can only start on a development chain")]
    Tsp2ActivationUnavailable,""", """    #[error("a public network runs private transfers only as its genesis set does")]
    Tsp2ActivationUnavailable,"""),

    (SCHEDULE, "use tenrai_primitives::{Hash384, params::consensus::ScheduledUpgrade, sha384_domain_hash};",
     """use tenrai_primitives::{
    Hash384,
    params::consensus::{ConsensusParams, ScheduledUpgrade},
    sha384_domain_hash,
};"""),
    (SCHEDULE, """    #[error(
        "TSP-2 activation is unavailable until its verifier and state transition are integrated"
    )]
    Tsp2ActivationUnavailable,
    #[error("only development chains take upgrades from their genesis manifest")]
    ManifestUpgrades,""", """    #[error(
        "private transfers start on a public network only with its genesis, and stay as they are"
    )]
    Tsp2ActivationUnavailable,
    #[error("only development chains take upgrades or a genesis parameter set from their manifest")]
    ManifestUpgrades,"""),
    (SCHEDULE, """    pub fn for_network_with_day_for_test(
        network: Network,
        identity: super::NetworkIdentity,
        committee: CommitteeLimits,
        cuts_per_day: u64,
        manifest_upgrades: &[ScheduledUpgrade],
    ) -> Result<Self, RuleScheduleError> {
        if network != Network::Development && !manifest_upgrades.is_empty() {
            return Err(RuleScheduleError::ManifestUpgrades);
        }""", """    pub fn for_network_with_day_for_test(
        network: Network,
        identity: super::NetworkIdentity,
        committee: CommitteeLimits,
        genesis_params: ConsensusParams,
        cuts_per_day: u64,
        manifest_upgrades: &[ScheduledUpgrade],
    ) -> Result<Self, RuleScheduleError> {
        if network != Network::Development
            && (!manifest_upgrades.is_empty() || genesis_params != network.params())
        {
            return Err(RuleScheduleError::ManifestUpgrades);
        }"""),
    (SCHEDULE, """            ConsensusProfile::with_committee_and_day_for_test(
                network,
                identity,
                committee,
                cuts_per_day,
            )?,""", """            ConsensusProfile::genesis_with_day_for_test(
                network,
                identity,
                committee,
                genesis_params,
                cuts_per_day,
            )?,"""),
    (SCHEDULE, """    pub fn for_network(
        network: Network,
        identity: super::NetworkIdentity,
        committee: CommitteeLimits,
        manifest_upgrades: &[ScheduledUpgrade],
    ) -> Result<Self, RuleScheduleError> {
        if network != Network::Development && !manifest_upgrades.is_empty() {
            return Err(RuleScheduleError::ManifestUpgrades);
        }
        let genesis = ConsensusProfile::with_committee(network, identity, committee)?;""", """    pub fn for_network(
        network: Network,
        identity: super::NetworkIdentity,
        committee: CommitteeLimits,
        manifest_upgrades: &[ScheduledUpgrade],
    ) -> Result<Self, RuleScheduleError> {
        Self::for_network_from(
            network,
            identity,
            committee,
            network.params(),
            manifest_upgrades,
        )
    }

    /// [`Self::for_network`] with the genesis under `genesis_params`, which only a development chain
    /// may choose.
    pub fn for_network_from(
        network: Network,
        identity: super::NetworkIdentity,
        committee: CommitteeLimits,
        genesis_params: ConsensusParams,
        manifest_upgrades: &[ScheduledUpgrade],
    ) -> Result<Self, RuleScheduleError> {
        if network != Network::Development
            && (!manifest_upgrades.is_empty() || genesis_params != network.params())
        {
            return Err(RuleScheduleError::ManifestUpgrades);
        }
        let genesis = ConsensusProfile::genesis_with(network, identity, committee, genesis_params)?;"""),
    (SCHEDULE, """/// Private transfers start only on a development chain, by an upgrade after genesis, and stay.
fn validate_tsp2_transition(
    before: ConsensusProfile,
    after: ConsensusProfile,
) -> Result<(), RuleScheduleError> {
    let (old, new) = (before.tsp2_parameters(), after.tsp2_parameters());
    if new.private_profile().is_some() && after.execution_rules().network() != Network::Development
    {
        return Err(RuleScheduleError::Tsp2ActivationUnavailable);
    }""", """/// Private transfers start on a public network only with its genesis, on a development chain also by
/// an upgrade, and once on they stay as they are.
fn validate_tsp2_transition(
    before: ConsensusProfile,
    after: ConsensusProfile,
) -> Result<(), RuleScheduleError> {
    let (old, new) = (before.tsp2_parameters(), after.tsp2_parameters());
    if old.private_profile().is_none()
        && new.private_profile().is_some()
        && after.execution_rules().network() != Network::Development
    {
        return Err(RuleScheduleError::Tsp2ActivationUnavailable);
    }"""),
    (SCHEDULE_TESTS, """/// Private transfers run on development chains only, from an upgrade after genesis on.
#[test]
fn private_transfers_start_only_on_development_chains() {
    use tenrai_primitives::params::consensus::DEVELOPMENT_PRIVATE;

    for network in [Network::Mainnet, Network::Testnet, Network::Testnet100] {
        assert_eq!(
            ConsensusProfile::upgrade_for(
                network,
                identity(),
                DEVELOPMENT_PRIVATE,
                RuleSetParameters::v1(),
                GENESIS_PROTOCOL_VERSION + 1,
            )
            .err(),
            Some(ConsensusProfileError::Tsp2ActivationUnavailable),
        );
    }""", """/// Mainnet runs private transfers from its genesis, and a later upgrade keeps them; testnet and
/// testnet100 run none and no upgrade starts them there; only a development chain names a genesis
/// set of its own.
#[test]
fn private_transfers_run_from_the_genesis_of_mainnet() {
    use crate::consensus_profile::canonical::committee;
    use tenrai_primitives::params::consensus::{DEVELOPMENT_PRIVATE, MAINNET};

    let genesis = ConsensusProfile::for_network(Network::Mainnet, identity()).unwrap();
    assert!(genesis.tsp2_parameters().private_profile().is_some());
    assert_eq!(
        RuleSchedule::genesis_only(genesis).private_transfers_from(),
        Some(0)
    );
    for network in [Network::Testnet, Network::Testnet100] {
        let genesis = ConsensusProfile::for_network(network, identity()).unwrap();
        assert!(
            genesis.tsp2_parameters().private_profile().is_none(),
            "{network:?}"
        );
        assert_eq!(
            ConsensusProfile::upgrade_for(
                network,
                identity(),
                ConsensusParams {
                    private_transfers: true,
                    ..network.params()
                },
                RuleSetParameters::v1(),
                GENESIS_PROTOCOL_VERSION + 1,
            )
            .err(),
            Some(ConsensusProfileError::Tsp2ActivationUnavailable),
            "{network:?}"
        );
    }
    let mainnet = ConsensusProfile::for_network(Network::Mainnet, identity()).unwrap();
    let later = ConsensusProfile::upgrade_for(
        Network::Mainnet,
        identity(),
        MAINNET,
        RuleSetParameters::v1(),
        GENESIS_PROTOCOL_VERSION + 1,
    )
    .unwrap();
    let rules = RuleSchedule::new(mainnet, [(40, later)]).expect("a later mainnet upgrade");
    assert!(
        rules.at(39).tsp2_parameters().private_profile()
            == rules.at(40).tsp2_parameters().private_profile(),
        "the upgrade keeps the private rules as they are"
    );
    assert_eq!(
        RuleSchedule::for_network_from(
            Network::Testnet100,
            identity(),
            committee(Network::Testnet100),
            DEVELOPMENT_PRIVATE,
            &[],
        )
        .err(),
        Some(RuleScheduleError::ManifestUpgrades),
    );
    let private_genesis = RuleSchedule::for_network_from(
        Network::Development,
        identity(),
        committee(Network::Development),
        DEVELOPMENT_PRIVATE,
        &[],
    )
    .unwrap();
    assert_eq!(private_genesis.private_transfers_from(), Some(0));
"""),
    (MANIFEST, """    /// Rule changes of a development chain; public networks take theirs from the release.
    #[serde(default)]
    pub upgrades: Vec<ManifestUpgrade>,""", """    /// Rule changes of a development chain; public networks take theirs from the release.
    #[serde(default)]
    pub upgrades: Vec<ManifestUpgrade>,
    /// The parameter set a development chain's genesis runs, by name as in `upgrades`; public
    /// networks run their own.
    #[serde(default)]
    pub params: Option<String>,"""),
    (MANIFEST, """    #[error("upgrade parameter set `{0}` is unknown")]
    UpgradeParams(String),""", """    #[error("upgrade parameter set `{0}` is unknown")]
    UpgradeParams(String),
    #[error("genesis parameter set `{0}` is unknown")]
    GenesisParams(String),"""),
    (MANIFEST, "        let rules = self.rule_schedule(network, identity, &upgrades)?;", """        let genesis_params = match &self.params {
            Some(name) => consensus::named(name)
                .ok_or_else(|| GenesisManifestError::GenesisParams(name.clone()))?,
            None => network.params(),
        };
        let rules = self.rule_schedule(network, identity, genesis_params, &upgrades)?;"""),
    (MANIFEST, """    fn rule_schedule(
        &self,
        network: Network,
        identity: NetworkIdentity,
        upgrades: &[ScheduledUpgrade],
    ) -> Result<RuleSchedule, GenesisManifestError> {
        #[cfg(any(test, feature = "test-support"))]
        if let Some(cuts) = self.cuts_per_day {
            return Ok(RuleSchedule::for_network_with_day_for_test(
                network,
                identity,
                committee(network),
                cuts,
                upgrades,
            )?);
        }
        Ok(RuleSchedule::for_network(
            network,
            identity,
            committee(network),
            upgrades,
        )?)
    }""", """    fn rule_schedule(
        &self,
        network: Network,
        identity: NetworkIdentity,
        genesis_params: consensus::ConsensusParams,
        upgrades: &[ScheduledUpgrade],
    ) -> Result<RuleSchedule, GenesisManifestError> {
        #[cfg(any(test, feature = "test-support"))]
        if let Some(cuts) = self.cuts_per_day {
            return Ok(RuleSchedule::for_network_with_day_for_test(
                network,
                identity,
                committee(network),
                genesis_params,
                cuts,
                upgrades,
            )?);
        }
        Ok(RuleSchedule::for_network_from(
            network,
            identity,
            committee(network),
            genesis_params,
            upgrades,
        )?)
    }"""),
    (FLOWS, """#[test]
#[ignore = "needs the production prover binary; runs three real proofs"]
fn transparent_value_enters_moves_privately_and_exits_across_a_restart() {
    let mut fixture = funded_manifest_with(MlDsa65KeygenSeed::from_bytes([0x6c; 32]), FUNDING);
    let depositor = MlDsa65KeygenSeed::from_bytes([0x6c; 32]);
    fixture.manifest.upgrades = vec![ManifestUpgrade {
        height: ACTIVATION,
        protocol_version: tenrai_traidag::GENESIS_PROTOCOL_VERSION + 1,
        params: "development-private".into(),
    }];
    let path = directory("private-end-to-end");""", """#[test]
#[ignore = "needs the production prover binary; runs three real proofs"]
fn transparent_value_enters_moves_privately_and_exits_across_a_restart() {
    enter_move_and_exit(false);
}

/// The same on a chain whose genesis runs private transfers: they start with cut 1.
#[test]
#[ignore = "needs the production prover binary; runs three real proofs"]
fn private_transfers_from_genesis_enter_move_and_exit_across_a_restart() {
    enter_move_and_exit(true);
}

fn enter_move_and_exit(from_genesis: bool) {
    let mut fixture = funded_manifest_with(MlDsa65KeygenSeed::from_bytes([0x6c; 32]), FUNDING);
    let depositor = MlDsa65KeygenSeed::from_bytes([0x6c; 32]);
    let activation = if from_genesis {
        fixture.manifest.params = Some("development-private".into());
        1
    } else {
        fixture.manifest.upgrades = vec![ManifestUpgrade {
            height: ACTIVATION,
            protocol_version: tenrai_traidag::GENESIS_PROTOCOL_VERSION + 1,
            params: "development-private".into(),
        }];
        ACTIVATION
    };
    let path = directory(if from_genesis {
        "private-from-genesis"
    } else {
        "private-end-to-end"
    });"""),
    (FLOWS, """    // Private transfers start with the activation cut.
    for _ in 1..=ACTIVATION {""", """    // Private transfers start with the activation cut.
    for _ in 1..=activation {"""),
    (PRIVATE_TESTS, """/// A development chain that runs private transfers from `ACTIVATION` on, with the test seats.
pub(crate) fn private_manifest() -> GenesisManifest {
    let mut fixture = funded_manifest_with(MlDsa65KeygenSeed::from_bytes([0x6c; 32]), FUNDING);
""" + FIXTURE_UPGRADE + """    fixture.manifest
}""", """/// Starts private transfers on a test chain: by an upgrade at `ACTIVATION`, or from genesis on
/// while `TENRAI_PRIVATE_FROM_GENESIS` is set, so that the same tests run the genesis path.
pub(crate) fn start_private(manifest: &mut GenesisManifest) {
    if std::env::var_os("TENRAI_PRIVATE_FROM_GENESIS").is_some() {
        manifest.upgrades.clear();
        manifest.params = Some("development-private".into());
    } else {
        manifest.upgrades = vec![ManifestUpgrade {
            height: ACTIVATION,
            protocol_version: tenrai_traidag::GENESIS_PROTOCOL_VERSION + 1,
            params: "development-private".into(),
        }];
    }
}

/// A development chain that runs private transfers from `ACTIVATION` on, with the test seats.
pub(crate) fn private_manifest() -> GenesisManifest {
    let mut fixture = funded_manifest_with(MlDsa65KeygenSeed::from_bytes([0x6c; 32]), FUNDING);
    start_private(&mut fixture.manifest);
    fixture.manifest
}"""),
    (PRIVATE_VALIDATORS, FIXTURE_UPGRADE, FIXTURE_SWITCH),
    (PRIVATE_COPIES, FIXTURE_UPGRADE, FIXTURE_SWITCH),
    (PRIVATE_RELAY, FIXTURE_UPGRADE, FIXTURE_SWITCH),
    # The relay fixture named the upgrade itself; with the switch it needs no import of its own.
    (PRIVATE_RELAY, "use tenrai_traidag::ManifestUpgrade;\n", ""),
    (FLOWS, """#[test]
fn a_follower_builds_the_cut_from_the_items_it_names() {
    let (leader_path, manifest) = private_chain("private-items-leader");
    let (follower_path, _) = private_chain("private-items-follower");
    let mut leader = open(&leader_path, &manifest);
    let mut follower = open(&follower_path, &manifest);
    for _ in 1..=ACTIVATION {""", """#[test]
fn a_follower_builds_the_cut_from_the_items_it_names() {
    follower_builds_named_items(false);
}

/// The same on a chain whose genesis runs private transfers.
#[test]
fn a_follower_builds_the_cut_from_the_items_it_names_from_genesis() {
    follower_builds_named_items(true);
}

fn follower_builds_named_items(from_genesis: bool) {
    let (leader_path, mut manifest) = private_chain("private-items-leader");
    let (follower_path, _) = private_chain("private-items-follower");
    let activation = if from_genesis {
        manifest.upgrades.clear();
        manifest.params = Some("development-private".into());
        1
    } else {
        ACTIVATION
    };
    let mut leader = open(&leader_path, &manifest);
    let mut follower = open(&follower_path, &manifest);
    for _ in 1..=activation {"""),
]
