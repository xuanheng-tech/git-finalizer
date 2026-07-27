from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock


BRIDGE_PATH = Path(__file__).with_name("codex_git_finalize_bridge.py")
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

    def invoke_main(
        self, event: dict[str, object]
    ) -> tuple[dict[str, object], mock.Mock]:
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

    def test_non_finalizer_and_nested_string_disguise_are_rejected(self) -> None:
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

    def test_nested_finalizer_disguise_gets_hook_deny(self) -> None:
        command = "/bin/bash -lc " + shlex.quote(self.command)

        output, run = self.invoke_main(self.event(command))

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        run.assert_not_called()

    def test_finalizer_path_spoof_is_rejected(self) -> None:
        command = self.command.replace(bridge.FINALIZER, "/tmp/codex-git-finalize", 1)

        with self.assertRaises(bridge.BridgeError):
            bridge.validate_hook_event(self.event(command))

    def test_repo_must_match_hook_cwd(self) -> None:
        other_repo = self.root / "other"
        other_repo.mkdir()
        command = self.command.replace(str(self.repo), str(other_repo), 1)

        with self.assertRaises(bridge.BridgeError):
            bridge.validate_hook_event(self.event(command))

    def test_explicit_paths_must_stay_in_repo_scope(self) -> None:
        prefix = self.command.rsplit(" -- ", 1)[0]
        for paths in ("../outside.py", "/tmp/outside.py", ".", "-A", "file.py ; true"):
            with self.subTest(paths=paths):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(prefix + " -- " + paths))

    def test_tool_input_missing_or_malformed_is_rejected(self) -> None:
        values = (
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

    def test_initial_publish_requires_remote_and_remote_requires_initial_mode(self) -> None:
        initial_without_remote = self.initial_command.replace(" --remote origin", "", 1)
        normal_with_remote = self.command.replace(" --repo ", " --remote origin --repo ", 1)

        for command in (initial_without_remote, normal_with_remote):
            with self.subTest(command=command):
                with self.assertRaises(bridge.BridgeError):
                    bridge.validate_hook_event(self.event(command))

    def test_remote_missing_duplicate_and_invalid_values_are_rejected(self) -> None:
        prefix, paths = self.initial_command.split(" -- ", 1)
        without_remote = prefix.replace(" --remote origin", "", 1)
        commands = (
            without_remote + " --remote -- " + paths,
            prefix.replace(
                " --remote origin", " --remote origin --remote backup", 1
            )
            + " -- "
            + paths,
            prefix.replace(" --remote origin", " --remote ''", 1) + " -- " + paths,
            prefix.replace(" --remote origin", " --remote -origin", 1)
            + " -- "
            + paths,
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


if __name__ == "__main__":
    unittest.main()
