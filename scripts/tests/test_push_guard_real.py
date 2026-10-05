"""Opt-in integration tests using a locally installed Betterleaks 1.8.1."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import string
import subprocess
import unittest
from typing import Iterator

try:
    import scripts.tests.test_push_guard as _push_guard_helpers
except ModuleNotFoundError:
    import test_push_guard as _push_guard_helpers


SCANNER_ENV = "PUSH_GUARD_REAL_SCANNER"


def synthetic_github_pat() -> str:
    alphabet = string.ascii_letters + string.digits
    return "ghp_" + "".join(secrets.choice(alphabet) for _ in range(36))


@contextmanager
def real_scanner_repo(scanner: str) -> Iterator[object]:
    """Reuse the hermetic local-bare-repository fixture with the real scanner."""
    fixture = _push_guard_helpers.PushGuardTests("test_clean_local_push_succeeds_and_scans_full_tip")
    fixture.setUp()
    fixture.env["BETTERLEAKS_BIN"] = scanner
    try:
        yield fixture
    finally:
        fixture.tearDown()


class RealScannerPushGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        scanner = os.environ.get(SCANNER_ENV)
        if not scanner:
            raise unittest.SkipTest(f"set {SCANNER_ENV} to run real-scanner integration tests")
        scanner_path = Path(scanner).expanduser()
        if not scanner_path.is_absolute():
            raise AssertionError(f"{SCANNER_ENV} must be an absolute executable path")
        if not scanner_path.is_file() or not os.access(scanner_path, os.X_OK):
            raise AssertionError(f"{SCANNER_ENV} does not name an executable file")
        cls.scanner = str(scanner_path.resolve())

    def test_clean_push_produces_valid_empty_sarif(self) -> None:
        with real_scanner_repo(self.scanner) as repo:
            commit = repo.commit_file("ordinary.txt", "clean fixture data\n", "clean change")
            result = repo.push("refs/heads/main:refs/heads/main")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(repo.remote_ref("refs/heads/main"), commit)

            reports = re.findall(r"Betterleaks reports: (.+)", result.stderr)
            self.assertEqual(len(reports), 1)
            report_path = Path(reports[0]) / "history.sarif"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["version"], "2.1.0")
            self.assertEqual(report["runs"][0]["tool"]["driver"]["name"].lower(), "betterleaks")
            self.assertEqual(report["runs"][0]["results"], [])
            for invocation in report["runs"][0].get("invocations", []):
                self.assertIs(invocation["executionSuccessful"], True)

    def test_removed_historical_synthetic_pat_still_blocks_without_advancing_ref(self) -> None:
        with real_scanner_repo(self.scanner) as repo:
            token = synthetic_github_pat()
            repo.commit_file("temporary-token.txt", token + "\n", "add temporary test fixture")
            (repo.repo / "temporary-token.txt").unlink()
            repo.git("add", "--", "temporary-token.txt")
            repo.git("commit", "-m", "remove temporary test fixture")
            tip = repo.rev_parse("HEAD")

            result = repo.push("refs/heads/main:refs/heads/main")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(repo.remote_ref("refs/heads/main"), repo.baseline)
            self.assertNotEqual(tip, repo.baseline)

    def test_clean_annotated_tag_push_succeeds(self) -> None:
        with real_scanner_repo(self.scanner) as repo:
            repo.git("tag", "-a", "v-clean-real-scan", "-m", "clean test tag")
            tag_object = repo.rev_parse("refs/tags/v-clean-real-scan")
            result = repo.push("refs/tags/v-clean-real-scan:refs/tags/v-clean-real-scan")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(repo.remote_ref("refs/tags/v-clean-real-scan"), tag_object)

    def test_annotated_tag_synthetic_pat_blocks_clean_commit(self) -> None:
        with real_scanner_repo(self.scanner) as repo:
            repo.commit_file("ordinary.txt", "clean commit\n", "clean commit for tag")
            token = synthetic_github_pat()
            message_file = repo.root / "synthetic-tag-message.txt"
            message_file.write_text(token + "\n", encoding="utf-8")
            repo.git("tag", "-a", "v-test-token", "-F", str(message_file))
            tag_object = repo.rev_parse("refs/tags/v-test-token")

            result = repo.push("refs/tags/v-test-token:refs/tags/v-test-token")
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(repo.remote_ref("refs/tags/v-test-token"))
            self.assertNotEqual(tag_object, repo.baseline)

    def test_linked_worktree_clean_push_succeeds(self) -> None:
        with real_scanner_repo(self.scanner) as repo:
            linked = repo.root / "real scanner linked worktree"
            added = repo.run_git("worktree", "add", "-b", "real-linked", str(linked), "main", cwd=repo.repo)
            self.assertEqual(added.returncode, 0, added.stderr)
            (linked / "linked.txt").write_text("clean linked worktree data\n", encoding="utf-8")
            staged = repo.run_git("add", "linked.txt", cwd=linked)
            self.assertEqual(staged.returncode, 0, staged.stderr)
            committed = repo.run_git("commit", "-m", "clean linked worktree", cwd=linked)
            self.assertEqual(committed.returncode, 0, committed.stderr)
            tip = repo.run_git("rev-parse", "HEAD", cwd=linked).stdout.strip()

            result = repo.run_git(
                "push", "origin", "refs/heads/real-linked:refs/heads/real-linked",
                cwd=linked, env=repo.env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(repo.remote_ref("refs/heads/real-linked"), tip)


if __name__ == "__main__":
    unittest.main()
