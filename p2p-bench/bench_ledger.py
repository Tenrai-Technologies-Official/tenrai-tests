"""The money of a bench run, worked out from what the run sent and compared with what every node holds.

The payers' holdings start at their genesis coins and take every corpus transaction that the nodes hold final, in
the order of its cut; from a node only that answer is taken, not an amount. The economics of every node are read at
one height and must agree: conservation, the fee burn of the private statements that ran, and the minting of the
emission schedule."""

import json
import subprocess
import time

from bench_agree import balances_at_one_height, read_or_status, same_height

# Half of a private statement's fee is burned, rounded down per statement; a transparent fee never is.
PRIVATE_FEE_BURN_DENOMINATOR = 2
# A cut's subsidy goes half to work, rounded down with a carry (economics/policy.rs split_reward).
WORK_SHARE = (1, 2)
# The supply fields that hold value; minted less burned is their sum, unbonding being what the node cannot place.
SUPPLY_DOMAINS = ("circulating_supply", "bonded", "unbonding", "private_supply", "work_reserve", "stake_reserve")


def command(tool):
    """bench-private as a command line: its path, or the command that stands in for it in a test."""
    return list(tool) if isinstance(tool, (list, tuple)) else [str(tool)]


def corpus_effects(tool, manifest, files):
    """The genesis coins (outpoint to address and atoms) and per corpus transaction its inputs and outputs."""
    process = subprocess.run([*command(tool), "corpus-effects", str(manifest), *map(str, files)],
                             capture_output=True, text=True, check=True)
    lines = process.stdout.splitlines()
    genesis = {(txid, index): (address, int(amount))
               for txid, index, address, amount in json.loads(lines[0])["genesis"]}
    corpus = {}
    for line in lines[1:]:
        entry = json.loads(line)
        corpus[entry["txid"]] = ([(txid, index) for txid, index in entry["inputs"]],
                                 [(address, int(amount)) for address, amount in entry["outputs"]])
    return genesis, corpus


def payers(corpus):
    """Every address the corpus pays; the bench's payers pay only each other."""
    return sorted({address for _, outputs in corpus.values() for address, _ in outputs})


def expected_holdings(genesis, corpus, final_at, tracked):
    """What each address in `tracked` holds as (atoms, coins) once the corpus transactions in `final_at` (txid to
    height) ran in the order of their heights, a parent before what spends it within one cut. Also the transactions
    that spent a coin nobody created or somebody spent before: on a sound chain there are none."""
    coins = dict(genesis)
    by_height = {}
    for txid, height in final_at.items():
        by_height.setdefault(height, []).append(txid)
    unspendable = []
    for height in sorted(by_height):
        waiting = sorted(by_height[height])
        while waiting:
            left = []
            for txid in waiting:
                inputs, outputs = corpus[txid]
                if len(set(inputs)) != len(inputs) or not all(spent in coins for spent in inputs):
                    left.append(txid)
                    continue
                for spent in inputs:
                    del coins[spent]
                for index, coin in enumerate(outputs):
                    coins[(txid, index)] = coin
            if len(left) == len(waiting):
                unspendable += [{"transaction": txid, "height": height} for txid in left]
                break
            waiting = left
    holdings = {address: [0, 0] for address in tracked}
    for address, atoms in coins.values():
        if address in holdings:
            holdings[address][0] += atoms
            holdings[address][1] += 1
    return {address: tuple(value) for address, value in holdings.items()}, unspendable


def compare_holdings(ports, expected, settle_s=90):
    """Every node's balances of the expected addresses, read at one indexed height, against the expectation."""
    height, rows = balances_at_one_height(ports, sorted(expected), settle_s)
    differences = {}
    for name, found in rows.items():
        held = {address: (int(balance), int(utxos)) for address, balance, utxos in found}
        wrong = [{"address": address, "expected": list(map(str, value)),
                  "held": list(map(str, held[address])) if address in held else None}
                 for address, value in sorted(expected.items()) if held.get(address) != value]
        if wrong:
            differences[name] = {"count": len(wrong), "first": wrong[:5]}
    return {"height": height, "differences": differences}


def private_fee(entry, deposit):
    """The fee of one corpus statement from what it moves: an ENTER pays it from the depositor's whole coin, a
    transfer and an EXIT from the sender's notes."""
    moved = {key: int(entry[key]) for key in ("sender", "receiver", "exit_address", "private")}
    if entry["class"] == "enter":
        return deposit - moved["sender"]
    return -(moved["sender"] + moved["receiver"] + moved["exit_address"])


def expected_burn(entries, executed, deposit):
    """What the private statements that ran burned: half of each fee, rounded down per statement."""
    return sum(private_fee(entry, deposit) // PRIVATE_FEE_BURN_DENOMINATOR
               for entry in entries if entry["digest"] in executed)


def economics(ports):
    """Supply, emission and the validator list of every node, each read at one height all of them stood at."""
    return {"supply": same_height(ports, "/v1/economics/supply"),
            "emission": same_height(ports, "/v1/economics/emission"),
            "staking": same_height(ports, "/v1/staking/validators")}


def pinned_issuance(port, tries=40):
    """One node's height, minted atoms and carried work subsidy, all of one height: the issuance read on both sides of
    the supply must name the supply's minted atoms, which every cut changes."""
    for _ in range(tries):
        before = read_or_status(port, "/v1/chain/issuance")
        supply = read_or_status(port, "/v1/economics/supply")
        after = read_or_status(port, "/v1/chain/issuance")
        minted = {before.get("minted_atoms"), supply.get("minted"), after.get("minted_atoms")}
        if len(minted) == 1 and None not in minted:
            return {"height": int(supply["finalized_height"]), "minted": int(supply["minted"]),
                    "carried": int(before["unminted_work_carry_atoms"])}
        time.sleep(0.2)
    raise RuntimeError("the issuance and the supply never named one height")


def subsidies(began, low, last):
    """(stake, work) of every cut from `low` to `last` as the consensus schedules them (economics/state.rs): the
    interval of the reading `began` pays the subsidy it pays now on each of its cuts, the next interval releases
    `next_interval_subsidy`, spread over its cuts with the remainder on the first ones; each cut goes half to work,
    rounded down with a carry. None when the cuts reach past the next interval or before the current one, or the
    current subsidy is odd, so its split carries."""
    boundary = int(began["finalized_height"]) + int(began["cuts_until_reduction"])
    length = int(began["reduction_interval_cuts"])
    if int(began["cut_subsidy"]) % WORK_SHARE[1] or low <= boundary - length or last > boundary + length:
        return None
    current = (int(began["cut_stake_subsidy"]), int(began["cut_work_subsidy"]))
    schedule = {height: current for height in range(low, min(last, boundary) + 1)}
    remaining, carry = int(began["next_interval_subsidy"]), 0
    for index, height in enumerate(range(boundary + 1, last + 1)):
        subsidy = -(-remaining // (length - index))
        remaining -= subsidy
        work, carry = divmod(subsidy * WORK_SHARE[0] + carry, WORK_SHARE[1])
        schedule[height] = (subsidy - work, work)
    return schedule


def expected_minting(start, end, began, window):
    """What the cuts after `start` up to `end` mint by the consensus rules: every cut its stake subsidy, and at every
    height the anchor `window` heights below it closes (daa/controller.rs close_due_anchor; anchors count from 0) with
    the work subsidy of the cut after it, which goes to the carry instead when no work registered for that anchor and
    leaves the carry with the next anchor that has work. Returns the atoms (None when the schedule is not one this
    computes), the anchors closed and the schedule."""
    first, last = max(0, start["height"] + 1 - window), end["height"] - window
    anchors = range(first, last + 1)
    schedule = subsidies(began, min(start["height"] + 1, first + 1), end["height"])
    if schedule is None:
        return None, len(anchors), {}
    stake = sum(schedule[height][0] for height in range(start["height"] + 1, end["height"] + 1))
    work = sum(schedule[anchor + 1][1] for anchor in anchors)
    return stake + work - (end["carried"] - start["carried"]), len(anchors), schedule


def conserved(supply, unbonding):
    """Minted less burned is all the value the node can place, it never mints past the cap, and nothing is
    unbonding but what the run may unbond (`unbonding`, the amounts allowed)."""
    value = {key: int(supply[key]) for key in ("minted", "burned", "total_supply", "max_supply", *SUPPLY_DOMAINS)}
    return (value["minted"] - value["burned"] == sum(value[key] for key in SUPPLY_DOMAINS)
            and value["total_supply"] == value["minted"] - value["burned"]
            and value["minted"] <= value["max_supply"] and value["unbonding"] in unbonding)


def economics_checks(start, end, burn, unbonding=(0,), stake_moves=False):
    """The economics parts of the gate from two readings of every node: `start` before the run sent anything, `end`
    once the chain came to rest. A run that moves stake keeps its bonds checked by its own committee check."""
    supplies, emissions, staking = end["supply"], end["emission"], end["staking"]
    first = next(iter(supplies.values()))
    before = next(iter(start["supply"].values()))
    began, ended = next(iter(start["emission"].values())), next(iter(emissions.values()))
    stake, work = int(began["cut_stake_subsidy"]), int(began["cut_work_subsidy"])
    window = int(start["window"])
    expected, closed, schedule = expected_minting(start["issuance"], end["issuance"], began, window)
    minted = end["issuance"]["minted"] - start["issuance"]["minted"]
    # The node's own emission reading at its height must name what the schedule gives there.
    reported = schedule.get(int(ended["finalized_height"]))
    burned = int(first["burned"]) - int(before["burned"])
    lists = list(staking.values())
    parts = {
        "supply_equal": all(value == first for value in supplies.values())
        and all(value == before for value in start["supply"].values()),
        "emission_equal": all(value == ended for value in emissions.values()),
        "stake_equal": all(value == lists[0] for value in lists),
        "value_conserved": all(conserved(value, unbonding) for value in supplies.values())
        and all(conserved(value, unbonding) for value in start["supply"].values()),
        "burn_as_expected": burned == burn,
        "minting_as_scheduled": expected is not None and minted == expected
        and reported == (int(ended["cut_stake_subsidy"]), int(ended["cut_work_subsidy"])),
        "bonds_listed": all(int(value["bonded"]) == int(listed["total_bonded"])
                            for value, listed in zip(supplies.values(), staking.values())),
    }
    if not stake_moves:
        parts["bonds_unchanged"] = int(first["bonded"]) == int(before["bonded"])
    told = {"start_height": start["issuance"]["height"], "end_height": end["issuance"]["height"],
            "minted": str(minted), "expected_minting": None if expected is None else str(expected),
            "anchors_closed": closed,
            "stake_subsidy": str(stake), "work_subsidy": str(work),
            "carried": [str(start["issuance"]["carried"]), str(end["issuance"]["carried"])],
            "burned": str(burned), "expected_burn": str(burn), "supply": first, "bonded": first["bonded"]}
    return parts, told
