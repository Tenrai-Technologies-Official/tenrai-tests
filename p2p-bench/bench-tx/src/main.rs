//! Pre-signed transactions for the P2P benchmark.
//!
//!   bench-tx addresses NETWORK COUNT          spender addresses, one per line
//!   bench-tx sign MANIFEST COUNT OUT_DIR [ROUNDS] [DEPTH] [LIMIT]
//!                                              round1.txt, round2.txt (round3.txt) and pay.txt
//!   bench-tx speed COUNT                      ML-DSA-65 sign and verify times on one core
//!
//! Rounds 1 and 2 split each coin into 8; pay.txt spends the 64 coins per spender as one-in-two-out
//! payments with the key referenced. With DEPTH 3 a third round splits them again, and pay.txt
//! spends the 512 coins per spender, each payment on a coin of its own. LIMIT keeps the first
//! payments only. Lines hold `TXID WIRE_HEX`.

use std::{fs, io::Write, path::Path, time::Instant};
use tenrai_auth::{MlDsa65KeygenSeed, MlDsa65SigningKey};
use tenrai_primitives::{Address, Hash384, SpendLock};
use tenrai_traidag::consensus_profile::canonical::NetworkIdentity;
use tenrai_traidag::genesis::allocation::{GenesisAllocation, GenesisAllocationManifest};
use tenrai_traidag::transaction::{SPEND_CONTEXT, TransactionBody};
use tenrai_traidag::{
    ConsensusProfile, Network, OutPoint, Target, TransactionType, TxOutput, WireTransaction,
    Witness,
};

const FAN_OUT: usize = 8;
const FEE: u128 = 100_000;
const VALID_FROM: u64 = 1;
const VALID_UNTIL: u64 = 1_025;

fn seed(index: usize) -> MlDsa65KeygenSeed {
    let mut bytes = [0_u8; 32];
    bytes[..8].copy_from_slice(&(index as u64).to_be_bytes());
    bytes[8] = 0xB7;
    MlDsa65KeygenSeed::from_bytes(bytes)
}

fn network(name: &str) -> Network {
    Network::parse(name).unwrap_or_else(|| panic!("unknown network {name}"))
}

fn hash(value: &serde_json::Value) -> Hash384 {
    let bytes: [u8; 48] = hex::decode(value.as_str().unwrap())
        .unwrap()
        .try_into()
        .unwrap();
    Hash384(bytes)
}

fn target(value: &serde_json::Value) -> Target {
    let bytes: [u8; 32] = hex::decode(value.as_str().unwrap())
        .unwrap()
        .try_into()
        .unwrap();
    Target::from_be_bytes(bytes).unwrap()
}

struct Coin {
    outpoint: OutPoint,
    amount: u128,
}

struct Signer {
    key: MlDsa65SigningKey,
    lock: SpendLock,
}

fn sign(
    profile: ConsensusProfile,
    signer: &Signer,
    coin: &Coin,
    outputs: Vec<TxOutput>,
    first_use: bool,
) -> WireTransaction {
    let rules = profile.execution_rules();
    let limits = rules.ledger().transaction_limits();
    let body = TransactionBody::new(
        rules.protocol_version(),
        rules.chain_id(),
        rules.ledger().hash(),
        TransactionType::Payment,
        VALID_FROM,
        VALID_UNTIL,
        vec![coin.outpoint],
        outputs,
        None,
        limits,
    )
    .expect("a payment body");
    let message = body
        .spend_message(0, coin.amount, signer.lock)
        .expect("the spend message");
    let signature = signer
        .key
        .sign(message.as_bytes(), SPEND_CONTEXT, [7; 32])
        .expect("the signature")
        .to_bytes();
    let witness = if first_use {
        Witness::new(signer.key.public_key().to_bytes(), signature)
    } else {
        Witness::referenced(signature)
    };
    WireTransaction::new(body, vec![witness], limits).expect("a valid transaction")
}

fn split(coin: &Coin, lock: SpendLock) -> Vec<TxOutput> {
    let spendable = coin.amount - FEE;
    let each = spendable / FAN_OUT as u128;
    (0..FAN_OUT)
        .map(|index| TxOutput {
            amount: if index == 0 {
                each + spendable % FAN_OUT as u128
            } else {
                each
            },
            lock,
        })
        .collect()
}

/// A third of the coin to `to`, the rest back to the signer as the coin of its next payment.
fn pay(profile: ConsensusProfile, signer: &Signer, coin: &Coin, to: SpendLock) -> WireTransaction {
    let spendable = coin.amount - FEE;
    let sent = spendable / 3;
    let outputs = vec![
        TxOutput {
            amount: sent,
            lock: to,
        },
        TxOutput {
            amount: spendable - sent,
            lock: signer.lock,
        },
    ];
    sign(profile, signer, coin, outputs, false)
}

fn coins_of(transaction: &WireTransaction) -> Vec<Coin> {
    let id = transaction.transaction_id();
    transaction
        .body()
        .outputs()
        .iter()
        .enumerate()
        .map(|(index, output)| Coin {
            outpoint: OutPoint {
                transaction_id: id,
                output_index: index as u32,
            },
            amount: output.amount,
        })
        .collect()
}

/// Spender by spender on all cores; each spender's coins are handled in order.
fn per_spender<T: Send>(count: usize, work: impl Fn(usize) -> T + Sync) -> Vec<T> {
    let threads = std::thread::available_parallelism().map_or(4, |n| n.get());
    let per = count.div_ceil(threads);
    let mut out: Vec<Option<T>> = (0..count).map(|_| None).collect();
    std::thread::scope(|scope| {
        for (chunk, slots) in out.chunks_mut(per).enumerate() {
            let work = &work;
            scope.spawn(move || {
                for (offset, slot) in slots.iter_mut().enumerate() {
                    *slot = Some(work(chunk * per + offset));
                }
            });
        }
    });
    out.into_iter().map(Option::unwrap).collect()
}

fn write(path: &Path, transactions: &[&WireTransaction]) {
    let mut file = std::io::BufWriter::new(fs::File::create(path).unwrap());
    let mut bytes = 0;
    for transaction in transactions {
        let wire = transaction.canonical_bytes();
        bytes += wire.len();
        writeln!(
            file,
            "{} {}",
            hex::encode(transaction.transaction_id().as_bytes()),
            hex::encode(&wire)
        )
        .unwrap();
    }
    println!(
        "{}: {} transactions, {} bytes each on average",
        path.display(),
        transactions.len(),
        bytes / transactions.len().max(1)
    );
}

fn addresses(network: Network, count: usize) {
    for index in 0..count {
        let key = seed(index).public_key().to_bytes();
        println!(
            "{}",
            Address::from_mldsa65_public_key(&key).to_text(network.address_network())
        );
    }
}

/// `rounds` rounds of payments: the first spends the funded coins, each later one the change
/// of the round before, so a run can pay for longer than the coins it was funded with.
fn sign_all(manifest: &Path, count: usize, out: &Path, rounds: usize, depth: usize, limit: usize) {
    let started = Instant::now();
    let json: serde_json::Value = serde_json::from_slice(&fs::read(manifest).unwrap()).unwrap();
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

    let allocations = json["allocations"]
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
        .collect();
    let set = GenesisAllocationManifest::new(profile.emission(), allocations).unwrap();

    let signers = per_spender(count, |index| {
        let key = seed(index).expand();
        let lock = SpendLock::for_mldsa65(&key.public_key().to_bytes());
        Signer { key, lock }
    });
    let genesis: Vec<Coin> = signers
        .iter()
        .map(|signer| {
            let index = set
                .allocations()
                .iter()
                .position(|allocation| allocation.lock == signer.lock)
                .expect("every spender is funded in the manifest");
            Coin {
                outpoint: set.outpoint(index).unwrap(),
                amount: set.allocations()[index].amount,
            }
        })
        .collect();
    println!(
        "{} keys and genesis coins in {} ms",
        count,
        started.elapsed().as_millis()
    );

    let first = per_spender(count, |index| {
        let signer = &signers[index];
        sign(
            profile,
            signer,
            &genesis[index],
            split(&genesis[index], signer.lock),
            true,
        )
    });
    let second = per_spender(count, |index| {
        let signer = &signers[index];
        coins_of(&first[index])
            .iter()
            .map(|coin| sign(profile, signer, coin, split(coin, signer.lock), false))
            .collect::<Vec<_>>()
    });
    let third = (depth > 2).then(|| {
        per_spender(count, |index| {
            let signer = &signers[index];
            second[index]
                .iter()
                .flat_map(coins_of)
                .map(|coin| sign(profile, signer, &coin, split(&coin, signer.lock), false))
                .collect::<Vec<_>>()
        })
    });
    let funded = third.as_ref().unwrap_or(&second);
    let payments = per_spender(count, |index| {
        let signer = &signers[index];
        let to = signers[(index + 1) % count].lock;
        funded[index]
            .iter()
            .flat_map(coins_of)
            .map(|coin| pay(profile, signer, &coin, to))
            .collect::<Vec<_>>()
    });
    let mut payment_rounds = vec![payments];
    for _ in 1..rounds {
        let previous = payment_rounds.last().expect("the first round is there");
        let next = per_spender(count, |index| {
            let signer = &signers[index];
            let to = signers[(index + 1) % count].lock;
            previous[index]
                .iter()
                .map(|payment| pay(profile, signer, &coins_of(payment)[1], to))
                .collect::<Vec<_>>()
        });
        payment_rounds.push(next);
    }
    println!("signed in {} ms", started.elapsed().as_millis());

    fs::create_dir_all(out).unwrap();
    write(&out.join("round1.txt"), &first.iter().collect::<Vec<_>>());
    let width = FAN_OUT;
    let round2: Vec<&WireTransaction> = (0..width)
        .flat_map(|slot| second.iter().map(move |own| &own[slot]))
        .collect();
    write(&out.join("round2.txt"), &round2);
    if let Some(third) = &third {
        let width = FAN_OUT * FAN_OUT;
        let round3: Vec<&WireTransaction> = (0..width)
            .flat_map(|slot| third.iter().map(move |own| &own[slot]))
            .collect();
        write(&out.join("round3.txt"), &round3);
    }
    let slots = FAN_OUT.pow(depth as u32);
    let pay: Vec<&WireTransaction> = payment_rounds
        .iter()
        .flat_map(|round| (0..slots).flat_map(move |slot| round.iter().map(move |own| &own[slot])))
        .take(limit)
        .collect();
    write(&out.join("pay.txt"), &pay);
}

/// The node verifies with libcrux's portable code; the multiplexed call picks AVX2 where the CPU has it.
fn speed(count: usize) {
    use libcrux_ml_dsa::ml_dsa_65;
    let key = seed(1).expand();
    let public = key.public_key().clone();
    let messages: Vec<[u8; 48]> = (0..count).map(|index| [index as u8; 48]).collect();
    let started = Instant::now();
    let signatures: Vec<_> = messages
        .iter()
        .map(|message| {
            key.sign(message, SPEND_CONTEXT, [9; 32])
                .expect("the signature")
        })
        .collect();
    let sign = started.elapsed().as_secs_f64() * 1e6 / count as f64;
    let started = Instant::now();
    for (message, signature) in messages.iter().zip(&signatures) {
        public
            .verify(message, SPEND_CONTEXT, signature)
            .expect("a valid signature");
    }
    let node = started.elapsed().as_secs_f64() * 1e6 / count as f64;
    let verification_key = ml_dsa_65::MLDSA65VerificationKey::new(public.to_bytes());
    let started = Instant::now();
    for (message, signature) in messages.iter().zip(&signatures) {
        let signature = ml_dsa_65::MLDSA65Signature::new(signature.to_bytes());
        ml_dsa_65::verify(&verification_key, message, SPEND_CONTEXT, &signature)
            .expect("a valid signature");
    }
    let multiplexed = started.elapsed().as_secs_f64() * 1e6 / count as f64;
    println!(
        "sign {sign:.0} us, verify as the node does {node:.0} us, verify multiplexed {multiplexed:.0} us"
    );
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("addresses") => addresses(network(&args[2]), args[3].parse().unwrap()),
        Some("sign") => sign_all(
            Path::new(&args[2]),
            args[3].parse().unwrap(),
            Path::new(&args[4]),
            args.get(5).map_or(1, |rounds| rounds.parse().unwrap()),
            args.get(6).map_or(2, |depth| depth.parse().unwrap()),
            args.get(7)
                .map_or(usize::MAX, |limit| limit.parse().unwrap()),
        ),
        Some("speed") => speed(args[2].parse().unwrap()),
        _ => {
            eprintln!(
                "bench-tx addresses NETWORK COUNT | sign MANIFEST COUNT OUT_DIR [ROUNDS] [DEPTH] [LIMIT] | speed COUNT"
            );
            std::process::exit(2);
        }
    }
}
