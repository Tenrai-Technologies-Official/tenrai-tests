"""The pure parts of the determinism gate, on chains and ledgers small enough to read."""
import unittest

from bench_determinism import compare_chains, compare_payments, compare_proofs, outcome, ranges, reward_differences
from bench_ledger import (conserved, economics_checks, expected_burn, expected_holdings, expected_minting, payers,
                          private_fee, subsidies)

def chain(top, changed=None):
    table = {height: (f"cut{height}", f"root{height}", "set") for height in range(1, top + 1)}
    table.update(changed or {})
    return table


class ChainTests(unittest.TestCase):
    def test_runs_of_heights(self):
        self.assertEqual(ranges([5, 1, 2, 3, 7, 8]), [[1, 3], [5, 5], [7, 8]])
        self.assertEqual(ranges([]), [])

    def test_the_same_chain_on_every_node_passes(self):
        nodes, conflicts, agreed = compare_chains({"v1": chain(5), "v2": chain(5)}, {"v1": 1, "v2": 1}, 5)
        self.assertEqual(conflicts, [])
        self.assertTrue(all(not node["missing"] for node in nodes.values()))
        self.assertEqual(agreed[5], ("cut5", "root5", "set"))

    def test_two_cuts_at_one_height_are_a_conflict(self):
        _, conflicts, _ = compare_chains({"v1": chain(5), "v2": chain(5, {3: ("other", "root3", "set")})},
                                         {"v1": 1, "v2": 1}, 5)
        self.assertEqual([conflict["height"] for conflict in conflicts], [3])

    def test_a_different_state_root_alone_is_a_conflict(self):
        _, conflicts, _ = compare_chains({"v1": chain(4), "v2": chain(4, {4: ("cut4", "other", "set")})},
                                         {"v1": 1, "v2": 1}, 4)
        self.assertEqual(len(conflicts), 1)

    def test_a_height_a_node_lacks_is_missing_but_not_below_its_floor(self):
        lagging = chain(6)
        del lagging[4]
        joined = {height: value for height, value in chain(6).items() if height >= 3}
        nodes, conflicts, _ = compare_chains({"v1": lagging, "v5": joined}, {"v1": 1, "v5": 3}, 6)
        self.assertEqual(nodes["v1"]["missing"], [[4, 4]])
        self.assertEqual(nodes["v5"]["missing"], [])
        self.assertEqual(conflicts, [])

    def test_a_node_short_of_the_top_is_missing_those_heights(self):
        nodes, _, _ = compare_chains({"v1": chain(6), "v2": chain(4)}, {"v1": 1, "v2": 1}, 6)
        self.assertEqual(nodes["v2"]["missing"], [[5, 6]])

    def test_rewards_differ_only_where_both_report_them(self):
        same = {1: ("10", "5"), 2: (None, None)}
        self.assertEqual(reward_differences({"v1": same, "v2": {1: ("10", "5"), 2: ("1", "1")}}), [])
        found = reward_differences({"v1": same, "v2": {1: ("11", "5")}})
        self.assertEqual([entry["height"] for entry in found], [1])

    def test_proofs_must_cover_every_height_with_the_chain_cuts(self):
        agreed = chain(3)
        good = {"cuts": [[height, *agreed[height]] for height in range(1, 4)]}
        report = compare_proofs({"v1": good}, agreed, {"v1": 1}, 3)
        self.assertEqual((report["v1"]["verified"], report["v1"]["expected"], report["v1"]["different"]), (3, 3, []))
        wrong = {"cuts": [[1, *agreed[1]], [2, "other", "root2", "set"]], "error": "bad proof", "height": 3}
        report = compare_proofs({"v1": wrong}, agreed, {"v1": 1}, 3)
        self.assertEqual((report["v1"]["verified"], report["v1"]["different"]), (2, [2]))
        self.assertEqual((report["v1"]["error"], report["v1"]["error_height"]), ("bad proof", 3))


class PaymentTests(unittest.TestCase):
    def status(self, height=None, cut="cut2", lifecycle="finalized", failure=None):
        return {"lifecycle": lifecycle, "failure": failure,
                "finalized_cut": {"height": height, "cut_id": cut} if height else None}

    def test_outcomes_keep_only_what_every_node_shares(self):
        self.assertEqual(outcome(self.status(2)), ("final", 2, "cut2"))
        self.assertEqual(outcome(None), ("open",))
        self.assertEqual(outcome(self.status(lifecycle="receiving")), ("open",))
        self.assertEqual(outcome(self.status(lifecycle="failed", failure="dropped")), ("open",))
        self.assertEqual(outcome(self.status(2, lifecycle="failed", failure="conflict")), ("failed conflict", 2, "cut2"))

    def test_one_outcome_everywhere_passes(self):
        final = ("final", 2, "cut2")
        agreed, found = compare_payments({"v1": {"a": final}, "v2": {"a": final}}, {"v1": 1, "v2": 1}, chain(3),
                                         {"a": final})
        self.assertEqual(agreed, {"a": final})
        self.assertTrue(all(not value for value in found.values()))

    def test_two_outcomes_are_a_conflict(self):
        _, found = compare_payments({"v1": {"a": ("final", 2, "cut2")}, "v2": {"a": ("final", 3, "cut3")}},
                                    {"v1": 1, "v2": 1}, chain(3), {})
        self.assertEqual(len(found["conflicts"]), 1)

    def test_a_node_without_a_final_payment_lacks_it_unless_it_joined_later(self):
        final = ("final", 2, "cut2")
        _, found = compare_payments({"v1": {"a": final}, "v2": {}, "v5": {}}, {"v1": 1, "v2": 1, "v5": 3},
                                    chain(3), {})
        self.assertEqual(found["lacking"][0]["nodes"], ["v2"])

    def test_a_payment_final_at_a_cut_the_chain_does_not_hold_is_off_the_chain(self):
        _, found = compare_payments({"v1": {"a": ("final", 2, "elsewhere")}}, {"v1": 1}, chain(3), {})
        self.assertEqual(len(found["off_chain"]), 1)

    def test_a_node_that_started_after_a_payment_is_not_compared_for_it(self):
        agreed = chain(512)
        found = {"v1": {"a": ("final", 511, "cut511")}, "v5": {"a": ("failed conflict", 512, "cut512")}}
        result, found = compare_payments(found, {"v1": 1, "v5": 512}, agreed, {"a": ("final", 511, "cut511")})
        self.assertEqual(result, {"a": ("final", 511, "cut511")})
        self.assertTrue(all(not value for value in found.values()), found)
        _, found = compare_payments({"v1": {"b": ("final", 512, "cut512")}, "v5": {}}, {"v1": 1, "v5": 512}, agreed,
                                    {})
        self.assertEqual(found["lacking"][0]["nodes"], ["v5"])

    def test_a_payment_the_run_saw_final_must_stay_there(self):
        _, found = compare_payments({"v1": {"a": ("open",)}}, {"v1": 1}, chain(3), {"a": ("final", 2, "cut2")})
        self.assertEqual(len(found["moved"]), 1)
        _, found = compare_payments({"v1": {"a": ("final", 2, "cut2")}}, {"v1": 1}, chain(3), {"a": ("open",)})
        self.assertEqual(found["moved"], [])


class HoldingsTests(unittest.TestCase):
    def setUp(self):
        self.genesis = {("g", 0): ("alice", 1000), ("g", 1): ("bob", 1000), ("g", 2): ("other", 5)}
        self.corpus = {
            "split": ([("g", 0)], [("alice", 450), ("alice", 450)]),
            "pay": ([("split", 0)], [("bob", 100), ("alice", 250)]),
            "rival": ([("split", 0)], [("bob", 350)]),
        }

    def test_payments_move_exactly_their_outputs_and_fees(self):
        held, unspendable = expected_holdings(self.genesis, self.corpus, {"split": 2, "pay": 3}, payers(self.corpus))
        self.assertEqual(unspendable, [])
        self.assertEqual(held, {"alice": (700, 2), "bob": (1100, 2)})

    def test_a_parent_in_the_same_cut_goes_first(self):
        held, unspendable = expected_holdings(self.genesis, self.corpus, {"split": 3, "pay": 3}, ["alice", "bob"])
        self.assertEqual(unspendable, [])
        self.assertEqual(held["bob"], (1100, 2))

    def test_a_coin_spent_twice_is_unspendable(self):
        _, unspendable = expected_holdings(self.genesis, self.corpus, {"split": 2, "pay": 3, "rival": 4},
                                           ["alice", "bob"])
        self.assertEqual(unspendable, [{"transaction": "rival", "height": 4}])

    def test_a_coin_nobody_created_is_unspendable(self):
        _, unspendable = expected_holdings(self.genesis, self.corpus, {"pay": 3}, ["alice"])
        self.assertEqual(unspendable, [{"transaction": "pay", "height": 3}])


class EconomicsTests(unittest.TestCase):
    def supply(self, height, minted, burned=0, bonded=300, unbonding=0):
        circulating = minted - burned - bonded - unbonding - 100
        return {"finalized_height": height, "max_supply": 10 ** 9, "minted": str(minted), "burned": str(burned),
                "total_supply": str(minted - burned), "circulating_supply": str(circulating), "bonded": str(bonded),
                "unbonding": str(unbonding), "private_supply": "40", "work_reserve": "30", "stake_reserve": "30"}

    def emission(self, height, reduction_at=10 ** 6):
        return {"finalized_height": height, "cut_subsidy": "10", "cut_work_subsidy": "6", "cut_stake_subsidy": "4",
                "cuts_until_reduction": str(reduction_at - height), "reduction_interval_cuts": str(10 ** 6),
                "next_interval_subsidy": "9970000"}

    # Fixture spans a subsidy reduction and the release of carried issuance.
    DAY = {"finalized_height": 486, "cut_subsidy": "105468750000", "cut_work_subsidy": "52734375000",
           "cut_stake_subsidy": "52734375000", "cuts_until_reduction": "26", "reduction_interval_cuts": "512",
           "next_interval_subsidy": "53838000000000"}

    def readings(self, height, minted, burned=0, carried=0, nodes=("v1", "v2")):
        return {"supply": {name: self.supply(height, minted, burned) for name in nodes},
                "emission": {name: self.emission(height) for name in nodes},
                "staking": {name: {"finalized_height": height, "total_bonded": "300"} for name in nodes},
                "issuance": {"height": height, "minted": minted, "carried": carried}, "window": 50}

    def test_private_fees_come_from_what_a_statement_moves(self):
        enter = {"class": "enter", "sender": "990", "receiver": "0", "exit_address": "0", "private": "990"}
        transfer = {"class": "transfer", "sender": "-507", "receiver": "500", "exit_address": "0", "private": "-7"}
        leave = {"class": "exit", "sender": "-300", "receiver": "0", "exit_address": "295", "private": "-300"}
        self.assertEqual([private_fee(entry, 1000) for entry in (enter, transfer, leave)], [10, 7, 5])
        entries = [dict(entry, digest=str(index)) for index, entry in enumerate((enter, transfer, leave))]
        self.assertEqual(expected_burn(entries, {"0", "1", "2"}, 1000), 5 + 3 + 2)
        self.assertEqual(expected_burn(entries, {"1"}, 1000), 3)

    def test_conservation_and_the_allowed_unbonding(self):
        self.assertTrue(conserved(self.supply(5, 1000), (0,)))
        self.assertFalse(conserved(dict(self.supply(5, 1000), total_supply="999"), (0,)))
        self.assertFalse(conserved(self.supply(5, 1000, unbonding=20), (0,)))
        self.assertTrue(conserved(self.supply(5, 1000, unbonding=20), (0, 20)))

    def test_every_cut_mints_its_stake_and_each_closed_anchor_its_work(self):
        at = lambda height, carried=0: {"height": height, "minted": 0, "carried": carried}
        self.assertEqual(expected_minting(at(110), at(210, 12), self.emission(110), 50)[:2],
                         (100 * 4 + 100 * 6 - 12, 100))
        self.assertEqual(expected_minting(at(10), at(40), self.emission(10), 50)[:2], (30 * 4, 0))
        self.assertEqual(expected_minting(at(10), at(110), self.emission(10), 50)[:2], (100 * 4 + 61 * 6, 61))

    def test_a_window_across_an_interval_takes_each_cut_and_anchor_at_its_own_subsidy(self):
        start = {"height": 486, "minted": 0, "carried": 19300781250000}
        end = {"height": 662, "minted": 37830849609375, "carried": 0}
        expected, closed, schedule = expected_minting(start, end, self.DAY, 96)
        self.assertEqual((expected, closed), (37830849609375, 176))
        self.assertEqual(schedule[512], (52734375000, 52734375000))
        self.assertEqual(schedule[513], (52576171875, 52576171875))

    def test_the_next_interval_spreads_its_remainder_over_its_first_cuts_and_splits_with_a_carry(self):
        began = dict(self.emission(9, reduction_at=10), reduction_interval_cuts="4", cut_subsidy="10",
                     next_interval_subsidy="7")
        schedule = subsidies(began, 7, 14)
        self.assertEqual([schedule[height] for height in range(11, 15)], [(1, 1), (1, 1), (1, 1), (1, 0)])
        self.assertIsNone(subsidies(began, 7, 15))
        self.assertIsNone(subsidies(began, 6, 14))
        self.assertIsNone(subsidies(dict(began, cut_subsidy="9"), 7, 14))

    def test_a_run_that_minted_as_scheduled_and_burned_its_fees_passes(self):
        start, end = self.readings(10, 1000), self.readings(110, 1000 + 766, burned=8)
        parts, told = economics_checks(start, end, burn=8)
        self.assertTrue(all(parts.values()), parts)
        self.assertEqual((told["minted"], told["expected_minting"], told["anchors_closed"]), ("766", "766", 61))

    def test_double_or_lost_minting_unequal_supplies_and_a_wrong_burn_fail(self):
        start = self.readings(10, 1000)
        for minted in (1000 + 2 * 766, 1000 + 766 - 6, 1000 + 766 + 1):
            parts, _ = economics_checks(start, self.readings(110, minted), burn=0)
            self.assertFalse(parts["minting_as_scheduled"], minted)
        parts, _ = economics_checks(start, self.readings(110, 1000 + 760, carried=6), burn=0)
        self.assertTrue(parts["minting_as_scheduled"])
        end = self.readings(110, 1766)
        end["supply"]["v2"] = self.supply(110, 1767)
        parts, _ = economics_checks(start, end, burn=0)
        self.assertFalse(parts["supply_equal"])
        parts, _ = economics_checks(start, self.readings(110, 1766, burned=3), burn=0)
        self.assertFalse(parts["burn_as_expected"])

    def test_a_run_across_an_interval_passes_only_with_what_the_next_interval_releases(self):
        start = self.readings(486, 10 ** 16, carried=19300781250000)
        start["emission"] = {name: dict(self.DAY) for name in ("v1", "v2")}
        start["window"] = 96
        end = self.readings(662, 10 ** 16 + 37830849609375)
        end["emission"] = {name: dict(self.DAY, finalized_height=662, cut_subsidy="105152343750",
                                      cut_work_subsidy="52576171875", cut_stake_subsidy="52576171875")
                           for name in ("v1", "v2")}
        for supply in (*start["supply"].values(), *end["supply"].values()):
            supply["max_supply"] = str(2 * 10 ** 16)
        parts, told = economics_checks(start, end, burn=0)
        self.assertTrue(parts["minting_as_scheduled"], told)
        end["issuance"]["minted"] += 1
        self.assertFalse(economics_checks(start, end, burn=0)[0]["minting_as_scheduled"])
        end["issuance"]["minted"] -= 1
        end["emission"]["v1"]["cut_stake_subsidy"] = "52576171874"
        self.assertFalse(economics_checks(start, end, burn=0)[0]["minting_as_scheduled"])

    def test_a_window_past_the_next_interval_is_no_schedule_the_gate_computes(self):
        start = self.readings(10, 1000)
        start["emission"] = {name: dict(self.emission(10, reduction_at=60), reduction_interval_cuts="20")
                             for name in ("v1", "v2")}
        parts, told = economics_checks(start, self.readings(110, 1766), burn=0)
        self.assertFalse(parts["minting_as_scheduled"])
        self.assertIsNone(told["expected_minting"])

    def test_bonds_must_match_the_list_and_stay_put_unless_the_run_moves_them(self):
        end = self.readings(110, 1766)
        end["staking"] = {name: {"finalized_height": 110, "total_bonded": "301"} for name in ("v1", "v2")}
        parts, _ = economics_checks(self.readings(10, 1000), end, burn=0)
        self.assertFalse(parts["bonds_listed"])
        parts, _ = economics_checks(self.readings(10, 1000), self.readings(110, 1766), burn=0, stake_moves=True)
        self.assertNotIn("bonds_unchanged", parts)


if __name__ == "__main__":
    unittest.main()
