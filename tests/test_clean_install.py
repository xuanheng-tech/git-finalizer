"""Exercise the release installation recipe with no optional tools or user configuration."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import runpy
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = (
    "env", "bash", "git", "realpath", "stat", "grep", "sed", "head", "tail",
    "cat", "cut", "tr", "sort", "wc", "date", "dirname", "basename", "sha256sum",
    "readlink", "rm", "awk", "od", "cmp", "comm", "find", "chmod",
)


@unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "CLI requires an ordinary POSIX user")
class CleanInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = tempfile.TemporaryDirectory(prefix="gf-clean-install-bundle-")
        cls.addClassCleanup(cls.bundle.cleanup)
        cls.bundle_root = Path(cls.bundle.name)
        release = runpy.run_path(str(ROOT / "scripts/release.py"))
        match = re.search(r'^readonly VERSION="([0-9.]+)"$',
                          (ROOT / "git-finalize").read_text(), re.MULTILINE)
        if match is None:
            raise AssertionError("missing source version")
        cls.version = match.group(1)
        tag = f"v{cls.version}"
        artifact = release["build"](tag, dist=cls.bundle_root / "dist")
        release["verify"](tag, dist=cls.bundle_root / "dist")
        unpacked = cls.bundle_root / "unpacked"
        unpacked.mkdir()
        # Match the documented GNU tar recipe after rejecting unsafe archive members.
        subprocess.run(["tar", "-xzf", str(artifact), "-C", str(unpacked)], check=True)
        cls.package = cls.bundle_root / "unpacked" / f"git-finalizer-{cls.version}"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gf-clean-installed-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        self.scratch = self.base / "temporary"
        self.scratch.mkdir()
        for name in TOOLS:
            executable = shutil.which(name)
            if executable is None:
                self.fail(f"missing baseline command: {name}")
            (self.bin / name).symlink_to(Path(executable).resolve())
        # The child must use the interpreter selected by CI, not /usr/bin/python3.
        (self.bin / "python3").symlink_to(Path(sys.executable).resolve())
        for source in self.package.glob("git-finalize*"):
            shutil.copy2(source, self.bin / source.name)
        self.executable = str(self.bin / "git-finalize")
        self.environment = {
            "PATH": str(self.bin), "HOME": str(self.home), "LC_ALL": "C",
            "TMPDIR": str(self.scratch), "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never", "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.branch = "feature/clean-install"
        self.git("init", "-q", f"--initial-branch={self.branch}")
        self.git("config", "user.name", "Synthetic Installer")
        self.git("config", "user.email", "installer@example.invalid")
        self.path = self.repo / "example.txt"
        self.path.write_text("initial installed state\n")
        self.run_cli("--initial-commit-only", "--message", "Synthetic initial state",
                     "--", "example.txt")

    def git(self, *args: str, repo: Path | None = None) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo or self.repo), *args], env=self.environment,
            stderr=subprocess.PIPE, text=True, timeout=30,
        ).strip()

    def state(self) -> tuple[str, bytes, str, bytes]:
        return (self.git("rev-parse", "HEAD"), (self.repo / ".git/index").read_bytes(),
                self.git("status", "--porcelain"), self.path.read_bytes())

    def run_cli(self, *args: str, succeeds: bool = True,
                environment: dict[str, str] | None = None) -> dict:
        result = subprocess.run(
            [self.executable, "--summary", "--repo", str(self.repo), *args],
            env=environment or self.environment, cwd=self.repo, capture_output=True,
            text=True, timeout=45,
        )
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def publish_initial(self) -> Path:
        remote = self.base / "remote.git"
        remote.mkdir()
        self.git("init", "-q", "--bare", "--initial-branch=main", repo=remote)
        self.git("remote", "add", "origin", str(remote))
        data = self.run_cli("--publish-existing-branch", self.git("rev-parse", "HEAD"),
                            "--remote", "origin", "--remote-branch", self.branch)
        self.assertFalse(data["commit"]["created"])
        self.assertEqual(data["mode_result"]["post_verify"], "passed")
        return remote

    def test_installed_core_lifecycle_uses_selected_python_without_integrations(self) -> None:
        for optional in ("worktree-controller", "snapshot-runner", "project-context", "tool-skill-sync"):
            self.assertIsNone(shutil.which(optional, path=self.environment["PATH"]))
        for companion in self.bin.glob("git-finalize-*.py"):
            compile(companion.read_bytes(), str(companion), "exec")
        selected = subprocess.check_output(
            ["python3", "-c", "import sys; print(sys.version_info[:2])"],
            env=self.environment, text=True, timeout=30,
        ).strip()
        self.assertEqual(selected, str(sys.version_info[:2]))
        self.path.write_text("verified installed state\n")
        before = self.state()
        data = self.run_cli("--mode", "verify-only", "--", "example.txt")
        self.assertFalse(data["commit"]["created"])
        self.assertFalse(data["push"]["executed"])
        self.assertEqual(self.state(), before)
        data = self.run_cli("--mode", "commit-only", "--message", "Installed local change",
                            "--", "example.txt")
        self.assertTrue(data["commit"]["created"])
        self.assertFalse(data["push"]["executed"])
        remote = self.publish_initial()
        self.path.write_text("published installed state\n")
        data = self.run_cli("--message", "Installed published change", "--", "example.txt")
        self.assertTrue(data["commit"]["created"])
        self.assertTrue(data["push"]["executed"])
        self.assertEqual(data["mode_result"]["post_verify"], "passed")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", self.branch, repo=remote))
        self.assertFalse((self.home / ".agents").exists())
        self.assertFalse((self.repo / ".git/worktree-controller").exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_installed_resume_reuses_commit_after_rejected_push(self) -> None:
        remote = self.publish_initial()
        hook = remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.path.write_text("recoverable installed change\n")
        data = self.run_cli("--message", "Installed recoverable change", "--", "example.txt",
                            succeeds=False)
        self.assertTrue(data["commit"]["created"])
        committed = self.git("rev-parse", "HEAD")
        hook.unlink()
        data = self.run_cli("--resume-publish", committed)
        self.assertFalse(data["commit"]["created"])
        self.assertEqual(self.git("rev-parse", "HEAD"), committed)
        self.assertEqual(self.git("rev-parse", self.branch, repo=remote), committed)
        self.assertEqual(data["mode_result"]["post_verify"], "passed")

    def test_runtime_identity_measures_installed_entry_and_changed_sidecar(self) -> None:
        sidecar = self.bin / "git-finalize-content-scan.py"
        sidecar.write_bytes(sidecar.read_bytes() + b"\n# Synthetic installation identity change.\n")
        self.path.write_text("runtime observation candidate\n")
        before = self.state()
        data = self.run_cli("--mode", "verify-only", "--", "example.txt")
        self.assertEqual(self.state(), before)
        runtime = data["diagnostics"]["runtime"]
        for key, path in (("entrypoint", Path(self.executable)), ("content_scanner", sidecar)):
            self.assertEqual(runtime[key], {
                "path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        self.assertNotEqual(runtime["content_scanner"]["sha256"],
                            hashlib.sha256((ROOT / sidecar.name).read_bytes()).hexdigest())
        self.assertIsNone(data["diagnostics"]["content_scan"])

    def test_symlink_entry_uses_its_resolved_installation_identity(self) -> None:
        target = Path(self.executable)
        alias = self.bin / "synthetic-finalizer-link"
        alias.symlink_to(target)
        self.executable = str(alias)
        self.path.write_text("symlink entry candidate\n")
        data = self.run_cli("--mode", "verify-only", "--", "example.txt")
        self.assertEqual(data["diagnostics"]["runtime"]["entrypoint"], {
            "path": str(target.resolve()), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        })

    def test_invalid_sidecar_report_fails_closed_without_echoing_output(self) -> None:
        sentinel = "SYNTHETIC_SIDECAR_OUTPUT_012345"
        sidecar = self.bin / "git-finalize-content-scan.py"
        sidecar.write_text(f"print({sentinel!r})\n")
        self.path.write_text("invalid report candidate\n")
        before = self.state()
        data = self.run_cli("--mode", "verify-only", "--", "example.txt", succeeds=False)
        self.assertEqual(self.state(), before)
        self.assertFalse(data["commit"]["created"])
        self.assertFalse(data["push"]["executed"])
        self.assertEqual(data["diagnostics"]["content_scan"]["detector"], "content-scan-error")
        self.assertNotIn(sentinel, json.dumps(data))

    def test_symlink_sidecar_has_no_target_checksum_and_remains_blocked(self) -> None:
        target = self.base / "synthetic-target.txt"
        target.write_text("Synthetic unrelated file, never a scanner.\n")
        sidecar = self.bin / "git-finalize-content-scan.py"
        sidecar.unlink()
        sidecar.symlink_to(target)
        self.path.write_text("symlink scanner candidate\n")
        before = self.state()
        data = self.run_cli("--mode", "verify-only", "--", "example.txt", succeeds=False)
        self.assertEqual(self.state(), before)
        self.assertIsNone(data["diagnostics"]["runtime"]["content_scanner"]["sha256"])
        self.assertEqual(data["diagnostics"]["content_scan"]["verdict"], "ERROR")

    def test_candidate_scanner_failure_reports_no_byte_identity(self) -> None:
        interpreter = self.bin / "python3"
        interpreter.unlink()
        interpreter.write_text(
            '#!/bin/sh\ncase "$3" in report-candidate) exit 1;; esac\n'
            f'exec {shlex.quote(sys.executable)} "$@"\n'
        )
        interpreter.chmod(0o755)
        self.path.write_text("candidate classifier failure\n")
        before = self.state()
        data = self.run_cli("--mode", "verify-only", "--", "example.txt", succeeds=False)
        self.assertEqual(self.state(), before)
        diagnostic = data["diagnostics"]["content_scan"]
        self.assertEqual(diagnostic["path"], "example.txt")
        self.assertEqual(diagnostic["stage"], "candidate")
        self.assertEqual(diagnostic["detector"], "content-scan-error")
        self.assertIsNone(diagnostic["sha256"])
        self.assertIsNone(diagnostic["blob_oid"])

    def test_unwritable_temporary_store_is_precise_and_preserves_git_state(self) -> None:
        blocked = self.base / "blocked-temporary"
        blocked.mkdir(mode=0o500)
        self.addCleanup(blocked.chmod, 0o700)
        bootstrap = self.base / "bootstrap"
        bootstrap.mkdir()
        (bootstrap / "sitecustomize.py").write_text(
            "import os, tempfile\ntempfile.tempdir = os.environ['GF_TEST_TEMP_DIR']\n"
        )
        self.path.write_text("readonly candidate state\n")
        before = self.state()
        data = self.run_cli(
            "--mode", "verify-only", "--", "example.txt", succeeds=False,
            environment={**self.environment, "PYTHONPATH": str(bootstrap),
                         "GF_TEST_TEMP_DIR": str(blocked)},
        )
        self.assertIn("candidate object temporary directory is unavailable", data["reason"])
        self.assertFalse(data["commit"]["created"])
        self.assertFalse(data["push"]["executed"])
        self.assertEqual(self.state(), before)
        self.assertEqual(list(blocked.iterdir()), [])

    def test_filter_failure_is_not_misreported_as_a_temporary_directory_denial(self) -> None:
        (self.repo / ".gitattributes").write_text("new.txt filter=broken\n")
        self.git("add", "--", ".gitattributes")
        self.git("commit", "-qm", "Synthetic filter attributes")
        self.git("config", "filter.broken.clean", "false")
        self.git("config", "filter.broken.required", "true")
        (self.repo / "new.txt").write_text("filter candidate\n")
        before = self.state()
        data = self.run_cli("--mode", "verify-only", "--", "new.txt", succeeds=False)
        self.assertEqual(data["status"], "blocked")
        self.assertNotIn("temporary directory is unavailable", data["reason"])
        self.assertEqual(self.state(), before)
        self.assertEqual(list(self.scratch.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
