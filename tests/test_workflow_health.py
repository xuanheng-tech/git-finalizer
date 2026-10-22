from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tooling import tool_skill_sync as sync
from tooling import workflow_health as health
import test_tool_skill_sync as sync_tests


class WorkflowHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agents = self.root / ".agents"
        self.codex = self.root / ".codex"
        self.codex.mkdir()
        self.skill = self.agents / "skills/worktree-lifecycle/SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("workflow rules\n")
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic")
        self.git("config", "user.email", "synthetic@example.invalid")
        (self.repo / "AGENTS.md").write_text("repository rules\n")
        self.git("add", "AGENTS.md")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        (self.codex / "AGENTS.md").write_text("global rules\n")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout

    def test_instruction_refresh_changes_with_content_without_writing(self) -> None:
        before = (self.repo / ".git/index").read_bytes()
        first = health.instruction_state(self.repo, self.agents, self.codex)
        second = health.instruction_state(self.repo, self.agents, self.codex)
        self.assertEqual(health.fingerprint(first), health.fingerprint(second))
        self.assertEqual(first["status"], "PASS")
        (self.repo / "AGENTS.md").write_text("updated rules\n")
        changed = health.instruction_state(self.repo, self.agents, self.codex)
        self.assertNotEqual(health.fingerprint(first), health.fingerprint(changed))
        self.assertEqual(len(changed["pending_checkouts"]), 1)
        self.assertEqual((self.repo / ".git/index").read_bytes(), before)
        self.assertEqual((self.repo / "AGENTS.md").read_text(), "updated rules\n")

    def test_overrides_and_nested_scope_follow_discovery(self) -> None:
        (self.codex / "AGENTS.md").write_text("x" * 40000)
        (self.codex / "AGENTS.override.md").write_text("override\n")
        nested = self.repo / "module"
        nested.mkdir()
        (nested / "AGENTS.md").write_text("module scope\n")
        result = health.instruction_state(nested, self.agents, self.codex)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual([Path(item["path"]).name for item in result["files"]],
                         ["AGENTS.override.md", "AGENTS.md", "AGENTS.md"])
        self.assertEqual(result["files"][-1]["path"], str(nested / "AGENTS.md"))

    def test_discovery_budget_is_not_silently_increased(self) -> None:
        (self.codex / "config.toml").write_text("project_doc_max_bytes = 10\n")
        result = health.instruction_state(self.repo, self.agents, self.codex)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["configured_limit_bytes"], 10)
        self.assertEqual((self.codex / "config.toml").read_text(), "project_doc_max_bytes = 10\n")

    def test_claude_shared_link_is_one_source(self) -> None:
        bridge = self.root / ".claude"
        bridge.mkdir()
        (bridge / "skills").symlink_to(self.agents / "skills", target_is_directory=True)
        result = health.instruction_state(self.repo, self.agents, self.codex)
        self.assertFalse(result["warnings"])
        self.assertEqual(health.file_identity(bridge / "skills/worktree-lifecycle/SKILL.md")["real_path"],
                         health.file_identity(self.skill)["real_path"])

    def test_claude_synced_skills_do_not_leak_into_shared_root(self) -> None:
        bridge = self.root / ".claude"
        bridge.mkdir()
        (bridge / "skills").symlink_to(self.agents / "skills", target_is_directory=True)
        (self.agents / "skills/synced").mkdir()
        result = health.instruction_state(self.repo, self.agents, self.codex)
        self.assertIn("Claude-synced skills leak into the shared personal skills root", result["warnings"])

    def test_claude_per_skill_links_expose_every_shared_skill(self) -> None:
        skills = self.root / ".claude/skills"
        skills.mkdir(parents=True)
        (skills / "synced").mkdir()
        (skills / "worktree-lifecycle").symlink_to(self.skill.parent, target_is_directory=True)
        self.assertFalse(health.instruction_state(self.repo, self.agents, self.codex)["warnings"])
        (self.agents / "skills/new-skill").mkdir()
        (self.agents / "skills/new-skill/SKILL.md").write_text("new\n")
        result = health.instruction_state(self.repo, self.agents, self.codex)
        self.assertEqual(result["warnings"], ["Claude skills do not expose shared skill new-skill"])

    def test_custom_shared_root_does_not_move_claude_home(self) -> None:
        shared = self.root / "custom-shared"
        shared.mkdir()
        bridge = self.root / ".claude"
        bridge.mkdir()
        (bridge / "CLAUDE.md").write_text("@../.codex/AGENTS.md\n")
        first = health.instruction_state(self.repo, shared, self.codex)
        self.assertIn(str(bridge / "CLAUDE.md"), [item["path"] for item in first["references"]])
        (bridge / "CLAUDE.md").write_text("changed bridge\n")
        second = health.instruction_state(self.repo, shared, self.codex)
        self.assertNotEqual(health.fingerprint(first), health.fingerprint(second))

    def test_claude_home_and_project_folder_are_fingerprinted(self) -> None:
        custom = self.root / "custom-claude"
        custom.mkdir()
        (custom / "CLAUDE.md").write_text("custom global rules\n")
        (custom / "skills").symlink_to(self.root / "missing-skills", target_is_directory=True)
        folder = self.repo / ".claude"
        folder.mkdir()
        (folder / "CLAUDE.md").write_text("project rules\n")
        first = health.instruction_state(self.repo, self.agents, self.codex, custom)
        self.assertIn("Claude personal skills link is broken", first["warnings"])
        self.assertEqual(len(first["pending_checkouts"]), 1)
        (folder / "CLAUDE.md").write_text("changed project rules\n")
        second = health.instruction_state(self.repo, self.agents, self.codex, custom)
        self.assertNotEqual(health.fingerprint(first), health.fingerprint(second))

    def test_claude_config_environment_and_explicit_option(self) -> None:
        with mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.root / "custom")}, clear=False):
            automatic = sync.parser().parse_args(["doctor"])
            explicit = sync.parser().parse_args(["doctor", "--claude-home", str(self.root / "explicit")])
        self.assertEqual(automatic.claude_home, self.root / "custom")
        self.assertEqual(explicit.claude_home, self.root / "explicit")

    def controller_fixture(self, contract_version=37, *, directory_name="a" * 40):
        compatibility = sync.read_json(
            Path(__file__).resolve().parents[1] / "toolchain_compatibility.json"
        )
        requirements = compatibility["runtime_requirements"]["worktree-controller"]
        caps = {"code": "CAPABILITIES", "contract_version": contract_version,
                "storage_layout_version": 4, **requirements["capability_versions"]}
        contract = {**caps, "tool_version": "9.0.0"}
        runtime_root = self.root / f"controller-{contract_version}"
        runtime = runtime_root / "versions" / directory_name / "worktree-controller"
        site = runtime / "lib/python3.12/site-packages/worktree_controller"
        site.mkdir(parents=True)
        frozen = json.dumps(contract)
        (site / "tool_cli_contract.json").write_text(frozen)
        (runtime_root / "active").symlink_to(runtime, target_is_directory=True)
        envelope = {"tool_version": "9.0.0", "decision": caps, "errors": []}
        return runtime_root, requirements, envelope, frozen

    def probe_controller(self, runtime_root, requirements, envelope, frozen, on_probe=None,
                         manifest_repo=None, on_contract=None):
        def run(args, **kwargs):
            if "capabilities" in args:
                if on_probe is not None:
                    on_probe()
                output = json.dumps(envelope)
            elif manifest_repo and args[-1] == "--show-toplevel":
                output = str(manifest_repo)
            elif manifest_repo and args[-1] == "--git-common-dir":
                output = str(manifest_repo / ".git")
            elif manifest_repo and "rev-list" in args:
                output = "c" * 40
            elif args[-1].endswith(":skills/worktree-lifecycle/SKILL.md"):
                output = "workflow rules\n"
            else:
                if on_contract:
                    on_contract()
                output = frozen
            return subprocess.CompletedProcess(args, 0, output, "")
        with mock.patch.object(sync, "run", side_effect=run), \
             mock.patch.object(sync, "verify_installed_record", return_value={"verified_files": 2}), \
             mock.patch.object(sync, "git_head", return_value="b" * 40), \
             mock.patch.object(sync, "git_dirty", return_value=True):
            return health.controller_state(requirements, Path("/synthetic/controller"),
                                           runtime_root, self.repo, self.agents)

    def test_controller_candidate_is_not_an_activation_target(self) -> None:
        root, requirements, envelope, frozen = self.controller_fixture()
        result = self.probe_controller(root, requirements, envelope, frozen)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["active_commit"], "a" * 40)
        self.assertEqual(result["source_head"], "b" * 40)
        self.assertTrue(result["source_dirty"])
        self.assertEqual((root / "active").resolve().parent.name, "a" * 40)

    def source_manifest(self, root, frozen):
        repository = self.root / "public-controller"
        repository.mkdir()
        common = repository / ".git"
        common.mkdir()
        manifest = (root / "active").resolve() / "controller-source.json"
        value = {"schema_version": 1, "contract": "controller-runtime-source/v1", "commit": "a" * 40,
                 "source_repository": str(repository), "git_common_dir": str(common),
                 "root_commit": "c" * 40, "contract_sha256": hashlib.sha256(frozen.encode()).hexdigest(),
                 "skill_sha256": hashlib.sha256(self.skill.read_bytes()).hexdigest()}
        sync.write_json(manifest, value)
        return manifest, repository, value

    def test_installer_source_manifest_selects_public_history_without_activation(self) -> None:
        root, requirements, envelope, frozen = self.controller_fixture(61)
        manifest, repository, _ = self.source_manifest(root, frozen)
        before = manifest.read_bytes()
        result = self.probe_controller(root, requirements, envelope, frozen, manifest_repo=repository)
        self.assertEqual(result["status"], "PASS", result["errors"])
        self.assertEqual(result["source_repo"], str(repository))
        self.assertEqual(result["source_selection"], "runtime-source-manifest")
        self.assertEqual(result["source_manifest"]["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual((root / "active").resolve().parent.name, "a" * 40)

    def test_invalid_source_manifest_never_falls_back_to_legacy_history(self) -> None:
        root, requirements, envelope, frozen = self.controller_fixture(61)
        manifest, repository, value = self.source_manifest(root, frozen)
        for changes in ({"schema_version": True}, {"commit": "b" * 40}, {"root_commit": "d" * 40},
                        {"contract_sha256": "0" * 64}, {"skill_sha256": "0" * 64},
                        {"unknown": "field"}, {"source_repository": "relative"}):
            with self.subTest(changes=changes):
                sync.write_json(manifest, {**value, **changes})
                before = manifest.read_bytes()
                result = self.probe_controller(root, requirements, envelope, frozen, manifest_repo=repository)
                self.assertEqual(result["status"], "FAIL")
                self.assertTrue(result["errors"])
                self.assertEqual(manifest.read_bytes(), before)
                self.assertEqual((root / "active").resolve().parent.name, "a" * 40)

    def test_source_manifest_symlink_is_refused_before_reading(self) -> None:
        root, requirements, envelope, frozen = self.controller_fixture()
        manifest, _, _ = self.source_manifest(root, frozen)
        foreign = self.root / "owner-data"
        foreign.write_bytes(b"unrelated owner data\x00")
        manifest.unlink()
        manifest.symlink_to(foreign)
        with mock.patch.object(sync, "read_json") as read:
            result = self.probe_controller(root, requirements, envelope, frozen)
        self.assertEqual(result["status"], "FAIL")
        read.assert_not_called()
        self.assertEqual(foreign.read_bytes(), b"unrelated owner data\x00")

    def test_source_manifest_drift_during_observation_is_not_pass(self) -> None:
        root, requirements, envelope, frozen = self.controller_fixture()
        manifest, repository, value = self.source_manifest(root, frozen)
        def changed():
            sync.write_json(manifest, {**value, "contract_sha256": "0" * 64})
        result = self.probe_controller(root, requirements, envelope, frozen,
                                       manifest_repo=repository, on_contract=changed)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("Controller source manifest changed during observation; retry read-only check", result["errors"])

    def test_controller_short_directory_uses_full_installed_commit(self) -> None:
        root, requirements, envelope, frozen = self.controller_fixture(directory_name="a" * 7)
        runtime = (root / "active").resolve()
        origin = runtime / "lib/python3.12/site-packages/worktree_controller-9.0.0.dist-info/direct_url.json"
        origin.parent.mkdir()
        for requested in ("a" * 7, "a" * 40):
            with self.subTest(requested_revision=requested):
                origin.write_text(json.dumps({"vcs_info": {
                    "vcs": "git", "commit_id": "a" * 40, "requested_revision": requested,
                }}))
                before = origin.read_bytes()
                result = self.probe_controller(root, requirements, envelope, frozen)
                self.assertEqual(result["status"], "PASS", result["errors"])
                self.assertEqual(result["active_commit"], "a" * 40)
                self.assertEqual(result["source_head"], "b" * 40)
                self.assertEqual((root / "active").resolve(), runtime)
                self.assertEqual(origin.read_bytes(), before)

    def test_controller_short_directory_requires_pinned_git_origin(self) -> None:
        root, requirements, envelope, frozen = self.controller_fixture(directory_name="a" * 7)
        runtime = (root / "active").resolve()
        origin = runtime / "lib/python3.12/site-packages/worktree_controller-9.0.0.dist-info/direct_url.json"
        result = self.probe_controller(root, requirements, envelope, frozen)
        self.assertEqual(result["status"], "FAIL")
        origin.parent.mkdir()
        valid = {"vcs": "git", "commit_id": "a" * 40, "requested_revision": "a" * 7}
        for invalid in ({}, [], {**valid, "vcs": "hg"}, {**valid, "commit_id": "a" * 7},
                        {**valid, "commit_id": "b" * 40}, {**valid, "requested_revision": "main"}):
            with self.subTest(vcs_info=invalid):
                origin.write_text(json.dumps({"vcs_info": invalid}))
                result = self.probe_controller(root, requirements, envelope, frozen)
                self.assertEqual(result["status"], "FAIL")
                self.assertTrue(result["errors"])
        origin.write_text(json.dumps({"vcs_info": valid}))
        other = origin.parent.with_name("worktree_controller-8.0.0.dist-info")
        other.mkdir()
        (other / origin.name).write_bytes(origin.read_bytes())
        result = self.probe_controller(root, requirements, envelope, frozen)
        self.assertEqual(result["status"], "FAIL")

    def test_reviewed_controller_cli_contracts(self) -> None:
        for version in (37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51):
            with self.subTest(contract_version=version):
                root, requirements, envelope, frozen = self.controller_fixture(version)
                result = self.probe_controller(root, requirements, envelope, frozen)
                self.assertEqual(result["status"], "PASS", result["errors"])
                self.assertEqual(result["cli_contract_version"], version)

    def test_unreviewed_controller_cli_contract_is_rejected_without_writing(self) -> None:
        compatibility = sync.read_json(
            Path(__file__).resolve().parents[1] / "toolchain_compatibility.json"
        )
        unreviewed_version = max(
            compatibility["runtime_requirements"]["worktree-controller"]["cli_contract_versions"]
        ) + 1
        root, requirements, envelope, frozen = self.controller_fixture(unreviewed_version)
        active_runtime = (root / "active").resolve()
        skill_before = self.skill.read_bytes()
        result = self.probe_controller(root, requirements, envelope, frozen)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn(f"unreviewed contract_version: {unreviewed_version}", result["errors"])
        self.assertEqual((root / "active").resolve(), active_runtime)
        self.assertEqual(self.skill.read_bytes(), skill_before)

    def test_matching_cli_contract_does_not_hide_missing_capability(self) -> None:
        root, requirements, envelope, frozen = self.controller_fixture()
        envelope["decision"].pop("writer_session_version")
        result = self.probe_controller(root, requirements, envelope, frozen)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("writer_session_version" in item for item in result["errors"]))

    def test_software_and_storage_mismatch_are_independent(self) -> None:
        root, requirements, envelope, frozen = self.controller_fixture()
        envelope["tool_version"] = "8.0.0"
        envelope["decision"]["storage_layout_version"] = 3
        result = self.probe_controller(root, requirements, envelope, frozen)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("software" in item for item in result["errors"]))
        self.assertTrue(any("storage" in item for item in result["errors"]))

    def test_active_runtime_change_during_probe_is_not_healthy(self) -> None:
        root, requirements, envelope, frozen = self.controller_fixture()
        other = root / "versions" / ("c" * 40) / "worktree-controller"
        other.mkdir(parents=True)
        def change_runtime():
            (root / "active").unlink()
            (root / "active").symlink_to(other, target_is_directory=True)
        result = self.probe_controller(root, requirements, envelope, frozen, change_runtime)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("changed during observation" in e for e in result["errors"]))

    def test_controller_skill_requires_same_active_lineage(self) -> None:
        root, requirements, envelope, frozen = self.controller_fixture()
        self.skill.write_text("uncommitted candidate Skill\n")
        result = self.probe_controller(root, requirements, envelope, frozen)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("Skill differs from active commit" in e for e in result["errors"]))
        self.assertEqual(self.skill.read_text(), "uncommitted candidate Skill\n")

    def test_malformed_requirements_fail_with_structured_diagnostics(self) -> None:
        root, _, envelope, frozen = self.controller_fixture()
        for requirements in (None, [], {"cli_contract_versions": [True]}):
            with self.subTest(requirements=requirements):
                result = self.probe_controller(root, requirements, envelope, frozen)
                self.assertEqual(result["status"], "FAIL")
                self.assertTrue(result["errors"])

    def test_linked_checkout_fingerprint_includes_canonical_governance(self) -> None:
        linked = self.root / "linked"
        self.git("worktree", "add", "-q", "-b", "fixture-linked", str(linked))
        (self.repo / "AGENTS.md").write_text("canonical revision 1\n")
        first = health.instruction_state(linked, self.agents, self.codex)
        (self.repo / "AGENTS.md").write_text("canonical revision 2\n")
        second = health.instruction_state(linked, self.agents, self.codex)
        self.assertNotEqual(health.fingerprint(first), health.fingerprint(second))
        self.assertEqual((linked / "AGENTS.md").read_text(), "repository rules\n")
        self.assertEqual(second["canonical_references"][0]["path"], str(self.repo / "AGENTS.md"))

    def test_read_error_cannot_be_reported_as_healthy(self) -> None:
        (self.codex / "config.toml").write_text("broken[")
        result = health.instruction_state(self.repo, self.agents, self.codex)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(result["errors"])

    def test_same_version_sidecar_drift_is_detected(self) -> None:
        fixture = sync_tests.SkillSyncTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        sync.install(fixture.sources, "git-finalizer", fixture.install_root,
                     allow_dirty_source=False, activate_production=True,
                     agents_root=fixture.agents_root, bin_dir=fixture.bin_dir,
                     hooks_dir=fixture.hooks_dir)
        result = health.installed_tool(fixture.sources, "git-finalizer",
                                       fixture.agents_root, fixture.bin_dir, fixture.hooks_dir)
        self.assertEqual(result["status"], "PASS", result)
        companion = fixture.bin_dir / "git-finalize-snapshot-verify.py"
        companion.write_text("changed without a version bump\n")
        result = health.installed_tool(fixture.sources, "git-finalizer",
                                       fixture.agents_root, fixture.bin_dir, fixture.hooks_dir)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn(str(companion), result["binary_content_drift"])

    def test_repeat_fingerprint_never_claims_session_reload(self) -> None:
        sources = mock.Mock()
        sources.names.return_value = ("context-loader",)
        sources.compatibility = {}
        kwargs = dict(agents_root=self.agents, bin_dir=None, hooks_dir=self.root,
                      repo=self.repo, codex_home=self.codex, controller=None,
                      controller_runtime_root=self.root, controller_source=self.repo)
        with mock.patch.object(health, "installed_tool", return_value={"tool": "context-loader", "status": "PASS"}), \
             mock.patch.object(health, "controller_state", return_value={"tool": "worktree-controller", "status": "PASS"}):
            first = health.doctor(sources, **kwargs)
            second = health.doctor(sources, **kwargs, previous_fingerprint=first["fingerprint"])
            sources.compatibility = {"reviewed_requirement": "changed"}
            third = health.doctor(sources, **kwargs, previous_fingerprint=first["fingerprint"])
        self.assertTrue(third["refresh_required"])
        self.assertFalse(second["refresh_required"])
        self.assertFalse(second["mutation_performed"])
        self.assertIn("cannot verify model context", second["instructions"]["session_reload"])
        self.assertEqual(health.summary(second)["fingerprint"], first["fingerprint"])

    def test_pinned_release_ignores_dirty_package_working_files(self) -> None:
        fixture = sync_tests.SkillSyncTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        repo = fixture.sources.repo("snapshot-runner")
        oid = sync.git_head(repo)
        live = repo / "pyproject.toml"
        live.write_text("unfinished local candidate\n")
        before_index = (repo / ".git/index").read_bytes()
        pinned = sync.Sources(fixture.sources_root, source_refs={"snapshot-runner": oid})
        check = sync.check_tool(pinned, "snapshot-runner")
        self.assertEqual(check["status"], "PASS", check)
        self.assertEqual(check["canonical_commit"], oid)
        self.assertFalse(check["canonical_source_dirty"])
        self.assertTrue(check["source_checkout_dirty"])
        self.assertEqual(check["source_selection"], "pinned_commit")
        bundle = sync.build_bundle(pinned, "snapshot-runner", fixture.install_root,
                                   allow_dirty_source=False)
        receipt = sync.verify_bundle(bundle)
        self.assertEqual(receipt["tool_commit"], oid)
        self.assertEqual(receipt["source_state"], "committed_release")
        self.assertEqual(live.read_text(), "unfinished local candidate\n")
        self.assertEqual((repo / ".git/index").read_bytes(), before_index)

    def test_pinned_source_does_not_bypass_dirty_owner_skill(self) -> None:
        fixture = sync_tests.SkillSyncTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        repo = fixture.sources.repo("snapshot-runner")
        (fixture.sources.owner_repo / "pending.md").write_text("uncommitted\n")
        pinned = sync.Sources(fixture.sources_root,
                              source_refs={"snapshot-runner": sync.git_head(repo)})
        with self.assertRaisesRegex(sync.SyncError, "dirty"):
            sync.build_bundle(pinned, "snapshot-runner", fixture.install_root,
                              allow_dirty_source=False)

    def test_source_file_tool_cannot_use_metadata_only_pin(self) -> None:
        fixture = sync_tests.SkillSyncTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        pinned = sync.Sources(fixture.sources_root, source_refs={
            "git-finalizer": sync.git_head(fixture.sources.owner_repo)})
        check = sync.check_tool(pinned, "git-finalizer")
        self.assertEqual(check["status"], "FAIL")
        self.assertTrue(any("Python console-script" in e for e in check["errors"]))

    def test_v6_source_gate_requires_independent_controller_bindings(self) -> None:
        fixture = sync_tests.SkillSyncTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.sources.compatibility["toolchain_contract_version"] = 6
        result = sync.check_tool(fixture.sources, "git-finalizer")
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("requires explicit runtime_requirements" in e for e in result["errors"]))
        _, requirements, _, _ = self.controller_fixture()
        requirements["capability_versions"]["writer_session_version"] = True
        fixture.sources.compatibility["runtime_requirements"] = {"worktree-controller": requirements}
        result = sync.check_tool(fixture.sources, "git-finalizer")
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("invalid Controller capability" in e for e in result["errors"]))

    def test_invalid_source_contract_stops_upgrade_before_installer(self) -> None:
        fixture = sync_tests.SkillSyncTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        (fixture.sources.repo("snapshot-runner") / "tool_cli_contract.json").write_text("{}\n")
        with mock.patch.object(sync, "install_python_tool") as installer:
            with self.assertRaisesRegex(sync.SyncError, "canonical check failed"):
                sync.upgrade(fixture.sources, "snapshot-runner", bin_dir=fixture.bin_dir)
        installer.assert_not_called()

if __name__ == "__main__":
    unittest.main()
