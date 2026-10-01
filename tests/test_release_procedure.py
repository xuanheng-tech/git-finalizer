from __future__ import annotations

import os
import io
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tarfile
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

    def test_bundle_documentation_links_are_pinned_without_changing_source(self) -> None:
        repository = self.fixture()
        originals = {name: (repository / name).read_text(encoding="utf-8")
                     for name in ("README.md", "CONTRIBUTING.md")}
        readme = repository / "README.md"
        readme.write_text(originals["README.md"] + "\n[Anchor](docs/agent-contract.md#scope)\n",
                          encoding="utf-8")
        originals["README.md"] = readme.read_text(encoding="utf-8")
        result = run_script("build", repository)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(repository / "dist" / f"{PACKAGE}.tar.gz", "r:gz") as archive:
            for name, original in originals.items():
                with self.subTest(file=name):
                    stream = archive.extractfile(f"{PACKAGE}/{name}")
                    self.assertIsNotNone(stream)
                    packaged = stream.read().decode("utf-8")
                    links = re.findall(r"\]\((docs/[^)]+)\)", original)
                    self.assertTrue(links)
                    expected = original
                    for target in links:
                        expected = expected.replace(f"]({target})", f"](https://github.com/"
                                                    f"xuanheng-tech/git-finalizer/blob/{RELEASE_TAG}/{target})")
                    self.assertEqual(packaged, expected)
                    self.assertEqual((repository / name).read_text(encoding="utf-8"), original)
                    self.assertNotIn("](docs/", packaged)

    def test_packaging_rejects_missing_or_escaping_documentation(self) -> None:
        repository = self.fixture()
        readme = repository / "README.md"
        original = readme.read_text(encoding="utf-8")
        for target in ("docs/missing.md", "docs/../LICENSE"):
            with self.subTest(target=target):
                readme.write_text(original + f"\n[Broken]({target})\n", encoding="utf-8")
                result = run_script("build", repository)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("packaged documentation link has no safe source file", result.stderr)
                self.assertFalse((repository / "dist" / f"{PACKAGE}.tar.gz").exists())

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

    def test_just_setup_uses_only_the_correct_hosts_credential_authority(self) -> None:
        for relative in (".github/workflows/ci.yml", ".github/workflows/release.yml",
                         ".gitea/workflows/quality.yml", ".gitea/workflows/release.yml"):
            with self.subTest(workflow=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                setup = text.split("uses: extractions/setup-just@", 1)[1].split("- name:", 1)[0]
                if relative.startswith(".github/"):
                    self.assertIn("github-token: ${{ github.token }}", setup)
                else:
                    self.assertIn('github-token: ""', setup)
                    self.assertNotIn("github.token", setup)

    def test_checkouts_fetch_the_tags_the_gates_read(self) -> None:
        """A depth-1 checkout has no tags, and the contract gate reads them."""
        for relative in (
            ".gitea/workflows/quality.yml",
            ".gitea/workflows/release.yml",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
        ):
            with self.subTest(workflow=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("fetch-depth: 0", text, f"{relative} cannot see any tags")

    def test_release_jobs_cannot_publish_the_same_tag_twice(self) -> None:
        """Both publishers are check-then-create, so one tag must mean one run at a time."""
        for relative in (".gitea/workflows/release.yml", ".github/workflows/release.yml"):
            with self.subTest(workflow=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("concurrency:", text, f"{relative} can run two releases at once")
                self.assertIn("group: release-${{ github.ref_name }}", text)
                self.assertIn("cancel-in-progress: false", text)

    def test_linter_warm_up_never_blocks_a_run_that_can_lint(self) -> None:
        """scripts/lint.sh resolves shellcheck itself; provisioning steps may not be fatal."""
        for relative in (
            ".gitea/workflows/quality.yml",
            ".gitea/workflows/release.yml",
        ):
            with self.subTest(workflow=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                step = text.split("- name: Install shellcheck", 1)[1].split("- name:", 1)[0]
                self.assertIn("continue-on-error: true", step)
                lint = text.split("- name: Lint shell scripts", 1)[1].split("- name:", 1)[0]
                self.assertIn("just lint", lint)
                self.assertNotIn("continue-on-error", lint)

    def test_both_hosts_pin_the_same_build_tools(self) -> None:
        """A digest divergence between hosts is a release defect, so the inputs may not drift.

        One host's dependency bot can otherwise move a pin that the other host keeps, and nothing
        in the suite would notice.
        """
        workflows = (
            ".gitea/workflows/quality.yml",
            ".gitea/workflows/release.yml",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
        )
        actions: dict[str, set[str]] = {}
        versions: dict[str, set[str]] = {}
        for relative in workflows:
            text = (ROOT / relative).read_text(encoding="utf-8")
            for line in text.splitlines():
                pinned = re.search(r"uses:\s*(\S+)/(\S+)@([0-9a-f]{40})", line)
                if pinned is not None:
                    action = f"{pinned.group(1)}/{pinned.group(2)}"
                    actions.setdefault(action, set()).add(pinned.group(3))
                declared = re.search(
                    r"(?:version|just-version):\s*\"([0-9][^\"]*)\"|uv python install (\S+)", line
                )
                if declared is not None:
                    key = "python" if declared.group(2) else "tool"
                    value = declared.group(2) or declared.group(1)
                    versions.setdefault(f"{key}:{value}", set()).add(relative)
        self.assertTrue(actions, "no pinned actions found; the parser stopped matching")
        for action, revisions in actions.items():
            self.assertEqual(
                len(revisions),
                1,
                f"{action} is pinned to different revisions across the hosts: {sorted(revisions)}",
            )
        for key, files in versions.items():
            self.assertEqual(
                len(files),
                len(workflows),
                f"{key} is not pinned by every workflow: missing from "
                f"{sorted(set(workflows) - files)}",
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

    def test_verify_refuses_to_author_its_own_checksums(self) -> None:
        """A missing SHA256SUMS.txt must fail, not be regenerated from the bytes under test."""
        repository = self.fixture()
        run_script("build", repository)
        (repository / "dist" / "SHA256SUMS.txt").unlink()
        result = run_script("verify", repository)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("only `build` may author a checksum file", result.stderr)

    def test_build_refuses_an_invalid_tag_before_touching_outputs(self) -> None:
        repository = self.fixture()
        output = repository / "dist"
        output.mkdir()
        sentinel = output / "user-notes.txt"
        sentinel.write_text("preserve this data\n")
        result = run_script("build", repository, tag="not-a-release")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(sentinel.read_text(), "preserve this data\n")

    def test_build_preserves_unknown_output_and_staging_content(self) -> None:
        repository = self.fixture()
        output = repository / "dist"
        output.mkdir()
        sentinel = output / "user-notes.txt"
        sentinel.write_text("preserve this data\n")
        stage = repository / ".release-stage"
        stage.mkdir()
        (stage / "user-notes.txt").write_text("preserve staging data\n")
        result = run_script("build", repository)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(sentinel.read_text(), "preserve this data\n")
        self.assertEqual((stage / "user-notes.txt").read_text(), "preserve staging data\n")

    def test_verify_rejects_links_and_duplicate_members(self) -> None:
        repository = self.fixture()
        self.assertEqual(run_script("build", repository).returncode, 0)
        artifact = repository / "dist" / f"{PACKAGE}.tar.gz"
        original = artifact.read_bytes()
        for kind in ("symlink", "hardlink", "duplicate"):
            with self.subTest(kind=kind):
                with tarfile.open(fileobj=io.BytesIO(original), mode="r:gz") as source:
                    with tarfile.open(artifact, "w:gz") as target:
                        for member in source.getmembers():
                            target.addfile(member, source.extractfile(member) if member.isfile() else None)
                        if kind == "duplicate":
                            member = source.getmember(f"{PACKAGE}/LICENSE")
                            target.addfile(member, source.extractfile(member))
                        else:
                            member = tarfile.TarInfo(f"{PACKAGE}/extra-link")
                            member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                            member.linkname = "/outside-the-package"
                            target.addfile(member)
                # Check the archive contract independently of the checksum gate.
                namespace = __import__("runpy").run_path(str(repository / "scripts/release.py"))
                with self.assertRaises(namespace["ReleaseError"]):
                    namespace["verify_bytes"](artifact, RELEASE_TAG)

    def test_verify_requires_one_exact_checksum_entry(self) -> None:
        repository = self.fixture()
        self.assertEqual(run_script("build", repository).returncode, 0)
        checksum = repository / "dist/SHA256SUMS.txt"
        original = checksum.read_text()
        checksum.write_text(original + original)
        result = run_script("verify", repository)
        self.assertEqual(result.returncode, 2, result.stdout)

    @staticmethod
    def _mkdir(path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path


if __name__ == "__main__":
    unittest.main()
