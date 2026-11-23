"""Evidence-reader counterexamples with temporary data; not native release qualification."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import records
import release_evidence as evidence
import release_genesis

BASE = {
    ("desktop", "desktop-e2e"), ("checks", "mainnet-identities"), ("checks", "node-suite"), ("checks", "decentralized-finality"),
    ("checks", "finality-simulation"), ("checks", "many-stakers"), ("checks", "private-cross-pool"),
    ("checks", "tenq-production"), ("checks", "crate-tests"),
    ("checks", "clippy"), ("checks", "live-tenrai-reference-pool"), ("checks", "live-tenrai-pool"),
    ("checks", "browser-rounds"), ("checks", "private-long-offline-pruned"), ("checks", "private-long-offline-full"),
    ("checks", "package-join"), ("checks", "package-join-pruned"), ("checks", "full-nodes-alone"),
    ("checks", "three-full-nodes-alone"),
}


class StrictReleaseEvidence(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="tenrai-release-evidence-")
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        (self.work / "genesis").mkdir()
        (self.work / "bin").mkdir()
        (self.work / "config").mkdir()
        (self.work / "logs").mkdir()
        self.values = dict(network="mainnet", chain_id="a" * 96, genesis_id="b" * 96,
                           params_hash="c" * 96, rules_digest="d" * 96)
        public_directory = tempfile.TemporaryDirectory(prefix="tenrai-public-identity-evidence-")
        self.addCleanup(public_directory.cleanup)
        self.public = Path(public_directory.name)
        self.public_genesis = self.public / "genesis"
        self.public_genesis.mkdir()
        self.git("init", "--quiet")
        (self.public_genesis / "identity.json").write_text(json.dumps({
            "network": "mainnet", "chain_id": "e" * 96, "genesis_id": "f" * 96,
        }) + "\n", encoding="utf8", newline="\n")
        self.git("add", "--", "genesis/identity.json")
        self.git("commit", "--quiet", "-m", "Record a public prototype identity fixture")
        self.history = {"format": "tenrai-identity-history-v1", "events": [
            {"type": "retire", "chain_id": "e" * 96},
            {"type": "activate", "chain_id": self.values["chain_id"],
             "genesis_id": self.values["genesis_id"]},
        ]}
        self.write_history()
        public = patch.object(evidence.identity_history, "public_files", return_value=self.public)
        public.start()
        self.addCleanup(public.stop)
        self.put("genesis/mainnet.traidag.json", {"network": "mainnet", "chain_id": self.values["chain_id"]})
        self.identity = dict(self.values, private_transfers_from_genesis=True, private_transfers_from=0,
                             manifest_sha256=evidence.sha(self.work / "genesis/mainnet.traidag.json"),
                             identity_history=evidence.identity_history.authorize(
                                 self.public, self.values["chain_id"], self.values["genesis_id"]))
        self.put("genesis/identity.json", self.identity)
        self.node = self.work / "bin/tenraid.exe"
        self.node.write_bytes(b"data fixture; never executable qualification")
        node_hash = evidence.sha(self.node)
        self.put("bin/manifest.json", {"files": {"tenraid.exe": node_hash}})
        (self.work / "config/mainnet.traidag.json").write_bytes((self.work / "genesis/mainnet.traidag.json").read_bytes())
        for name, key in (("mainnet.chain-id", "chain_id"), ("mainnet.genesis-id", "genesis_id")):
            (self.work / "config" / name).write_text(self.values[key] + "\n")
        self.lines = []
        for phase, name in sorted(BASE | {("checks", "private-from-genesis")}):
            path = self.work / "logs" / (name + ".log")
            path.write_text("fixture output for " + name + "\n")
            self.lines.append("\t".join((phase, name, "0", str(path))))
        self.table()
        self.output = "".join(f"{key}={value}\n" for key, value in self.values.items()).encode()
        (self.work / "genesis/built-in.stdout.log").write_bytes(self.output)
        (self.work / "genesis/built-in.stderr.log").write_bytes(b"")
        self.put("genesis/built-in.preflight.json", {"status": "PASS", "native_exit_code": 0})
        self.receipt = {
            "status": "PASS", "native_exit_code": 0, "checked_at_utc": "2026-11-10T00:00:00+00:00",
            "command": [str(self.node), "--no-color", "genesis", "--network", "mainnet"],
            "values": self.values, "executable_sha256": node_hash,
            "identity_sha256": evidence.sha(self.work / "genesis/identity.json"),
            "manifest_sha256": self.identity["manifest_sha256"],
            "stdout_log": "genesis/built-in.stdout.log", "stdout_sha256": evidence.sha(self.work / "genesis/built-in.stdout.log"),
            "stderr_log": "genesis/built-in.stderr.log", "stderr_sha256": evidence.sha(self.work / "genesis/built-in.stderr.log"),
            "preflight_log": "genesis/built-in.preflight.json", "preflight_sha256": evidence.sha(self.work / "genesis/built-in.preflight.json"),
            "tool_sha256": {name: evidence.sha(evidence.TOOLS / name) for name in
                           ("release_genesis.py", "release_evidence.py", "release_identity.py")},
        }
        self.put("genesis/built-in.json", self.receipt)

    def put(self, name, value):
        evidence.write(self.work / name, value)

    def git(self, *arguments):
        environment = dict(os.environ)
        for key in list(environment):
            if key.upper().startswith("GIT_"):
                environment.pop(key)
        environment["GIT_NO_REPLACE_OBJECTS"] = "1"
        return subprocess.run([
            "git", "--no-replace-objects", "-C", str(self.public),
            "-c", "core.hooksPath=" + str(self.public / "absent-hooks"),
            "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
            "-c", "user.name=Release evidence fixture", "-c", "user.email=release-evidence@invalid",
            *arguments,
        ], check=True, capture_output=True, text=True, env=environment)

    def write_history(self):
        (self.public_genesis / "identity-history.json").write_text(
            json.dumps(self.history, indent=2) + "\n", encoding="utf8", newline="\n")

    def table(self):
        (self.work / "checks.tsv").write_text("\n".join(self.lines) + ("\n" if self.lines else ""))

    def qualify(self):
        return evidence.qualify(self.work, self.work)

    def test_required_set_matches_actual_driver_and_private_choice(self):
        inactive, _ = evidence.required_checks(False)
        active, _ = evidence.required_checks(True)
        self.assertEqual(set(inactive), BASE)
        self.assertEqual(set(active), BASE | {("checks", "private-from-genesis")})
        result = self.qualify()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["mandatory_checks"]), len(BASE) + 1)

    def test_empty_or_missing_private_or_desktop_check_never_qualifies(self):
        original = list(self.lines)
        for omitted in (None, "private-from-genesis", "desktop-e2e"):
            with self.subTest(omitted=omitted):
                self.lines = [] if omitted is None else [line for line in original if line.split("\t")[1] != omitted]
                self.table()
                with self.assertRaises(evidence.GateError):
                    self.qualify()

    def test_duplicate_check_and_reused_log_are_invalid(self):
        original = list(self.lines)
        self.lines.append(self.lines[0])
        self.table()
        with self.assertRaises(evidence.GateError):
            self.qualify()
        self.lines = list(original)
        first, second = self.lines[0].split("\t"), self.lines[1].split("\t")
        second[3] = first[3]
        self.lines[1] = "\t".join(second)
        self.table()
        with self.assertRaises(evidence.GateError):
            self.qualify()

    def test_missing_or_empty_log_is_not_a_pass(self):
        path = Path(self.lines[0].split("\t")[3])
        path.write_bytes(b"")
        with self.assertRaises(evidence.GateError):
            self.qualify()
        path.unlink()
        with self.assertRaises(evidence.GateError):
            self.qualify()

    def test_failing_command_is_fail_even_with_complete_coverage(self):
        row = self.lines[0].split("\t")
        row[2] = "37"
        self.lines[0] = "\t".join(row)
        self.table()
        with self.assertRaises(evidence.GateError) as caught:
            self.qualify()
        self.assertEqual(caught.exception.status, "FAIL")

    def test_library_values_without_a_builtin_receipt_do_not_qualify(self):
        (self.work / "genesis/built-in.json").unlink()
        with self.assertRaises(evidence.GateError) as caught:
            self.qualify()
        self.assertEqual(caught.exception.status, "NOT TESTED")

    def test_binary_identity_and_output_tampering_invalidate_receipt(self):
        self.node.write_bytes(self.node.read_bytes() + b"changed")
        with self.assertRaises(evidence.GateError):
            self.qualify()
        self.node.write_bytes(b"data fixture; never executable qualification")
        self.put("genesis/identity.json", dict(self.identity, new_public_note="changed after native run"))
        with self.assertRaises(evidence.GateError):
            self.qualify()
        self.put("genesis/identity.json", self.identity)
        (self.work / "genesis/built-in.stdout.log").write_bytes(self.output + b"changed")
        with self.assertRaises(evidence.GateError):
            self.qualify()

    def test_cli_requires_mainnet_rules_digest_and_unique_fields(self):
        for output in (self.output.replace(b"mainnet", b"testnet"),
                       b"\n".join(line for line in self.output.splitlines() if not line.startswith(b"rules_digest=")),
                       self.output + b"rules_digest=" + b"d" * 96 + b"\n"):
            with self.subTest(output=output), self.assertRaises(evidence.GateError):
                evidence.cli_values(output.decode())

    def test_retired_public_history_invalidates_an_otherwise_complete_qualification(self):
        self.assertEqual(self.qualify()["status"], "PASS")
        fresh_chain, fresh_genesis = "2" * 96, "3" * 96
        self.history["events"].extend([
            {"type": "retire", "chain_id": self.values["chain_id"]},
            {"type": "activate", "chain_id": fresh_chain, "genesis_id": fresh_genesis},
        ])
        self.write_history()
        (self.public_genesis / "identity.json").write_text(json.dumps({
            "network": "mainnet", "chain_id": fresh_chain, "genesis_id": fresh_genesis,
        }) + "\n", encoding="utf8", newline="\n")
        self.git("add", "--", "genesis/identity-history.json", "genesis/identity.json")
        self.git("commit", "--quiet", "-m", "Retire the earlier release fixture identity")
        with self.assertRaises(evidence.GateError):
            self.qualify()

        # Even a current genuine seal cannot authorize the retired pair in the old work
        # directory. All other identity/binary/native-output fixture bindings still match.
        self.identity["identity_history"] = evidence.identity_history.authorize(
            self.public, fresh_chain, fresh_genesis)
        self.put("genesis/identity.json", self.identity)
        self.receipt["identity_sha256"] = evidence.sha(self.work / "genesis/identity.json")
        self.put("genesis/built-in.json", self.receipt)
        with self.assertRaises(evidence.GateError):
            self.qualify()

    def test_an_identity_without_its_public_history_seal_cannot_qualify(self):
        missing_seal = dict(self.identity)
        missing_seal.pop("identity_history")
        self.put("genesis/identity.json", missing_seal)
        self.receipt["identity_sha256"] = evidence.sha(self.work / "genesis/identity.json")
        self.put("genesis/built-in.json", self.receipt)
        with self.assertRaises(evidence.GateError):
            self.qualify()

    def test_native_failure_invalidates_an_earlier_builtin_pass_and_keeps_raw_exit(self):
        tools = self.work / "fixture-tools"
        producer = tools / "release/release_genesis.py"
        sources = [producer, producer.with_name("release_evidence.py"), producer.with_name("release_identity.py"),
                   tools / "qualification/envcheck.py",
                   tools / "p2p-bench/preflight.py", tools / "p2p-bench/bench_proc.py"]
        for path in sources:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture source; not executed\n")
        preflight = SimpleNamespace(returncode=0, stdout=b"fixture preflight\n", stderr=b"")
        native = SimpleNamespace(returncode=37, stdout=self.output, stderr=b"fixture native failure\n")
        arguments = SimpleNamespace(work=str(self.work), tenraid=str(self.node), holder="reserved-release-slot")
        real_run = subprocess.run

        def fixture_run(command, *args, **kwargs):
            if str(command[0]) == str(self.node):
                return native
            if str(tools / "qualification/envcheck.py") in [str(part) for part in command]:
                return preflight
            return real_run(command, *args, **kwargs)

        with patch.object(release_genesis, "__file__", str(producer)), \
                patch.object(release_genesis.subprocess, "run", side_effect=fixture_run) as spawned:
            with self.assertRaises(SystemExit):
                release_genesis.built_in(arguments)
        receipt = evidence.load(self.work / "genesis/built-in.json")
        self.assertEqual(receipt["status"], "FAIL")
        self.assertEqual(receipt["native_exit_code"], 37)
        command = next(call.args[0] for call in spawned.call_args_list
                       if str(tools / "qualification/envcheck.py") in [str(part) for part in call.args[0]])
        self.assertEqual(command[-2:], ["--holder", "reserved-release-slot"])
        self.assertNotIn("--allow", command)

    def test_raw_evidence_cannot_be_written_into_a_source_repository(self):
        repo = self.work / "source-repo"
        (repo / ".git").mkdir(parents=True)
        with self.assertRaises(evidence.GateError):
            evidence.write(repo / "RELEASE.json", {"status": "PASS"})
        self.assertFalse((repo / "RELEASE.json").exists())

    def test_failed_record_generation_cannot_leave_an_earlier_pass(self):
        self.put("RELEASE.json", {"status": "PASS", "qualification_status": "PASS"})
        self.lines = []
        self.table()
        with self.assertRaises(SystemExit):
            records.release_record(SimpleNamespace(work=str(self.work), out=str(self.work)))
        self.assertNotEqual(evidence.load(self.work / "RELEASE.json")["status"], "PASS")

    def test_begin_attempt_preserves_success_before_any_resumed_phase(self):
        for phase in ("binaries", "web", "records"):
            with self.subTest(phase=phase):
                self.put("RELEASE.json", {"status": "PASS", "qualification_status": "PASS"})
                (self.work / "README.md").write_bytes(b"# Previous release\n\nStatus: PASS.\n")
                names = ("RELEASE.json", "README.md", "checks.tsv", "genesis/built-in.json")
                previous = {name: (self.work / name).read_bytes() for name in names}
                history = records.begin_attempt(SimpleNamespace(work=str(self.work), phase=phase))
                self.assertIsNotNone(history)
                for name, exact_bytes in previous.items():
                    self.assertEqual((history / name).read_bytes(), exact_bytes)
                self.assertEqual(evidence.load(self.work / "RELEASE.json")["status"], "NOT TESTED")
                self.assertIn("Status: NOT TESTED.", (self.work / "README.md").read_text())
                self.assertFalse(evidence.load(self.work / "RELEASE.json")["publication_authorized"])
                archive = evidence.load(history / "archive.json")
                self.assertEqual(archive["resumed_phase"], phase)
                for name in names:
                    self.assertEqual(archive["files_sha256"][name], evidence.sha(history / name))

    def test_begin_attempt_refuses_repository_output_before_changing_records(self):
        repo = self.work / "source-repo"
        (repo / ".git").mkdir(parents=True)
        previous = b'{"status":"PASS"}\n'
        (repo / "RELEASE.json").write_bytes(previous)
        with self.assertRaises(evidence.GateError):
            records.begin_attempt(SimpleNamespace(work=str(repo), phase="web"))
        self.assertEqual((repo / "RELEASE.json").read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
