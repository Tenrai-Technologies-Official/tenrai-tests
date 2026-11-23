"""Public identity-history invariants with synthetic IDs and real temporary Git baselines."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import release_identity as history

A, B, C = "11" * 48, "22" * 48, "33" * 48
GA, GB, GC = "a1" * 48, "b2" * 48, "c3" * 48


def retire(chain):
    return {"type": "retire", "chain_id": chain}


def activate(chain, genesis):
    return {"type": "activate", "chain_id": chain, "genesis_id": genesis}


def encoded(events):
    return (json.dumps({"format": history.FORMAT, "events": events}, indent=2) + "\n").encode()


def git(root, *arguments):
    # Test-only repositories use empty hook/template directories, never the user's hooks.
    empty = root.parent / "empty-git-hooks"
    empty.mkdir(exist_ok=True)
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    command = ["git", "--no-replace-objects", "--no-optional-locks", "-c", "safe.directory=" + str(root),
               "-c", "core.hooksPath=" + str(empty), "-c", "init.templateDir=" + str(empty),
               "-c", "user.name=Tenrai identity fixture", "-c", "user.email=fixture@example.invalid",
               "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false",
               "-c", "core.autocrlf=false", "-C", str(root), *arguments]
    result = subprocess.run(command, capture_output=True, timeout=15, env=environment, check=False)
    if result.returncode:
        raise AssertionError("test-only Git fixture command failed: " + result.stderr.decode("utf-8", "replace"))
    return result.stdout.decode("utf-8").strip()


def public_repository(parent):
    root = parent / "public"
    (root / "genesis").mkdir(parents=True)
    git(root, "init", "--quiet")
    (root / history.PUBLISHED).write_text(json.dumps({"chain_id": A, "genesis_id": GA}), encoding="utf-8")
    git(root, "add", history.PUBLISHED)
    git(root, "commit", "--quiet", "--no-verify", "-m", "synthetic prototype identity")
    (root / history.LEDGER).write_bytes(encoded([retire(A), activate(B, GB)]))
    return root


class EventInvariants(unittest.TestCase):
    def test_retired_or_previously_used_cid_never_reactivates_with_old_or_new_genesis(self):
        for genesis in (GA, GB):
            for events in ([retire(A), activate(A, genesis)],
                           [activate(A, GA), retire(A), activate(A, genesis)],
                           [activate(A, GA), retire(A), activate(B, GB), retire(B), activate(A, genesis)]):
                with self.subTest(events=events), self.assertRaises(history.IdentityHistoryError):
                    history.parse(encoded(events))

    def test_new_identity_needs_the_previous_active_one_retired(self):
        with self.assertRaisesRegex(history.IdentityHistoryError, "retire the previous"):
            history.parse(encoded([activate(A, GA), activate(B, GB)]))
        events, active = history.parse(encoded([activate(A, GA), retire(A), activate(B, GB)]))
        self.assertEqual(active, (B, GB))
        self.assertEqual(len(events), 3)

    def test_duplicate_json_unknown_fields_zero_and_placeholders_fail(self):
        bad = [b'{"format":"tenrai-identity-history-v1","format":"tenrai-identity-history-v1","events":[]}',
               encoded([{"type": "activate", "chain_id": B, "genesis_id": GB, "allow_reuse": True}]),
               encoded([activate("0" * 96, GB)]), encoded([activate(B, "TODO")]),
               encoded([retire(A), retire(A)]), b'[]']
        for raw in bad:
            with self.subTest(raw=raw[:70]), self.assertRaises(history.IdentityHistoryError):
                history.parse(raw)

    def test_fixed_count_and_byte_bounds_apply_before_acceptance(self):
        for raw in (encoded([retire(A)] * (history.MAX_EVENTS + 1)), b" " * (history.MAX_BYTES + 1)):
            with self.assertRaises(history.IdentityHistoryError):
                history.parse(raw)


class CommittedBaseline(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="tenrai-identity-history-")
        self.addCleanup(folder.cleanup)
        self.parent = Path(folder.name)
        self.root = public_repository(self.parent)

    def commit_active(self):
        (self.root / history.PUBLISHED).write_text(json.dumps({"chain_id": B, "genesis_id": GB}), encoding="utf-8")
        git(self.root, "add", history.PUBLISHED, history.LEDGER)
        git(self.root, "commit", "--quiet", "--no-verify", "-m", "synthetic first unique launch")

    def test_ordinary_release_accepts_the_same_pair_without_another_activation(self):
        seal = history.authorize(self.root, B, GB)
        self.assertEqual(history.verify(self.root, B, GB, seal), seal)
        self.commit_active()
        current = history.verify(self.root, B, GB, seal)
        self.assertEqual(current["sha256"], seal["sha256"])
        self.assertEqual(current["baseline_kind"], "ledger")

    def test_equivalent_lf_crlf_and_json_formatting_keep_the_same_public_history_identity(self):
        seal = history.authorize(self.root, B, GB)
        path = self.root / history.LEDGER
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(history.verify(self.root, B, GB, seal)["sha256"], seal["sha256"])
        path.write_text(json.dumps({"events": [retire(A), activate(B, GB)], "format": history.FORMAT},
                                  separators=(",", ":")), encoding="utf-8")
        self.assertEqual(history.verify(self.root, B, GB, seal)["sha256"], seal["sha256"])

    def test_initial_migration_is_bound_to_committed_not_edited_public_identity(self):
        (self.root / history.PUBLISHED).write_text(json.dumps({"chain_id": C, "genesis_id": GC}), encoding="utf-8")
        (self.root / history.LEDGER).write_bytes(encoded([retire(C), activate(B, GB)]))
        with self.assertRaisesRegex(history.IdentityHistoryError, "committed prototype"):
            history.authorize(self.root, B, GB)
        (self.root / history.LEDGER).write_bytes(encoded([retire(A), activate(B, GB)]))
        history.authorize(self.root, B, GB)

    def test_committed_prefix_cannot_be_truncated_or_rebound_to_another_genesis(self):
        self.commit_active()
        for events in ([retire(A)], [retire(A), activate(B, GC)], [activate(B, GB)]):
            (self.root / history.LEDGER).write_bytes(encoded(events))
            with self.subTest(events=events), self.assertRaisesRegex(history.IdentityHistoryError, "prefix"):
                history.authorize(self.root, B, GB)

    def test_an_already_committed_self_consistent_rewrite_cannot_erase_cid_history(self):
        self.commit_active()
        (self.root / history.LEDGER).write_bytes(encoded([retire(A), activate(C, GC)]))
        (self.root / history.PUBLISHED).write_text(json.dumps({"chain_id": C, "genesis_id": GC}), encoding="utf-8")
        git(self.root, "add", history.LEDGER, history.PUBLISHED)
        git(self.root, "commit", "--quiet", "--no-verify", "-m", "invalid committed history rewrite")
        with self.assertRaisesRegex(history.IdentityHistoryError, "committed identity history"):
            history.authorize(self.root, C, GC)

    def test_committing_same_cid_with_another_gid_does_not_bypass_the_release_gate(self):
        self.commit_active()
        (self.root / history.LEDGER).write_bytes(encoded([retire(A), activate(B, GC)]))
        (self.root / history.PUBLISHED).write_text(json.dumps({"chain_id": B, "genesis_id": GC}), encoding="utf-8")
        git(self.root, "add", history.LEDGER, history.PUBLISHED)
        git(self.root, "commit", "--quiet", "--no-verify", "-m", "invalid committed CID rebinding")
        with self.assertRaisesRegex(history.IdentityHistoryError, "committed identity history"):
            history.authorize(self.root, B, GC)

    def test_a_merge_cannot_discard_the_other_actual_parents_identity_history(self):
        prototype = git(self.root, "rev-parse", "HEAD")
        main = git(self.root, "symbolic-ref", "--short", "HEAD")
        self.commit_active()
        git(self.root, "branch", "side-identity", prototype)
        git(self.root, "checkout", "--quiet", "side-identity")
        (self.root / history.LEDGER).write_bytes(encoded([retire(A), activate(C, GC)]))
        (self.root / history.PUBLISHED).write_text(json.dumps({"chain_id": C, "genesis_id": GC}), encoding="utf-8")
        git(self.root, "add", history.LEDGER, history.PUBLISHED)
        git(self.root, "commit", "--quiet", "--no-verify", "-m", "synthetic other identity branch")
        git(self.root, "checkout", "--quiet", main)
        git(self.root, "merge", "--quiet", "--no-ff", "--strategy=ours", "-m", "invalid discard of side history", "side-identity")
        with self.assertRaisesRegex(history.IdentityHistoryError, "parent event prefix"):
            history.authorize(self.root, B, GB)

    def test_shallow_metadata_cannot_hide_reachable_historical_rewrites(self):
        self.commit_active()
        commit = git(self.root, "rev-parse", "HEAD")
        (self.root / ".git/shallow").write_text(commit + "\n", encoding="ascii")
        with self.assertRaisesRegex(history.IdentityHistoryError, "complete Git ancestry"):
            history.authorize(self.root, B, GB)

    def test_committed_ledger_deletion_cannot_restore_implicit_bootstrap(self):
        self.commit_active()
        (self.root / history.LEDGER).unlink()
        git(self.root, "add", "-u", "genesis")
        git(self.root, "commit", "--quiet", "--no-verify", "-m", "invalid ledger deletion")
        (self.root / history.LEDGER).write_bytes(encoded([retire(B), activate(C, GC)]))
        with self.assertRaisesRegex(history.IdentityHistoryError, "ledger was deleted"):
            history.authorize(self.root, C, GC)

    def test_retirement_invalidates_an_old_seal_even_when_it_was_once_valid(self):
        seal = history.authorize(self.root, B, GB)
        (self.root / history.LEDGER).write_bytes(encoded([retire(A), activate(B, GB), retire(B), activate(C, GC)]))
        with self.assertRaisesRegex(history.IdentityHistoryError, "active public pair"):
            history.verify(self.root, B, GB, seal)
        with self.assertRaisesRegex(history.IdentityHistoryError, "changed since"):
            history.verify(self.root, C, GC, seal)

    def test_empty_or_uncommitted_implicit_bootstrap_is_refused(self):
        other = self.parent / "empty-public"
        other.mkdir()
        git(other, "init", "--quiet")
        (other / "genesis").mkdir()
        (other / history.PUBLISHED).write_text(json.dumps({"chain_id": A, "genesis_id": GA}), encoding="utf-8")
        (other / history.LEDGER).write_bytes(encoded([retire(A), activate(B, GB)]))
        with self.assertRaises(history.IdentityHistoryError):
            history.authorize(other, B, GB)

    def test_git_repository_selection_environment_cannot_redirect_the_baseline(self):
        poisoned = {"GIT_DIR": str(self.parent / "does-not-exist"),
                    "GIT_WORK_TREE": str(self.parent), "GIT_INDEX_FILE": str(self.parent / "foreign-index"),
                    "GIT_CONFIG_PARAMETERS": "not valid config", "GIT_COMMON_DIR": str(self.parent)}
        with patch.dict(os.environ, poisoned):
            history.authorize(self.root, B, GB)

    def test_git_replace_objects_cannot_substitute_another_public_history(self):
        original = git(self.root, "rev-parse", "HEAD")
        (self.root / history.PUBLISHED).write_text(json.dumps({"chain_id": C, "genesis_id": GC}), encoding="utf-8")
        git(self.root, "add", history.PUBLISHED)
        git(self.root, "commit", "--quiet", "--no-verify", "-m", "synthetic replacement target")
        replacement = git(self.root, "rev-parse", "HEAD")
        git(self.root, "update-ref", "HEAD", original)
        git(self.root, "replace", original, replacement)
        # The working tree may differ; only the unreplaced immutable original commit is authority.
        history.authorize(self.root, B, GB)

    def test_baseline_commit_changes_during_authorization_are_rejected(self):
        actual = history._git
        changed = False

        def concurrent_commit(root, arguments):
            nonlocal changed
            result = actual(root, arguments)
            if not changed and arguments[:2] == ["ls-tree", "-z"]:
                changed = True
                (self.root / history.PUBLISHED).write_text(json.dumps({"chain_id": C, "genesis_id": GC}), encoding="utf-8")
                git(self.root, "add", history.PUBLISHED)
                git(self.root, "commit", "--quiet", "--no-verify", "-m", "concurrent synthetic publication")
            return result

        with patch.object(history, "_git", concurrent_commit):
            with self.assertRaisesRegex(history.IdentityHistoryError, "changed during authorization"):
                history.authorize(self.root, B, GB)


if __name__ == "__main__":
    unittest.main()
