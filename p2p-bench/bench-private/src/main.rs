//! Private statements for the P2P benchmark, proven beforehand against one anchor of its chain, and
//! the checks that compare what the validators hold afterwards.
//!
//!   bench-private addresses NETWORK COUNT   depositor addresses, one per line
//!   bench-private version                   the protocol version the private rules run under
//!   bench-private registration MANIFEST ANCHOR ACCOUNT FUNDER
//!                                           the registration of `sender` or `receiver` with the transaction
//!                                           that pays its fee from the coin of depositor FUNDER, hex
//!   bench-private prepare MANIFEST ANCHOR RECORDS LEDGER COUNT OUT_DIR
//!                                           COUNT ENTER frames from the first depositors, each leaving
//!                                           two notes of the sender, for the statements that spend them
//!   bench-private prove MANIFEST ANCHOR RECORDS LEDGER NOTES FIRST COUNT OUT_DIR
//!                                           COUNT statements in turn ENTER, TRANSFER, EXIT: ENTERs from
//!                                           depositor FIRST on, transfers and EXITs of the sender's notes
//!   bench-private prove-classes MANIFEST ANCHOR RECORDS LEDGER NOTES FIRST OUT_DIR
//!                                           the two circuits `prove` leaves out: an ENTER with one note
//!                                           from depositor FIRST and an EXIT of two notes
//!
//! Both write `corpus.json` beside the frames: per frame its digest and what it moves, in atoms, for
//! the sender, the receiver, the EXIT address and the private total.
//!   bench-private exit-address NETWORK      the transparent address every EXIT pays to
//!   bench-private scan MANIFEST ACCOUNT     the account's private notes, checked from the genesis: asks
//!                                           for answers on stdout, one JSON line each, reads them on stdin
//!   bench-private cut-fields PROOF_FILE     the fields of a finalized cut proof's cut and state, JSON; the
//!                                           file holds the proof's hex, too long for a Windows command line
//!   bench-private anchor-fields MANIFEST ANCHOR
//!                                           the head and the private roots of an anchor, JSON
//!   bench-private verify-proof DIGEST PROOF_FILE
//!                                           which pinned circuits accept this proof of the statement
//!                                           digest, JSON; the file holds the proof's hex
//!   bench-private corpus-effects MANIFEST FILE...
//!                                           the genesis coins, then per signed transaction of the files
//!                                           (lines `TXID WIRE_HEX`) what it spends and creates, JSON lines
//!   bench-private verify-cuts MANIFEST FIRST LAST
//!                                           the cut of every height from FIRST to LAST as its finality
//!                                           proof shows it, each proof checked from the genesis over the
//!                                           bridges; asks for answers on stdout as the scan does
//!
//! ANCHOR, RECORDS, LEDGER and NOTES are the JSON answers of /v1/private/anchor, the rows of
//! /v1/private/records, /v1/transactions/manifest and the rows of /v1/private/notes. Proofs come from
//! the production prover, `TENRAI_TSP2_PROVER`, one at a time. Frames are files `NNNN-CLASS.hex`.

mod corpus;
mod verify_cuts;

use rand_core::{OsRng, RngCore};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, Write},
    path::Path,
    process::{Command, Stdio},
    time::Instant,
};
use tenrai_auth::{
    ML_DSA65_SIGNING_RANDOMNESS_BYTES, MlDsa65KeygenSeed, WalletRoot, derive_tsp2_account_keys,
};
use tenrai_primitives::{Address, Hash384, SPEND_LOCK_BYTES, SpendLock};
use tenrai_traidag::consensus_profile::canonical::NetworkIdentity;
use tenrai_traidag::genesis::allocation::{GenesisAllocation, GenesisAllocationManifest};
use tenrai_traidag::transaction::SPEND_CONTEXT;
use tenrai_traidag::tsp2::profile::Tsp2Profile;
use tenrai_traidag::tsp2::wallet::{
    Tsp2EnterPlan, Tsp2ExitPlan, Tsp2RecipientPath, Tsp2ScanRefresh, Tsp2ScanRequest,
    Tsp2ScanState, Tsp2ServedAnchor, Tsp2ServedNote, Tsp2TransferPlan, Tsp2WalletAccount,
    Tsp2WalletAnchor, Tsp2WalletNotes,
};
use tenrai_traidag::tsp2::{
    Tsp2PrivateNoteLogEntryBytes, Tsp2RecipientRecord, Tsp2RecipientRegistryAuthorization,
    Tsp2RegistrationFrame, tsp2_registration_fee_quote,
};
use tenrai_traidag::{
    ConsensusProfile, FinalizedHead, GENESIS_PROTOCOL_VERSION, GenesisManifest, NativeOperation,
    Network, PaymentRules, StateCommitment, Target, TransactionBody, TransactionType,
    TrustedGenesisFinalityBundle, TxOutput, WireTransaction, Witness,
};

const SENDER_ENTROPY: [u8; 32] = [0x71; 32];
const RECEIVER_ENTROPY: [u8; 32] = [0x72; 32];
/// The first note of an ENTER; the second takes the rest of the coin.
const FIRST_NOTE: u128 = 1_000_000_000;
/// What a transfer pays the receiver; the rest of its two notes comes back to the sender.
const TRANSFERRED: u128 = 500_000_000;
/// As long as a statement may stay valid, so every run from one snapshot can use the same frames.
const VALIDITY: u64 = 1_800;
/// Where every EXIT pays to.
const EXIT_SEED: [u8; 32] = [0x6d; 32];

fn depositor(index: usize) -> MlDsa65KeygenSeed {
    let mut bytes = [0_u8; 32];
    bytes[..8].copy_from_slice(&(index as u64).to_be_bytes());
    bytes[8] = 0xC9;
    MlDsa65KeygenSeed::from_bytes(bytes)
}

fn network(name: &str) -> Network {
    Network::parse(name).unwrap_or_else(|| panic!("unknown network {name}"))
}

fn hash(value: &Value) -> Hash384 {
    let bytes: [u8; 48] = hex::decode(value.as_str().unwrap())
        .unwrap()
        .try_into()
        .unwrap();
    Hash384(bytes)
}

fn hexed(value: &Value) -> Vec<u8> {
    hex::decode(value.as_str().unwrap()).unwrap()
}

fn target(value: &Value) -> Target {
    let bytes: [u8; 32] = hexed(value).try_into().unwrap();
    Target::from_be_bytes(bytes).unwrap()
}

fn document(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn account(chain_id: Hash384, name: &str) -> Tsp2WalletAccount {
    let entropy = match name {
        "sender" => SENDER_ENTROPY,
        "receiver" => RECEIVER_ENTROPY,
        other => panic!("no account {other}"),
    };
    let root = WalletRoot::new(&entropy, "").unwrap();
    Tsp2WalletAccount::new(derive_tsp2_account_keys(&root, chain_id.as_bytes(), 0).unwrap())
        .unwrap()
}

fn head_of(anchor: &Value) -> FinalizedHead {
    FinalizedHead::decode_record(&hexed(&anchor["head"]))
        .unwrap()
        .into_head()
}

fn wallet_anchor(served: &Value) -> Tsp2WalletAnchor {
    let state = StateCommitment::from_bytes(&hexed(&served["state"])).unwrap();
    Tsp2WalletAnchor::new(
        head_of(served),
        state,
        &hexed(&served["record"]),
        &hexed(&served["registry"]),
    )
    .unwrap()
}

fn addresses(network: Network, count: usize) {
    for index in 0..count {
        let key = depositor(index).public_key().to_bytes();
        println!(
            "{}",
            Address::from_mldsa65_public_key(&key).to_text(network.address_network())
        );
    }
}

/// The genesis allocations of the bench manifest, in its order.
fn allocations(json: &Value, network: Network) -> Vec<GenesisAllocation> {
    json["allocations"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| {
            let lock = Address::parse_for_network(
                entry["address"].as_str().unwrap(),
                network.address_network(),
            )
            .unwrap()
            .lock();
            let amount = entry["amount"]
                .as_u64()
                .map(u128::from)
                .unwrap_or_else(|| entry["amount"].to_string().parse().unwrap());
            GenesisAllocation::new(lock, amount).unwrap()
        })
        .collect()
}

/// A node takes a registration only with its fee paid, so the funder's genesis coin pays it, as a
/// wallet pays from a coin of its own; no ENTER spends that coin.
fn registration(manifest: &Path, anchor: &Path, name: &str, funder: usize) {
    let json = document(manifest);
    let network = network(json["network"].as_str().unwrap());
    let (rules, _) = GenesisManifest::from_json(&fs::read(manifest).unwrap())
        .unwrap()
        .build()
        .unwrap();
    let head = head_of(&document(anchor));
    let mut randomness = [0; ML_DSA65_SIGNING_RANDOMNESS_BYTES];
    OsRng.fill_bytes(&mut randomness);
    let signed = account(hash(&json["chain_id"]), name)
        .registration(head.params_hash(), randomness)
        .unwrap();
    let authorization = Tsp2RecipientRegistryAuthorization::from_bytes(&signed.encode()).unwrap();
    let authorization_id = authorization.id();
    let from = head.height() + 1;
    let payment = PaymentRules::from_execution_rules(rules.at(from).execution_rules());
    let limits = payment.ledger().transaction_limits();
    let owner = depositor(funder);
    let lock = SpendLock::for_mldsa65(&owner.public_key().to_bytes());
    let set = GenesisAllocationManifest::new(rules.at(0).emission(), allocations(&json, network))
        .unwrap();
    let at = set
        .allocations()
        .iter()
        .position(|allocation| allocation.lock == lock)
        .expect("the funder is funded in the manifest");
    let (outpoint, amount) = (set.outpoint(at).unwrap(), set.allocations()[at].amount);
    let funding = |change| {
        let body = TransactionBody::new(
            payment.protocol_version(),
            payment.chain_id(),
            payment.ledger().hash(),
            TransactionType::PrivateRegister,
            from,
            from + 600,
            vec![outpoint],
            vec![TxOutput {
                amount: change,
                lock,
            }],
            Some(NativeOperation::PrivateRegister { authorization_id }),
            limits,
        )
        .unwrap();
        let message = body.spend_message(0, amount, lock).unwrap();
        let mut randomness = [0; ML_DSA65_SIGNING_RANDOMNESS_BYTES];
        OsRng.fill_bytes(&mut randomness);
        let signature = owner
            .sign(message.as_bytes(), SPEND_CONTEXT, randomness)
            .unwrap();
        let witness = Witness::inline(*owner.public_key().as_bytes(), *signature.as_bytes());
        WireTransaction::new(body, vec![witness], limits).unwrap()
    };
    let fee = tsp2_registration_fee_quote(&funding(1), payment)
        .unwrap()
        .minimum_fee_atoms;
    let frame = Tsp2RegistrationFrame::new(authorization, funding(amount - fee), limits).unwrap();
    println!("{}", hex::encode(frame.canonical_bytes()));
}

fn prove(input: &str) -> Vec<u8> {
    let prover =
        std::env::var_os("TENRAI_TSP2_PROVER").expect("TENRAI_TSP2_PROVER names the prover");
    let mut child = Command::new(prover)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("the prover starts");
    child
        .stdin
        .take()
        .unwrap()
        .write_all(input.as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "the prover refused: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}

/// Everything a statement of the bench chain is built from.
struct Chain {
    profile: ConsensusProfile,
    set: GenesisAllocationManifest,
    anchor: Tsp2WalletAnchor,
    rules_hash: Hash384,
    sender: Tsp2WalletAccount,
    paths: Vec<Tsp2RecipientPath>,
}

impl Chain {
    fn open(manifest: &Path, anchor: &Path, records: &Path, ledger: &Path) -> Self {
        let json = document(manifest);
        let network = network(json["network"].as_str().unwrap());
        let profile = ConsensusProfile::for_network(
            network,
            NetworkIdentity {
                chain_id: hash(&json["chain_id"]),
                work_profile_id: hash(&json["work_profile_id"]),
                registration_profile_id: hash(&json["registration_profile_id"]),
                controller_segment_id: hash(&json["controller_segment_id"]),
                initial_target: target(&json["initial_target"]),
                pow_limit: target(&json["pow_limit"]),
            },
        )
        .expect("the profile builds");
        let set = GenesisAllocationManifest::new(profile.emission(), allocations(&json, network))
            .unwrap();
        let anchor = wallet_anchor(&document(anchor));
        let chain_id = hash(&json["chain_id"]);
        let rows: Vec<(SpendLock, Tsp2RecipientRecord)> = document(records)
            .as_array()
            .unwrap()
            .iter()
            .map(|row| {
                let owner: [u8; SPEND_LOCK_BYTES] = hexed(&row["owner"]).try_into().unwrap();
                let record = Tsp2RecipientRecord::from_bytes(&hexed(&row["record"])).unwrap();
                (SpendLock::from_bytes(owner).unwrap(), record)
            })
            .collect();
        let (sender, receiver) = (account(chain_id, "sender"), account(chain_id, "receiver"));
        let index = |owner: SpendLock| {
            rows.iter()
                .position(|(lock, _)| *lock == owner)
                .expect("the account is registered") as u64
        };
        let paths = anchor
            .record_paths(&rows, &[index(sender.owner()), index(receiver.owner())])
            .unwrap();
        Self {
            profile,
            set,
            anchor,
            rules_hash: hash(&document(ledger)["ledger_rules_hash"]),
            sender,
            paths,
        }
    }

    fn enter(&self, number: usize) -> Proven {
        self.enter_notes(number, false)
    }

    /// An ENTER of a whole depositor coin: its value less the fee in one note, or split in two.
    fn enter_notes(&self, number: usize, whole: bool) -> Proven {
        let private = Tsp2Profile::release();
        let fee = private.transfer_fee_atoms();
        let limits = self.profile.execution_rules().ledger().transaction_limits();
        let height = self.anchor.height();
        let owner = depositor(number);
        let lock = SpendLock::for_mldsa65(&owner.public_key().to_bytes());
        let at = self
            .set
            .allocations()
            .iter()
            .position(|allocation| allocation.lock == lock)
            .expect("every depositor is funded in the manifest");
        let (outpoint, amount) = (
            self.set.outpoint(at).unwrap(),
            self.set.allocations()[at].amount,
        );
        let split = [
            (&self.paths[0], FIRST_NOTE),
            (&self.paths[0], amount - fee - FIRST_NOTE),
        ];
        let one = [(&self.paths[0], amount - fee)];
        let enter = self
            .anchor
            .prepare_enter(
                private.suite(),
                &private.schedule_bytes(),
                &Tsp2EnterPlan {
                    inputs: &[(outpoint, amount)],
                    notes: if whole { &one } else { &split },
                    valid_from_height: height + 1,
                    valid_until_height: height + VALIDITY,
                },
                &mut OsRng,
            )
            .unwrap();
        let body = enter.transaction_body(self.rules_hash, limits).unwrap();
        let message = body.spend_message(0, amount, lock).unwrap();
        let mut randomness = [0; ML_DSA65_SIGNING_RANDOMNESS_BYTES];
        OsRng.fill_bytes(&mut randomness);
        let signature = owner
            .sign(message.as_bytes(), SPEND_CONTEXT, randomness)
            .unwrap();
        let witness = Witness::inline(*owner.public_key().as_bytes(), *signature.as_bytes());
        let transaction = WireTransaction::new(body, vec![witness], limits).unwrap();
        let entered = (amount - fee) as i128;
        Proven {
            class: "enter",
            digest: enter.statement_digest(),
            frame: enter
                .frame(transaction, prove(enter.prover_input()))
                .unwrap(),
            moves: Moves {
                sender: entered,
                private: entered,
                ..Moves::default()
            },
        }
    }

    fn transfer(&self, wallet: &Tsp2WalletNotes, sources: [u64; 2]) -> Proven {
        let private = Tsp2Profile::release();
        let height = self.anchor.height();
        let transfer = wallet
            .prepare_transfer(
                &self.sender,
                &self.anchor,
                private.suite(),
                &private.schedule_bytes(),
                &Tsp2TransferPlan {
                    sources,
                    sender: &self.paths[0],
                    receiver: &self.paths[1],
                    amount_atoms: TRANSFERRED,
                    valid_from_height: height + 1,
                    valid_until_height: height + VALIDITY,
                },
                &mut OsRng,
            )
            .unwrap();
        let fee = private.transfer_fee_atoms() as i128;
        let sent = TRANSFERRED as i128;
        Proven {
            class: "transfer",
            digest: transfer.statement_digest(),
            frame: transfer.artifact(prove(transfer.prover_input())).unwrap(),
            moves: Moves {
                sender: -sent - fee,
                receiver: sent,
                private: -fee,
                ..Moves::default()
            },
        }
    }

    fn exit(&self, wallet: &Tsp2WalletNotes, sources: &[u64]) -> Proven {
        let private = Tsp2Profile::release();
        let height = self.anchor.height();
        let destination = SpendLock::for_mldsa65(
            MlDsa65KeygenSeed::from_bytes(EXIT_SEED)
                .public_key()
                .as_bytes(),
        );
        let exit = wallet
            .prepare_exit(
                &self.sender,
                &self.anchor,
                private.suite(),
                &private.schedule_bytes(),
                &Tsp2ExitPlan {
                    sources,
                    sender: &self.paths[0],
                    destination,
                    valid_from_height: height + 1,
                    valid_until_height: height + VALIDITY,
                },
                &mut OsRng,
            )
            .unwrap();
        let value: i128 = sources
            .iter()
            .map(|source| wallet.note(*source).expect("an unspent note").value_atoms() as i128)
            .sum();
        let fee = private.transfer_fee_atoms() as i128;
        Proven {
            class: "exit",
            digest: exit.statement_digest(),
            frame: exit.frame(prove(exit.prover_input())).unwrap(),
            moves: Moves {
                sender: -value,
                exit_address: value - fee,
                private: -value,
                ..Moves::default()
            },
        }
    }
}

/// What one statement moves, in atoms.
#[derive(Default)]
struct Moves {
    sender: i128,
    receiver: i128,
    exit_address: i128,
    private: i128,
}

struct Proven {
    class: &'static str,
    digest: Hash384,
    frame: Vec<u8>,
    moves: Moves,
}

/// The frames of one corpus and what each moves, written as they are proven.
struct Corpus<'a> {
    out: &'a Path,
    entries: Vec<Value>,
}

impl<'a> Corpus<'a> {
    fn new(out: &'a Path) -> Self {
        fs::create_dir_all(out).unwrap();
        Self {
            out,
            entries: Vec::new(),
        }
    }

    fn add(&mut self, number: usize, proven: Proven, started: Instant) {
        let file = format!("{number:04}-{}.hex", proven.class);
        fs::write(self.out.join(&file), hex::encode(&proven.frame)).unwrap();
        let digest = hex::encode(proven.digest.as_bytes());
        println!(
            "{number} {}: {digest} in {:.1} s, {} bytes",
            proven.class,
            started.elapsed().as_secs_f64(),
            proven.frame.len()
        );
        let moves = &proven.moves;
        self.entries.push(json!({
            "file": file,
            "class": proven.class,
            "digest": digest,
            "sender": moves.sender.to_string(),
            "receiver": moves.receiver.to_string(),
            "exit_address": moves.exit_address.to_string(),
            "private": moves.private.to_string(),
        }));
        let written = serde_json::to_vec_pretty(&self.entries).unwrap();
        fs::write(self.out.join("corpus.json"), written).unwrap();
    }
}

fn prepare(chain: &Chain, count: usize, out: &Path) {
    let mut corpus = Corpus::new(out);
    for number in 0..count {
        let started = Instant::now();
        corpus.add(number, chain.enter(number), started);
    }
}

/// The sender's unspent notes at the anchor, from the note log rows as a node serves them.
fn sender_notes(chain: &Chain, notes: &Path) -> Tsp2WalletNotes {
    let mut wallet = Tsp2WalletNotes::empty();
    for row in document(notes).as_array().unwrap() {
        let entry =
            Tsp2PrivateNoteLogEntryBytes::from_canonical_bytes(&hexed(&row["entry"])).unwrap();
        wallet
            .scan(&[&chain.sender], &entry, &hexed(&row["capsule"]))
            .unwrap();
    }
    assert!(
        wallet.matches(&chain.anchor),
        "the note rows are the anchor's log"
    );
    wallet
}

fn prove_mixed(chain: &Chain, notes: &Path, first: usize, count: usize, out: &Path) {
    let wallet = sender_notes(chain, notes);
    let mut leaves = wallet
        .notes()
        .map(|note| note.leaf_index())
        .collect::<Vec<_>>()
        .into_iter();
    let mut depositors = first..;
    let mut corpus = Corpus::new(out);
    for number in 0..count {
        let started = Instant::now();
        let proven = match number % 3 {
            0 => chain.enter(depositors.next().unwrap()),
            1 => chain.transfer(
                &wallet,
                [
                    leaves.next().expect("a note"),
                    leaves.next().expect("a note"),
                ],
            ),
            _ => chain.exit(&wallet, &[leaves.next().expect("a note")]),
        };
        corpus.add(number, proven, started);
    }
}

/// One statement of each circuit that `prove_mixed` does not reach.
fn prove_classes(chain: &Chain, notes: &Path, first: usize, out: &Path) {
    let wallet = sender_notes(chain, notes);
    let leaves: Vec<u64> = wallet
        .notes()
        .map(|note| note.leaf_index())
        .take(2)
        .collect();
    let mut corpus = Corpus::new(out);
    let started = Instant::now();
    corpus.add(0, chain.enter_notes(first, true), started);
    let started = Instant::now();
    corpus.add(1, chain.exit(&wallet, &leaves), started);
}

/// Runs the refresh every wallet runs, with the bench script as its way to the node.
fn scan(manifest: &Path, name: &str) {
    let bundle =
        TrustedGenesisFinalityBundle::from_test_network_manifest(&fs::read(manifest).unwrap())
            .unwrap();
    let account = account(bundle.rules().chain_id(), name);
    let mut state = Tsp2ScanState::new(bundle).unwrap();
    let mut refresh = Tsp2ScanRefresh::start(&state, 0);
    let stdin = std::io::stdin();
    let mut lines = stdin.lock().lines();
    let mut ask = |request: Value| -> Value {
        println!("{request}");
        std::io::stdout().flush().unwrap();
        serde_json::from_str(&lines.next().expect("an answer").unwrap()).unwrap()
    };
    // The account-local scratch pages of the historical recovery, kept for this one scan.
    let mut local: std::collections::HashMap<_, Vec<u8>> = std::collections::HashMap::new();
    let started = Instant::now();
    loop {
        let request = match refresh.next(&mut state, &account) {
            Ok(Some(request)) => request,
            Ok(None) => break,
            Err(error) => {
                println!("{}", json!({"error": error.to_string()}));
                std::process::exit(1);
            }
        };
        let outcome = match request {
            Tsp2ScanRequest::LocalStart => refresh.start_local(&mut OsRng),
            Tsp2ScanRequest::LocalRead { generation, page } => {
                let key = (generation, page.order as u8, page.pass, page.index);
                let sealed = local.get(&key).expect("a local page this scan wrote");
                refresh.take_local_read(&account, sealed)
            }
            Tsp2ScanRequest::LocalWrite { generation, page } => {
                let key = (generation, page.order as u8, page.pass, page.index);
                match refresh.local_write_bytes(&account, &mut OsRng) {
                    Ok(sealed) => {
                        local.insert(key, sealed.to_vec());
                        refresh.local_written()
                    }
                    Err(error) => Err(error),
                }
            }
            Tsp2ScanRequest::Anchor => {
                let served = ask(json!({"get": "/v1/private/anchor"}));
                refresh.take_anchor(
                    &mut state,
                    Tsp2ServedAnchor {
                        head: hexed(&served["head"]),
                        state: hexed(&served["state"]),
                        record: hexed(&served["record"]),
                        registry: hexed(&served["registry"]),
                    },
                )
            }
            Tsp2ScanRequest::Bridge { after } => {
                let served = ask(
                    json!({"get": format!("/v1/finality/bridge?after={}", hex::encode(after.encode()))}),
                );
                refresh.take_bridge(&mut state, &hexed(&served["bytes"]))
            }
            Tsp2ScanRequest::CutProof { height } => {
                let served = ask(json!({"get": format!("/v1/finality/cuts/{height}")}));
                refresh.take_cut_proof(&mut state, &hexed(&served["proof"]))
            }
            Tsp2ScanRequest::Notes { from } => {
                let served =
                    ask(json!({"get": format!("/v1/private/notes?from={from}&limit=512")}));
                let rows: Vec<Tsp2ServedNote> = served["notes"]
                    .as_array()
                    .unwrap_or_else(|| panic!("the node answered the notes with {served}"))
                    .iter()
                    .map(|row| Tsp2ServedNote {
                        leaf_index: row["leaf_index"].as_u64().unwrap(),
                        entry: hexed(&row["entry"]),
                        capsule: hexed(&row["capsule"]),
                    })
                    .collect();
                refresh.take_notes(&mut state, &account, &rows)
            }
            Tsp2ScanRequest::SpendProof {
                from_height,
                at_height,
                root,
            } => {
                let served = ask(json!({"get": format!(
                    "/v1/private/nullifier-updates?from_height={from_height}&at_height={at_height}&root={}",
                    hex::encode(root.as_bytes())
                )}));
                if served.get("status").and_then(Value::as_u64) == Some(404) {
                    refresh.spend_proof_refused()
                } else {
                    refresh.take_spend_proof(
                        &mut state,
                        &hexed(&served["proof"]),
                        served["next_height"].as_u64(),
                    )
                }
            }
            Tsp2ScanRequest::SpendKeys { at_height, after } => {
                let after = after
                    .map(|key| format!("&after={}", hex::encode(key.as_bytes())))
                    .unwrap_or_default();
                let served = ask(json!({"get": format!(
                    "/v1/private/nullifier-keys?at_height={at_height}{after}&limit=512"
                )}));
                if served.get("status").and_then(Value::as_u64) == Some(404) {
                    refresh.spend_keys_refused(&mut state)
                } else {
                    let key = |value: &Value| Hash384::from_hex(value.as_str().unwrap()).unwrap();
                    let keys: Vec<Hash384> = served["keys"]
                        .as_array()
                        .unwrap_or_else(|| panic!("the node answered the keys with {served}"))
                        .iter()
                        .map(key)
                        .collect();
                    let next = served.get("next").filter(|next| !next.is_null()).map(key);
                    refresh.take_spend_keys(
                        &mut state,
                        served["at_height"].as_u64().unwrap(),
                        &keys,
                        next,
                    )
                }
            }
        };
        if let Err(error) = outcome {
            println!("{}", json!({"error": error.to_string()}));
            std::process::exit(1);
        }
    }
    let notes = state.notes();
    println!(
        "{}",
        json!({
            "done": true,
            "account": name,
            "height": state.checked_height(),
            "balance_atoms": notes.balance_atoms(account.account()).to_string(),
            "notes": notes.notes().count(),
            "seconds": started.elapsed().as_secs_f64(),
        })
    );
}

fn h(value: Hash384) -> String {
    hex::encode(value.as_bytes())
}

fn cut_fields(proof: &str) {
    let bytes = hex::decode(proof.trim()).unwrap();
    // Version, cut commitment and state commitment lead the proof; neither needs the rules to read.
    let commitment_end = 2 + tenrai_traidag::CUT_COMMITMENT_BYTES;
    let commitment = tenrai_traidag::CutCommitment::from_bytes(&bytes[2..commitment_end]).unwrap();
    let state = StateCommitment::from_bytes(
        &bytes[commitment_end..commitment_end + tenrai_traidag::STATE_COMMITMENT_BYTES],
    )
    .unwrap();
    println!(
        "{}",
        json!({
            "height": commitment.height(),
            "cut_id": h(commitment.id()),
            "protocol_version": commitment.protocol_version(),
            "params_hash": h(commitment.params_hash()),
            "validator_set_id": h(commitment.validator_set_id()),
            "state_root": h(state.root()),
            "utxo_root": h(state.utxo().root()),
            "utxo_value_atoms": state.utxo().subtotal().to_string(),
            "key_registry_root": h(state.key_registry_root()),
            "validator_registry_root": h(state.validator_registry_root()),
            "stake_state_root": h(state.stake_state_root()),
            "pending_exit_root": h(state.pending_exit_root()),
            "epoch_state_root": h(state.epoch_state_root()),
            "emission_accounting_root": h(state.emission_accounting_root()),
            "settlement_root": h(state.settlement_root()),
            "compute_root": h(state.compute_root()),
        })
    );
}

fn anchor_fields(anchor: &Path) {
    let served = document(anchor);
    let anchor = wallet_anchor(&served);
    let head = head_of(&served);
    println!(
        "{}",
        json!({
            "height": anchor.height(),
            "cut_id": h(head.cut_id()),
            "note_tree_root": h(anchor.note_tree_root()),
            "permanent_nullifier_root": h(anchor.permanent_nullifier_root()),
            "recipient_registry_root": h(anchor.recipient_registry_root()),
            "private_extension_root": h(anchor.private_extension_root()),
            "notes": anchor.note_count().to_string(),
            "records": anchor.record_count().to_string(),
            "confidential_atoms": served["confidential_atoms"],
        })
    );
}

/// Asks the verifier of every circuit, the one a node runs, about one proof.
fn verify_proof(digest: &str, proof_file: &Path) {
    let digest: [u8; 48] = hex::decode(digest).unwrap().try_into().unwrap();
    let proof = hex::decode(fs::read_to_string(proof_file).unwrap().trim()).unwrap();
    let accepted: Vec<String> = tenrai_tsp2_verifier::Tsp2Circuit::ALL
        .iter()
        .filter(|circuit| {
            tenrai_tsp2_verifier::Tsp2Verifier::pinned(**circuit)
                .unwrap()
                .verify(&proof, &digest)
                .is_ok()
        })
        .map(|circuit| format!("{circuit:?}"))
        .collect();
    println!(
        "{}",
        json!({ "accepted_by": accepted, "proof_bytes": proof.len() })
    );
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let path = |index: usize| Path::new(&args[index]);
    match args.get(1).map(String::as_str) {
        Some("addresses") => addresses(network(&args[2]), args[3].parse().unwrap()),
        Some("version") => println!("{}", GENESIS_PROTOCOL_VERSION + 1),
        Some("registration") => registration(path(2), path(3), &args[4], args[5].parse().unwrap()),
        Some("prepare") => {
            let chain = Chain::open(path(2), path(3), path(4), path(5));
            prepare(&chain, args[6].parse().unwrap(), path(7));
        }
        Some("prove") => {
            let chain = Chain::open(path(2), path(3), path(4), path(5));
            prove_mixed(
                &chain,
                path(6),
                args[7].parse().unwrap(),
                args[8].parse().unwrap(),
                path(9),
            );
        }
        Some("prove-classes") => {
            let chain = Chain::open(path(2), path(3), path(4), path(5));
            prove_classes(&chain, path(6), args[7].parse().unwrap(), path(8));
        }
        Some("exit-address") => {
            let key = MlDsa65KeygenSeed::from_bytes(EXIT_SEED)
                .public_key()
                .to_bytes();
            println!(
                "{}",
                Address::from_mldsa65_public_key(&key).to_text(network(&args[2]).address_network())
            );
        }
        Some("scan") => scan(path(2), &args[3]),
        Some("cut-fields") => cut_fields(&fs::read_to_string(path(2)).unwrap()),
        Some("anchor-fields") => anchor_fields(path(3)),
        Some("verify-proof") => verify_proof(&args[2], path(3)),
        Some("corpus-effects") => corpus::corpus_effects(path(2), &args[3..]),
        Some("verify-cuts") => {
            verify_cuts::verify_cuts(path(2), args[3].parse().unwrap(), args[4].parse().unwrap())
        }
        _ => {
            eprintln!("see the head of src/main.rs for the commands");
            std::process::exit(2);
        }
    }
}
