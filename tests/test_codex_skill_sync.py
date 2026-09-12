from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tooling import codex_skill_sync as sync


TOOLS = {
    "context-loader": ("0.1.5", ("codex-project-context",)),
    "snapshot-runner": ("1.4.0", ("snapshot-runner",)),
    "git-finalizer": ("0.9.3", ("codex-git-finalize",)),
}


def write_json(path: Path, value: object) -> None:
    path.write_bytes(sync.canonical_json(value))


def run_git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(repo), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


class SkillSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sources_root = self.root / "sources"
        self.install_root = self.root / "install"
        self.agents_root = self.root / "agents"
        self.bin_dir = self.root / "bin"
        self.hooks_dir = self.root / "hooks"
        self.sources_root.mkdir()
        self.bin_dir.mkdir()
        self.hooks_dir.mkdir()
        self._build_sources()
        self._build_installed_pair()
        self.sources = sync.Sources(self.sources_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _init_repo(self, repo: Path) -> None:
        run_git(repo, "init", "-q")
        run_git(repo, "config", "user.name", "Skill Sync Test")
        run_git(repo, "config", "user.email", "skill-sync@example.invalid")

    def _commit(self, repo: Path) -> None:
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-qm", "fixture")

    def _build_sources(self) -> None:
        owner = self.sources_root / "git-finalizer"
        skill = owner / "skills" / sync.SKILL_NAME
        for relative in sync.SKILL_PAYLOAD:
            path = skill / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture {relative}\n", encoding="utf-8")
        skill_sha = sync.tree_sha256(skill, sync.SKILL_PAYLOAD)
        compatibility_skill = (
            owner / "skills" / sync.COMPATIBILITY_SKILL_NAME / "SKILL.md"
        )
        compatibility_skill.parent.mkdir(parents=True)
        compatibility_skill.write_text(
            "---\n"
            f"name: {sync.COMPATIBILITY_SKILL_NAME}\n"
            "description: Deprecated compatibility entry.\n"
            "---\n\n"
            "Use $git-change-delivery.\n",
            encoding="utf-8",
        )
        compatibility_sha = sync.sha256_file(compatibility_skill)

        compatibility = {
            "schema_version": 1,
            "toolchain_contract_version": 3,
            "workflow_skill": {
                "name": sync.SKILL_NAME,
                "contract_version": 1,
                "canonical_owner": "git-finalizer",
                "compatibility_shims": [sync.COMPATIBILITY_SKILL_NAME],
            },
            "context_loader_contract_version": 1,
            "snapshot_runner_contract_version": 1,
            "git_finalizer_contract_version": 1,
            "worktree_controller_contract_version": 1,
            "worktree_controller_status": "available_external",
            "tools": {
                name: {
                    "source_directory": name,
                    "public_cli_contract_version": 1,
                    "skill_contract_version": 1,
                }
                for name in TOOLS
            },
        }
        write_json(owner / "toolchain_compatibility.json", compatibility)

        for name, (version, entrypoints) in TOOLS.items():
            repo = self.sources_root / name
            repo.mkdir(exist_ok=True)
            contract = {
                "schema_version": 1,
                "contract_version": 1,
                "tool_name": name,
                "tool_version": version,
                "commands": [
                    {
                        "name": entrypoints[0],
                        "flags": ["--version"],
                        "operation_class": "read_only",
                        "preconditions": [],
                        "result_statuses": ["complete"],
                    }
                ],
                "flags": {},
                "operation_classes": ["read_only"],
                "protected_operations": [],
            }
            if name == "git-finalizer":
                contract["host_bridge"] = {
                    "exposed_options": {"--summary": {"arity": 0, "repeatable": False}},
                    "path_delimiter": "--",
                    "unknown_options": "reject",
                }
            write_json(repo / "tool_cli_contract.json", contract)
            if name == "git-finalizer":
                executable_kind = "source_files"
                executable = repo / "codex-git-finalize"
                executable.write_text(
                    "#!/usr/bin/env bash\n"
                    f'readonly VERSION="{version}"\n'
                    "printf 'codex-git-finalize %s\\n' \"$VERSION\"\n",
                    encoding="utf-8",
                )
                executable.chmod(0o755)
                companion = repo / "codex-git-finalize-snapshot-verify.py"
                companion.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
                companion.chmod(0o755)
                bridge = repo / "hooks" / "codex_git_finalize_bridge.py"
                bridge.parent.mkdir()
                bridge.write_text(
                    "BRIDGE_OPTION_SPECS: dict[str, tuple[int, bool]] = "
                    '{"--summary": (0, False)}\n',
                    encoding="utf-8",
                )
                sync_entry = repo / "codex-skill-sync"
                sync_entry.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    'script_dir=$(CDPATH=\'\' cd -- "$(dirname -- "$0")" && pwd)\n'
                    'implementation="$script_dir/tooling/codex_skill_sync.py"\n'
                    'if [[ ! -f "$implementation" ]]; then\n'
                    '    implementation="$script_dir/codex-skill-sync-support/'
                    'codex_skill_sync.py"\n'
                    "fi\n"
                    'exec python3 -B "$implementation" "$@"\n',
                    encoding="utf-8",
                )
                sync_entry.chmod(0o755)
                sync_implementation = repo / "tooling" / "codex_skill_sync.py"
                sync_implementation.parent.mkdir()
                sync_implementation.write_text(
                    "#!/usr/bin/env python3\n"
                    "import argparse\n"
                    "parser = argparse.ArgumentParser(prog='codex-skill-sync')\n"
                    "parser.add_argument('--version', action='version', "
                    f"version='codex-skill-sync {sync.VERSION}')\n"
                    "parser.parse_args()\n",
                    encoding="utf-8",
                )
                sync_implementation.chmod(0o755)
                executable_definition = {
                    "kind": executable_kind,
                    "paths": [
                        "codex-git-finalize",
                        "codex-git-finalize-snapshot-verify.py",
                        "hooks/codex_git_finalize_bridge.py",
                        "codex-skill-sync",
                        "tooling/codex_skill_sync.py",
                    ],
                    "entrypoints": list(entrypoints),
                    "production_targets": {
                        "codex-git-finalize": "bin/codex-git-finalize",
                        "codex-git-finalize-snapshot-verify.py": (
                            "bin/codex-git-finalize-snapshot-verify.py"
                        ),
                        "hooks/codex_git_finalize_bridge.py": (
                            "hooks/codex_git_finalize_bridge.py"
                        ),
                        "codex-skill-sync": "bin/codex-skill-sync",
                        "tooling/codex_skill_sync.py": (
                            "bin/codex-skill-sync-support/codex_skill_sync.py"
                        ),
                    },
                }
            else:
                executable_kind = "python_console_scripts"
                scripts = "\n".join(
                    f'{entry} = "package.cli:main"' for entry in entrypoints
                )
                (repo / "pyproject.toml").write_text(
                    "[project]\n"
                    f'name = "{name}"\n'
                    f'version = "{version}"\n'
                    "[project.scripts]\n"
                    f"{scripts}\n",
                    encoding="utf-8",
                )
                executable_definition = {
                    "kind": executable_kind,
                    "entrypoints": list(entrypoints),
                }
            manifest = {
                "schema_version": sync.TOOL_SKILL_MANIFEST_SCHEMA_VERSION,
                "tool_name": name,
                "tool_version": version,
                "tool_commit": "@release",
                "skill_contract_version": 1,
                "public_cli_contract_sha256": sync.sha256_file(
                    repo / "tool_cli_contract.json"
                ),
                "canonical_skill_sha256": skill_sha,
                "compatible_toolchain_contract_version": 3,
                "canonical_skill": {
                    "owner": "git-finalizer",
                    "path": f"skills/{sync.SKILL_NAME}/SKILL.md",
                },
                "compatibility_skill": {
                    "name": sync.COMPATIBILITY_SKILL_NAME,
                    "path": (f"skills/{sync.COMPATIBILITY_SKILL_NAME}/SKILL.md"),
                    "sha256": compatibility_sha,
                },
                "executable": executable_definition,
                "install_targets": {
                    "stable_entries": list(entrypoints),
                    "skill_directory": sync.SKILL_NAME,
                },
            }
            write_json(repo / "tool_skill_manifest.json", manifest)
            self._init_repo(repo)
            self._commit(repo)

    def _build_installed_pair(self) -> None:
        installed_skill = self.agents_root / "skills" / sync.SKILL_NAME
        source_skill = self.sources_root / "git-finalizer" / "skills" / sync.SKILL_NAME
        for relative in sync.SKILL_PAYLOAD:
            target = installed_skill / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_skill / relative, target)
        compatibility_source = (
            self.sources_root
            / "git-finalizer"
            / "skills"
            / sync.COMPATIBILITY_SKILL_NAME
            / "SKILL.md"
        )
        compatibility_target = (
            self.agents_root / "skills" / sync.COMPATIBILITY_SKILL_NAME / "SKILL.md"
        )
        compatibility_target.parent.mkdir(parents=True)
        shutil.copyfile(compatibility_source, compatibility_target)
        for _, (version, entrypoints) in TOOLS.items():
            for entrypoint in entrypoints:
                path = self.bin_dir / entrypoint
                path.write_text(
                    f"#!/usr/bin/env sh\nprintf '%s {version}\\n' '{entrypoint}'\n",
                    encoding="utf-8",
                )
                path.chmod(0o755)
        (self.bin_dir / "codex-git-finalize-snapshot-verify.py").write_text(
            "old companion\n", encoding="utf-8"
        )
        (self.hooks_dir / "codex_git_finalize_bridge.py").write_text(
            "BRIDGE_OPTION_SPECS: dict[str, tuple[int, bool]] = "
            "{'--old': (0, False)}\n",
            encoding="utf-8",
        )

    def _current(self, tool: str) -> Path:
        return (self.install_root / "tools" / tool / "CURRENT").resolve(strict=True)

    def _update_skill_manifest_hashes(self) -> None:
        skill = self.sources_root / "git-finalizer" / "skills" / sync.SKILL_NAME
        digest = sync.tree_sha256(skill, sync.SKILL_PAYLOAD)
        compatibility = (
            self.sources_root
            / "git-finalizer"
            / "skills"
            / sync.COMPATIBILITY_SKILL_NAME
            / "SKILL.md"
        )
        compatibility_digest = sync.sha256_file(compatibility)
        for tool in TOOLS:
            path = self.sources_root / tool / "tool_skill_manifest.json"
            manifest = sync.read_json(path)
            manifest["canonical_skill_sha256"] = digest
            manifest["compatibility_skill"]["sha256"] = compatibility_digest
            write_json(path, manifest)

    def _install_production(
        self,
        tool: str,
        *,
        allow_dirty_source: bool = False,
    ) -> dict[str, object]:
        return sync.install(
            self.sources,
            tool,
            self.install_root,
            allow_dirty_source=allow_dirty_source,
            activate_production=True,
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )

    def _deployment(self, tool: str = "snapshot-runner") -> dict[str, object]:
        return sync.check_tool_deployment(
            self.sources, tool, self.agents_root, self.bin_dir
        )

    def _rewrite_entry(self, entrypoint: str, version: str) -> None:
        path = self.bin_dir / entrypoint
        path.write_text(
            f"#!/usr/bin/env sh\nprintf '%s {version}\\n' '{entrypoint}'\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def test_check_deployment_passes_when_installation_matches(self) -> None:
        report = self._deployment()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["source_contract_status"], "PASS")
        self.assertEqual(report["installed_drift"], "none")
        self.assertEqual(report["installed_binary_version"], "1.4.0")
        self.assertEqual(report["errors"], [])
        # 2.0.0 ships a single provider-neutral console script.
        self.assertEqual(sorted(report["installed_binaries"]), ["snapshot-runner"])

    def test_check_deployment_fails_on_binary_only_drift(self) -> None:
        for entrypoint in TOOLS["snapshot-runner"][1]:
            self._rewrite_entry(entrypoint, "9.9.9")
        report = self._deployment()
        self.assertEqual(report["status"], "FAIL")
        # The source contract is still internally valid; only production drifted.
        self.assertEqual(report["source_contract_status"], "PASS")
        self.assertEqual(report["installed_drift"], "binary_only")
        self.assertEqual(report["installed_binary_version"], "9.9.9")
        self.assertTrue(any("binary_only" in error for error in report["errors"]))

    def test_check_deployment_detects_missing_primary_entrypoint(self) -> None:
        (self.bin_dir / "snapshot-runner").unlink()
        report = self._deployment()
        self.assertEqual(report["status"], "FAIL")
        # An unresolvable installation reports no version at all.
        self.assertIsNone(report["installed_binary_version"])
        self.assertEqual(report["installed_drift"], "binary_only")
        self.assertIsNone(report["installed_binaries"]["snapshot-runner"]["path"])

    def test_check_deployment_detects_wrong_primary_entrypoint_version(self) -> None:
        self._rewrite_entry("snapshot-runner", "9.9.9")
        report = self._deployment()
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["installed_binary_version"], "9.9.9")
        self.assertEqual(report["installed_drift"], "binary_only")
        self.assertEqual(
            report["installed_binaries"]["snapshot-runner"]["version"], "9.9.9"
        )

    def test_check_deployment_fails_when_manifest_omits_primary_entrypoint(
        self,
    ) -> None:
        path = self.sources_root / "snapshot-runner" / "tool_skill_manifest.json"
        manifest = sync.read_json(path)
        entrypoints = [
            name
            for name in manifest["executable"]["entrypoints"]
            if name != "snapshot-runner"
        ]
        manifest["executable"]["entrypoints"] = entrypoints
        manifest["install_targets"]["stable_entries"] = entrypoints
        write_json(path, manifest)
        report = self._deployment()
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["source_contract_status"], "FAIL")

    def test_source_only_check_ignores_installed_drift(self) -> None:
        for entrypoint in TOOLS["snapshot-runner"][1]:
            self._rewrite_entry(entrypoint, "9.9.9")
        self.assertEqual(
            sync.check_tool(self.sources, "snapshot-runner")["status"], "PASS"
        )
        self.assertEqual(self._deployment()["status"], "FAIL")

    # --- safe production upgrade path -------------------------------------------------

    def _uv_tool_layout(
        self,
        version: str,
        entrypoints: tuple[str, ...] | None = None,
        bin_dir: Path | None = None,
    ) -> Path:
        """Build a uv-tool-shaped installation for snapshot-runner in the fixture."""
        root = self.root / "uvtools" / "snapshot-runner"
        site = root / "lib" / "python3.12" / "site-packages"
        package = site / "codex_snapshot_runner"
        package.mkdir(parents=True, exist_ok=True)
        module = package / "__init__.py"
        module.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        dist_info = site / f"snapshot_runner-{version}.dist-info"
        if dist_info.exists():
            shutil.rmtree(dist_info)
        dist_info.mkdir(parents=True)
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(module.read_bytes()).digest())
            .rstrip(b"=")
            .decode()
        )
        (dist_info / "RECORD").write_text(
            f"codex_snapshot_runner/__init__.py,sha256={digest},{module.stat().st_size}\n"
            f"../../../bin/snapshot-runner,sha256=ignored,1\n"
            f"snapshot_runner-{version}.dist-info/RECORD,,\n",
            encoding="utf-8",
        )
        for stale in site.glob("snapshot_runner-*.dist-info"):
            if stale != dist_info:
                shutil.rmtree(stale)
        venv_bin = root / "bin"
        venv_bin.mkdir(exist_ok=True)
        entrypoints = entrypoints or TOOLS["snapshot-runner"][1]
        install_dir = bin_dir or self.bin_dir
        entries = ",\n".join(
            f'    {{ name = "{name}", install-path = "{install_dir / name}", '
            f'from = "snapshot-runner" }}'
            for name in entrypoints
        )
        (root / "uv-receipt.toml").write_text(
            "[tool]\n"
            f'requirements = [{{ name = "snapshot-runner", specifier = "=={version}" }}]\n'
            f"entrypoints = [\n{entries},\n]\n"
            "\n[tool.options]\n"
            'index = [{ url = "https://pypi.org/simple" }]\n'
            "no-build = true\n",
            encoding="utf-8",
        )
        for name in entrypoints:
            target = venv_bin / name
            target.write_text(
                f"#!/usr/bin/env sh\nprintf '%s {version}\\n' '{name}'\n",
                encoding="utf-8",
            )
            target.chmod(0o755)
            link = install_dir / name
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(target)
        return root

    def _upgrade(self, **kwargs):
        return sync.upgrade(
            self.sources, "snapshot-runner", bin_dir=self.bin_dir, **kwargs
        )

    def test_upgrade_is_unchanged_and_verified_when_already_current(self) -> None:
        self._uv_tool_layout("1.4.0")
        report = self._upgrade()
        self.assertEqual(report["status"], "UNCHANGED")
        self.assertEqual(report["target_version"], "1.4.0")
        self.assertEqual(report["no_build"], True)
        self.assertEqual(report["bin_dir"], str(self.bin_dir))
        self.assertEqual(report["verification"]["record"]["verified_files"], 1)
        self.assertEqual(
            sorted(report["verification"]["entries"]),
            sorted(TOOLS["snapshot-runner"][1]),
        )

    def test_upgrade_preserves_bin_dir_index_and_no_build(self) -> None:
        self._uv_tool_layout("1.3.0")
        captured: dict[str, object] = {}

        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            captured.update(
                tool=tool,
                version=version,
                bin_dir=bin_dir,
                indexes=indexes,
                no_build=no_build,
            )
            self._uv_tool_layout(version)

        with mock.patch.object(sync, "install_python_tool", fake_install):
            report = self._upgrade()
        self.assertEqual(report["status"], "UPGRADED")
        self.assertEqual(captured["version"], "1.4.0")
        self.assertEqual(captured["bin_dir"], str(self.bin_dir))
        self.assertEqual(captured["no_build"], True)
        self.assertEqual(captured["indexes"], ["https://pypi.org/simple"])
        self.assertEqual(report["verification"]["version"], "1.4.0")

    def test_failed_upgrade_restores_the_previous_working_installation(self) -> None:
        """An alias conflict or any install failure must not strand the CLIs."""
        self._uv_tool_layout("1.3.0")
        attempts: list[str] = []

        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            attempts.append(version)
            if version == "1.4.0":
                raise sync.SyncError(
                    "uv tool install failed: Executables already exist: codex-diff-audit"
                )
            self._uv_tool_layout(version)

        with mock.patch.object(sync, "install_python_tool", fake_install):
            with self.assertRaises(sync.SyncError) as error:
                self._upgrade()
        self.assertIn("restored", str(error.exception))
        self.assertEqual(attempts, ["1.4.0", "1.3.0"])
        # The previous release is installed and every entrypoint still works.
        state = sync.installed_binary_state(TOOLS["snapshot-runner"][1], self.bin_dir)
        self.assertEqual(state["version"], "1.3.0")
        self.assertEqual(len(state["entries"]), 1)

    def test_upgrade_fails_closed_when_verification_rejects_the_result(self) -> None:
        self._uv_tool_layout("1.3.0")

        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            # Simulate an install that silently produced the wrong version.
            self._uv_tool_layout("9.9.9" if version == "1.4.0" else version)

        with mock.patch.object(sync, "install_python_tool", fake_install):
            with self.assertRaises(sync.SyncError):
                self._upgrade()
        state = sync.installed_binary_state(TOOLS["snapshot-runner"][1], self.bin_dir)
        self.assertEqual(state["version"], "1.3.0")

    def test_upgrade_detects_runtime_files_that_do_not_match_the_artifact(self) -> None:
        root = self._uv_tool_layout("1.4.0")
        module = root / "lib" / "python3.12" / "site-packages" / "codex_snapshot_runner"
        (module / "__init__.py").write_text(
            "__version__ = 'tampered'\n", encoding="utf-8"
        )
        with self.assertRaises(sync.SyncError) as error:
            self._upgrade()
        self.assertIn("does not match the published artifact", str(error.exception))

    def test_upgrade_accepts_a_release_that_removes_entrypoints(self) -> None:
        """A major release may drop console scripts; retained ones must keep their paths."""
        legacy = ("snapshot-runner", "legacy-alias")
        root = self._uv_tool_layout("1.3.0", entrypoints=legacy)
        self.assertTrue((self.bin_dir / "legacy-alias").exists())

        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            # The new release ships only the neutral primary command.
            self._uv_tool_layout(version, entrypoints=("snapshot-runner",))
            (self.bin_dir / "legacy-alias").unlink()

        with mock.patch.object(sync, "install_python_tool", fake_install):
            report = self._upgrade()
        self.assertEqual(report["status"], "UPGRADED")
        self.assertEqual(report["verification"]["version"], "1.4.0")
        self.assertEqual(
            report["verification"]["retired_entrypoints"], ["legacy-alias"]
        )
        self.assertEqual(sorted(report["verification"]["entries"]), ["snapshot-runner"])

    def test_upgrade_still_rejects_a_moved_retained_entrypoint(self) -> None:
        self._uv_tool_layout("1.3.0")
        moved = self.root / "elsewhere"
        moved.mkdir()

        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            self._uv_tool_layout(version)
            link = self.bin_dir / "snapshot-runner"
            link.unlink()
            target = moved / "snapshot-runner"
            target.write_text(
                "#!/usr/bin/env sh\nprintf '%s 1.4.0\\n' 'snapshot-runner'\n",
                encoding="utf-8",
            )
            target.chmod(0o755)
            link.symlink_to(target)

        # Relocating a retained entrypoint out of its uv tool root must fail closed.
        with mock.patch.object(sync, "install_python_tool", fake_install):
            with self.assertRaises(sync.SyncError):
                self._upgrade()

    def _declare_uv_policy(self, bin_dir: Path, *, no_build: bool = True) -> None:
        path = self.sources_root / "git-finalizer" / "toolchain_compatibility.json"
        compatibility = sync.read_json(path)
        compatibility["tools"]["snapshot-runner"]["uv_install_policy"] = {
            "bin_dir": str(bin_dir),
            "index": ["https://pypi.org/simple"],
            "no_build": no_build,
        }
        write_json(path, compatibility)
        self.sources = sync.Sources(self.sources_root)

    def test_declared_policy_normalizes_a_drifted_receipt(self) -> None:
        """A drifted bin dir/index/no-build is normalized, not preserved."""
        self._uv_tool_layout("1.4.0")
        drifted = self.root / "drifted-bin"
        drifted.mkdir()
        self._declare_uv_policy(drifted)
        captured: dict[str, object] = {}

        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            captured.update(bin_dir=bin_dir, indexes=indexes, no_build=no_build)
            self._uv_tool_layout(version, bin_dir=drifted)

        with mock.patch.object(sync, "install_python_tool", fake_install):
            report = self._upgrade()
        # Already at the target version, so this is a deployment-only normalization.
        self.assertEqual(report["status"], "NORMALIZED")
        self.assertTrue(report["receipt_drifted"])
        self.assertEqual(report["bin_dir"], str(drifted))
        self.assertEqual(captured["bin_dir"], str(drifted))
        self.assertEqual(captured["indexes"], ["https://pypi.org/simple"])
        self.assertEqual(captured["no_build"], True)

    def test_declared_policy_reports_unchanged_when_already_canonical(self) -> None:
        self._uv_tool_layout("1.4.0")
        self._declare_uv_policy(self.bin_dir)
        with mock.patch.object(
            sync, "install_python_tool", side_effect=AssertionError("must not install")
        ):
            report = self._upgrade()
        self.assertEqual(report["status"], "UNCHANGED")
        self.assertFalse(report["receipt_drifted"])

    def test_without_a_declared_policy_the_receipt_is_preserved(self) -> None:
        self._uv_tool_layout("1.4.0")
        report = self._upgrade()
        self.assertEqual(report["status"], "UNCHANGED")
        self.assertFalse(report["receipt_drifted"])
        self.assertEqual(report["bin_dir"], str(self.bin_dir))

    def test_upgrade_dry_run_does_not_install(self) -> None:
        self._uv_tool_layout("1.3.0")
        with mock.patch.object(
            sync, "install_python_tool", side_effect=AssertionError("must not install")
        ):
            report = self._upgrade(dry_run=True)
        self.assertEqual(report["status"], "DRY_RUN")
        self.assertEqual(report["previous_version"], "1.3.0")
        self.assertEqual(report["target_version"], "1.4.0")

    def test_check_and_status_report_matching_pair(self) -> None:
        for tool in TOOLS:
            self.assertEqual(sync.check_tool(self.sources, tool)["status"], "PASS")
        report = sync.status(self.sources, self.agents_root, self.bin_dir)
        self.assertEqual(
            {item["tool_name"]: item["drift"] for item in report["tools"]},
            {tool: "none" for tool in TOOLS},
        )

    def test_status_classifies_binary_skill_and_incompatible_drift(self) -> None:
        context_entry = self.bin_dir / "codex-project-context"
        context_entry.write_text(
            "#!/usr/bin/env sh\nprintf 'tool 9.9.9\\n'\n", encoding="utf-8"
        )
        context_entry.chmod(0o755)
        report = sync.status(self.sources, self.agents_root, self.bin_dir)
        by_tool = {item["tool_name"]: item for item in report["tools"]}
        self.assertEqual(by_tool["context-loader"]["drift"], "binary_only")

        installed_skill = self.agents_root / "skills" / sync.SKILL_NAME / "SKILL.md"
        installed_skill.write_text("drift\n", encoding="utf-8")
        report = sync.status(self.sources, self.agents_root, self.bin_dir)
        by_tool = {item["tool_name"]: item for item in report["tools"]}
        self.assertEqual(by_tool["context-loader"]["drift"], "incompatible")
        self.assertEqual(by_tool["snapshot-runner"]["drift"], "skill_only")

    def test_check_fails_on_contract_hash_mismatch(self) -> None:
        contract = self.sources_root / "context-loader" / "tool_cli_contract.json"
        payload = sync.read_json(contract)
        payload["protected_operations"].append("tampered")
        write_json(contract, payload)
        checked = sync.check_tool(self.sources, "context-loader")
        self.assertEqual(checked["status"], "FAIL")
        self.assertIn("public CLI contract SHA mismatch", checked["errors"][0])

    def test_check_rejects_git_finalizer_bridge_contract_drift(self) -> None:
        bridge = (
            self.sources_root
            / "git-finalizer"
            / "hooks"
            / "codex_git_finalize_bridge.py"
        )
        bridge.write_text(
            "BRIDGE_OPTION_SPECS: dict[str, tuple[int, bool]] = "
            '{"--unknown": (0, False)}\n',
            encoding="utf-8",
        )

        checked = sync.check_tool(self.sources, "git-finalizer")

        self.assertEqual(checked["status"], "FAIL")
        self.assertIn("host bridge and CLI contract differ", checked["errors"][0])

    def test_check_rejects_nondeterministic_json_serialization(self) -> None:
        contract = self.sources_root / "context-loader" / "tool_cli_contract.json"
        contract.write_text(
            contract.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        checked = sync.check_tool(self.sources, "context-loader")
        self.assertEqual(checked["status"], "FAIL")
        self.assertIn("not deterministically serialized", checked["errors"][0])

    def test_incompatible_public_contract_version_fails(self) -> None:
        compatibility_path = (
            self.sources_root / "git-finalizer" / "toolchain_compatibility.json"
        )
        compatibility = sync.read_json(compatibility_path)
        compatibility["tools"]["snapshot-runner"]["public_cli_contract_version"] = 2
        write_json(compatibility_path, compatibility)
        sources = sync.Sources(self.sources_root)
        checked = sync.check_tool(sources, "snapshot-runner")
        self.assertEqual(checked["status"], "FAIL")
        self.assertIn("incompatible", checked["errors"][0])

    def test_install_all_tools_is_atomic_and_deterministic(self) -> None:
        for tool in TOOLS:
            installed = sync.install(
                self.sources,
                tool,
                self.install_root,
                allow_dirty_source=False,
            )
            self.assertEqual(installed["status"], "INSTALLED")
            current = self._current(tool)
            release_before = sync.sha256_file(current / "release_manifest.json")
            repeated = sync.install(
                self.sources,
                tool,
                self.install_root,
                allow_dirty_source=False,
            )
            self.assertEqual(repeated["status"], "UNCHANGED")
            self.assertEqual(
                release_before,
                sync.sha256_file(self._current(tool) / "release_manifest.json"),
            )

    def test_install_failure_keeps_previous_pair_active(self) -> None:
        sync.install(
            self.sources,
            "git-finalizer",
            self.install_root,
            allow_dirty_source=False,
        )
        before = self._current("git-finalizer")
        with mock.patch.object(
            sync, "smoke_bundle", side_effect=sync.SyncError("smoke failed")
        ):
            with self.assertRaisesRegex(sync.SyncError, "smoke failed"):
                sync.install(
                    self.sources,
                    "git-finalizer",
                    self.install_root,
                    allow_dirty_source=False,
                )
        self.assertEqual(self._current("git-finalizer"), before)

    def test_pre_rename_bundle_remains_verifiable_for_migration(self) -> None:
        sync.install(
            self.sources,
            "git-finalizer",
            self.install_root,
            allow_dirty_source=False,
        )
        legacy = self.root / "legacy-bundle"
        shutil.copytree(self._current("git-finalizer"), legacy)
        manifest_path = legacy / "tool_skill_manifest.json"
        manifest = sync.read_json(manifest_path)
        manifest["schema_version"] = 1
        manifest.pop("compatibility_skill")
        manifest["canonical_skill"]["path"] = (
            f"skills/{sync.COMPATIBILITY_SKILL_NAME}/SKILL.md"
        )
        manifest["install_targets"]["skill_directory"] = sync.COMPATIBILITY_SKILL_NAME
        write_json(manifest_path, manifest)
        shutil.rmtree(legacy / "compatibility")
        release_path = legacy / "release_manifest.json"
        release = sync.read_json(release_path)
        release["manifest_sha"] = sync.sha256_file(manifest_path)
        write_json(release_path, release)

        verified = sync.verify_bundle(legacy, "git-finalizer")

        self.assertEqual(verified["tool_name"], "git-finalizer")

    def test_production_activation_installs_verified_binary_hook_and_skill(
        self,
    ) -> None:
        installed = self._install_production("git-finalizer")

        self.assertTrue(installed["production_activation"])
        current = self._current("git-finalizer")
        self.assertEqual(
            (self.bin_dir / "codex-git-finalize").read_bytes(),
            (current / "executable" / "codex-git-finalize").read_bytes(),
        )
        self.assertEqual(
            (self.hooks_dir / "codex_git_finalize_bridge.py").read_bytes(),
            (
                current / "executable" / "hooks" / "codex_git_finalize_bridge.py"
            ).read_bytes(),
        )
        self.assertEqual(
            sync.tree_sha256(
                self.agents_root / "skills" / sync.SKILL_NAME,
                sync.SKILL_PAYLOAD,
            ),
            sync.read_json(current / "release_manifest.json")["skill_sha"],
        )
        self.assertEqual(
            (
                self.agents_root / "skills" / sync.COMPATIBILITY_SKILL_NAME / "SKILL.md"
            ).read_bytes(),
            (current / sync.COMPATIBILITY_BUNDLE_PATH).read_bytes(),
        )
        smoke = subprocess.run(
            (str(self.bin_dir / "codex-skill-sync"), "--version"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        self.assertIn(sync.VERSION, smoke.stdout)

    def test_activation_accepts_verified_previous_sync_version(self) -> None:
        implementation = (
            self.sources_root / "git-finalizer" / "tooling" / "codex_skill_sync.py"
        )
        current_source = implementation.read_text(encoding="utf-8")
        implementation.write_text(
            current_source.replace(sync.VERSION, "1.0.0"),
            encoding="utf-8",
        )
        sync.install(
            self.sources,
            "git-finalizer",
            self.install_root,
            allow_dirty_source=True,
        )
        legacy = self._current("git-finalizer")
        legacy_manifest_path = legacy / "tool_skill_manifest.json"
        legacy_manifest = sync.read_json(legacy_manifest_path)
        legacy_manifest["schema_version"] = 1
        legacy_manifest.pop("compatibility_skill")
        legacy_manifest["canonical_skill"]["path"] = (
            f"skills/{sync.COMPATIBILITY_SKILL_NAME}/SKILL.md"
        )
        legacy_manifest["install_targets"]["skill_directory"] = (
            sync.COMPATIBILITY_SKILL_NAME
        )
        write_json(legacy_manifest_path, legacy_manifest)
        shutil.rmtree(legacy / "compatibility")
        legacy_release_path = legacy / "release_manifest.json"
        legacy_release = sync.read_json(legacy_release_path)
        legacy_release["manifest_sha"] = sync.sha256_file(legacy_manifest_path)
        write_json(legacy_release_path, legacy_release)

        implementation.write_text(current_source, encoding="utf-8")
        installed = self._install_production("git-finalizer")

        self.assertTrue(installed["production_activation"])
        smoke = subprocess.run(
            (str(self.bin_dir / "codex-skill-sync"), "--version"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        self.assertIn(sync.VERSION, smoke.stdout)

    def test_production_verification_failure_restores_active_pair(self) -> None:
        binary = self.bin_dir / "codex-git-finalize"
        skill = self.agents_root / "skills" / sync.SKILL_NAME / "SKILL.md"
        bridge = self.hooks_dir / "codex_git_finalize_bridge.py"
        before = (binary.read_bytes(), skill.read_bytes(), bridge.read_bytes())
        real_verify = sync.verify_production_bundle

        with mock.patch.object(
            sync,
            "verify_production_bundle",
            side_effect=[sync.SyncError("post-install verify failed")],
        ):
            with self.assertRaisesRegex(sync.SyncError, "post-install verify failed"):
                self._install_production("git-finalizer")

        self.assertEqual(
            (binary.read_bytes(), skill.read_bytes(), bridge.read_bytes()), before
        )
        self.assertFalse(
            (self.install_root / "tools" / "git-finalizer" / "CURRENT").exists()
        )
        self.assertTrue(callable(real_verify))

    def test_production_keyboard_interrupt_restores_active_pair(self) -> None:
        binary = self.bin_dir / "codex-git-finalize"
        skill = self.agents_root / "skills" / sync.SKILL_NAME / "SKILL.md"
        before = (binary.read_bytes(), skill.read_bytes())

        with mock.patch.object(
            sync, "verify_production_bundle", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                self._install_production("git-finalizer")

        self.assertEqual((binary.read_bytes(), skill.read_bytes()), before)
        self.assertFalse(
            (self.install_root / "tools" / "git-finalizer" / "CURRENT").exists()
        )

    def test_interrupted_install_cleans_stage_and_keeps_current(self) -> None:
        sync.install(
            self.sources,
            "git-finalizer",
            self.install_root,
            allow_dirty_source=False,
        )
        before = self._current("git-finalizer")
        with mock.patch.object(sync, "smoke_bundle", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                sync.install(
                    self.sources,
                    "git-finalizer",
                    self.install_root,
                    allow_dirty_source=False,
                )
        self.assertEqual(self._current("git-finalizer"), before)
        bundle_root = self.install_root / "bundles" / "git-finalizer"
        self.assertFalse(
            any(path.name.startswith(".staging-") for path in bundle_root.iterdir())
        )

    def test_rollback_switches_binary_and_skill_as_one_bundle(self) -> None:
        first = self._install_production("git-finalizer")
        first_bundle = first["current_bundle"]
        skill = (
            self.sources_root
            / "git-finalizer"
            / "skills"
            / sync.SKILL_NAME
            / "SKILL.md"
        )
        skill.write_text("fixture changed Skill\n", encoding="utf-8")
        self._update_skill_manifest_hashes()
        executable = self.sources_root / "git-finalizer" / "codex-git-finalize"
        executable.write_text(
            executable.read_text(encoding="utf-8") + "# second bundle\n",
            encoding="utf-8",
        )
        second = self._install_production("git-finalizer", allow_dirty_source=True)
        self.assertNotEqual(second["current_bundle"], first_bundle)
        second_binary = (self.bin_dir / "codex-git-finalize").read_bytes()
        rolled_back = sync.rollback(
            "git-finalizer",
            self.install_root,
            activate_production=True,
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )
        self.assertEqual(rolled_back["current_bundle"], first_bundle)
        release = sync.verify_bundle(self._current("git-finalizer"), "git-finalizer")
        self.assertEqual(release["source_state"], "committed_release")
        self.assertNotEqual(
            (self.bin_dir / "codex-git-finalize").read_bytes(), second_binary
        )
        sync.verify_production_bundle(
            self._current("git-finalizer"),
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )

    def test_production_rollback_accepts_identical_pair_in_distinct_bundle(
        self,
    ) -> None:
        first = self._install_production("git-finalizer")
        (self.sources_root / "git-finalizer" / "unbundled-note.txt").write_text(
            "changes release identity only\n", encoding="utf-8"
        )
        second = self._install_production("git-finalizer", allow_dirty_source=True)
        self.assertNotEqual(second["current_bundle"], first["current_bundle"])

        rolled_back = sync.rollback(
            "git-finalizer",
            self.install_root,
            activate_production=True,
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )

        self.assertEqual(rolled_back["current_bundle"], first["current_bundle"])
        sync.verify_production_bundle(
            self._current("git-finalizer"),
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )

    def test_bundle_hash_tampering_fails_without_switch(self) -> None:
        sync.install(
            self.sources,
            "snapshot-runner",
            self.install_root,
            allow_dirty_source=False,
        )
        current = self._current("snapshot-runner")
        contract = current / "tool_cli_contract.json"
        payload = sync.read_json(contract)
        payload["protected_operations"].append("tampered")
        write_json(contract, payload)
        before = self._current("snapshot-runner")
        with self.assertRaisesRegex(sync.SyncError, "cli_contract_sha mismatch"):
            sync.install(
                self.sources,
                "snapshot-runner",
                self.install_root,
                allow_dirty_source=False,
            )
        self.assertEqual(self._current("snapshot-runner"), before)


if __name__ == "__main__":
    unittest.main()
