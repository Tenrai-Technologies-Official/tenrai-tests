"""The real history gate precedes publication/native evidence; all IDs and CLIs are fixtures."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import release_evidence as evidence
import release_genesis as release
import release_identity as history
from test_release_identity import A, B, C, GA, GB, GC, activate, encoded, git, public_repository, retire


class ReleaseHistoryGate(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="tenrai-release-history-gate-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.files = public_repository(self.root)
        self.core = self.root / "core"
        (self.core / "config").mkdir(parents=True)
        (self.core / "config/mainnet.chain-id").write_text(B + "\n", encoding="utf-8")
        (self.core / "config/mainnet.traidag.json").write_bytes(b"previous public manifest\n")
        (self.core / "config/mainnet.genesis-id").write_text(GA + "\n", encoding="utf-8")
        self.work = self.root / "work"
        (self.work / "genesis").mkdir(parents=True)
        self.treasury = "trai1" + "q" * 70
        manifest = {"network": "mainnet", "chain_id": B,
                    "allocations": [{"address": self.treasury, "amount": 1}]}
        (self.work / "genesis/mainnet.traidag.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.values = {"network": "mainnet", "chain_id": B, "genesis_id": GB,
                       "params_hash": "c4" * 48, "rules_digest": "d5" * 48}
        self.commands = []
        for owner, name, value in ((release, "REPO", self.core), (release, "FILES", self.files)):
            handle = patch.object(owner, name, value)
            handle.start()
            self.addCleanup(handle.stop)
        handle = patch.object(history, "public_files", return_value=self.files)
        handle.start()
        self.addCleanup(handle.stop)
        self.arguments = SimpleNamespace(work=str(self.work), tenraid="synthetic-node-not-executed",
            wallet="synthetic-wallet-not-executed", treasury=self.treasury, fingerprint="TRAI-1234-5678",
            bonds="400000,300000,300000", private_from_genesis="no", reuse_current_treasury=True)

    def fake_cli(self, command):
        self.commands.append(command)
        if command[0] == self.arguments.tenraid:
            return "".join(f"{key}={value}\n" for key, value in self.values.items())
        self.assertEqual(command, [self.arguments.wallet, "--output", "json", "address-words", self.treasury])
        return json.dumps({"network": "mainnet", "address": self.treasury,
                           "verification_words": "public checksum fixture"}) + "\n"

    def derive(self, runner=None):
        # Existing treasury/activation guards have their own actual unit tests. Only CLI output
        # and those unrelated input fixtures are mocked here; history/Git verification is real.
        with patch.object(release, "check_inputs"), patch.object(release, "run", runner or self.fake_cli), contextlib.redirect_stdout(io.StringIO()):
            release.identity(self.arguments)
        return json.loads((self.work / "genesis/identity.json").read_text(encoding="utf-8"))

    def retire_current(self):
        (self.files / history.LEDGER).write_bytes(encoded([retire(A), activate(B, GB), retire(B), activate(C, GC)]))

    def test_generation_checks_history_before_the_wallet_or_identity_output(self):
        self.retire_current()
        with self.assertRaises(history.IdentityHistoryError):
            self.derive()
        self.assertEqual(len(self.commands), 1, "a rejected identity never invokes the wallet helper")
        self.assertFalse((self.work / "genesis/identity.json").exists())

    def test_history_change_during_public_address_checksum_prevents_the_identity_write(self):
        def changing_cli(command):
            output = self.fake_cli(command)
            if command[0] == self.arguments.wallet:
                self.retire_current()
            return output
        with self.assertRaises(history.IdentityHistoryError):
            self.derive(changing_cli)
        self.assertFalse((self.work / "genesis/identity.json").exists())

    def test_same_chain_manifest_edit_after_native_derivation_cannot_seal_old_gid_to_new_bytes(self):
        def editing_cli(command):
            output = self.fake_cli(command)
            if command[0] == self.arguments.wallet:
                path = self.work / "genesis/mainnet.traidag.json"
                document = json.loads(path.read_text())
                document["allocations"][0]["amount"] = 2
                path.write_text(json.dumps(document), encoding="utf-8")
            return output
        with self.assertRaisesRegex(SystemExit, "changed during native identity derivation"):
            self.derive(editing_cli)
        self.assertFalse((self.work / "genesis/identity.json").exists())
        snapshots = list((self.work / "genesis/identity-inputs").glob("*.json"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(json.loads(snapshots[0].read_text())["allocations"][0]["amount"], 1)
        self.assertEqual(self.commands[0][-1], str(snapshots[0]), "native CLI read the pinned snapshot")

    def test_valid_unique_pair_installs_and_the_same_pair_is_valid_after_committing_the_ledger(self):
        record = self.derive()
        with contextlib.redirect_stdout(io.StringIO()):
            release.install(self.arguments)
        self.assertEqual((self.core / "config/mainnet.genesis-id").read_text().strip(), GB)
        self.assertEqual(json.loads((self.files / history.PUBLISHED).read_text())["chain_id"], B)
        self.assertEqual(record["identity_history"]["sha256"], history.authorize(self.files, B, GB)["sha256"])
        git(self.files, "add", history.LEDGER, history.PUBLISHED)
        git(self.files, "commit", "--quiet", "--no-verify", "-m", "publish synthetic unique identity")
        checked = evidence.identity_inputs(self.work)
        self.assertEqual(checked["identity_history"]["baseline_kind"], "ledger")

    def test_direct_install_without_history_seal_is_refused_before_any_replace(self):
        record = self.derive()
        del record["identity_history"]
        (self.work / "genesis/identity.json").write_text(json.dumps(record), encoding="utf-8")
        with patch.object(release, "replace") as publication:
            with self.assertRaises(evidence.GateError):
                release.install(self.arguments)
            publication.assert_not_called()

    def test_a_previously_valid_but_now_retired_identity_cannot_install(self):
        self.derive()
        previous = (self.core / "config/mainnet.traidag.json").read_bytes()
        self.retire_current()
        with patch.object(release, "replace") as publication:
            with self.assertRaises(evidence.GateError):
                release.install(self.arguments)
            publication.assert_not_called()
        self.assertEqual((self.core / "config/mainnet.traidag.json").read_bytes(), previous)

    def test_install_rechecks_between_files_when_history_changes_during_publication(self):
        self.derive()
        actual = release.replace
        calls = []

        def concurrent_change(work, target, content):
            actual(work, target, content)
            calls.append(target)
            self.retire_current()

        with patch.object(release, "replace", concurrent_change), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises((history.IdentityHistoryError, evidence.GateError)):
                release.install(self.arguments)
        self.assertEqual(len(calls), 1, "further files cannot publish from a stale authorization")
        self.assertEqual(json.loads((self.files / history.PUBLISHED).read_text())["chain_id"], A)

    def test_builtin_invalidates_an_old_pass_before_rejecting_a_retired_identity(self):
        self.derive()
        receipt = self.work / "genesis/built-in.json"
        receipt.write_text(json.dumps({"status": "PASS", "native_exit_code": 0}), encoding="utf-8")
        self.retire_current()
        with self.assertRaises(SystemExit):
            release.built_in(self.arguments)
        refused = json.loads(receipt.read_text())
        self.assertEqual(refused["status"], "INVALID")
        self.assertIn("active public pair", refused["reason"])
        self.assertFalse((self.work / "genesis/built-in.stdout.log").exists(), "no native command starts")

    def test_record_and_manifest_chain_mismatch_never_reaches_publication(self):
        record = self.derive()
        record["chain_id"] = C
        (self.work / "genesis/identity.json").write_text(json.dumps(record), encoding="utf-8")
        with patch.object(release, "replace") as publication:
            with self.assertRaises(evidence.GateError):
                release.install(self.arguments)
            publication.assert_not_called()


if __name__ == "__main__":
    unittest.main()
