"""Local, dependency-free integration tests for the Betterleaks push gate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "TEST_ONLY_PUSH_GUARD_SENTINEL"

GUARD = ROOT / "scripts" / "check-betterleaks.sh"
HOOK = ROOT / ".githooks" / "pre-push"
DISPATCHER = ROOT / ".githooks" / "dispatch-pre-push"
VERSION = ROOT / "scripts" / "betterleaks-version.txt"
VALIDATOR = ROOT / "scripts" / "validate-betterleaks-report.pl"


MOCK_SCANNER = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import subprocess
import sys

base = Path(__file__).resolve().parent
config_path = base / "scanner.json"
calls_path = base / "scanner.calls.jsonl"
config = json.loads(config_path.read_text()) if config_path.exists() else {}
args = sys.argv[1:]

if args == ["--version"]:
    print("betterleaks version " + config.get("version", "1.8.1"))
    raise SystemExit(0)

text = " ".join(args)
match = re.search(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])", text)
commit = match.group(0).lower() if match else None
call = {"args": args, "commit": commit, "cwd": os.getcwd()}
with calls_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(call) + "\n")

mode = config.get("mode", "clean")
history = ""
if commit:
    result = subprocess.run(
        ["git", "log", "--full-history", "--diff-merges=first-parent", "-p", commit],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    history = result.stdout
if args and args[0] == "dir" and len(args) > 1:
    source = Path(args[1])
    if source.is_dir():
        history = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in source.rglob("*"))
if config.get("find_sentinel", False) and "TEST_ONLY_PUSH_GUARD_SENTINEL" in history:
    mode = "finding"

if mode == "nonzero":
    print("mock scanner failed", file=sys.stderr)
    raise SystemExit(9)
if mode == "warning":
    print("WRN mock warning", file=sys.stderr)
if mode == "error":
    print("ERR mock error", file=sys.stderr)

report_path = None
for index, arg in enumerate(args[:-1]):
    if arg == "--report-path":
        report_path = Path(args[index + 1])
        break

report_mode = config.get("report", "valid")
if report_mode != "missing" and report_path is not None:
    if report_mode == "malformed":
        report_path.write_text("not-json", encoding="utf-8")
    else:
        results = []
        if mode == "finding" or report_mode == "nonempty":
            results = [{"ruleId": "MOCK_TEST", "message": {"text": "test sentinel detected"}}]
        invocation = {"executionSuccessful": report_mode != "execution-false"}
        if report_mode == "execution-missing":
            invocation = {}
        if report_mode == "no-invocation":
            invocations = []
        else:
            invocations = [invocation]
        report = {
            "version": "2.1.0",
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{
                "tool": {"driver": {"name": "Betterleaks"}},
                "results": results,
                "invocations": invocations,
            }],
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report), encoding="utf-8")

if mode == "finding":
    print("ERR mock finding", file=sys.stderr)
    raise SystemExit(1)
if mode == "clean":
    print("INF no leaks found", file=sys.stderr)
raise SystemExit(0)
'''


class PushGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = (GUARD, HOOK, DISPATCHER, VERSION, VALIDATOR)
        missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
        if missing:
            raise unittest.SkipTest("push guard files not available: " + ", ".join(missing))

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="rustytransfer-push-guard-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "work tree with spaces"
        self.remote = self.root / "local bare remote.git"
        self.repo.mkdir()
        self.scanner = self.root / "mock betterleaks"
        self.scanner.mkdir()
        self.scanner_bin = self.scanner / "betterleaks"
        self.scanner_bin.write_text(MOCK_SCANNER, encoding="utf-8")
        self.scanner_bin.chmod(0o755)
        self.scanner_config = self.scanner / "scanner.json"
        self.calls_path = self.scanner / "scanner.calls.jsonl"
        self.scanner_config.write_text("{}", encoding="utf-8")

        scripts = self.repo / "scripts"
        hooks = self.repo / ".githooks"
        scripts.mkdir()
        hooks.mkdir()
        shutil.copy2(GUARD, scripts / GUARD.name)
        shutil.copy2(VERSION, scripts / VERSION.name)
        shutil.copy2(VALIDATOR, scripts / VALIDATOR.name)
        shutil.copy2(HOOK, hooks / HOOK.name)
        (scripts / GUARD.name).chmod(0o755)

        self.git("init", "--initial-branch=main")
        installed_hook = self.repo / ".git" / "hooks" / "pre-push"
        shutil.copy2(DISPATCHER, installed_hook)
        installed_hook.chmod(0o755)
        self.git("config", "user.name", "Push Guard Test")
        self.git("config", "user.email", "push-guard-test@example.invalid")
        self.git("remote", "add", "origin", str(self.remote))
        self.run_git("init", "--bare", str(self.remote), cwd=self.root)
        (self.repo / "base.txt").write_text("fixture baseline\n", encoding="utf-8")
        self.git("add", "base.txt", "scripts", ".githooks")
        self.git("commit", "-m", "baseline")
        self.baseline = self.rev_parse("HEAD")
        # Seed only this disposable local bare repository; no network remote is used.
        baseline_push = self.run_git(
            "-c", "core.hooksPath=/dev/null", "push", "origin",
            "refs/heads/main:refs/heads/main", cwd=self.repo,
        )
        if baseline_push.returncode:
            self.fail(f"could not seed disposable local bare remote: {baseline_push.stderr}")
        self.env = os.environ.copy()
        self.env["BETTERLEAKS_BIN"] = str(self.scanner_bin.resolve())
        self.env.pop("BETTERLEAKS_CONFIG", None)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def run_git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *map(str, args)],
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def git(self, *args: str) -> str:
        result = self.run_git(*args, cwd=self.repo)
        if result.returncode:
            self.fail(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout.strip()

    def rev_parse(self, ref: str) -> str:
        return self.git("rev-parse", ref)

    def commit_file(self, name: str, text: str, message: str) -> str:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.git("add", "--", name)
        self.git("commit", "-m", message)
        return self.rev_parse("HEAD")

    def scanner_mode(self, **settings: object) -> None:
        config: dict[str, object] = {}
        if self.scanner_config.exists():
            config.update(json.loads(self.scanner_config.read_text(encoding="utf-8")))
        config.update(settings)
        self.scanner_config.write_text(json.dumps(config), encoding="utf-8")
        if self.calls_path.exists():
            self.calls_path.unlink()

    def calls(self) -> list[dict[str, object]]:
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text(encoding="utf-8").splitlines()]

    def push(self, *specs: str) -> subprocess.CompletedProcess[str]:
        return self.run_git("push", "origin", *specs, cwd=self.repo, env=self.env)

    def remote_ref(self, ref: str) -> str | None:
        result = self.run_git("--git-dir", str(self.remote), "rev-parse", "--verify", ref, cwd=self.root)
        return result.stdout.strip() if result.returncode == 0 else None

    def guard(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(self.repo / "scripts" / GUARD.name), *args],
            cwd=self.repo,
            env=env or self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def write_sentinel_commit(self, name: str = "sentinel.txt") -> str:
        self.scanner_mode(find_sentinel=True)
        return self.commit_file(name, SENTINEL + "\n", "add test sentinel")

    def test_clean_local_push_succeeds_and_scans_full_tip(self) -> None:
        commit = self.commit_file("clean.txt", "ordinary test content\n", "clean change")
        result = self.push("refs/heads/main:refs/heads/main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_ref("refs/heads/main"), commit)
        calls = self.calls()
        self.assertEqual([call["commit"] for call in calls], [commit])
        args = calls[0]["args"]
        self.assertIn("--report-format", args)
        self.assertIn("sarif", args)
        self.assertIn("--redact=100", args)
        self.assertIn("--exit-code", args)
        self.assertIn("1", args)
        self.assertIn("--ignore-gitleaks-allow", args)
        self.assertTrue(any(str(arg).startswith("--log-opts=") and commit in str(arg) for arg in args))

    def test_new_branch_non_head_tip_is_scanned(self) -> None:
        self.git("checkout", "-b", "topic")
        commit = self.commit_file("topic.txt", "clean topic\n", "topic change")
        self.git("checkout", "main")
        self.assertEqual(self.rev_parse("HEAD"), self.baseline)
        result = self.push("refs/heads/topic:refs/heads/topic")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_ref("refs/heads/topic"), commit)
        self.assertEqual([call["commit"] for call in self.calls()], [commit])

    def test_linked_worktree_push_uses_its_actual_non_head_tip(self) -> None:
        linked = self.root / "linked worktree"
        added = self.run_git("worktree", "add", "-b", "linked-topic", str(linked), "main", cwd=self.repo)
        self.assertEqual(added.returncode, 0, added.stderr)
        (linked / "linked.txt").write_text("linked worktree clean change\n", encoding="utf-8")
        staged = self.run_git("add", "linked.txt", cwd=linked)
        self.assertEqual(staged.returncode, 0, staged.stderr)
        committed = self.run_git("commit", "-m", "linked worktree change", cwd=linked)
        self.assertEqual(committed.returncode, 0, committed.stderr)
        tip = self.run_git("rev-parse", "HEAD", cwd=linked).stdout.strip()

        result = self.run_git(
            "push", "origin", "refs/heads/linked-topic:refs/heads/linked-topic",
            cwd=linked, env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_ref("refs/heads/linked-topic"), tip)
        self.assertEqual([call["commit"] for call in self.calls()], [tip])

    def test_bad_tip_blocks_push_and_leaves_remote_ref_unchanged(self) -> None:
        bad_tip = self.write_sentinel_commit()
        result = self.push("refs/heads/main:refs/heads/main")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.remote_ref("refs/heads/main"), self.baseline)
        self.assertEqual([call["commit"] for call in self.calls()], [bad_tip])

    def test_multiple_updates_scan_both_actual_tips_before_any_ref_changes(self) -> None:
        self.git("checkout", "-b", "clean-branch")
        clean_tip = self.commit_file("clean-branch.txt", "clean\n", "clean branch")
        self.git("checkout", "main")
        self.git("checkout", "-b", "bad-branch")
        bad_tip = self.write_sentinel_commit("bad-branch.txt")
        self.git("checkout", "main")

        result = self.push(
            "refs/heads/clean-branch:refs/heads/clean-branch",
            "refs/heads/bad-branch:refs/heads/bad-branch",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call["commit"] for call in self.calls()], [clean_tip, bad_tip])
        self.assertIsNone(self.remote_ref("refs/heads/clean-branch"))
        self.assertIsNone(self.remote_ref("refs/heads/bad-branch"))

    def test_deleted_ref_is_safe_and_does_not_run_scanner(self) -> None:
        result = self.push(":refs/heads/main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.remote_ref("refs/heads/main"))
        self.assertEqual(self.calls(), [])

    def test_bad_older_commit_still_blocks_after_file_removed_from_tip(self) -> None:
        self.write_sentinel_commit("temporary.txt")
        (self.repo / "temporary.txt").unlink()
        self.git("add", "--", "temporary.txt")
        self.git("commit", "-m", "remove test sentinel")
        tip = self.rev_parse("HEAD")
        result = self.push("refs/heads/main:refs/heads/main")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.remote_ref("refs/heads/main"), self.baseline)
        self.assertEqual([call["commit"] for call in self.calls()], [tip])

    def test_guard_rejects_incomplete_invocation_and_false_execution_status(self) -> None:
        result = self.guard("abcd")
        self.assertNotEqual(result.returncode, 0)
        self.scanner_mode(report="execution-false")
        result = self.guard(self.baseline)
        self.assertNotEqual(result.returncode, 0)

    def test_manual_guard_uses_full_head_object_when_reference_is_omitted(self) -> None:
        result = self.guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["commit"] for call in self.calls()], [self.baseline])

    def test_guard_rejects_missing_or_wrong_scanner_and_wrong_version(self) -> None:
        env = self.env.copy()
        env["BETTERLEAKS_BIN"] = str(self.root / "missing-betterleaks")
        self.assertNotEqual(self.guard(self.baseline, env=env).returncode, 0)
        self.scanner_mode(version="9.9.9")
        self.assertNotEqual(self.guard(self.baseline).returncode, 0)

    def test_guard_writes_optional_report_path(self) -> None:
        report = self.root / "caller report path.sarif"
        result = self.guard(self.baseline, str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "2.1.0")
        self.assertEqual(data["runs"][0]["tool"]["driver"]["name"], "Betterleaks")
        self.assertEqual(data["runs"][0]["results"], [])

    def test_guard_rejects_shallow_repository(self) -> None:
        (self.repo / ".git" / "shallow").write_text(self.baseline + "\n", encoding="ascii")
        result = self.guard(self.baseline)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_guard_rejects_findings_nonzero_and_scanner_warning_or_error(self) -> None:
        self.scanner_mode(find_sentinel=True)
        self.write_sentinel_commit()
        tip = self.rev_parse("HEAD")
        self.assertNotEqual(self.guard(tip).returncode, 0)

        for mode in ("nonzero", "warning", "error"):
            with self.subTest(mode=mode):
                self.scanner_mode(mode=mode)
                self.assertNotEqual(self.guard(self.baseline).returncode, 0)

    def test_guard_rejects_missing_malformed_or_nonempty_reports(self) -> None:
        for report_mode in ("missing", "malformed", "nonempty", "execution-missing"):
            with self.subTest(report=report_mode):
                self.scanner_mode(report=report_mode)
                self.assertNotEqual(self.guard(self.baseline).returncode, 0)

    def test_guard_blocks_environment_and_repository_scanner_overrides(self) -> None:
        for name in (
            "BETTERLEAKS_CONFIG",
            "GITLEAKS_CONFIG",
            "BETTERLEAKS_CONFIG_TOML",
            "GITLEAKS_CONFIG_TOML",
        ):
            with self.subTest(environment=name):
                env = self.env.copy()
                env[name] = str(self.root / "override.yml")
                self.assertNotEqual(self.guard(self.baseline, env=env).returncode, 0)

        for name in (".betterleaksignore", ".gitleaksignore", ".betterleaks.toml", ".gitleaks.toml"):
            with self.subTest(repository_override=name):
                path = self.repo / name
                path.write_text("test-only ignore override\n", encoding="utf-8")
                self.assertNotEqual(self.guard(self.baseline).returncode, 0)
                path.unlink()

    def test_pre_push_blocks_findings_for_a_new_branch(self) -> None:
        self.git("checkout", "-b", "new-risk")
        tip = self.write_sentinel_commit("new-risk.txt")
        self.git("checkout", "main")
        result = self.push("refs/heads/new-risk:refs/heads/new-risk")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.remote_ref("refs/heads/main"), self.baseline)
        self.assertIsNone(self.remote_ref("refs/heads/new-risk"))
        self.assertEqual([call["commit"] for call in self.calls()], [tip])

    def test_annotated_tag_metadata_is_scanned_and_can_block_the_tag_ref(self) -> None:
        self.commit_file("tag-base.txt", "clean commit\n", "clean tag base")
        self.git("tag", "-a", "v-test", "-m", SENTINEL)
        self.scanner_mode(find_sentinel=True)
        tag_object = self.rev_parse("refs/tags/v-test")
        result = self.push("refs/tags/v-test:refs/tags/v-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(self.remote_ref("refs/tags/v-test"))
        calls = self.calls()
        self.assertEqual(calls[0]["commit"], tag_object)
        self.assertIsNone(calls[1]["commit"], "tag message is scanned as a directory artifact")

    def test_clean_annotated_tag_push_scans_tag_object_and_metadata(self) -> None:
        self.git("tag", "-a", "v-clean", "-m", "clean test tag")
        tag_object = self.rev_parse("refs/tags/v-clean")
        result = self.push("refs/tags/v-clean:refs/tags/v-clean")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_ref("refs/tags/v-clean"), tag_object)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["commit"], tag_object)
        self.assertIsNone(calls[1]["commit"])


if __name__ == "__main__":
    unittest.main()
