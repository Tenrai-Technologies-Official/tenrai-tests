"""Ordinary local receipt counterexamples; no native program is executed by these tests."""
from argparse import Namespace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import records
import release_evidence as evidence
import release_runtime as runtime


class CommandSourceBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tenrai-release-boundary-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "work"
        self.repo = self.root / "public-source"
        self.work.mkdir()
        self.repo.mkdir()
        (self.repo / "source.rs").write_text("const VALUE: u8 = 1;\n")
        self.guard = self.root / "tools/qualification/envcheck.py"
        for path in (self.guard, self.root / "tools/p2p-bench/preflight.py",
                     self.root / "tools/p2p-bench/bench_proc.py"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# data fixture; never imported/executed\n")
        (self.guard.parent / "stacks.py").write_text("# original controller fixture\n")
        self.admission_code = 0
        self.commands = []
        self.mock = patch.object(runtime.subprocess, "run", side_effect=self.call)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def call(self, command, **kwargs):
        if command[0] == "rg":
            folder = Path(kwargs["cwd"])
            names = sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())
            return SimpleNamespace(returncode=0, stdout=("\0".join(names) + "\0").encode(), stderr=b"")
        self.commands.append(command)
        return SimpleNamespace(returncode=self.admission_code, stdout=b"fixture preflight\n", stderr=b"")

    def begin(self, name="compile", kind="build", allow=(), frozen=()):
        arguments = Namespace(work=str(self.work), name=name, repo=[str(self.repo)],
                              kind=kind, allow_change=list(allow), envcheck=str(self.guard),
                              holder="ordinary-unit-fixture", commit_gb=8, room=[], file=[],
                              frozen=list(frozen))
        output = io.StringIO()
        with redirect_stdout(output):
            code = runtime.begin(arguments)
        tickets = sorted((self.work / "source-boundaries").glob("*/before.json"))
        return code, tickets[-1]

    def end(self, ticket, code=0, artifacts=(), trees=()):
        arguments = Namespace(work=str(self.work), ticket=str(ticket), exit_code=code,
                              artifact=list(artifacts), artifact_tree=list(trees))
        with redirect_stdout(io.StringIO()):
            return runtime.end(arguments)

    def verify(self, name="compile"):
        return runtime.verify(Namespace(work=str(self.work), require=[name]))

    def complete(self, name="compile"):
        code, ticket = self.begin(name)
        self.assertEqual(code, 0)
        self.assertEqual(self.end(ticket), 0)
        return ticket

    def test_resource_refusal_records_invalid_and_cannot_qualify(self):
        self.admission_code = 3
        code, ticket = self.begin()
        self.assertEqual(code, 3)
        self.assertEqual(evidence.load(ticket)["status"], "INVALID")
        self.assertEqual(len(self.commands), 1)
        self.assertIn("--holder", self.commands[0])
        self.assertNotIn("--allow", self.commands[0])
        with self.assertRaises(evidence.GateError):
            self.verify()

    def test_changed_qualification_helper_invalidates_an_otherwise_unchanged_check(self):
        ticket = self.complete()
        (self.guard.parent / "stacks.py").write_text("# changed controller fixture\n")
        with self.assertRaisesRegex(evidence.GateError, "qualification controls changed"):
            self.verify()
        self.assertEqual(evidence.load(ticket.with_name("after.json"))["status"], "PASS")
        self.assertNotEqual(evidence.load(self.work / "COMMAND-SOURCES.json")["status"], "PASS")

    def test_added_p2p_helper_invalidates_control_membership_after_a_pass(self):
        ticket = self.complete()
        (self.guard.parents[1] / "p2p-bench/new_helper.py").write_text("# newly added controller fixture\n")
        with self.assertRaisesRegex(evidence.GateError, "qualification controls changed"):
            self.verify()
        self.assertEqual(evidence.load(ticket.with_name("after.json"))["status"], "PASS")
        self.assertNotEqual(evidence.load(self.work / "COMMAND-SOURCES.json")["status"], "PASS")

    def test_a_helper_changed_during_the_command_records_invalid_despite_native_zero(self):
        code, ticket = self.begin()
        self.assertEqual(code, 0)
        (self.guard.parent / "stacks.py").write_text("# changed while command runs\n")
        self.assertEqual(self.end(ticket), 2)
        after = evidence.load(ticket.with_name("after.json"))
        self.assertEqual(after["status"], "INVALID")
        self.assertIn("qualification controls changed", after["reason"])

    def test_native_failure_preserves_its_code_and_never_becomes_pass(self):
        code, ticket = self.begin()
        self.assertEqual(code, 0)
        self.assertEqual(self.end(ticket, 37), 37)
        result = evidence.load(ticket.with_name("after.json"))
        self.assertEqual(result["native_exit_code"], 37)
        self.assertEqual(result["status"], "FAIL")
        with self.assertRaises(evidence.GateError):
            self.verify()

    def test_source_bytes_and_membership_changes_refuse_native_success(self):
        for added in (False, True):
            with self.subTest(added=added):
                # A new candidate is required after an unexplained source change.
                self.work = self.root / ("work-added" if added else "work-bytes")
                self.work.mkdir()
                code, ticket = self.begin()
                self.assertEqual(code, 0)
                changed = self.repo / ("added.rs" if added else "source.rs")
                changed.write_text("different public input\n")
                self.assertEqual(self.end(ticket), 2)
                self.assertEqual(evidence.load(ticket.with_name("after.json"))["status"], "INVALID")
                if added:
                    changed.unlink()

    def test_unexplained_between_step_change_is_refused_before_another_admission(self):
        self.complete()
        count = len(self.commands)
        (self.repo / "source.rs").write_text("unrecorded change\n")
        code, ticket = self.begin("next")
        self.assertEqual(code, 3)
        self.assertEqual(len(self.commands), count)
        self.assertIn("between commands", evidence.load(ticket)["reason"])

    def test_explicit_mutation_requires_a_new_build_before_final_qualification(self):
        self.complete()
        code, ticket = self.begin("document-update", "mutation", [str(self.repo) + "=source.rs"])
        self.assertEqual(code, 0)
        (self.repo / "source.rs").write_text("recorded new input\n")
        self.assertEqual(self.end(ticket), 0)
        with self.assertRaises(evidence.GateError):
            self.verify()
        self.assertNotEqual(evidence.load(self.work / "COMMAND-SOURCES.json")["status"], "PASS")
        self.complete()
        self.assertGreater(self.verify()["commands"], 1)

    def test_mutation_cannot_change_an_unlisted_input(self):
        code, ticket = self.begin("pins", "mutation", [str(self.repo) + "=allowed.json"])
        self.assertEqual(code, 0)
        (self.repo / "source.rs").write_text("outside the allowed output\n")
        self.assertEqual(self.end(ticket), 2)
        self.assertIn("outside", evidence.load(ticket.with_name("after.json"))["reason"])

    def test_changed_output_tree_membership_and_frozen_binary_are_refused(self):
        outputs = self.work / "published"
        outputs.mkdir()
        (outputs / "app.js").write_text("public fixture output\n")
        frozen = self.work / "frozen"
        frozen.mkdir()
        executable = frozen / "node.exe"
        executable.write_bytes(b"data fixture; never executed")
        code, ticket = self.begin(frozen=[str(frozen)])
        self.assertEqual(code, 0)
        self.assertEqual(self.end(ticket, trees=[str(outputs)]), 0)
        self.verify()
        injected = outputs / "extra.js"
        injected.write_text("unexpected published entry\n")
        with self.assertRaises(evidence.GateError):
            self.verify()
        injected.unlink()
        executable.write_bytes(b"changed frozen fixture")
        with self.assertRaises(evidence.GateError):
            self.verify()

    def test_release_event_order_does_not_follow_wall_clock(self):
        with patch.object(runtime.time, "time_ns", return_value=-1):
            first = runtime.sequence_event(self.work)
            second = runtime.sequence_event(self.work)
        self.assertLess(first, second)

    def test_make_tokens_preserve_windows_separators_and_unescape_spaces(self):
        root = PureWindowsPath(self.root)
        paths = [str(root / "node checkout" / "src" / "lib.rs"), str(root / "second.rs")]
        encoded = " ".join("".join(f"\\{char}" if char.isspace() else char for char in path) for path in paths)
        self.assertEqual(runtime.make_words(encoded), paths)
        doubled = paths[1].removesuffix("second.rs") + r"one\\two.rs"
        escaped = "".join(f"\\{char}" if char.isspace() else char for char in doubled)
        self.assertEqual(runtime.make_words(escaped), [doubled])

    def test_archive_refuses_foreign_root_and_receipts_and_preserves_owned_artifacts(self):
        for path in (self.root, self.work, self.work / "source-boundaries"):
            with self.subTest(path=path), self.assertRaises(evidence.GateError):
                runtime.archive_owned(Namespace(work=str(self.work), path=str(path)))
        old = self.work / "old-artifact"
        old.mkdir()
        (old / "public.txt").write_text("preserved\n")
        self.assertEqual(runtime.archive_owned(Namespace(work=str(self.work), path=str(old))), 0)
        kept = list((self.work / "evidence-history").glob("*/old-artifact/public.txt"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(), "preserved\n")

    def test_direct_record_generation_cannot_bypass_missing_source_evidence(self):
        arguments = Namespace(work=str(self.work), out=str(self.work))
        with patch.object(records.evidence, "qualify", return_value={"status": "PASS"}), \
             patch.object(records.runtime, "require_final", side_effect=evidence.GateError("missing source boundary")):
            with self.assertRaises(SystemExit):
                records.release_record(arguments)
        self.assertNotEqual(evidence.load(self.work / "RELEASE.json")["status"], "PASS")

    def test_required_child_cannot_be_replaced_by_an_unrelated_success(self):
        self.complete("wasm-wallet-build")
        self.complete("unrelated-build")
        with self.assertRaises(evidence.GateError):
            runtime.verify(Namespace(work=str(self.work),
                                     require=["wasm-wallet-build", "wasm-tenq-build"]))

    def test_cargo_local_include_must_belong_to_the_recorded_before_inputs(self):
        target = self.work / "scratch/target/release/fixture.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"compiler artifact fixture; never executable")
        source = self.repo / "source.rs"
        escaped = lambda value: str(value).replace(" ", chr(92) + " ")
        target.with_suffix(".d").write_text(escaped(target) + ": " + escaped(source) + "\n")
        saved = {"work": str(self.work),
                 "sources_before": {str(self.repo): {"files": {"source.rs": evidence.sha(source)}}}}
        checked = runtime.rust_inputs(target, saved)
        self.assertEqual(checked["local_inputs_sha256"], {str(source.resolve()): evidence.sha(source)})
        # Missing build-script watch paths do not count as inputs.
        absent = [self.repo / "capabilities", self.repo / "prover/x86_64-pc-windows-msvc/tool.exe"]
        target.with_suffix(".d").write_text(escaped(target) + ": " + " ".join(escaped(path) for path in [source, *absent]) + "\n")
        checked = runtime.rust_inputs(target, saved)
        self.assertEqual(checked["local_inputs_sha256"], {str(source.resolve()): evidence.sha(source)})
        # Resolve relative paths from the build workspace.
        target.with_suffix(".d").write_text(escaped(target) + ": " + escaped(source.name) + " missing.rs\n")
        checked = runtime.rust_inputs(target, saved)
        self.assertEqual(checked["local_inputs_sha256"], {str(source.resolve()): evidence.sha(source)})
        unlisted = self.repo / "unlisted.rs"
        unlisted.write_text("not in the compiled-input boundary\n")
        for named in (unlisted, unlisted.name):
            target.with_suffix(".d").write_text(escaped(target) + ": " + escaped(named) + "\n")
            with self.assertRaises(evidence.GateError):
                runtime.rust_inputs(target, saved)

    def test_published_binary_hashes_must_match_the_actual_compile_receipts(self):
        names = ["tenraid.exe", "tenrai-release.exe", "tenrai-wallet.exe", "tenrai-miner.exe",
                 "tenrai-stratum-bridge.exe", "tenrai-reference-pool.exe", "tenrai-pool.exe",
                 "tenrai-explorer.exe", "tenrai-api.exe", "tenrai-tsp2-prove.exe"]
        jobs = ["binaries-workspace"] * 5 + ["binaries-tenrai-reference-pool", "binaries-tenrai-pool",
                "binaries-tenrai-explorer", "binaries-tenrai-api", "binaries-tenrai-tsp2-prove"]
        compiled = self.work / "scratch/compiled"
        compiled.mkdir(parents=True)
        (self.work / "bin").mkdir()
        (self.work / "desktop").mkdir()
        by_name = {}
        published = {}
        for name, job in zip(names, jobs):
            source = compiled / name
            source.write_bytes(("ordinary fixture: " + name).encode())
            output = self.work / "bin" / name
            output.write_bytes(source.read_bytes())
            published[name] = evidence.sha(output)
            by_name.setdefault(job, ("1", None, {}, {"artifacts_sha256": {}, "production_profiles": {"fixture": True}}))[3]["artifacts_sha256"][str(source)] = evidence.sha(source)
        desktop = self.work / "desktop/desktop-v1.exe"
        desktop.write_bytes(b"ordinary desktop fixture; never executed")
        by_name["desktop-release-build"] = ("1", None, {}, {"artifacts_sha256": {str(compiled / "desktop-release.exe"): evidence.sha(desktop)}})
        evidence.write(self.work / "desktop/manifest.json", {"files": {desktop.name: evidence.sha(desktop)}})
        evidence.write(self.work / "bin/manifest.json", {"files": published})
        self.assertEqual(runtime.published_artifacts(self.work, by_name)["binaries"], published)
        # A self-consistent new frozen manifest is still insufficient without a matching compile.
        (self.work / "bin/tenraid.exe").write_bytes(b"another internally consistent published artifact")
        changed = dict(published, **{"tenraid.exe": evidence.sha(self.work / "bin/tenraid.exe")})
        evidence.write(self.work / "bin/manifest.json", {"files": changed})
        with self.assertRaises(evidence.GateError):
            runtime.published_artifacts(self.work, by_name)

    def test_cargo_test_profile_or_test_support_stream_cannot_publish_as_production(self):
        target = self.work / "scratch/target/release/fixture.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"ordinary artifact fixture; never executed")
        source = self.repo / "source.rs"
        escaped = lambda value: str(value).replace(" ", chr(92) + " ")
        target.with_suffix(".d").write_text(escaped(target) + ": " + escaped(source) + "\n")
        info = {target: {"sources": {str(self.repo): {"files": {"source.rs": evidence.sha(source)}}}}}
        message = {"reason": "compiler-artifact", "package_id": "path+file:///ordinary-fixture",
                   "features": [], "target": {"kind": ["bin"], "name": "fixture"},
                   "filenames": [str(target)],
                   "profile": {"test": False, "debug_assertions": False, "opt_level": "3"}}
        log = self.work / "cargo.log"
        log.write_text(json.dumps(message) + "\n")
        self.assertEqual(runtime.production_profiles(log, info)["artifacts"][str(target)]["target"], "fixture")
        for property, value in (("test", True), ("debug_assertions", True), ("opt_level", "0")):
            with self.subTest(property=property):
                changed = dict(message, profile=dict(message["profile"], **{property: value}))
                log.write_text(json.dumps(changed) + "\n")
                with self.assertRaises(evidence.GateError):
                    runtime.production_profiles(log, info)
        log.write_text(json.dumps(dict(message, features=["test-support"])) + "\n")
        with self.assertRaises(evidence.GateError):
            runtime.production_profiles(log, info)


if __name__ == "__main__":
    unittest.main()
