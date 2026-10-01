from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeliveryInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gf-delivery-invariants-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.environment = {
            "PATH": os.defpath,
            "HOME": str(self.root),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
        self.git("init", "-q", "--initial-branch=feature", str(self.repo))
        self.git("config", "user.name", "Synthetic Author")
        self.git("config", "user.email", "synthetic@example.invalid")
        (self.repo / "wanted.txt").write_text("base\n")
        self.git("add", "--", "wanted.txt")
        self.git("commit", "-qm", "base")
        self.git("init", "-q", "--bare", "--initial-branch=feature", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "--set-upstream", "origin", "feature")
        self.base = self.git("rev-parse", "HEAD").strip()
        (self.repo / "wanted.txt").write_text("reviewed candidate\n")

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repo if self.repo.exists() else self.root), *arguments],
            env=self.environment, stderr=subprocess.PIPE, text=True,
        )

    def finalize(self, mode: str = "normal") -> tuple[subprocess.CompletedProcess[str], dict]:
        command = [str(ROOT / "git-finalize"), "--summary", "--repo", str(self.repo)]
        if mode != "normal":
            command.extend(["--mode", mode])
        if mode != "verify-only":
            command.extend(["--message", "reviewed change"])
        result = subprocess.run(
            [*command, "--", "wanted.txt"], env=self.environment,
            capture_output=True, text=True, timeout=30,
        )
        return result, json.loads(result.stdout)

    def remote_head(self) -> str:
        return self.git("ls-remote", "--refs", "origin", "refs/heads/feature").split()[0]

    def clean_filter(self, body: str) -> None:
        filter_script = self.root / "filter.py"
        filter_script.write_text("import sys\nsys.stdin.buffer.read()\n" + body)
        self.git("config", "filter.synthetic.clean", f"/usr/bin/python3 {filter_script}")
        self.git("config", "filter.synthetic.required", "true")
        (self.repo / ".git/info/attributes").write_text("wanted.txt filter=synthetic\n")

    def readonly_state(self) -> tuple:
        return (
            self.git("rev-parse", "HEAD"), (self.repo / ".git/index").read_bytes(),
            self.git("status", "--porcelain=v1", "--untracked-files=all"),
            (self.repo / "wanted.txt").read_bytes(),
            sorted(str(path.relative_to(self.repo)) for path in (self.repo / ".git/objects").rglob("*")),
            (self.repo / ".git/config").read_bytes(),
        )

    def test_verify_only_checks_filtered_content_without_repository_writes(self) -> None:
        self.clean_filter("sys.stdout.write('ghp_' + 'A' * 24 + '\\n')\n")
        before = self.readonly_state()
        result, data = self.finalize("verify-only")
        self.assertEqual(result.returncode, 1, data)
        self.assertFalse(data["commit"]["created"])
        self.assertIn("候选敏感文件", data["reason"])
        self.assertEqual(self.readonly_state(), before)

    def test_verify_only_checks_filtered_binary_size(self) -> None:
        self.clean_filter("sys.stdout.buffer.write(b'\\0' * (6 * 1024 * 1024))\n")
        before = self.readonly_state()
        result, data = self.finalize("verify-only")
        self.assertEqual(result.returncode, 1, data)
        self.assertIn("5 MiB", data["reason"])
        self.assertEqual(self.readonly_state(), before)

    def test_verify_only_accepts_safe_filter_conversion(self) -> None:
        self.clean_filter("sys.stdout.write('safe normalized candidate\\n')\n")
        before = self.readonly_state()
        result, data = self.finalize("verify-only")
        self.assertEqual(result.returncode, 0, data)
        self.assertEqual(self.readonly_state(), before)

    def resume(self, *extra: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        result = subprocess.run(
            [str(ROOT / "git-finalize"), "--summary", "--repo", str(self.repo),
             "--resume-publish", self.git("rev-parse", "HEAD").strip(), *extra],
            env=self.environment, capture_output=True, text=True, timeout=30,
        )
        return result, json.loads(result.stdout)

    def test_resume_allows_deleting_a_published_path_now_ignored(self) -> None:
        (self.repo / "wanted.txt").write_text("base\n")
        cache = self.repo / "cache"
        cache.mkdir()
        (cache / "generated.pyc").write_bytes(b"synthetic published cache\n")
        self.git("add", "--", "cache/generated.pyc")
        self.git("commit", "-qm", "Synthetic legacy cache")
        self.git("push", "-q", "origin", "feature")
        (self.repo / ".gitignore").write_text("cache/\n")
        self.git("rm", "-q", "--", "cache/generated.pyc")
        self.git("add", "--", ".gitignore")
        self.git("commit", "-qm", "Remove and ignore synthetic cache")
        head = self.git("rev-parse", "HEAD").strip()
        result, data = self.resume()
        self.assertEqual(result.returncode, 0, data)
        self.assertEqual(self.remote_head(), head)

    def test_deleted_unpublished_ignored_blob_is_still_checked(self) -> None:
        (self.repo / "wanted.txt").write_text("base\n")
        cache = self.repo / "cache"
        cache.mkdir()
        (cache / "generated.pyc").write_bytes(b"synthetic unpublished cache\n")
        self.git("add", "--", "cache/generated.pyc")
        self.git("commit", "-qm", "Synthetic unpublished cache")
        (self.repo / ".gitignore").write_text("cache/\n")
        self.git("rm", "-q", "--", "cache/generated.pyc")
        self.git("add", "--", ".gitignore")
        self.git("commit", "-qm", "Remove synthetic cache")
        result, data = self.resume()
        self.assertEqual(result.returncode, 1, data)
        self.assertIn("ignored", data["reason"])
        self.assertFalse(data["push"]["executed"])
        self.assertEqual(self.remote_head(), self.base)

    def test_resume_revalidates_an_explicit_large_binary_exception(self) -> None:
        (self.repo / "wanted.txt").write_text("base\n")
        (self.repo / "sample.bin").write_bytes(b"\0" * (6 * 1024 * 1024))
        committed = subprocess.run(
            [str(ROOT / "git-finalize"), "--summary", "--repo", str(self.repo),
             "--mode", "commit-only", "--message", "Synthetic approved binary",
             "--allow-large-binary", "sample.bin", "--", "sample.bin"],
            env=self.environment, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(committed.returncode, 0, committed.stdout)
        before = self.git("rev-parse", "HEAD").strip()
        result, data = self.resume()
        self.assertEqual(result.returncode, 1, data)
        self.assertFalse(data["push"]["executed"])
        result, data = self.resume("--allow-large-binary", "sample.bin")
        self.assertEqual(result.returncode, 0, data)
        self.assertFalse(data["commit"]["created"])
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        self.assertEqual(self.remote_head(), before)

    def test_resume_large_binary_exception_does_not_cover_an_older_blob(self) -> None:
        (self.repo / "wanted.txt").write_text("base\n")
        target = self.repo / "sample.bin"
        for value in (b"\0", b"A\0"):
            target.write_bytes(value * (6 * 1024 * 1024))
            self.git("add", "--", "sample.bin")
            self.git("commit", "-qm", "Synthetic binary revision")
        result, data = self.resume("--allow-large-binary", "sample.bin")
        self.assertEqual(result.returncode, 1, data)
        self.assertFalse(data["push"]["executed"])
        self.assertEqual(self.remote_head(), self.base)

    def test_hook_cannot_replace_reviewed_content_in_the_same_path(self) -> None:
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'unreviewed hook content\\n' > wanted.txt\ngit add -- wanted.txt\n")
        hook.chmod(0o755)
        result, summary = self.finalize()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(summary["commit"]["created"])
        self.assertFalse(summary["push"]["executed"])
        self.assertEqual(self.remote_head(), self.base)
        self.assertIn("tree", summary["reason"])

    def test_pending_operations_never_turn_into_a_commit(self) -> None:
        marker = self.repo / ".git/CHERRY_PICK_HEAD"
        marker.write_text(self.base + "\n")
        before = (self.repo / ".git/index").read_bytes()
        for mode in ("verify-only", "commit-only", "normal"):
            with self.subTest(mode=mode):
                result, summary = self.finalize(mode)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertFalse(summary["commit"]["created"])
                self.assertFalse(summary["push"]["executed"])
                self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base)
                self.assertEqual((self.repo / ".git/index").read_bytes(), before)
        self.assertEqual(marker.read_text(), self.base + "\n")

    def test_failed_commit_process_reports_the_commit_it_already_created(self) -> None:
        binary = self.root / "bin"
        binary.mkdir()
        wrapper = binary / "git"
        wrapper.write_text(
            '#!/bin/sh\nis_commit=0\nfor arg in "$@"; do\n'
            '  [ "$arg" = commit ] && is_commit=1\ndone\n'
            '/usr/bin/git "$@"\nresult=$?\n'
            '[ "$is_commit" = 1 ] && [ "$result" = 0 ] && exit 1\nexit "$result"\n'
        )
        wrapper.chmod(0o755)
        self.environment["PATH"] = str(binary) + os.pathsep + os.defpath
        result, summary = self.finalize()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(summary["commit"]["created"])
        self.assertFalse(summary["push"]["executed"])
        self.assertNotEqual(self.git("rev-parse", "HEAD").strip(), self.base)
        self.assertEqual(self.remote_head(), self.base)

    def test_normal_publish_checks_existing_unpublished_commits(self) -> None:
        # Synthetic data only; the path itself exercises the sensitive-path gate.
        (self.repo / ".env").write_text("SYNTHETIC_FIXTURE=example\n")
        self.git("add", "--", ".env")
        self.git("commit", "-qm", "unreviewed local history")
        unpublished = self.git("rev-parse", "HEAD").strip()
        before_index = (self.repo / ".git/index").read_bytes()
        result, summary = self.finalize()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(summary["commit"]["created"])
        self.assertFalse(summary["push"]["executed"])
        self.assertEqual(self.remote_head(), self.base)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), unpublished)
        self.assertEqual((self.repo / ".git/index").read_bytes(), before_index)

    def test_summary_uses_the_python_selected_from_path(self) -> None:
        binary = self.root / "bin"
        binary.mkdir()
        probe = binary / "python3"
        log = self.root / "python-probe.log"
        probe.write_text(f'#!/bin/sh\nprintf "selected\\n" >> "{log}"\nexec "{sys.executable}" "$@"\n')
        probe.chmod(0o755)
        self.environment["PATH"] = str(binary) + os.pathsep + os.defpath
        result, summary = self.finalize("verify-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(summary["status"], "success")
        self.assertTrue(log.exists(), "the CLI bypassed the caller's Python selection")

    def test_initial_branch_publish_checks_inherited_local_history(self) -> None:
        self.git("config", "--unset", "branch.feature.remote")
        self.git("config", "--unset", "branch.feature.merge")
        self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        self.git("-C", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main")
        self.git("push", "-q", "origin", ":refs/heads/feature")
        (self.repo / ".env").write_text("SYNTHETIC_FIXTURE=example\n")
        self.git("add", "--", ".env")
        self.git("commit", "-qm", "unreviewed local history")
        before = self.git("rev-parse", "HEAD").strip()
        result = subprocess.run(
            [str(ROOT / "git-finalize"), "--summary", "--initial-branch-publish",
             "--remote", "origin", "--remote-branch", "feature", "--repo", str(self.repo),
             "--message", "reviewed change", "--", "wanted.txt"],
            env=self.environment, capture_output=True, text=True, timeout=30,
        )
        summary = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(summary["commit"]["created"])
        self.assertFalse(summary["push"]["executed"])
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        self.assertEqual(self.git("ls-remote", "--refs", "origin", "refs/heads/feature"), "")

    def test_quoted_json_credential_assignment_is_checked(self) -> None:
        (self.repo / "wanted.txt").write_text(json.dumps({"password": "synthetic-fixture-value"}))
        result, summary = self.finalize("verify-only")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(summary["commit"]["created"])
        self.assertIn("credential", summary["reason"])


if __name__ == "__main__":
    unittest.main()
