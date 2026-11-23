use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, Write},
    path::Path,
};
use tenrai_primitives::Hash384;
use tenrai_traidag::{FinalityTracker, TrustedGenesisFinalityBundle};

/// Checks the finality proof of every height from `first` to `last` as a wallet does, from the genesis on over the
/// bridges, and prints the cut each proof authenticates. Asks for answers on stdout and reads them on stdin, as the
/// scan does; the first proof that does not hold ends the check with its height.
pub fn verify_cuts(manifest: &Path, first: u64, last: u64) {
    let bundle =
        TrustedGenesisFinalityBundle::from_test_network_manifest(&fs::read(manifest).unwrap())
            .unwrap();
    let mut tracker = FinalityTracker::from_trust_root(bundle).unwrap();
    let stdin = std::io::stdin();
    let mut lines = stdin.lock().lines();
    let mut ask = |path: String| -> Value {
        println!("{}", json!({ "get": path }));
        std::io::stdout().flush().unwrap();
        serde_json::from_str(&lines.next().expect("an answer").unwrap()).unwrap()
    };
    let mut cuts = Vec::new();
    for height in first..=last {
        match verify(&mut tracker, &mut ask, height) {
            Ok(cut) => cuts.push(cut),
            Err(error) => {
                println!(
                    "{}",
                    json!({ "error": error, "height": height, "cuts": cuts })
                );
                return;
            }
        }
    }
    println!("{}", json!({ "cuts": cuts }));
}

fn verify(
    tracker: &mut FinalityTracker,
    ask: &mut impl FnMut(String) -> Value,
    height: u64,
) -> Result<Value, String> {
    while tracker
        .needs_bridge_for(height)
        .map_err(|error| error.to_string())?
    {
        let after = hex::encode(tracker.anchor().head().encode());
        let served = ask(format!("/v1/finality/bridge?after={after}"));
        let bridge = served_bytes(&served, "bytes")?;
        tracker.advance(&bridge).map_err(|error| {
            format!("bridge after {}: {error}", tracker.anchor().head().height())
        })?;
    }
    let served = ask(format!("/v1/finality/cuts/{height}"));
    let proof = served_bytes(&served, "proof")?;
    let head = tracker
        .verify_cut(&proof)
        .map_err(|error| error.to_string())?
        .head();
    if head.height() != height {
        return Err(format!("the proof is of height {}", head.height()));
    }
    Ok(json!([
        height,
        text(head.cut_id()),
        text(head.state_root()),
        text(head.validator_set_id())
    ]))
}

fn served_bytes(served: &Value, field: &str) -> Result<Vec<u8>, String> {
    let hex = served[field]
        .as_str()
        .ok_or_else(|| format!("the answer holds no {field}: {served}"))?;
    hex::decode(hex).map_err(|error| error.to_string())
}

fn text(value: Hash384) -> String {
    hex::encode(value.as_bytes())
}
