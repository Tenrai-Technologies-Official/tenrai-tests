use serde_json::json;
use std::{
    fs,
    io::{BufRead, BufReader, BufWriter, Write},
    path::Path,
};
use tenrai_primitives::{Address, SpendLock};
use tenrai_traidag::genesis::allocation::GenesisAllocationManifest;
use tenrai_traidag::{GenesisManifest, PaymentRules, WireTransaction};

use crate::{allocations, document, network};

/// What the signed transactions in `files` spend and create, read from their own bytes: first the genesis coins, then
/// one JSON line per transaction, so a run can work out from what it sent what the chain must hold.
pub fn corpus_effects(manifest: &Path, files: &[String]) {
    let json = document(manifest);
    let network = network(json["network"].as_str().unwrap());
    let (rules, _) = GenesisManifest::from_json(&fs::read(manifest).unwrap())
        .unwrap()
        .build()
        .unwrap();
    let set = GenesisAllocationManifest::new(rules.at(0).emission(), allocations(&json, network))
        .unwrap();
    let address = |lock: SpendLock| Address::new(lock).to_text(network.address_network());
    let genesis: Vec<_> = set
        .allocations()
        .iter()
        .enumerate()
        .map(|(index, allocation)| {
            let outpoint = set.outpoint(index).unwrap();
            json!([
                hex::encode(outpoint.transaction_id.as_bytes()),
                outpoint.output_index,
                address(allocation.lock),
                allocation.amount.to_string()
            ])
        })
        .collect();
    let mut out = BufWriter::new(std::io::stdout().lock());
    writeln!(out, "{}", json!({ "genesis": genesis })).unwrap();
    let limits = PaymentRules::from_execution_rules(rules.at(1).execution_rules())
        .ledger()
        .transaction_limits();
    for file in files {
        for line in BufReader::new(fs::File::open(file).unwrap()).lines() {
            let line = line.unwrap();
            let Some((id, wire)) = line.split_once(' ') else {
                continue;
            };
            let transaction =
                WireTransaction::from_bytes(&hex::decode(wire.trim()).unwrap(), limits).unwrap();
            let txid = hex::encode(transaction.transaction_id().as_bytes());
            assert_eq!(txid, id, "a line of {file} names another id than its bytes");
            let inputs: Vec<_> = transaction
                .body()
                .inputs()
                .iter()
                .map(|input| {
                    json!([
                        hex::encode(input.transaction_id.as_bytes()),
                        input.output_index
                    ])
                })
                .collect();
            let outputs: Vec<_> = transaction
                .body()
                .outputs()
                .iter()
                .map(|output| json!([address(output.lock), output.amount.to_string()]))
                .collect();
            writeln!(
                out,
                "{}",
                json!({ "txid": txid, "inputs": inputs, "outputs": outputs })
            )
            .unwrap();
        }
    }
}
