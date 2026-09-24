from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest

from tooling import tool_skill_sync as sync


ROOT = Path(__file__).resolve().parents[1]


class ToolContractTests(unittest.TestCase):
    def test_finalizer_contract_manifest_and_source_agree(self) -> None:
        contract_path = ROOT / "tool_cli_contract.json"
        manifest_path = ROOT / "tool_skill_manifest.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(sync.read_json(contract_path), contract)
        self.assertEqual(sync.read_json(manifest_path), manifest)
        version_match = re.search(
            r'^readonly VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$',
            (ROOT / "git-finalize").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        companion_version_match = re.search(
            r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$',
            (ROOT / "git-finalize-integration-publish.py").read_text(
                encoding="utf-8"
            ),
            re.MULTILINE,
        )

        self.assertIsNotNone(version_match)
        self.assertIsNotNone(companion_version_match)
        assert version_match is not None
        assert companion_version_match is not None
        self.assertEqual(contract["schema_version"], 1)
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(contract["contract_version"], 4)
        self.assertEqual(contract["tool_name"], "git-finalizer")
        self.assertEqual(contract["tool_version"], manifest["tool_version"])
        self.assertEqual(contract["tool_version"], version_match.group(1))
        self.assertEqual(contract["tool_version"], companion_version_match.group(1))
        bootstrap_version = re.search(
            r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$',
            (ROOT / "git-finalize-repo-bootstrap.py").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        self.assertIsNotNone(bootstrap_version)
        assert bootstrap_version is not None
        self.assertEqual(contract["tool_version"], bootstrap_version.group(1))
        self.assertEqual(manifest["tool_commit"], "@release")
        self.assertEqual(
            manifest["public_cli_contract_sha256"],
            hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["canonical_skill_sha256"],
            sync.tree_sha256(
                ROOT / "skills" / sync.SKILL_NAME,
                sync.SKILL_PAYLOAD,
            ),
        )
        self.assertEqual(
            manifest["compatibility_skill"]["sha256"],
            sync.sha256_file(
                ROOT
                / "skills"
                / sync.COMPATIBILITY_SKILL_NAME
                / "SKILL.md"
            ),
        )
        commands = {command["name"]: command for command in contract["commands"]}
        self.assertEqual(
            set(commands),
            {
                "repo_plan",
                "repo_ensure",
                "verify_only",
                "commit_only",
                "initial_commit_only",
                "normal_publish",
                "initial_publish",
                "initial_branch_publish",
                "resume_initial_publish",
                "resume_publish",
                "publish_existing_branch",
                "publish_existing_history",
                "resume_existing_history_publish",
                "integration_candidate_publish",
                "retire_remote_branch",
                "retire_local_branch",
            },
        )
        self.assertEqual(
            commands["retire_remote_branch"]["operation_class"], "remote_mutation"
        )
        self.assertEqual(
            commands["retire_local_branch"]["operation_class"], "local_mutation"
        )
        self.assertEqual(
            commands["integration_candidate_publish"]["operation_class"],
            "remote_mutation",
        )
        self.assertEqual(commands["repo_plan"]["operation_class"], "read_only")
        self.assertEqual(commands["repo_ensure"]["operation_class"], "remote_mutation")
        self.assertIn(
            "REMOTE_DELETE_UNVERIFIED",
            commands["retire_remote_branch"]["result_statuses"],
        )

    def test_every_skill_manifest_binds_the_canonical_skill_payload(self) -> None:
        compatibility = json.loads(
            (ROOT / "toolchain_compatibility.json").read_text(encoding="utf-8")
        )
        expected_canonical = {
            "owner": compatibility["workflow_skill"]["canonical_owner"],
            "path": f"skills/{sync.SKILL_NAME}/SKILL.md",
        }
        mirrors = {
            details["skill_manifest"]
            for details in compatibility["tools"].values()
            if "skill_manifest" in details
        }
        manifest_paths = [ROOT / "tool_skill_manifest.json"] + [
            ROOT / relative for relative in sorted(mirrors)
        ]
        self.assertEqual(len(manifest_paths), 3)
        canonical_payload = sync.tree_sha256(
            ROOT / "skills" / sync.SKILL_NAME,
            sync.SKILL_PAYLOAD,
        )
        compatibility_skill = sync.sha256_file(
            ROOT
            / "skills"
            / sync.COMPATIBILITY_SKILL_NAME
            / "SKILL.md"
        )
        for manifest_path in manifest_paths:
            with self.subTest(manifest=str(manifest_path.relative_to(ROOT))):
                manifest = sync.read_json(manifest_path)
                self.assertEqual(manifest["schema_version"], 2)
                self.assertEqual(manifest["canonical_skill"], expected_canonical)
                self.assertEqual(
                    manifest["canonical_skill_sha256"], canonical_payload
                )
                self.assertEqual(
                    manifest["compatibility_skill"]["name"],
                    sync.COMPATIBILITY_SKILL_NAME,
                )
                self.assertEqual(
                    manifest["compatibility_skill"]["sha256"], compatibility_skill
                )
                self.assertEqual(
                    manifest["compatible_toolchain_contract_version"],
                    compatibility["toolchain_contract_version"],
                )
                self.assertEqual(
                    manifest["skill_contract_version"],
                    compatibility["workflow_skill"]["contract_version"],
                )

    def test_toolchain_compatibility_binds_external_worktree_controller(self) -> None:
        compatibility = json.loads(
            (ROOT / "toolchain_compatibility.json").read_text(encoding="utf-8")
        )
        self.assertEqual(compatibility["toolchain_contract_version"], 5)
        self.assertEqual(compatibility["context_loader_contract_version"], 3)
        # Sibling contracts advanced to v3 (Context Loader 1.3.1, Snapshot Runner 2.3.1);
        # per-tool contract advances do not move toolchain_contract_version by themselves.
        self.assertEqual(compatibility["snapshot_runner_contract_version"], 3)
        self.assertEqual(compatibility["git_finalizer_contract_version"], 4)
        self.assertEqual(compatibility["worktree_controller_contract_version"], 3)
        self.assertEqual(
            compatibility["worktree_controller_status"],
            "available_external",
        )
        self.assertEqual(
            compatibility["workflow_skill"],
            {
                "canonical_owner": "git-finalizer",
                "compatibility_shims": ["three-tool-git-workflow"],
                "contract_version": 2,
                "name": "git-change-delivery",
            },
        )

    def test_reviewed_source_commands_declare_controller_linkage(self) -> None:
        contract = json.loads(
            (ROOT / "tool_cli_contract.json").read_text(encoding="utf-8")
        )
        commands = {command["name"]: command for command in contract["commands"]}
        linkage = ["--repository-id", "--allocation-id", "--task-key", "--authority-key"]
        precondition = "controller_linkage_required_with_reviewed_sensitive_source"
        for name in (
            "verify_only",
            "commit_only",
            "normal_publish",
            "publish_existing_branch",
        ):
            with self.subTest(command=name):
                command = commands[name]
                self.assertIn("--reviewed-sensitive-source", command["flags"])
                for flag in linkage:
                    self.assertIn(flag, command["flags"])
                self.assertIn(precondition, command["preconditions"])
        help_text = (ROOT / "git-finalize").read_text(encoding="utf-8")
        for flag in linkage:
            self.assertIn(f"{flag} <", help_text)
        self.assertNotIn(
            "Optional Worktree Controller repository linkage.", help_text
        )

    def test_contract_declares_truthful_exit_code_surface(self) -> None:
        contract = json.loads(
            (ROOT / "tool_cli_contract.json").read_text(encoding="utf-8")
        )
        exit_codes = contract["exit_codes"]
        self.assertEqual(
            sorted(key for key in exit_codes if key != "scope"),
            ["0", "1", "2", "3"],
        )
        bash_lines = (ROOT / "git-finalize").read_text(encoding="utf-8").splitlines()
        bash_codes = {
            match.group(1)
            for line in bash_lines
            if (match := re.search(r"^\s*exit ([0-9])$", line)) is not None
        }
        self.assertEqual(bash_codes, {"0", "1"})
        self.assertIn("bash-owned commands", exit_codes["scope"])

    def test_skill_payload_digests_agree_across_authorities(self) -> None:
        import importlib
        import sys

        skill_dir = ROOT / "skills" / "git-change-delivery"
        sys.path.insert(0, str(skill_dir))
        try:
            deploy = importlib.import_module("deploy")
        finally:
            sys.path.remove(str(skill_dir))
        payload_digest = sync.tree_sha256(skill_dir, sync.SKILL_PAYLOAD)
        deploy_digest = deploy.payload_tree_digest(skill_dir)
        self.assertEqual(payload_digest, deploy_digest)
        self.assertEqual(
            set(sync.RELEASED_CANONICAL_SKILL_SHA256),
            set(deploy.RELEASED_CANONICAL_SKILL_SHA256),
        )
        manifest_paths = [ROOT / "tool_skill_manifest.json"]
        manifest_paths.extend(
            sorted((ROOT / "manifests").glob("*/tool_skill_manifest.json"))
        )
        for manifest_path in manifest_paths:
            manifest = sync.read_json(manifest_path)
            with self.subTest(manifest=str(manifest_path.relative_to(ROOT))):
                self.assertEqual(manifest["canonical_skill_sha256"], payload_digest)

    def test_promotion_rule_and_glossary_are_documented(self) -> None:
        doc = (ROOT / "docs" / "tool-skill-sync.md").read_text(encoding="utf-8")
        self.assertIn("## Contract 升格规则", doc)
        readme = (
            ROOT / "skills" / "git-change-delivery" / "README.md"
        ).read_text(encoding="utf-8")
        self.assertIn("source canonical", readme)
        self.assertIn("RELEASED_CANONICAL_SKILL_SHA256", readme)

    def test_phase_closure_sop_links_existing_template(self) -> None:
        sop = ROOT / "docs" / "process" / "phase-closure.md"
        template = ROOT / "docs" / "process" / "phase-closure-report-template.md"
        self.assertTrue(sop.is_file())
        self.assertTrue(template.is_file())
        self.assertIn(
            "(phase-closure-report-template.md)",
            sop.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
