from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import re
import runpy
import subprocess
import unittest
from unittest import mock

from tooling import tool_skill_sync as sync


ROOT = Path(__file__).resolve().parents[1]

# Every canonical Skill payload tree that a released line shipped, paired
# with the revision that carries it. The registry gate and the history
# cross-check share this single list.
RELEASED_CANONICAL_LINEAGE = (
    ("c640bb1", "ff6d5bea2807b2c884c2ec5bee441e5fe8060abd9d03c0520c57ce39fe37adb5"),
    ("v1.1.0", "3c8679b6cfd6578da41007feeea43e7ff83e8152e3daea9ab9154055642a92e2"),
    ("v1.1.1", "3c8679b6cfd6578da41007feeea43e7ff83e8152e3daea9ab9154055642a92e2"),
    ("v1.2.0", "7b85ec9da739bd60f76736ae6352e642dbf880bbd089058cce4cf7a9c0b5c665"),
    ("v1.3.0", "9c05e5b279731a37b3ce15e2fbda7ae9962f81355cbafb86fa410f28ec19f527"),
    # The integration-boundary release and the first public release shipped the
    # same canonical payload, so both refs attest one digest.
    ("v1.4.0", "0c82bceb12edde749aa6deddab25acb4ec083aeea92a37970ec64179ef92726b"),
    ("v1.5.0", "0c82bceb12edde749aa6deddab25acb4ec083aeea92a37970ec64179ef92726b"),
    ("v1.6.0", "943ebf4e56ae344cf88ab3dc579d5e497663b052c8ccbade5c42f1f52b5efa93"),
)


class ToolContractTests(unittest.TestCase):
    def test_retirement_probe_fails_closed_on_malformed_or_timed_out_capabilities(self) -> None:
        verifier = runpy.run_path(str(ROOT / "git-finalize-retirement-plan.py"))
        cases = (
            subprocess.CompletedProcess([], 0, json.dumps({"decision": []}), ""),
            subprocess.CompletedProcess([], 0, json.dumps({"decision": {"branch_retirement_version": True}}), ""),
            subprocess.TimeoutExpired("synthetic-controller", 30),
        )
        for result in cases:
            with self.subTest(result=type(result).__name__):
                diagnostics = io.StringIO()
                outcome = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with mock.patch("shutil.which", return_value="synthetic-controller"), \
                     mock.patch("subprocess.run", **outcome), redirect_stderr(diagnostics):
                    with self.assertRaises(SystemExit) as blocked:
                        verifier["attest_controller_capability"](ROOT, 1)
                self.assertEqual(blocked.exception.code, 1)
                self.assertIn("retirement plan validation blocked", diagnostics.getvalue())

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
        self.assertEqual(contract["contract_version"], 7)
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
                "sync_published_branch",
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
        for name in ("verify_only", "commit_only"):
            self.assertIn("--fixture-exceptions", commands[name]["flags"])
            self.assertIn("exact_backend_test_assignment_review_when_requested",
                          commands[name]["preconditions"])
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
        self.assertEqual(compatibility["toolchain_contract_version"], 6)
        self.assertEqual(compatibility["context_loader_contract_version"], 3)
        # Sibling contracts advanced to v3 (Context Loader 1.3.1, Snapshot Runner 2.3.1);
        # per-tool contract advances do not move toolchain_contract_version by themselves.
        self.assertEqual(compatibility["snapshot_runner_contract_version"], 3)
        self.assertEqual(compatibility["git_finalizer_contract_version"], 7)
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

    @staticmethod
    def load_deploy_module():
        import importlib
        import sys

        skill_dir = ROOT / "skills" / "git-change-delivery"
        sys.path.insert(0, str(skill_dir))
        try:
            return importlib.import_module("deploy")
        finally:
            sys.path.remove(str(skill_dir))

    def _tagged_skill_tree(self, ref: str) -> str | None:
        lines = []
        for relative in sync.SKILL_PAYLOAD:
            result = subprocess.run(
                ["git", "show", f"{ref}:skills/git-change-delivery/{relative}"],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                return None
            digest = hashlib.sha256(result.stdout).hexdigest()
            lines.append(f"{digest}  {relative}\n")
        return hashlib.sha256("".join(lines).encode()).hexdigest()

    def test_released_lineage_registry_is_exact(self) -> None:
        deploy = self.load_deploy_module()
        expected = {digest for _, digest in RELEASED_CANONICAL_LINEAGE}
        self.assertEqual(set(sync.RELEASED_CANONICAL_SKILL_SHA256), expected)
        self.assertEqual(
            set(sync.RELEASED_CANONICAL_SKILL_SHA256),
            set(deploy.RELEASED_CANONICAL_SKILL_SHA256),
        )
        # The hand-enriched production tree captured during migration stays
        # untrusted on purpose: it was never a released canonical payload.
        self.assertNotIn(
            "c930b3564e5fbdd8ea9a1857eef9e5d1d7c04a2b02cd8ee2f0e95f2e12a3bc90",
            sync.RELEASED_CANONICAL_SKILL_SHA256,
        )
        # A payload is released lineage because a released ref carries it, not because a
        # maintainer says so: registration requires attestation by one of the listed refs, and a
        # payload no released ref carries must stay unregistered so activation treats it as drift.
        incoming = sync.tree_sha256(
            ROOT / "skills" / "git-change-delivery", sync.SKILL_PAYLOAD
        )
        attestations = [
            self._tagged_skill_tree(ref)
            for ref, digest in RELEASED_CANONICAL_LINEAGE
            if digest == incoming
        ]
        attested = [tree for tree in attestations if tree is not None]
        if incoming in sync.RELEASED_CANONICAL_SKILL_SHA256:
            self.assertIn(
                incoming,
                attested,
                "the source payload is registered, but no listed ref carries it",
            )
        elif attestations and not attested:
            self.skipTest("the refs that would attest the source payload are absent here")
        else:
            self.assertNotIn(
                incoming,
                sync.RELEASED_CANONICAL_SKILL_SHA256,
                "an unreleased payload must not be registered as released lineage",
            )

    def test_registry_entries_trace_to_released_history(self) -> None:
        for ref, digest in RELEASED_CANONICAL_LINEAGE:
            with self.subTest(ref=ref):
                tree = self._tagged_skill_tree(ref)
                if tree is None:
                    self.skipTest(f"{ref} is absent in this checkout")
                self.assertEqual(tree, digest)
                self.assertIn(digest, sync.RELEASED_CANONICAL_SKILL_SHA256)

    def test_exit_code_contract_matches_companion_classification(self) -> None:
        contract = json.loads(
            (ROOT / "tool_cli_contract.json").read_text(encoding="utf-8")
        )
        exit_codes = contract["exit_codes"]
        bootstrap = (
            ROOT / "git-finalize-repo-bootstrap.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'return (2 if decision.startswith("BLOCK_") else 3), receipt', bootstrap
        )
        self.assertIn("status=blocked", exit_codes["2"])
        self.assertIn("repo_plan", exit_codes["2"])
        self.assertIn("could not be classified", exit_codes["3"])
        self.assertNotIn("integration_candidate_publish", exit_codes["3"])
        # The contract may not claim an ordering the implementation breaks: a
        # BLOCK_REMOTE_MISMATCH is raised after the repository may exist, so
        # only the receipt's bootstrap.executed field may report mutation.
        self.assertIn("bootstrap refusal", exit_codes["2"])
        self.assertIn("bootstrap.executed", exit_codes["2"])
        self.assertNotIn("before any remote mutation", json.dumps(exit_codes))
        self.assertIn("remote changed before origin update", bootstrap)
        self.assertIn("bootstrap_executed = True", bootstrap)
        self.assertNotIn("confirmation", json.dumps(exit_codes, ensure_ascii=False))
        self.assertNotIn("input(", bootstrap)
        self.assertNotIn("sys.exit(1)", bootstrap)
        self.assertNotIn("SystemExit(1)", bootstrap)
        self.assertIn("unclassifiable failure", exit_codes["scope"])
        self.assertIn("pre-execution guard", exit_codes["scope"])
        self.assertNotIn("never returns", exit_codes["scope"])
        finalizer = (ROOT / "git-finalize").read_text(encoding="utf-8")
        self.assertIn("repository bootstrap companion is missing", finalizer)
        self.assertIn('"executed": bootstrap_executed', bootstrap)
        boundaries = (
            ROOT / "docs" / "integration-boundaries.md"
        ).read_text(encoding="utf-8")
        self.assertIn("决策无法归类", boundaries)
        self.assertNotIn(
            "destructive confirmation condition”描述与", boundaries
        )

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
