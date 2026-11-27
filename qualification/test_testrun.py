"""Qualification bookkeeping tests with mocked catalogs; no native test binary is started."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import testrun

FIRST = "chain::private_tests::validators::cross_pool::first"
SECOND = "chain::private_tests::validators::cross_pool::second"


def settings():
    return testrun.options(["--min-tests", "2", "--expect", FIRST, "--expect", SECOND])


def summary(passed=2, failed=0, ignored=0, status="ok"):
    return (f"test result: {status}. {passed} passed; {failed} failed; {ignored} ignored; "
            "0 measured; 0 filtered out; finished in 0.01s\n")


class EffectiveSelection(unittest.TestCase):
    def test_catalog_uses_execution_flags_and_rejects_a_skipped_required_case(self):
        with tempfile.TemporaryDirectory(prefix="tenrai-testrun-unit-") as temporary:
            store = Path(temporary)
            (store / "case.exe").write_bytes(b"data fixture, never executable")
            (store / "case.json").write_text('{"sha256":"fixed"}')
            configured = settings()
            configured["extra"] = ["--skip", FIRST]
            calls = []

            def catalog(command, **_kwargs):
                calls.append(command)
                # Removing --skip in the implementation makes this fixture expose both names.
                names = [SECOND] if "--skip" in command else [FIRST, SECOND]
                return SimpleNamespace(stdout="".join(name + ": test\n" for name in names), returncode=0)

            with patch.object(testrun, "STORE", store), \
                    patch.object(testrun.subprocess, "run", side_effect=catalog), \
                    patch.object(testrun.preflight, "sha256", return_value="fixed"), \
                    patch.object(testrun.os, "access", return_value=False), \
                    patch.object(testrun.preflight, "memory", return_value={"free_commit_gb": 16}), \
                    patch.object(testrun.preflight, "competing", return_value=[]):
                record, reasons = testrun.check("case", "cross_pool", configured)
            self.assertEqual(record["tests"], 1)
            self.assertTrue(any(FIRST in reason for reason in reasons))
            self.assertEqual(calls, [[str(store / "case.exe"),
                                      *testrun.test_arguments("cross_pool", configured), "--list"]])

    def test_exact_and_ignored_options_are_identical_in_catalog_and_run_arguments(self):
        configured = settings()
        configured["extra"] = ["--exact", FIRST]
        configured["ignored"] = False
        with patch.object(testrun.subprocess, "run", return_value=SimpleNamespace(
                stdout=FIRST + ": test\n", returncode=0)) as catalog:
            names, code = testrun.listed(Path("fixture.exe"), FIRST, {}, configured)
        self.assertEqual((names, code), ([FIRST], 0))
        self.assertEqual(catalog.call_args.args[0], ["fixture.exe", *testrun.test_arguments(FIRST, configured), "--list"])
        self.assertNotIn("--include-ignored", catalog.call_args.args[0])
        self.assertIn("--exact", catalog.call_args.args[0])
        self.assertEqual(catalog.call_args.args[0][catalog.call_args.args[0].index("--exact") + 1], FIRST)


class CatalogAndSmoke(unittest.TestCase):
    def checked(self, catalog_output, catalog_exit=0, smoke_output=None):
        with tempfile.TemporaryDirectory(prefix="tenrai-testrun-check-") as temporary:
            store = Path(temporary)
            (store / "case.exe").write_bytes(b"data fixture, never executable")
            (store / "case.json").write_text('{"sha256":"fixed"}')
            configured = testrun.options([])
            configured["smoke"] = FIRST if smoke_output is not None else None
            calls = []

            def mocked(command, **_kwargs):
                calls.append(command)
                if "--list" in command:
                    return SimpleNamespace(stdout=catalog_output, returncode=catalog_exit)
                return SimpleNamespace(stdout=smoke_output, returncode=0)

            with patch.object(testrun, "STORE", store), \
                    patch.object(testrun.subprocess, "run", side_effect=mocked), \
                    patch.object(testrun.preflight, "sha256", return_value="fixed"), \
                    patch.object(testrun.os, "access", return_value=False), \
                    patch.object(testrun.preflight, "memory", return_value={"free_commit_gb": 16}), \
                    patch.object(testrun.preflight, "competing", return_value=[]):
                record, reasons = testrun.check("case", "cross_pool", configured)
            return record, reasons, calls

    def test_nonzero_catalog_exit_is_rejected_even_with_complete_names(self):
        record, reasons, _ = self.checked(FIRST + ": test\n", catalog_exit=9)
        self.assertEqual(record["tests"], 1)
        self.assertEqual(record["catalog_exit"], 9)
        self.assertTrue(any("catalog exited 9" in reason for reason in reasons))

    def test_duplicate_catalog_names_are_rejected_even_above_the_minimum(self):
        record, reasons, _ = self.checked((FIRST + ": test\n") * 2)
        self.assertEqual(record["tests"], 2)
        self.assertTrue(any("duplicate names" in reason for reason in reasons))

    def test_smoke_failed_multiple_and_ignored_results_never_pass_preflight(self):
        for output in (summary(1, status="FAILED"), summary(1) + summary(0),
                       summary(1, ignored=1), summary(1, failed=1)):
            with self.subTest(output=output):
                record, reasons, calls = self.checked(FIRST + ": test\n", smoke_output=output)
                self.assertTrue(reasons)
                self.assertFalse(record["smoke"]["qualified"])
                self.assertEqual(record["smoke"]["native_exit"], 0)
                self.assertEqual(record["smoke"]["exit"], 2)
                self.assertEqual(calls[1][-3:], ["--include-ignored", "--exact", FIRST])

    def test_one_successful_smoke_keeps_the_existing_command_and_qualifies(self):
        record, reasons, calls = self.checked(FIRST + ": test\n", smoke_output=summary(1))
        self.assertEqual(reasons, [])
        self.assertTrue(record["smoke"]["qualified"])
        self.assertEqual(record["smoke"]["exit"], 0)
        self.assertEqual(calls[1][-3:], ["--include-ignored", "--exact", FIRST])


class RequiredResults(unittest.TestCase):
    def assert_rejected(self, text, selected=2, configured=None, native_exit=0):
        result = testrun.qualified_result(text, selected, configured or settings(), native_exit)
        self.assertFalse(result["qualified"])
        self.assertEqual(result["exit"], native_exit or 2)
        self.assertTrue(result["qualification_failures"])
        self.assertEqual(result["required_tests_passed"], [])
        return result

    def test_missing_zero_ignored_partial_and_duplicate_summaries_cannot_qualify(self):
        for text in ("", "test result: ok. malformed\n", summary(0), summary(1, ignored=1),
                     summary(1), summary() + summary()):
            with self.subTest(text=text):
                self.assert_rejected(text)

    def test_required_cases_cannot_hide_in_ignored_counts_even_when_the_minimum_passes(self):
        configured = settings()
        configured["min_tests"] = 1
        self.assert_rejected(summary(1, ignored=1), configured=configured)

    def test_count_agreement_and_one_summary_are_independent_requirements(self):
        configured = testrun.options([])
        self.assert_rejected(summary(1), selected=2, configured=configured)
        self.assert_rejected(summary() + summary(), selected=4, configured=configured)

    def test_minimum_means_passed_tests_and_zero_passes_never_qualify(self):
        configured = testrun.options(["--min-tests", "3"])
        self.assert_rejected(summary(), configured=configured)
        configured = testrun.options(["--no-ignored"])
        self.assert_rejected(summary(0, ignored=2), configured=configured)

    def test_native_failure_and_timeout_keep_their_exit_codes(self):
        self.assert_rejected(summary(1, failed=1, status="FAILED"), native_exit=1)
        self.assert_rejected(summary(), native_exit=7)
        self.assert_rejected("", native_exit=124)

    def test_every_selected_case_passed_proves_all_required_names_without_nocapture_matching(self):
        output = "test first ... proving progress\nproved in 42 seconds\nok\n" + summary()
        result = testrun.qualified_result(output, 2, settings(), 0)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["exit"], 0)
        self.assertEqual(result["native_exit"], 0)
        self.assertEqual(result["required_tests_passed"], [FIRST, SECOND])
        self.assertEqual(result["qualification_failures"], [])

    def test_run_records_native_success_as_unqualified_and_returns_two(self):
        with tempfile.TemporaryDirectory(prefix="tenrai-testrun-result-") as temporary:
            configured = settings()
            configured["out"] = temporary

            def process(_command, stdout, **_kwargs):
                stdout.write(summary(1).encode())
                return SimpleNamespace(wait=lambda timeout: 0)

            with patch.object(testrun, "check", return_value=({"tests": 2, "binary": __file__, "sha256": "fixture"}, [])), \
                    patch.object(testrun, "fresh_admission", return_value=None), \
                    patch.object(testrun.subprocess, "Popen", side_effect=process):
                with self.assertRaises(SystemExit) as stopped:
                    testrun.run("fixture", "cross_pool", configured)
            self.assertEqual(stopped.exception.code, 2)
            reports = list(Path(temporary).glob("*/result.json"))
            self.assertEqual(len(reports), 1)
            result = testrun.json.loads(reports[0].read_text())
            self.assertEqual(result["native_exit"], 0)
            self.assertEqual(result["exit"], 2)
            self.assertFalse(result["qualified"])
            self.assertTrue(result["qualification_failures"])

    def test_a_sharded_run_reports_its_summary_and_exits_with_its_result(self):
        with tempfile.TemporaryDirectory(prefix="tenrai-testrun-shards-") as temporary:
            configured = testrun.options(["--min-tests", "2", "--shards", "2"])
            configured["out"] = temporary
            combined = {"exit": 0, "native_exit": 0, "qualified": True, "qualification_failures": [], "passed": 2,
                        "failed": 0, "ignored": 0, "failures": [], "expected_tests": 2, "timed_out": False}
            record = {"tests": 2, "binary": __file__, "sha256": "fixture", "test_names": [FIRST, SECOND]}
            with patch.object(testrun, "check", return_value=(record, [])), \
                    patch.object(testrun, "fresh_admission", return_value=None), \
                    patch.object(testrun, "run_shards", return_value=dict(combined)):
                with self.assertRaises(SystemExit) as stopped:
                    testrun.run("fixture", "cross_pool", configured)
            self.assertEqual(stopped.exception.code, 0)
            self.assertEqual(len(list(Path(temporary).glob("*/result.json"))), 1)

    def test_an_ordinary_suite_can_still_report_other_ignored_cases(self):
        configured = testrun.options(["--no-ignored"])
        result = testrun.qualified_result(summary(1, ignored=1), 2, configured, 0)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["ignored"], 1)


class ExplicitReleaseAdmission(unittest.TestCase):
    def test_optional_holder_is_forwarded_and_refusal_remains_native_three(self):
        configured = testrun.options(["--holder", "ordinary-unit-holder", "--envcheck", "fixture-envcheck.py"])
        result = SimpleNamespace(returncode=3, stdout="fixture refusal\n", stderr="")
        with patch.object(testrun.preflight, "sha256", return_value="b" * 64), \
             patch.object(testrun.subprocess, "run", return_value=result) as invoked:
            admission = testrun.fresh_admission(Path("frozen-fixture.exe"), "a" * 64, configured)
        self.assertFalse(admission["admitted"])
        self.assertEqual(admission["native_exit"], 3)
        command = invoked.call_args.args[0]
        self.assertEqual(command[command.index("--holder") + 1], "ordinary-unit-holder")
        self.assertNotIn("--allow", command)

    def test_native_zero_does_not_admit_a_changed_preflight_source(self):
        configured = testrun.options(["--envcheck", "fixture-envcheck.py"])
        result = SimpleNamespace(returncode=0, stdout="fixture success\n", stderr="")
        with patch.object(testrun.preflight, "sha256", side_effect=["a" * 64, "b" * 64]), \
             patch.object(testrun.subprocess, "run", return_value=result):
            admission = testrun.fresh_admission(Path("frozen-fixture.exe"), "a" * 64, configured)
        self.assertFalse(admission["admitted"])
        self.assertEqual(admission["native_exit"], 0)
        self.assertFalse(admission["controls_unchanged"])




class BackgroundAdmission(unittest.TestCase):
    def checked(self, allowed, competitors, admission_exit=0):
        with tempfile.TemporaryDirectory(prefix="tenrai-background-check-") as temporary:
            store = Path(temporary)
            (store / "case.exe").write_bytes(b"data fixture, never executable")
            (store / "case.json").write_text('{"sha256":"fixed"}')
            configured = testrun.options([item for image in allowed for item in ["--allow", image]])
            calls = []
            def command(arguments, **_kwargs):
                calls.append(arguments)
                catalog = "--list" in arguments
                return SimpleNamespace(stdout=FIRST + ": test\n" if catalog else "admission fixture",
                                       stderr="", returncode=0 if catalog else admission_exit)
            with patch.object(testrun, "STORE", store), \
                    patch.object(testrun.subprocess, "run", side_effect=command), \
                    patch.object(testrun.preflight, "sha256", return_value="fixed"), \
                    patch.object(testrun.os, "access", return_value=False), \
                    patch.object(testrun.preflight, "memory", return_value={"free_commit_gb": 16}), \
                    patch.object(testrun.preflight, "competing", return_value=competitors):
                record, reasons = testrun.check("case", "first", configured)
            return record, reasons, calls

    def test_default_still_rejects_a_background_node(self):
        record, reasons, _ = self.checked([], ["tenraid.exe 47004"])
        self.assertTrue(any("tenraid.exe 47004" in reason for reason in reasons))
        self.assertEqual(record["competing"], ["tenraid.exe 47004"])

    def test_allowed_node_is_recorded_and_forwarded_to_fresh_admission(self):
        record, reasons, calls = self.checked(["tenraid.exe"], ["tenraid.exe 47004"])
        self.assertEqual(reasons, [])
        self.assertEqual(record["execution_scope"], "shared_functional_tests")
        self.assertEqual(record["allowed_background"], ["tenraid.exe 47004"])
        admission = [command for command in calls if "--list" not in command]
        self.assertEqual(len(admission), 1)
        self.assertEqual(admission[0][admission[0].index("--allow") + 1], "tenraid.exe")

    def test_allowing_one_image_does_not_allow_another_competing_job(self):
        record, reasons, _ = self.checked(["tenraid.exe"], ["tenraid.exe 47004", "cargo.exe 17"])
        self.assertEqual(record["competing"], ["cargo.exe 17"])
        self.assertTrue(any("cargo.exe 17" in reason for reason in reasons))

    def test_shared_mode_does_not_override_a_failed_resource_admission(self):
        record, reasons, calls = self.checked(["tenraid.exe"], ["tenraid.exe 47004"], 3)
        self.assertTrue(any("fresh catalog environment check refused" in reason for reason in reasons))
        self.assertFalse(any("--list" in command for command in calls))
        self.assertFalse(record["catalog_admission"]["admitted"])

if __name__ == "__main__":
    unittest.main()
