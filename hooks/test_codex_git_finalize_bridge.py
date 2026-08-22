from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock


BRIDGE_PATH = Path(__file__).with_name("codex_git_finalize_bridge.py")
ROOT = BRIDGE_PATH.parents[1]
SPEC = importlib.util.spec_from_file_location("codex_git_finalize_bridge", BRIDGE_PATH)
assert SPEC is not None and SPEC.loader is not None
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class GitFinalizerBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.transcript = self.sessions / "session.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.command = " ".join(
            (
                bridge.FINALIZER,
                "--repo",
                shlex.quote(str(self.repo)),
                "--message",
                shlex.quote("validated message"),
                "--",
                "scripts/runner.py",
                "tests/test_runner.py",
            )
        )
        self.initial_command = " ".join(
            (
                bridge.FINALIZER,
                "--initial-publish",
                "--remote",
                "origin",
                "--repo",
                shlex.quote(str(self.repo)),
                "--message",
                shlex.quote("validated initial message"),
                "--",
                "scripts/runner.py",
                "tests/test_runner.py",
            )
        )
        self.initial_branch_command = " ".join(
            (
                bridge.FINALIZER,
                "--initial-branch-publish",
                "--remote",
                "origin",
                "--remote-branch",
                "feat/validated-branch",
                "--repo",
                shlex.quote(str(self.repo)),
                "--message",
                shlex.quote("validated branch message"),
                "--",
                "scripts/runner.py",
                "tests/test_runner.py",
            )
        )
        self.snapshot_command = self.initial_command.replace(
            " --repo ", " --snapshot " + "a" * 64 + " --repo ", 1
        )
        self.summary_command = self.command.replace(" --repo ", " --summary --repo ", 1)
        self.commit_only_command = self.command.replace(
            " --repo ", " --mode commit-only --repo ", 1
        )
        self.verify_only_command = " ".join(
            (
                bridge.FINALIZER,
                "--mode",
                "verify-only",
                "--repo",
                shlex.quote(str(self.repo)),
                "--",
                "scripts/runner.py",
                "tests/test_runner.py",
            )
        )
        self.resume_publish_command = " ".join(
            (
                bridge.FINALIZER,
                "--resume-publish",
                "a" * 40,
                "--repo",
                shlex.quote(str(self.repo)),
            )
        )
        self.publish_existing_branch_command = " ".join(
            (
                bridge.FINALIZER,
                "--publish-existing-branch",
                "c" * 40,
                "--remote",
                "origin",
                "--remote-branch",
                "feat/validated-branch",
                "--repo",
                shlex.quote(str(self.repo)),
            )
        )
        self.retirement_command = " ".join(
            (
                bridge.FINALIZER,
                "--retire-remote-branch",
                "feat/validated-branch",
                "--remote",
                "origin",
                "--integrated-into",
                "main",
                "--expected-remote-oid",
                "d" * 40,
                "--ci-required",
                "--ci-status",
                "SUCCESS",
                "--ci-commit-oid",
                "e" * 40,
                "--ci-verification-source",
                "human_authenticated_ui",
                "--repo",
                shlex.quote(str(self.repo)),
            )
        )
        self.resume_initial_command = " ".join(
            (
                bridge.FINALIZER,
                "--resume-initial-publish",
                "b" * 40,
                "--remote",
                "origin",
                "--repo",
                shlex.quote(str(self.repo)),
            )
        )

    def event(self, command: object | None = None) -> dict[str, object]:
        return {
            "cwd": str(self.repo),
            "hook_event_name": "PreToolUse",
            "model": "gpt-test",
            "permission_mode": "default",
            "session_id": "session-test",
            "tool_input": {"command": self.command if command is None else command},
            "tool_name": "Bash",
            "tool_use_id": "exec-12345678-1234-1234-1234-123456789abc",
            "transcript_path": str(self.transcript),
            "turn_id": "turn-test",
        }

    def invoke_main(self, event: dict[str, object]) -> tuple[dict[str, Any], mock.Mock]:
        stdin = io.StringIO(json.dumps(event))
        stdout = io.StringIO()
        run = mock.Mock(return_value=(0, "synthetic success"))
        with (
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(bridge, "TRANSCRIPT_ROOT", self.sessions),
            mock.patch.object(bridge, "require_existing_allow_rule"),
            mock.patch.object(bridge, "run_finalizer", run),
        ):
            bridge.main()
        return json.loads(stdout.getvalue()), run

    def write_transcript_call(self, arguments: dict[str, object]) -> None:
        record = {
            "payload": {
                "arguments": json.dumps(arguments),
                "call_id": "exec-12345678-1234-1234-1234-123456789abc",
                "name": "exec_command",
                "type": "function_call",
            }
        }
        self.transcript.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def test_real_pretool_payload_shape_is_authoritative(self) -> None:
        command, argv, repo, dry_run = bridge.validate_hook_event(self.event())

        self.assertEqual(command, self.command)
        self.assertEqual(argv[0], bridge.FINALIZER)
        self.assertEqual(repo, self.repo)
        self.assertFalse(dry_run)

    def test_initial_publish_and_remote_are_accepted_together(self) -> None:
        command, argv, repo, dry_run = bridge.validate_hook_event(
            self.event(self.initial_command)
        )

        self.assertEqual(command, self.initial_command)
        self.assertIn("--initial-publish", argv)
        self.assertEqual(argv[argv.index("--remote") + 1], "origin")
        self.assertEqual(repo, self.repo)
        self.assertFalse(dry_run)

    def test_initial_publish_runs_only_the_original_validated_argv(self) -> None:
        output, run = self.invoke_main(self.event(self.initial_command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], shlex.split(self.initial_command))

    def test_initial_branch_publish_is_accepted_and_forwarded(self) -> None:
        command, argv, repo, dry_run = bridge.validate_hook_event(
            self.event(self.initial_branch_command)
        )

        self.assertEqual(command, self.initial_branch_command)
        self.assertIn("--initial-branch-publish", argv)
        self.assertEqual(argv[argv.index("--remote") + 1], "origin")
        self.assertEqual(
            argv[argv.index("--remote-branch") + 1], "feat/validated-branch"
        )
        self.assertEqual(repo, self.repo)
        self.assertFalse(dry_run)

        output, run = self.invoke_main(self.event(self.initial_branch_command))
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0], shlex.split(self.initial_branch_command)
        )

    def test_initial_publish_snapshot_is_accepted_and_forwarded(self) -> None:
        output, run = self.invoke_main(self.event(self.snapshot_command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], shlex.split(self.snapshot_command))

    def test_publish_existing_branch_is_accepted_and_forwarded(self) -> None:
        command, argv, repo, dry_run = bridge.validate_hook_event(
            self.event(self.publish_existing_branch_command)
        )

        self.assertEqual(command, self.publish_existing_branch_command)
        self.assertIn("--publish-existing-branch", argv)
        self.assertEqual(argv[argv.index("--remote") + 1], "origin")
        self.assertEqual(
            argv[argv.index("--remote-branch") + 1], "feat/validated-branch"
        )
        self.assertEqual(repo, self.repo)
        self.assertFalse(dry_run)

        output, run = self.invoke_main(self.event(self.publish_existing_branch_command))
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0], shlex.split(self.publish_existing_branch_command)
        )

    def test_summary_is_accepted_and_forwarded_without_rewriting(self) -> None:
        output, run = self.invoke_main(self.event(self.summary_command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], shlex.split(self.summary_command))

    def test_allow_test_fixture_is_accepted_and_forwarded_without_rewriting(
        self,
    ) -> None:
        command = self.command.replace(
            " -- ", " --allow-test-fixture tests/foo.py -- ", 1
        )

        output, run = self.invoke_main(self.event(command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], shlex.split(command))

    def test_legitimate_self_release_scope_is_accepted_and_forwarded(self) -> None:
        prefix = self.command.rsplit(" -- ", 1)[0]
        command = (
            prefix + " -- codex-git-finalize hooks/codex_git_finalize_bridge.py"
            " tests/test_codex_git_finalize_bridge.py tool_cli_contract.json"
        )

        output, run = self.invoke_main(self.event(command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], shlex.split(command))

    def test_repeated_allow_test_fixture_values_are_forwarded_in_order(self) -> None:
        command = self.command.replace(
            " -- ",
            " --allow-test-fixture tests/foo.py --allow-test-fixture tests/bar.py -- ",
            1,
        )

        output, run = self.invoke_main(self.event(command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], shlex.split(command))

    def test_fixture_path_semantics_are_left_to_finalizer(self) -> None:
        command = self.command.replace(
            " -- ", " --allow-test-fixture ../outside.py -- ", 1
        )

        output, run = self.invoke_main(self.event(command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], shlex.split(command))

    def test_bridge_option_schema_matches_public_cli_contract(self) -> None:
        contract = json.loads(
            (ROOT / "tool_cli_contract.json").read_text(encoding="utf-8")
        )
        expected = contract["host_bridge"]["exposed_options"]
        actual = {
            option: {"arity": arity, "repeatable": repeatable}
            for option, (arity, repeatable) in bridge.BRIDGE_OPTION_SPECS.items()
        }
        command_options = {
            flag.split(maxsplit=1)[0]
            for command in contract["commands"]
            for flag in command["flags"]
            if flag != "--"
        }

        self.assertEqual(actual, expected)
        self.assertEqual(set(expected), command_options)
        self.assertEqual(contract["host_bridge"]["path_delimiter"], "--")
        self.assertEqual(contract["host_bridge"]["unknown_options"], "reject")

    def test_remote_retirement_is_accepted_and_forwarded_without_rewriting(
        self,
    ) -> None:
        output, run = self.invoke_main(self.event(self.retirement_command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], shlex.split(self.retirement_command))

    def test_remote_retirement_contract_rejects_incomplete_or_unsafe_calls(
        self,
    ) -> None:
        commands = (
            self.retirement_command.replace(
                " --expected-remote-oid " + "d" * 40, "", 1
            ),
            self.retirement_command.replace(
                " --ci-status SUCCESS", " --ci-status FAILED", 1
            ),
            self.retirement_command.replace(
                " --ci-verification-source human_authenticated_ui",
                " --ci-verification-source not_required",
                1,
            ),
            self.retirement_command + " -- scripts/runner.py",
            self.retirement_command.replace(
                " --retire-remote-branch ",
                " --resume-publish " + "a" * 40 + " --retire-remote-branch ",
                1,
            ),
        )

        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(command))

    def test_commit_only_is_accepted_and_forwarded_without_rewriting(self) -> None:
        output, run = self.invoke_main(self.event(self.commit_only_command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], shlex.split(self.commit_only_command))

    def test_verify_only_is_accepted_without_message_and_forwarded(self) -> None:
        output, run = self.invoke_main(self.event(self.verify_only_command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], shlex.split(self.verify_only_command))

    def test_resume_publish_is_accepted_without_message_paths_or_remote(self) -> None:
        output, run = self.invoke_main(self.event(self.resume_publish_command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0], shlex.split(self.resume_publish_command)
        )

    def test_legacy_root_resume_is_accepted_and_forwarded(self) -> None:
        output, run = self.invoke_main(self.event(self.resume_initial_command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0], shlex.split(self.resume_initial_command)
        )

    def test_duplicate_summary_is_rejected(self) -> None:
        duplicate = self.summary_command.replace("--summary", "--summary --summary", 1)

        with self.assertRaises(bridge.BridgeError):
            bridge.validate_hook_event(self.event(duplicate))

    def test_missing_current_transcript_call_still_allows_valid_finalizer(self) -> None:
        output, run = self.invoke_main(self.event())

        decision = output["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "allow")
        self.assertEqual(decision["updatedInput"], {"command": "/usr/bin/true"})
        run.assert_called_once()

    def test_existing_direct_transcript_call_is_cross_checked(self) -> None:
        self.write_transcript_call(
            {
                "cmd": self.command,
                "sandbox_permissions": "require_escalated",
                "workdir": str(self.repo),
            }
        )

        output, run = self.invoke_main(self.event())

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()

    def test_conflicting_direct_transcript_call_is_denied(self) -> None:
        self.write_transcript_call(
            {
                "cmd": self.command + " unexpected.py",
                "sandbox_permissions": "require_escalated",
                "workdir": str(self.repo),
            }
        )

        output, run = self.invoke_main(self.event())

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        run.assert_not_called()

    def test_only_observed_bash_hook_name_is_accepted(self) -> None:
        for name in ("exec_command", "container.exec", "exec"):
            with self.subTest(name=name):
                event = self.event()
                event["tool_name"] = name
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(event)

    def test_direct_validator_rejects_non_direct_commands(self) -> None:
        for command in (
            "/usr/bin/true",
            "/bin/bash -lc " + shlex.quote(self.command),
            "/usr/bin/printf " + shlex.quote(self.command),
        ):
            with self.subTest(command=command):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(command))

    def test_non_finalizer_command_never_receives_a_bridge_allow(self) -> None:
        event = self.event("/usr/bin/true")
        stdin = io.StringIO(json.dumps(event))
        stdout = io.StringIO()
        run = mock.Mock(return_value=(0, "synthetic success"))
        with (
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(bridge, "run_finalizer", run),
        ):
            bridge.main()

        self.assertEqual(stdout.getvalue(), "")
        run.assert_not_called()

    def test_safe_finalizer_inspection_passes_through_without_bridge_allow(
        self,
    ) -> None:
        commands = (
            "rg -n 'codex-git-finalize|bridge' hooks README.md",
            "file /home/hsd/bin/codex-git-finalize",
            "readlink -f /home/hsd/bin/codex-git-finalize",
            "sed -n '1,80p' /home/hsd/bin/codex-git-finalize",
            "git status --short && git diff -- hooks/codex_git_finalize_bridge.py",
            "command -v codex-git-finalize | head -n 1",
            "/home/hsd/bin/codex-git-finalize --help | sed -n '1,80p'",
            "/home/hsd/bin/codex-git-finalize --version",
        )
        for command in commands:
            with self.subTest(command=command):
                event = self.event(command)
                stdin = io.StringIO(json.dumps(event))
                stdout = io.StringIO()
                run = mock.Mock(return_value=(0, "synthetic success"))
                with (
                    mock.patch.object(sys, "stdin", stdin),
                    mock.patch.object(sys, "stdout", stdout),
                    mock.patch.object(bridge, "run_finalizer", run),
                ):
                    bridge.main()

                self.assertEqual(stdout.getvalue(), "")
                run.assert_not_called()

    def test_unpublished_queue_path_data_passes_through_without_bridge_allow(
        self,
    ) -> None:
        command = " ".join(
            (
                "/usr/bin/python3",
                "-B",
                "/home/hsd/.agents/skills/three-tool-git-workflow/unpublished_queue.py",
                "upsert",
                "--repo",
                shlex.quote(str(self.repo)),
                "--workstream",
                "self-release",
                "--publication-decision",
                "intentionally_unpublished",
                "--finalization-scope",
                "not_applicable",
                "--finalization-outcome",
                "not_run",
                "--path",
                "codex-git-finalize",
            )
        )
        stdin = io.StringIO(json.dumps(self.event(command)))
        stdout = io.StringIO()
        run = mock.Mock(return_value=(0, "synthetic success"))
        with (
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(bridge, "run_finalizer", run),
        ):
            bridge.main()

        self.assertEqual(stdout.getvalue(), "")
        run.assert_not_called()

    def test_commands_that_can_execute_finalizer_stay_denied(self) -> None:
        commands = (
            "/bin/bash -lc " + shlex.quote(self.command),
            "python3 -c "
            + shlex.quote(
                "import subprocess; subprocess.run(['/home/hsd/bin/codex-git-finalize'])"
            ),
            "find . -exec /home/hsd/bin/codex-git-finalize --help \\;",
            "rg --pre=/home/hsd/bin/codex-git-finalize pattern .",
            "git -c alias.inspect=!/home/hsd/bin/codex-git-finalize inspect",
            "/tmp/rg -n codex-git-finalize README.md",
            "./git status --short -- codex-git-finalize",
            "/home/hsd/bin/codex-git-finalize --help -- README.md",
            "rg -n bridge README.md && " + self.verify_only_command,
        )
        for command in commands:
            with self.subTest(command=command):
                output, run = self.invoke_main(self.event(command))

                self.assertEqual(
                    output["hookSpecificOutput"]["permissionDecision"], "deny"
                )
                run.assert_not_called()

    def test_nested_finalizer_disguise_gets_hook_deny(self) -> None:
        command = "/bin/bash -lc " + shlex.quote(self.command)

        output, run = self.invoke_main(self.event(command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        run.assert_not_called()

    def test_finalizer_path_spoof_is_rejected(self) -> None:
        command = self.command.replace(bridge.FINALIZER, "/tmp/codex-git-finalize", 1)

        with self.assertRaises(bridge.BridgeError):
            bridge.validate_hook_event(self.event(command))

    def test_explicit_repo_controls_bridge_cwd_when_event_cwd_differs(self) -> None:
        other_repo = self.root / "other"
        other_repo.mkdir()
        command = self.command.replace(str(self.repo), str(other_repo), 1)

        output, run = self.invoke_main(self.event(command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[1], other_repo)

    def test_transcript_workdir_can_authorize_a_different_event_cwd(self) -> None:
        other = self.root / "session-cwd"
        other.mkdir()
        event = self.event()
        event["cwd"] = str(other)
        self.write_transcript_call(
            {
                "cmd": self.command,
                "sandbox_permissions": "require_escalated",
                "workdir": str(self.repo),
            }
        )

        output, run = self.invoke_main(event)

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
        run.assert_called_once()

    def test_transcript_workdir_mismatch_is_denied(self) -> None:
        other = self.root / "other-workdir"
        other.mkdir()
        self.write_transcript_call(
            {
                "cmd": self.command,
                "sandbox_permissions": "require_escalated",
                "workdir": str(other),
            }
        )

        output, run = self.invoke_main(self.event())

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        run.assert_not_called()

    def test_explicit_paths_must_stay_in_repo_scope(self) -> None:
        prefix = self.command.rsplit(" -- ", 1)[0]
        for paths in ("../outside.py", "/tmp/outside.py", ".", "-A", "file.py ; true"):
            with self.subTest(paths=paths):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(prefix + " -- " + paths))

    def test_tool_input_missing_or_malformed_is_rejected(self) -> None:
        values: tuple[object, ...] = (
            None,
            "not-an-object",
            {},
            {"command": 42},
            {"command": self.command, "x": 1},
        )
        for value in values:
            with self.subTest(value=value):
                event = self.event()
                event["tool_input"] = value
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(event)

    def test_initial_publish_requires_remote_and_remote_requires_initial_mode(
        self,
    ) -> None:
        initial_without_remote = self.initial_command.replace(" --remote origin", "", 1)
        normal_with_remote = self.command.replace(
            " --repo ", " --remote origin --repo ", 1
        )

        for command in (initial_without_remote, normal_with_remote):
            with self.subTest(command=command):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(command))

    def test_initial_branch_publish_requires_one_explicit_matching_option_set(
        self,
    ) -> None:
        missing_remote = self.initial_branch_command.replace(" --remote origin", "", 1)
        missing_branch = self.initial_branch_command.replace(
            " --remote-branch feat/validated-branch", "", 1
        )
        normal_with_branch = self.command.replace(
            " --repo ", " --remote-branch feat/validated-branch --repo ", 1
        )
        conflicting_modes = self.initial_branch_command.replace(
            " --initial-branch-publish",
            " --initial-publish --initial-branch-publish",
            1,
        )
        with_snapshot = self.initial_branch_command.replace(
            " --repo ", " --snapshot " + "a" * 64 + " --repo ", 1
        )
        invalid_branch = self.initial_branch_command.replace(
            "feat/validated-branch", shlex.quote("bad\tbranch"), 1
        )

        for command in (
            missing_remote,
            missing_branch,
            normal_with_branch,
            conflicting_modes,
            with_snapshot,
            invalid_branch,
        ):
            with self.subTest(command=command):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(command))

    def test_snapshot_requires_initial_mode_and_exact_id(self) -> None:
        normal_with_snapshot = self.command.replace(
            " --repo ", " --snapshot " + "a" * 64 + " --repo ", 1
        )
        invalid_snapshot = self.snapshot_command.replace("a" * 64, "ABC123", 1)

        for command in (normal_with_snapshot, invalid_snapshot):
            with self.subTest(command=command):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(command))

    def test_remote_missing_duplicate_and_invalid_values_are_rejected(self) -> None:
        prefix, paths = self.initial_command.split(" -- ", 1)
        without_remote = prefix.replace(" --remote origin", "", 1)
        commands = (
            without_remote + " --remote -- " + paths,
            prefix.replace(" --remote origin", " --remote origin --remote backup", 1)
            + " -- "
            + paths,
            prefix.replace(" --remote origin", " --remote ''", 1) + " -- " + paths,
            prefix.replace(" --remote origin", " --remote -origin", 1) + " -- " + paths,
            prefix.replace(
                " --remote origin", " --remote " + shlex.quote("bad\tremote"), 1
            )
            + " -- "
            + paths,
        )

        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(command))

    def test_options_with_missing_values_are_rejected(self) -> None:
        prefix, paths = self.command.split(" -- ", 1)
        commands = (
            prefix + " --remote -- " + paths,
            prefix.replace(" --message 'validated message'", " --message", 1)
            + " -- "
            + paths,
            prefix + " --allow-test-fixture -- " + paths,
        )

        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(command))

    def test_unapproved_finalizer_options_are_rejected(self) -> None:
        for option in ("--amend", "--force", "--force-with-lease", "--unknown"):
            with self.subTest(option=option):
                command = self.command.replace(" -- ", " " + option + " -- ", 1)
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(command))

    def test_explicit_modes_reject_invalid_duplicate_and_conflicting_options(
        self,
    ) -> None:
        commands = (
            self.command.replace(" --repo ", " --mode invalid --repo ", 1),
            self.commit_only_command.replace(
                " --mode commit-only ",
                " --mode commit-only --mode commit-only ",
                1,
            ),
            self.initial_command.replace(
                " --initial-publish ",
                " --initial-publish --mode commit-only ",
                1,
            ),
            self.initial_branch_command.replace(
                " --initial-branch-publish ",
                " --initial-branch-publish --mode commit-only ",
                1,
            ),
            self.verify_only_command.replace(
                " --repo ", " --message unexpected --repo ", 1
            ),
            self.verify_only_command.replace(" --repo ", " --dry-run --repo ", 1),
            self.initial_command.replace(
                " --initial-publish ",
                " --initial-publish --mode verify-only ",
                1,
            ),
        )

        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(command))

    def test_resume_publish_rejects_unsafe_or_conflicting_options(self) -> None:
        commands = (
            self.resume_publish_command.replace("a" * 40, "abc", 1),
            self.resume_publish_command.replace(
                " --repo ", " --remote origin --repo ", 1
            ),
            self.resume_publish_command.replace(
                " --repo ", " --message unexpected --repo ", 1
            ),
            self.resume_publish_command + " -- scripts/runner.py",
            self.resume_publish_command.replace(
                " --repo ", " --mode commit-only --repo ", 1
            ),
            self.resume_publish_command.replace(
                " --resume-publish ",
                " --resume-publish " + "b" * 40 + " --resume-initial-publish ",
                1,
            ),
            self.resume_initial_command.replace(" --remote origin", "", 1),
        )

        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(command))

    def test_publish_existing_branch_rejects_unsafe_or_conflicting_options(
        self,
    ) -> None:
        commands = (
            self.publish_existing_branch_command.replace("c" * 40, "abc", 1),
            self.publish_existing_branch_command.replace(" --remote origin", "", 1),
            self.publish_existing_branch_command.replace(
                " --remote-branch feat/validated-branch", "", 1
            ),
            self.publish_existing_branch_command.replace(
                " --repo ", " --message unexpected --repo ", 1
            ),
            self.publish_existing_branch_command.replace(
                " --repo ", " --mode commit-only --repo ", 1
            ),
            self.publish_existing_branch_command.replace(
                " --repo ", " --dry-run --repo ", 1
            ),
            self.publish_existing_branch_command + " -- scripts/runner.py",
            self.publish_existing_branch_command.replace(
                " --publish-existing-branch ",
                " --resume-publish " + "a" * 40 + " --publish-existing-branch ",
                1,
            ),
        )

        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(command))


if __name__ == "__main__":
    unittest.main()
