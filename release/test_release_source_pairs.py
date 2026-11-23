"""The release parity check must reject a missing or divergent split TenQ helper."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

DRIVER = Path(__file__).with_name("build-release.sh").read_text(encoding="utf-8")
COPIES = re.search(r"^copies\(\) \{\n.*?^\}$", DRIVER, re.M | re.S).group(0)
GIT_BASH = Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe"
BASH = os.environ.get("GIT_BASH") or (str(GIT_BASH) if GIT_BASH.is_file() else shutil.which("bash"))


@unittest.skipUnless(BASH, "Bash is required to exercise the release check")
class SplitTenQSourcePairs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tenrai-source-pairs-")
        self.addCleanup(temporary.cleanup)
        self.github = Path(temporary.name)
        self.repo = self.github / "tenrai-core"
        for crate in ("auth", "client-api", "metrics", "platform", "primitives", "wire"):
            for root in (self.repo, self.github / "tenrai-api"):
                path = root / "crates" / crate / "Cargo.toml"
                path.parent.mkdir(parents=True)
                path.write_text("same\n", encoding="utf-8")
        for root in (self.repo, self.github / "tenrai-api"):
            path = root / "protocol/test-vectors/tsp2-key-derivation.json"
            path.parent.mkdir(parents=True)
            path.write_text("{}\n", encoding="utf-8")
        for root, prefix in ((self.repo, "crates/tenq/src"),
                             (self.github / "tenrai-pool", "src/pow"),
                             (self.github / "tenrai-reference-pool", "src/pow")):
            for name in ("tenq.rs", "tenq_templates.rs"):
                path = root / prefix / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name + "\n", encoding="utf-8")
        for root, prefix in ((self.repo, "crates/tenq/vectors"),
                             (self.github / "tenrai-pool", "tests/vectors"),
                             (self.github / "tenrai-reference-pool", "tests/vectors")):
            path = root / prefix / "tenq_vectors.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")

    def check_copies(self):
        environment = {**os.environ, "REPO": self.repo.as_posix(), "GITHUB": self.github.as_posix()}
        return subprocess.run([BASH, "-c", COPIES + "\ncopies\n"], env=environment,
                              capture_output=True, text=True, check=False).returncode

    def test_both_source_pairs_match(self):
        self.assertEqual(self.check_copies(), 0)

    def test_either_pool_helper_divergence_fails(self):
        for pool in ("tenrai-pool", "tenrai-reference-pool"):
            with self.subTest(pool=pool):
                helper = self.github / pool / "src/pow/tenq_templates.rs"
                helper.write_text("divergent\n", encoding="utf-8")
                self.assertNotEqual(self.check_copies(), 0)
                helper.write_text("tenq_templates.rs\n", encoding="utf-8")

    def test_missing_pool_helper_fails(self):
        (self.github / "tenrai-pool/src/pow/tenq_templates.rs").unlink()
        self.assertNotEqual(self.check_copies(), 0)


if __name__ == "__main__":
    unittest.main()
