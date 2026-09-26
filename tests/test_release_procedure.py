from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release.py"


def declared_version() -> str:
    match = re.search(
        r'^readonly VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$',
        (ROOT / "git-finalize").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise AssertionError("cannot read the declared Finalizer version")
    return match.group(1)


# Derived so a version batch cannot silently leave this gate behind.
VERSION = declared_version()
RELEASE_TAG = f"v{VERSION}"
PACKAGE = f"git-finalizer-{VERSION}"


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
        self.assertIn(f"release_version={VERSION}", result.stdout)

    def test_preflight_rejects_a_declaration_mismatch(self) -> None:
        repository = self.fixture()
        readme = repository / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace(
                f"当前版本：`{VERSION}`", "当前版本：`0.0.0`"
            ),
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
        artifact = repository / "dist" / f"{PACKAGE}.tar.gz"
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
            ["tar", "-tzf", str(repository / "dist" / f"{PACKAGE}.tar.gz")],
            capture_output=True,
            text=True,
            check=True,
        )
        members = set(listing.stdout.splitlines())
        for required in (
            f"{PACKAGE}/LICENSE",
            f"{PACKAGE}/tool_cli_contract.json",
            f"{PACKAGE}/tool_skill_manifest.json",
            f"{PACKAGE}/git-finalize",
            f"{PACKAGE}/SECURITY.md",
            f"{PACKAGE}/CONTRIBUTING.md",
        ):
            self.assertIn(required, members)

    def test_ci_workflows_are_well_formed_and_gate_lint_and_checks(self) -> None:
        import re as _re

        workflows = (
            ".gitea/workflows/quality.yml",
            ".gitea/workflows/release.yml",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
        )
        for relative in workflows:
            with self.subTest(workflow=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("just check", text)
                self.assertIn("just lint", text)
                # A step header must sit at the six-space indentation used by these
                # files; deeper or shallower indentation silently breaks parsing.
                for line in text.splitlines():
                    header = _re.match(r"^( *)- name:", line)
                    if header is None:
                        continue
                    self.assertIn(
                        len(header.group(1)),
                        (0, 6),
                        f"{relative} has a mis-indented step header: {line!r}",
                    )

    def test_lint_script_resolves_a_usable_shellcheck(self) -> None:
        script = (ROOT / "scripts" / "lint.sh").read_text(encoding="utf-8")
        self.assertIn('"$@" --version', script)
        self.assertIn("GF_SHELLCHECK", script)
        self.assertIn("shellcheck-py", script)
        self.assertIn("no usable shellcheck", script)
        self.assertTrue((ROOT / "scripts" / "lint.sh").stat().st_mode & 0o111)

    def test_lint_resolver_uses_an_explicit_override_or_fails_cleanly(self) -> None:
        """A CI host may only have a shellcheck the build user cannot execute."""
        with tempfile.TemporaryDirectory(prefix="gf-lint-resolver-") as temporary:
            directory = Path(temporary)
            stub = directory / "shellcheck"
            log = directory / "invocations.log"
            stub.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "--version" ]; then echo stub; exit 0; fi\n'
                'echo "invoked: $*" >> "$STUB_LOG"\nexit 0\n',
                encoding="utf-8",
            )
            stub.chmod(0o755)
            environment = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(directory),
                "GF_SHELLCHECK": str(stub),
                "STUB_LOG": str(log),
            }
            override = subprocess.run(
                ["/bin/bash", "scripts/lint.sh"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(override.returncode, 0, override.stderr)
            invocations = log.read_text(encoding="utf-8")
            self.assertIn("--exclude=SC2016", invocations)
            self.assertIn("git-finalize", invocations)

            missing = subprocess.run(
                ["/bin/bash", "scripts/lint.sh"],
                cwd=ROOT,
                env={"PATH": str(directory / "empty"), "HOME": str(directory)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(missing.returncode, 2, missing.stdout)
            self.assertIn("no usable shellcheck", missing.stderr)


    def test_verify_rejects_a_package_with_the_wrong_file_set(self) -> None:
        # Removing one packaged file must fail the file-set gate, not silently pass.
        repository = self.fixture()
        run_script("build", repository)
        artifact = repository / "dist" / f"{PACKAGE}.tar.gz"
        staging = repository / "drop"
        subprocess.run(
            ["tar", "-xzf", str(artifact), "-C", str(self._mkdir(staging))], check=True
        )
        (staging / PACKAGE / "LICENSE").unlink()
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
                PACKAGE,
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
