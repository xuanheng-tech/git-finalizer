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
        self.assertEqual(contract["contract_version"], 3)
        self.assertEqual(contract["tool_name"], "git-finalizer")
        self.assertEqual(contract["tool_version"], manifest["tool_version"])
        self.assertEqual(contract["tool_version"], version_match.group(1))
        self.assertEqual(contract["tool_version"], companion_version_match.group(1))
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
            },
        )
        self.assertEqual(
            commands["retire_remote_branch"]["operation_class"], "remote_mutation"
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

    def test_toolchain_compatibility_binds_external_worktree_controller(self) -> None:
        compatibility = json.loads(
            (ROOT / "toolchain_compatibility.json").read_text(encoding="utf-8")
        )
        self.assertEqual(compatibility["toolchain_contract_version"], 4)
        self.assertEqual(compatibility["context_loader_contract_version"], 2)
        # Snapshot Runner 2.0.0 removed the provider-named aliases: public CLI contract 2.
        self.assertEqual(compatibility["snapshot_runner_contract_version"], 2)
        self.assertEqual(compatibility["git_finalizer_contract_version"], 3)
        self.assertEqual(compatibility["worktree_controller_contract_version"], 2)
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
