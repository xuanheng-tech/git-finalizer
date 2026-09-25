from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release.py"
RELEASE_TAG = "v1.4.0"


def run_script(action: str, cwd: Path, tag: str = RELEASE_TAG) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "scripts/release.py", action, tag],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


class ReleaseProcedureTests(unittest.TestCase):
    def fixture(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="gf-release-procedure-"))
        self.addCleanup(shutil.rmtree, directory, True)
        destination = directory / "repo"
        destination.mkdir()
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-s", "--cached"],
            capture_output=True,
            text=True,
            check=True,
        )
        modes = {}
        for line in tracked.stdout.splitlines():
            metadata, relative = line.split("\t", 1)
            modes[relative] = int(metadata.split()[0], 8)
        untracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=True,
        )
        for relative in untracked.stdout.splitlines():
            modes.setdefault(relative, 0o644)
        for relative, mode in sorted(modes.items()):
            source = ROOT / relative
            target = destination / relative
            if not source.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(mode & 0o755)
        # scripts/release.py reads the commit timestamp for the pinned mtime.
        subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
        subprocess.run(["git", "-C", str(destination), "config", "user.name", "test"], check=True)
        subprocess.run(
            ["git", "-C", str(destination), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", str(destination), "add", "--", "."], check=True)
        subprocess.run(
            ["git", "-C", str(destination), "commit", "-qm", "release fixture"], check=True
        )
        return destination

    def test_preflight_accepts_the_current_version_batch(self) -> None:
        result = run_script("preflight", ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release_version=1.4.0", result.stdout)

    def test_preflight_rejects_a_declaration_mismatch(self) -> None:
        repository = self.fixture()
        readme = repository / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace("当前版本：`1.4.0`", "当前版本：`1.4.1`"),
            encoding="utf-8",
        )
        result = run_script("preflight", repository)
        self.assertEqual(result.returncode, 2)
        self.assertIn("disagree", result.stderr)

    def test_preflight_rejects_an_unbound_contract_digest(self) -> None:
        repository = self.fixture()
        contract = repository / "tool_cli_contract.json"
        original = contract.read_text(encoding="utf-8")
        self.assertIn('"scope":', original)
        contract.write_text(
            original.replace('"scope":', '"scope_renamed":', 1), encoding="utf-8"
        )
        result = run_script("preflight", repository)
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not bind the shipped CLI contract bytes", result.stderr)

    def test_preflight_rejects_a_missing_license(self) -> None:
        repository = self.fixture()
        (repository / "LICENSE").unlink()
        result = run_script("preflight", repository)
        self.assertEqual(result.returncode, 2)
        self.assertIn("LICENSE is missing", result.stderr)

    def test_build_is_deterministic_across_umask_and_runs(self) -> None:
        repository = self.fixture()
        first = run_script("build", repository)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        artifact = repository / "dist" / "git-finalizer-1.4.0.tar.gz"
        self.assertTrue(artifact.is_file())
        digest_one = artifact.read_bytes()
        for umask in (0o022, 0o002, 0o077):
            previous = os.umask(umask)
            try:
                result = run_script("build", repository)
            finally:
                os.umask(previous)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(artifact.read_bytes(), digest_one, f"umask {umask:o} changed bytes")
        verify = run_script("verify", repository)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertIn("sha256=", verify.stdout)

    def test_packaged_surface_carries_the_contract(self) -> None:
        repository = self.fixture()
        result = run_script("build", repository)
        self.assertEqual(result.returncode, 0, result.stderr)
        listing = subprocess.run(
            ["tar", "-tzf", str(repository / "dist" / "git-finalizer-1.4.0.tar.gz")],
            capture_output=True,
            text=True,
            check=True,
        )
        members = set(listing.stdout.splitlines())
        for required in (
            "git-finalizer-1.4.0/LICENSE",
            "git-finalizer-1.4.0/tool_cli_contract.json",
            "git-finalizer-1.4.0/tool_skill_manifest.json",
            "git-finalizer-1.4.0/git-finalize",
            "git-finalizer-1.4.0/SECURITY.md",
            "git-finalizer-1.4.0/CONTRIBUTING.md",
        ):
            self.assertIn(required, members)

    def test_verify_rejects_a_package_with_the_wrong_file_set(self) -> None:
        # Removing one packaged file must fail the file-set gate, not silently pass.
        repository = self.fixture()
        run_script("build", repository)
        artifact = repository / "dist" / "git-finalizer-1.4.0.tar.gz"
        staging = repository / "drop"
        subprocess.run(
            ["tar", "-xzf", str(artifact), "-C", str(self._mkdir(staging))], check=True
        )
        (staging / "git-finalizer-1.4.0" / "LICENSE").unlink()
        packed = subprocess.run(
            [
                "tar",
                "--sort=name",
                "--format=ustar",
                "--owner=0",
                "--group=0",
                "--numeric-owner",
                "-C",
                str(staging),
                "-cf",
                "-",
                "git-finalizer-1.4.0",
            ],
            capture_output=True,
            check=True,
        ).stdout
        artifact.write_bytes(
            subprocess.run(["gzip", "-n"], input=packed, capture_output=True, check=True).stdout
        )
        result = run_script("verify", repository)
        self.assertEqual(result.returncode, 2)
        self.assertIn("file set does not match", result.stderr)

    @staticmethod
    def _mkdir(path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path


if __name__ == "__main__":
    unittest.main()
