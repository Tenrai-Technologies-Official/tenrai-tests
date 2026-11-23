"""The commit gate of a release, on real repositories in a temporary folder."""
from pathlib import Path
import subprocess
import tempfile
import unittest

import release_commits as commits


class PushedCommitsOnly(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tenrai-release-commits-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.upstream, self.repo = root / "upstream.git", root / "repo"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.upstream)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.upstream), str(self.repo)], check=True, capture_output=True)
        self.write("source.rs", "const VALUE: u8 = 1;\n")
        self.write("other.rs", "const OTHER: u8 = 2;\n")
        self.write(".gitignore", "target/\n")
        self.commit("first")
        self.git("push", "-u", "origin", "HEAD:main")

    def git(self, *arguments):
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                        *arguments], check=True, capture_output=True)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-m", message)

    def test_a_clean_pushed_repository_passes(self):
        self.assertEqual(commits.problems(self.repo), [])

    def test_a_changed_tracked_file_is_refused(self):
        self.write("source.rs", "const VALUE: u8 = 3;\n")
        found = commits.problems(self.repo)
        self.assertEqual(len(found), 1)
        self.assertIn("1 uncommitted files, the first is source.rs", found[0])

    def test_a_new_file_is_refused_and_an_ignored_one_is_not(self):
        self.write("target/build.bin", "ignored\n")
        self.assertEqual(commits.problems(self.repo), [])
        self.write("deep/new.rs", "const NEW: u8 = 4;\n")
        self.assertIn("deep/new.rs", commits.problems(self.repo)[0])

    def test_a_commit_that_is_not_pushed_is_refused(self):
        self.write("source.rs", "const VALUE: u8 = 5;\n")
        self.commit("second")
        found = commits.problems(self.repo)
        self.assertEqual(len(found), 1)
        self.assertIn("is not the pushed", found[0])

    def test_a_branch_without_upstream_is_refused(self):
        self.git("checkout", "-b", "local")
        self.assertIn("no upstream", commits.problems(self.repo)[0])

    def test_only_the_named_paths_may_change(self):
        self.write("source.rs", "const VALUE: u8 = 6;\n")
        self.assertEqual(commits.problems(self.repo, {"source.rs"}), [])
        self.write("other.rs", "const OTHER: u8 = 7;\n")
        found = commits.problems(self.repo, {"source.rs"})
        self.assertIn("1 uncommitted files, the first is other.rs", found[0])

    def test_a_rename_does_not_hide_the_change_after_it(self):
        self.git("mv", "other.rs", "renamed.rs")
        self.write("source.rs", "const VALUE: u8 = 8;\n")
        self.assertEqual(sorted(commits.changed_paths(self.repo)), ["renamed.rs", "source.rs"])

    def test_a_folder_that_is_no_repository_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="tenrai-release-commits-plain-") as plain:
            self.assertIn("no commit to name", commits.problems(plain)[0])


if __name__ == "__main__":
    unittest.main()
