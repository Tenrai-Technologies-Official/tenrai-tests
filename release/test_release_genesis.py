"""Release-input isolation: explicit treasury reuse never permits a validator destination."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("release_genesis_under_test", Path(__file__).with_name("release_genesis.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class TreasuryInputs(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="tenrai-release-input-test-")
        root = Path(self.workspace.name)
        self.addCleanup(self.workspace.cleanup)
        repo, files = root / "repo", root / "public"
        (repo / "config").mkdir(parents=True)
        self.treasury = "trai1" + "q" * 70
        self.other = "trai1" + "p" * 70
        self.validator = []
        self.fee = []
        allocations = [{"address": self.treasury, "amount": 1}]
        for index, name in enumerate(release.VALIDATORS):
            folder = files / name
            folder.mkdir(parents=True)
            self.validator.append("trai1" + "acde"[index] * 70)
            self.fee.append("trai1" + "fghj"[index] * 70)
            public = {"wallet_address": self.validator[-1], "heartbeat_fee_address": self.fee[-1]}
            (folder / "public.json").write_text(json.dumps(public), encoding="utf-8")
            allocations.extend({"address": address, "amount": 1} for address in public.values())
        self.manifest = repo / "config/mainnet.traidag.json"
        self.manifest.write_text(json.dumps({"allocations": allocations}), encoding="utf-8")
        self.consensus = root / "consensus.rs"
        self.consensus.write_text(
            "pub const MAINNET: ConsensusParams = ConsensusParams {\n    private_transfers: true,\n};\n"
            "pub const TESTNET: ConsensusParams = ConsensusParams {\n    private_transfers: false,\n    ..MAINNET\n};\n",
            encoding="utf-8")
        for name, value in (("REPO", repo), ("FILES", files), ("CONSENSUS", self.consensus)):
            handle = patch.object(release, name, value)
            handle.start()
            self.addCleanup(handle.stop)

    def arguments(self, address=None, reuse=False):
        return SimpleNamespace(treasury=address or self.treasury, fingerprint="TRAI-1234-5678",
                               bonds="400000,300000,300000", private_from_genesis="yes",
                               reuse_current_treasury=reuse)

    def check(self, arguments):
        with contextlib.redirect_stdout(io.StringIO()):
            release.check_inputs(arguments)

    def test_current_treasury_requires_explicit_reuse(self):
        with self.assertRaisesRegex(SystemExit, "requires --reuse-current-treasury"):
            self.check(self.arguments())
        self.check(self.arguments(reuse=True))

    def test_reuse_cannot_select_another_public_address(self):
        with self.assertRaisesRegex(SystemExit, "requires the current separate treasury"):
            self.check(self.arguments(self.other, reuse=True))
        self.check(self.arguments(self.other))

    def test_every_validator_and_fee_destination_is_rejected_with_or_without_reuse(self):
        for address in self.validator + self.fee:
            for reuse in (False, True):
                with self.subTest(address=address, reuse=reuse), self.assertRaisesRegex(SystemExit, "must differ"):
                    self.check(self.arguments(address, reuse))

    def test_ambiguous_genesis_treasury_fails_closed(self):
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["allocations"].append({"address": self.other, "amount": 1})
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "does not identify one separate treasury"):
            self.check(self.arguments(reuse=True))

    def test_a_retired_treasury_is_refused_even_as_the_current_one(self):
        (Path(release.FILES) / "treasury").mkdir()
        (Path(release.FILES) / "treasury" / "retired.json").write_text(
            json.dumps({"addresses": [{"address": self.treasury}]}), encoding="utf-8")
        for reuse in (False, True):
            with self.subTest(reuse=reuse), self.assertRaisesRegex(SystemExit, "retired address"):
                self.check(self.arguments(reuse=reuse))
        self.check(self.arguments(self.other))

    def test_treasury_reuse_never_bypasses_testnet_activation_guard(self):
        self.consensus.write_text(self.consensus.read_text(encoding="utf-8").replace(
            "    private_transfers: false,", "    private_transfers: true,"), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "runs private transfers on testnet"):
            self.check(self.arguments(reuse=True))


if __name__ == "__main__":
    unittest.main()
