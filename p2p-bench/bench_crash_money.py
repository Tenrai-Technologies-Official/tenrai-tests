"""Mandatory monetary oracles for transparent process-kill/restart acceptance.

This reads current Node APIs and the fresh run's own signed corpus. It starts no
process, creates no product failpoint, and does not model device power loss.
"""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

from bench_agree import balances_at_one_height, read_or_status, same_height
from bench_submit import BUSY_RETRIES, connect, is_busy, post, retry_after, statuses


def require(condition, message):
    if not condition:
        raise ValueError(message)


def amount(value):
    require(isinstance(value, str) and value.isascii() and value.isdigit(),
            "missing or non-integer atom amount")
    number = int(value)
    require(str(number) == value, "atom amount is not canonical decimal")
    require(0 <= number < 2 ** 128, "atom amount is outside u128")
    return number


def checked_read(port, path):
    for _ in range(BUSY_RETRIES):
        answer = read_or_status(port, path)
        if answer.get("status") not in (429, 503):
            require(isinstance(answer.get("status"), dict) or "status" not in answer,
                    f"monetary read failed: {path}: {answer}")
            return answer
        time.sleep(0.25)
    raise ValueError(f"monetary read stayed busy: {path}")


# Reads of one validator at once. The same reads as one after the other, only not waiting for each in turn: every
# applied payment is still read from every validator and checked in full.
PARALLEL_READS = 8


def transaction_views(port, txids):
    """The view of every transaction in `txids` from one validator, in that order."""
    with ThreadPoolExecutor(max_workers=PARALLEL_READS) as pool:
        return list(pool.map(lambda txid: checked_read(port, f"/v1/transactions/{txid}"), txids))


def balance_rows(rows):
    result = {}
    for address, balance, utxos in rows:
        require(address not in result, "duplicate address balance row")
        count = int(utxos)
        require(count >= 0, "negative UTXO count")
        result[address] = {"balance": amount(balance), "utxos": count}
    return result


def baseline(ports, wallets):
    require(bool(wallets) and len(set(wallets)) == len(wallets), "no unique tracked payment wallets")
    height, rows = balances_at_one_height(ports, wallets)
    found = {name: balance_rows(values) for name, values in rows.items()}
    first = next(iter(found.values()))
    require(all(values == first for values in found.values()), "initial wallet balances differ")
    require(set(first) == set(wallets), "initial balances omit a tracked wallet")
    return {"height": height, "wallets": first}


def corpus_hashes(payments):
    found = {}
    for txid, wire in payments:
        found.setdefault(txid, set()).add(hashlib.sha256(bytes.fromhex(wire)).hexdigest())
    require(bool(found), "empty payment corpus cannot qualify a monetary crash")
    return found


def finalized_ids(port, txids):
    return {txid for txid, status in statuses(port, txids).items()
            if str(status.get("lifecycle", "")).lower() == "finalized"
            and status.get("finalized_cut") is not None}


def payment(view, wires, after_height):
    txid = view["transaction_id"]
    require(txid in wires, "transaction does not belong to this fresh payment corpus")
    require(hashlib.sha256(bytes.fromhex(view["wire_hex"])).hexdigest() in wires[txid],
            "Node returned different signed bytes for a corpus payment")
    status = view["status"]
    require(str(status["lifecycle"]).lower() == "finalized", "payment has no applied terminal outcome")
    cut = status["finalized_cut"]
    require(cut and int(cut["height"]) > after_height, "payment was already final before this run")
    require(view["kind"] == "payment" and view["operation"] is None, "non-payment in transparent corpus")
    require(bool(view["inputs"]) and bool(view["outputs"]), "empty payment cannot qualify effects")
    inputs = []
    for value in view["inputs"]:
        spent = value["spends"]
        require(spent is not None, "input value/address history is unavailable for the oracle")
        inputs.append((value["transaction_id"], int(value["output_index"]), spent["address"],
                       amount(spent["amount"])))
    outputs = [(int(value["index"]), value["address"], amount(value["amount"])) for value in view["outputs"]]
    require(len({index for index, _, _ in outputs}) == len(outputs), "duplicate output index")
    input_total, output_total, fee = amount(view["input_total"]), amount(view["output_total"]), amount(view["fee"])
    require(sum(value for _, _, _, value in inputs) == input_total, "input amounts do not sum exactly")
    require(sum(value for _, _, value in outputs) == output_total, "output amounts do not sum exactly")
    require(input_total >= output_total and input_total - output_total == fee, "payment fee is not exact")
    require(bool(view["block_id"]) and bool(cut["cut_id"]) and bool(cut["state_root"]),
            "payment has no canonical block/cut roots")
    return {"transaction_id": txid, "inputs": inputs, "outputs": outputs, "fee": fee,
            "block_id": view["block_id"], "finalized_cut": cut}


def effects(views, initial):
    """Pure oracle: each applied input is unique, with exact balance, count and fee effects."""
    expected = {address: dict(value) for address, value in initial["wallets"].items()}
    consumed, fingerprints = {}, {}
    fees = 0
    for view in views:
        txid = view["transaction_id"]
        require(txid not in fingerprints, "the same applied payment was counted twice")
        for parent, index, address, value in view["inputs"]:
            outpoint = (parent, index)
            require(outpoint not in consumed, f"input applied twice: {outpoint}")
            require(address in expected, "payment spends an untracked wallet")
            consumed[outpoint] = txid
            expected[address]["balance"] -= value
            expected[address]["utxos"] -= 1
        for _, address, value in view["outputs"]:
            require(address in expected, "payment pays an untracked wallet")
            expected[address]["balance"] += value
            expected[address]["utxos"] += 1
        fees += view["fee"]
        fingerprints[txid] = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()
    require(bool(fingerprints) and fees > 0, "no applied fee-paying payment exercised the crash")
    require(all(value["balance"] >= 0 and value["utxos"] >= 0 for value in expected.values()),
            "payment effects overdraw initial wallet value or UTXOs")
    return {"expected": expected, "fees": fees, "inputs": len(consumed), "fingerprints": fingerprints}


def replay_finalized(ports, payments, initial, log):
    """Replay real finalized bytes while miners still run; return immutable receipt fingerprints."""
    wires = corpus_hashes(payments)
    shared = None
    for port in ports.values():
        found = finalized_ids(port, list(wires))
        shared = found if shared is None else shared & found
    selected = sorted(shared or ())[:64]
    require(bool(selected), "no common finalized payment available for late replay")
    frames = {}
    for txid, wire in payments:
        if txid in selected:
            frames.setdefault(txid, wire)
    reference, replies = {}, {}
    for name, port in ports.items():
        reference[name] = {txid: payment(checked_read(port, f"/v1/transactions/{txid}"), wires,
                                         initial["height"]) for txid in selected}
        connection = connect(port)
        try:
            replies[name] = {}
            for txid in selected:
                for _ in range(BUSY_RETRIES):
                    status, body = post(connection, "/v1/transactions", {"api_version": 1, "wire_hex": frames[txid]})
                    if not is_busy(status, body):
                        break
                    time.sleep(retry_after(body))
                require(status in (200, 400, 409, 422), f"replay API failed: {status}: {body[:200]!r}")
                replies[name][txid] = status
        finally:
            connection.close()
    log(f"replayed {len(selected)} finalized signed payments to each real validator")
    return {"selected": selected, "before": reference, "responses": replies}


def recovered_heads(ports, samples):
    """Read recovered canonical data; a missing pre-kill log is never a durable-head oracle."""
    result = {}
    for name, port in ports.items():
        sample = samples[name]
        height = int(sample["height"])
        cut = checked_read(port, f"/v1/cuts/{height}")["cut"]
        require(all(sample[field] == cut[field] for field in ("height", "cut_id", "state_root", "validator_set_id")),
                "reopened API head differs from its stored canonical cut")
        result[name] = {field: sample[field] for field in ("height", "cut_id", "state_root", "validator_set_id")}
    return result


def final_transparent_checks(ports, wallets, initial, payments, replay, log):
    require(bool(replay["selected"]) and set(replay["before"]) == set(ports),
            "late replay was not exercised on every validator")
    require(all(set(values) == set(replay["selected"]) for values in replay["before"].values()),
            "late replay is missing original terminal receipts")
    supplies = same_height(ports, "/v1/economics/supply")
    supply = next(iter(supplies.values()))
    height = int(supply["finalized_height"])
    require(height >= replay["minimum_height"], "no finalized progress after replay")
    domains = ("circulating_supply", "bonded", "unbonding", "private_supply", "work_reserve", "stake_reserve")
    accounted = all(amount(value["minted"]) - amount(value["burned"]) == sum(amount(value[key]) for key in domains)
                    and amount(value["total_supply"]) == amount(value["minted"]) - amount(value["burned"])
                    and amount(value["minted"]) <= amount(value["max_supply"])
                    and amount(value["private_supply"]) == 0 for value in supplies.values())
    cuts = {name: checked_read(port, f"/v1/cuts/{height}")["cut"] for name, port in ports.items()}
    first_cut = next(iter(cuts.values()))
    require(first_cut["height"] == height and first_cut["cut_id"] and first_cut["state_root"], "missing canonical cut")
    wires = corpus_hashes(payments)
    applied = {name: finalized_ids(port, list(wires)) for name, port in ports.items()}
    ids = next(iter(applied.values()))
    require(bool(ids) and all(found == ids for found in applied.values()), "validators disagree on applied corpus payments")
    applied_before_kill = ids & set(initial["accepted_before_kill"])
    require(bool(applied_before_kill), "no payment accepted before the kill has an applied monetary outcome")
    ordered = sorted(ids)
    with ThreadPoolExecutor(max_workers=len(ports)) as validators:
        views = dict(zip(ports, validators.map(lambda port: transaction_views(port, ordered), ports.values())))
    reference = None
    for name, port in ports.items():
        verified = effects((payment(view, wires, initial["height"]) for view in views[name]), initial)
        if reference is None:
            reference = verified
        require(verified == reference, "validators disagree on exact payment effects and fees")
        for txid, before in replay["before"][name].items():
            require(payment(checked_read(port, f"/v1/transactions/{txid}"), wires, initial["height"]) == before,
                    "late replay replaced a canonical location, block or fee")
    balance_height, rows = balances_at_one_height(ports, wallets)
    require(balance_height >= height, "address index predates the checked monetary head")
    balances = {name: balance_rows(values) for name, values in rows.items()}
    result = {"mode": "transparent", "anchor_height": height,
              "anchor_equal": all(value == first_cut for value in cuts.values()),
              "supply_equal": all(value == supply for value in supplies.values()),
              "cut_fields_equal": all(value == first_cut for value in cuts.values()),
              "value_accounted": accounted, "supply": supply, "cut_fields": first_cut,
              "balances_height": balance_height,
              "transparent_balances_equal": all(value == reference["expected"] for value in balances.values()),
              "balances_as_expected": all(value == reference["expected"] for value in balances.values()),
              "private_balances_equal": all(amount(value["private_supply"]) == 0 for value in supplies.values()),
              "applied_payments": len(ids), "applied_pre_kill_payments": len(applied_before_kill),
              "unique_spent_inputs": reference["inputs"],
              "fee_atoms": str(reference["fees"]), "one_time_effects": True,
              "replayed_payments": len(replay["selected"]), "replay_receipts_unchanged": True,
              "tracked_wallets": len(wallets)}
    log("transparent crash monetary state:", json.dumps({key: value for key, value in result.items()
                                                       if key not in ("supply", "cut_fields")}))
    return result
