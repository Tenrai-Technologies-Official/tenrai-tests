"""A resumed checks phase keeps a passed check only while its whole result still belongs to the build."""
from argparse import Namespace
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import release_evidence as evidence
import release_resume as resume
import release_runtime as runtime

DRIVER = Path(__file__).with_name("build-release.sh")
GIT_BASH = Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe"
BASH = os.environ.get("GIT_BASH") or (str(GIT_BASH) if GIT_BASH.is_file() else shutil.which("bash"))


class KeptChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tenrai-release-resume-")
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
        mock = patch.object(runtime.subprocess, "run", side_effect=self.call)
        mock.start()
        self.addCleanup(mock.stop)
        self.logs = self.work / "logs/attempt-A"
        self.logs.mkdir(parents=True)

    def call(self, command, **kwargs):
        if command[0] == "rg":
            folder = Path(kwargs["cwd"])
            names = sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())
            return SimpleNamespace(returncode=0, stdout=("\0".join(names) + "\0").encode(), stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"fixture preflight\n", stderr=b"")

    def frozen(self, attempt, content=b"frozen test binary fixture; never executed"):
        binary = self.work / "scratch" / attempt / "frozen-tests" / f"release-{attempt}.exe"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(content)
        evidence.write(binary.with_suffix(".json"), {"name": binary.stem, "sha256": evidence.sha(binary)})
        return binary

    def run_check(self, name, code=0, binaries=(), record_log=True, row=True, attempt="attempt-A"):
        arguments = Namespace(work=str(self.work), name="check-" + name, repo=[str(self.repo)], kind="build",
                              allow_change=[], envcheck=str(self.guard), holder="ordinary-unit-fixture",
                              commit_gb=8, room=[], file=[], frozen=[])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.begin(arguments), 0)
        ticket = sorted((self.work / "source-boundaries").glob("*/before.json"))[-1]
        log = self.work / "logs" / attempt / f"check-{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"{name}: test result: ok\n")
        for number, binary in enumerate(binaries):
            run = self.work / "checks/filters" / f"{name}-{number}"
            run.mkdir(parents=True)
            evidence.write(run / "preflight.json", {"binary": str(binary), "sha256": evidence.sha(binary)})
            evidence.write(run / "result.json", {"exit": 0, "qualified": True})
        ended = Namespace(work=str(self.work), ticket=str(ticket), exit_code=code, artifact=[], artifact_tree=[],
                          log=str(log) if record_log else None)
        with redirect_stdout(io.StringIO()):
            runtime.end(ended)
        if row:
            self.add_row("checks", name, code, log)
        return log

    def add_row(self, phase, name, code, log):
        with (self.work / "checks.tsv").open("a", encoding="utf-8", newline="\n") as table:
            table.write(f"{phase}\t{name}\t{code}\t{log}\n")

    def kept(self):
        return resume.kept_checks(self.work, "checks")

    def test_a_passed_check_whose_whole_result_holds_is_kept(self):
        self.run_check("alpha")
        self.assertEqual(self.kept(), {"phase": "checks", "kept": ["alpha"], "dropped": {}, "attempt": None})

    def test_each_broken_link_makes_its_check_run_again(self):
        cases = {
            "it ended with exit 1": lambda: self.run_check("alpha", code=1),
            "its log changed after its command": lambda: self.run_check("alpha").write_text("another log\n"),
            "inputs changed afterwards": lambda: (self.run_check("alpha"),
                                                  (self.repo / "source.rs").write_text("changed\n")),
            "qualification controls changed": lambda: (self.run_check("alpha"),
                                                       (self.guard.parent / "stacks.py").write_text("# changed\n")),
            "no command boundary recorded its log": lambda: self.run_check("alpha", record_log=False),
            "it ran again after this result": lambda: (self.run_check("alpha"),
                                                       self.run_check("alpha", code=1, row=False, attempt="attempt-B")),
        }
        for reason, make in cases.items():
            with self.subTest(reason=reason):
                shutil.rmtree(self.work)
                self.logs.mkdir(parents=True)
                (self.repo / "source.rs").write_text("const VALUE: u8 = 1;\n")
                (self.guard.parent / "stacks.py").write_text("# original controller fixture\n")
                make()
                result = self.kept()
                self.assertEqual(result["kept"], [])
                self.assertIn(reason, result["dropped"]["alpha"])

    def test_keep_leaves_other_phases_and_drops_the_checks_that_run_again(self):
        desktop = self.logs / "check-desktop-e2e.log"
        desktop.write_text("desktop: ok\n")
        self.add_row("desktop", "desktop-e2e", 0, desktop)
        self.run_check("alpha")
        self.run_check("beta", code=3)
        report = self.root / "report.json"
        with redirect_stdout(io.StringIO()) as output:
            resume.keep(Namespace(work=str(self.work), phase="checks", attempt="none", report=str(report)))
        names = [line.split("\t")[1] for line in (self.work / "checks.tsv").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(names, ["desktop-e2e", "alpha"])
        self.assertIn("runs again beta: it ended with exit 3", output.getvalue())
        self.assertEqual(evidence.load(report)["kept"], ["alpha"])

    def test_a_check_named_twice_refuses_the_resume(self):
        log = self.run_check("alpha")
        self.add_row("checks", "alpha", 0, log)
        with self.assertRaisesRegex(evidence.GateError, "names a check twice"):
            self.kept()

    def test_the_frozen_test_binary_the_kept_checks_ran_names_the_attempt(self):
        binary = self.frozen("attempt-A")
        self.run_check("alpha", binaries=[binary])
        self.assertEqual(self.kept()["attempt"], "attempt-A")
        binary.write_bytes(b"other bytes")
        result = self.kept()
        self.assertEqual((result["kept"], result["attempt"]), ([], None))
        self.assertIn("its frozen test binary changed", result["dropped"]["alpha"])

    def test_a_binary_outside_the_release_attempts_is_no_frozen_test_binary(self):
        stray = self.work / "elsewhere/release-attempt-A.exe"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"stray binary fixture")
        evidence.write(stray.with_suffix(".json"), {"sha256": evidence.sha(stray)})
        self.run_check("alpha", binaries=[stray])
        self.assertIn("not a frozen test binary of this release", self.kept()["dropped"]["alpha"])

    def test_a_kept_check_of_another_binary_runs_again(self):
        self.run_check("alpha", binaries=[self.frozen("attempt-A")])
        self.run_check("beta", binaries=[self.frozen("attempt-B", b"another frozen binary fixture")])
        result = self.kept()
        self.assertEqual((result["kept"], result["attempt"]), (["beta"], "attempt-B"))
        self.assertIn("another frozen test binary", result["dropped"]["alpha"])
        with self.assertRaisesRegex(evidence.GateError, "another frozen test binary than this attempt resumes"):
            resume.keep(Namespace(work=str(self.work), phase="checks", attempt="attempt-A",
                                  report=str(self.root / "report.json")))

    def test_the_build_must_give_the_frozen_bytes(self):
        binary = self.frozen("attempt-A")
        built = self.root / "built.exe"
        built.write_bytes(binary.read_bytes())
        arguments = Namespace(store=str(binary.parent), name=binary.stem, binary=str(built))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(resume.frozen(arguments), 0)
        built.write_bytes(b"a rebuilt binary with other bytes")
        with self.assertRaisesRegex(evidence.GateError, "the build gave"):
            resume.frozen(arguments)
        built.write_bytes(binary.read_bytes())
        binary.write_bytes(b"changed after its freeze")
        with self.assertRaisesRegex(evidence.GateError, "changed after its freeze"):
            resume.frozen(arguments)


@unittest.skipUnless(BASH, "needs bash")
class ResumedDriver(unittest.TestCase):
    def test_check_keeps_a_kept_row_and_runs_every_other(self):
        source = DRIVER.read_text(encoding="utf-8")
        function = re.search(r"^check\(\) \{\n.*?^\}$", source, re.M | re.S).group(0)
        with tempfile.TemporaryDirectory(prefix="tenrai-release-resume-driver-") as folder:
            out = Path(folder).as_posix()
            Path(folder, "checks.tsv").write_text("checks\talpha\t0\tlog\n", encoding="utf-8", newline="\n")
            script = "\n".join([
                "win() { printf '%s' \"$1\"; }", "say() { echo \"$*\"; }",
                "boundary_begin() { echo ticket; }", "boundary_end() { :; }",
                function,
                "check alpha sh -c 'echo ran alpha'", "check beta sh -c 'echo ran beta'",
                "cat \"$OUT/checks.tsv\"", "cat \"$L/check-beta.log\""])
            environment = {**os.environ, "OUT": out, "L": out, "PHASE": "checks", "resume": "1", "fail_fast": "0"}
            output = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=environment,
                                    check=True).stdout
        self.assertIn("check alpha: kept", output)
        self.assertNotIn("ran alpha", output)
        self.assertIn("check beta: exit 0", output)
        self.assertIn("ran beta", output)
        self.assertRegex(output, r"checks\tbeta\t0\t")

    def test_resume_goes_on_with_the_checks_phase_only(self):
        result = subprocess.run([BASH, str(DRIVER), "--from", "records", "--resume"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--from checks --resume", result.stdout)


if __name__ == "__main__":
    unittest.main()
