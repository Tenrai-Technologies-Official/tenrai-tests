"""Pure negative controls for the monetary oracle, separate from live acceptance."""
import hashlib
import unittest

from bench_crash_money import amount, effects, payment


class MonetaryOracleTests(unittest.TestCase):
    def setUp(self):
        self.initial = {"height": 1, "wallets": {"a": {"balance": 10, "utxos": 1},
                                                 "b": {"balance": 0, "utxos": 0}}}
        self.wires = {"tx": {hashlib.sha256(b"\x00").hexdigest()}}

    def view(self, txid="tx", parent="coin", fee=1):
        return {"transaction_id": txid, "wire_hex": "00", "kind": "payment", "operation": None,
                "inputs": [{"transaction_id": parent, "output_index": 0,
                            "spends": {"address": "a", "amount": "10"}}],
                "outputs": [{"index": 0, "address": "a", "amount": "5"},
                            {"index": 1, "address": "b", "amount": "4"}],
                "input_total": "10", "output_total": "9", "fee": str(fee), "block_id": "block",
                "status": {"lifecycle": "finalized", "finalized_cut": {
                    "height": 2, "cut_id": "cut", "state_root": "root", "validator_set_id": "set"}}}

    def test_one_payment_has_exact_value_fee_and_utxo_count_effects(self):
        row = payment(self.view(), self.wires, self.initial["height"])
        found = effects([row], self.initial)
        self.assertEqual(found["expected"], {"a": {"balance": 5, "utxos": 1},
                                             "b": {"balance": 4, "utxos": 1}})
        self.assertEqual((found["fees"], found["inputs"]), (1, 1))

    def test_a_repeated_input_under_distinct_transaction_ids_is_refused(self):
        first = payment(self.view(), self.wires, 1)
        second = dict(first, transaction_id="rival")
        with self.assertRaisesRegex(ValueError, "input applied twice"):
            effects([first, second], self.initial)

    def test_wrong_fee_and_unavailable_input_history_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "fee is not exact"):
            payment(self.view(fee=2), self.wires, 1)
        missing = self.view()
        missing["inputs"][0]["spends"] = None
        with self.assertRaisesRegex(ValueError, "history is unavailable"):
            payment(missing, self.wires, 1)

    def test_an_old_corpus_or_different_signed_bytes_cannot_qualify(self):
        with self.assertRaisesRegex(ValueError, "already final before"):
            payment(self.view(), self.wires, 2)
        changed = self.view()
        changed["wire_hex"] = "01"
        with self.assertRaisesRegex(ValueError, "different signed bytes"):
            payment(changed, self.wires, 1)

    def test_atom_amounts_are_canonical_exact_u128_values(self):
        self.assertEqual(amount(str(2 ** 128 - 1)), 2 ** 128 - 1)
        for value in (None, True, 1, 1.5, "-1", "1.0", "01", " 1", str(2 ** 128)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                amount(value)

    def test_zero_applied_payments_cannot_pass(self):
        with self.assertRaisesRegex(ValueError, "no applied fee-paying payment"):
            effects([], self.initial)


if __name__ == "__main__":
    unittest.main()
