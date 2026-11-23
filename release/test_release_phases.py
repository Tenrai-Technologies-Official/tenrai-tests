"""The loop of build-release.sh that runs a long acceptance test phase by phase, with the test run replaced."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

DRIVER = Path(__file__).with_name("build-release.sh").read_text(encoding="utf-8")
FUNCTIONS = "\n".join(re.search(rf"^{name}\(\) \{{\n.*?^\}}$", DRIVER, re.M | re.S).group(0)
                      for name in ("phases", "offline_from_full"))
GIT_BASH = Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe"
BASH = os.environ.get("GIT_BASH") or (str(GIT_BASH) if GIT_BASH.is_file() else shutil.which("bash"))
STUBS = r"""
win() { printf '%s' "$1"; }
testrun() {
  calls=$(( $(cat "$SCRATCH/calls" 2>/dev/null || echo 0) + 1 ))
  echo "$calls" > "$SCRATCH/calls"
  printf '%s\n' "$*" >> "$SCRATCH/arguments"
  [ "$calls" != "${FAIL_AT:-0}" ] || return 7
  mkdir -p "$SCRATCH/phases/$OWN"
  stage=away
  [ "$calls" != "${DONE_AT:-0}" ] || stage=done
  printf '{\n  "stage": "%s"\n}\n' "$stage" > "$SCRATCH/phases/$OWN/phase.json"
}
"""


class LongTestsInPhases(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tenrai-release-phases-")
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name)

    def run_driver(self, command, own="own", **settings):
        script = f'{STUBS}\n{FUNCTIONS}\n{command}\necho "exit $?"\n'
        environment = {**os.environ, "SCRATCH": self.scratch.as_posix(), "OWN": own, **settings}
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=environment, check=True).stdout

    def calls(self):
        path = self.scratch / "arguments"
        return path.read_text().splitlines() if path.exists() else []

    def test_every_phase_runs_until_the_test_is_done(self):
        output = self.run_driver("phases the-binary the-room own 5 the-test --env EXTRA=1", DONE_AT="3")
        self.assertIn("phase 3 of own: exit 0", output)
        self.assertTrue(output.endswith("exit 0\n"), output)
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        for call in calls:
            self.assertTrue(call.startswith("the-binary the-test --expect the-test "), call)
            for part in ("TENRAI_TEST_PHASE_DIR=", "--timeout 3600", "--room the-room", "--env EXTRA=1"):
                self.assertIn(part, call)
        self.assertFalse((self.scratch / "phases/own").exists(), "the stores of a test that passed go")

    def test_the_first_failed_phase_ends_the_test_with_its_code(self):
        output = self.run_driver("phases the-binary the-room own 5 the-test", FAIL_AT="2", DONE_AT="3")
        self.assertIn("phase 2 of own: exit 7", output)
        self.assertTrue(output.endswith("exit 7\n"), output)
        self.assertEqual(len(self.calls()), 2)
        self.assertTrue((self.scratch / "phases/own/phase.json").exists(), "the stores of a failed test stay")

    def test_a_test_that_never_ends_fails(self):
        output = self.run_driver("phases the-binary the-room own 2 the-test")
        self.assertIn("own: no end after 2 phases", output)
        self.assertTrue(output.endswith("exit 1\n"), output)
        self.assertEqual(len(self.calls()), 2)

    def test_the_full_source_does_not_run_without_the_shared_phases(self):
        output = self.run_driver("offline_from_full the-binary the-room 3 the-test")
        self.assertIn("not run", output)
        self.assertTrue(output.endswith("exit 3\n"), output)
        self.assertEqual(self.calls(), [])

    def test_a_shared_phase_that_did_not_reach_the_return_does_not_count(self):
        shared = self.scratch / "phases/private-long-offline"
        shared.mkdir(parents=True)
        (shared / "phase.json").write_text('{\n  "stage": "away"\n}\n')
        output = self.run_driver("offline_from_full the-binary the-room 3 the-test")
        self.assertTrue(output.endswith("exit 3\n"), output)
        self.assertEqual(self.calls(), [])

    def test_the_full_source_goes_on_and_the_shared_stores_go_after_it(self):
        shared = self.scratch / "phases/private-long-offline"
        shared.mkdir(parents=True)
        (shared / "phase.json").write_text('{\n  "stage": "back"\n}\n')
        output = self.run_driver("offline_from_full the-binary the-room 3 the-test", own="private-long-offline-full",
                                 DONE_AT="1")
        self.assertTrue(output.endswith("exit 0\n"), output)
        self.assertTrue(self.calls()[0].startswith("the-binary the-test "), self.calls()[0])
        self.assertFalse(shared.exists())


if __name__ == "__main__":
    unittest.main()
