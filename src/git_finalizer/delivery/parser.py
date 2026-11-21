"""State-aware single-pass CLI parser for Git Finalizer.

Prevents flag hijacking via option values (e.g. `--message --repo-plan`,
`--message --summary`, `--run-id --summary`) and prevents `--version`/`--help`
combined with `--summary` from emitting a fabricated success summary.
"""

from __future__ import annotations

import dataclasses
import shlex
from typing import Any

from git_finalizer import __version__

VALUE_FLAGS = frozenset(
    {
        "--mode",
        "--repo",
        "--message",
        "-m",
        "--remote",
        "--remote-branch",
        "--source-remote",
        "--expected-target-oid",
        "--snapshot",
        "--allow-test-fixture",
        "--fixture-exceptions",
        "--allow-large-binary",
        "--reviewed-sensitive-source",
        "--merge-parent",
        "--publish-existing-branch",
        "--publish-existing-history",
        "--resume-existing-history-publish",
        "--resume-publish",
        "--resume-initial-publish",
        "--sync-published-branch",
        "--publish-integration-candidate",
        "--retire-remote-branch",
        "--retire-local-branch",
        "--expected-remote-oid",
        "--expected-local-oid",
        "--expected-integrated-oid",
        "--integrated-into",
        "--retirement-plan-id",
        "--ci-status",
        "--ci-commit-oid",
        "--ci-verification-source",
        "--ci-verified-at",
        "--lease-id",
        "--run-id",
        "--gitea-url",
        "--owner",
        "--repo-name",
        "--visibility",
        "--remote-scheme",
        "--timeout",
        "--repository-id",
        "--allocation-id",
        "--task-key",
        "--authority-key",
        "--worktree-path",
        "--role",
        "--trailer-agent",
        "--trace-ref",
        "--scanner",
        "--sarif-cmd",
        "--opa-policy",
        "--opa-query",
    }
)

BOOLEAN_FLAGS = frozenset(
    {
        "--help",
        "-h",
        "--version",
        "-V",
        "--dry-run",
        "--summary",
        "--diagnostics",
        "--initial-publish",
        "--initial-commit-only",
        "--initial-branch-publish",
        "--repo-plan",
        "--repo-ensure",
        "--add-origin",
        "--allow-insecure-http",
        "--ci-required",
        "--agent-trace",
    }
)


@dataclasses.dataclass
class ParsedCLI:
    raw_argv: list[str]
    dispatch: str = "core"  # "version" | "help" | "bootstrap" | "integration_publish" | "core"
    mode: str = "normal"  # normalized summary mode name
    action: str = "normal"  # specific CLI sub-action (e.g. resume-publish vs resume-initial-publish)
    repo: str | None = None
    message: str | None = None
    remote: str | None = None
    remote_branch: str | None = None
    source_remote: str | None = None
    expected_target_oid: str | None = None
    snapshot: str | None = None
    allow_test_fixtures: list[str] = dataclasses.field(default_factory=list)
    fixture_exceptions: str | None = None
    allow_large_binary: str | None = None
    reviewed_sensitive_source: str | None = None
    merge_parent: str | None = None
    head_oid: str | None = None
    retire_branch: str | None = None
    expected_remote_oid: str | None = None
    expected_local_oid: str | None = None
    expected_integrated_oid: str | None = None
    integrated_into: str = "main"
    retirement_plan_id: str | None = None
    ci_required: bool = False
    ci_status: str | None = None
    ci_commit_oid: str | None = None
    ci_verification_source: str = "not_required"
    ci_verification_source_seen: bool = False
    ci_verified_at: str | None = None
    dry_run: bool = False
    summary: bool = False
    diagnostics: bool = False
    repository_id: str | None = None
    allocation_id: str | None = None
    task_key: str | None = None
    authority_key: str | None = None
    worktree_path: str | None = None
    role: str | None = None
    trailer_agent: str | None = None
    trace_ref: str | None = None
    agent_trace: bool = False
    scanners: list[str] = dataclasses.field(default_factory=list)
    sarif_cmd: str | None = None
    opa_policy: str | None = None
    opa_query: str = "data.git_finalizer.delivery.deny"
    paths: list[str] = dataclasses.field(default_factory=list)
    unknown_flags: list[str] = dataclasses.field(default_factory=list)
    parse_error: str | None = None
    parse_diagnostics: dict[str, Any] | None = None


def _format_command_hint(tokens: list[str]) -> str:
    return shlex.join(["git-finalize", *tokens])


def _strip_flags(
    argv: list[str],
    flag_names: set[str],
    *,
    value_flags: set[str] | None = None,
) -> list[str]:
    vflags = value_flags or set()
    cleaned: list[str] = []
    idx = 0
    total = len(argv)
    while idx < total:
        tok = argv[idx]
        if tok == "--":
            cleaned.extend(argv[idx:])
            break
        flag = tok.partition("=")[0] if tok.startswith("--") and "=" in tok else tok
        if flag in flag_names:
            if flag in vflags and "=" not in tok and idx + 1 < total:
                idx += 2
            else:
                idx += 1
            continue
        cleaned.append(tok)
        if flag in VALUE_FLAGS and "=" not in tok and idx + 1 < total:
            cleaned.append(argv[idx + 1])
            idx += 2
            continue
        idx += 1
    return cleaned


def _strip_paths_after_separator(argv: list[str]) -> list[str]:
    idx = 0
    total = len(argv)
    while idx < total:
        tok = argv[idx]
        if tok == "--":
            return list(argv[:idx])
        flag = tok.partition("=")[0] if tok.startswith("--") and "=" in tok else tok
        if flag in VALUE_FLAGS and "=" not in tok and idx + 1 < total:
            idx += 2
            continue
        idx += 1
    return list(argv)


def _set_cli_error(
    parsed: ParsedCLI,
    message: str,
    *,
    issue: str,
    suggested_argv: list[str] | None = None,
    note: str | None = None,
) -> None:
    parsed.parse_error = message
    hint: dict[str, Any] = {
        "issue": issue,
        "action": parsed.action,
    }
    if suggested_argv is not None:
        hint["suggested_command"] = _format_command_hint(suggested_argv)
    if note:
        hint["note"] = note
    parsed.parse_diagnostics = {"cli_hint": hint}


def usage_text() -> str:
    return f"""git-finalize {__version__}

Usage:
  git-finalize --mode verify-only --repo <repo> [--diagnostics] [--merge-parent <oid>] [--reviewed-sensitive-source <file>] [--] <path>...
  git-finalize --mode commit-only --repo <repo> --message <msg> [--reviewed-sensitive-source <file>] [--] <path>...
  git-finalize --repo <repo> --message <msg> [--summary] [--] <path>...
  git-finalize --initial-publish --remote <remote> --repo <repo> --message <msg> [--snapshot <64hex>] [--] <path>...
  git-finalize --initial-commit-only --repo <repo> --message <msg> [--] <path>...
  git-finalize --initial-branch-publish --remote <remote> --remote-branch <branch> --repo <repo> --message <msg> [--] <path>...
  git-finalize --publish-existing-branch <oid> --remote <remote> --remote-branch <branch> --repo <repo> [--fixture-exceptions <file>] [--allow-large-binary <path>] [--reviewed-sensitive-source <file>]
  git-finalize --publish-existing-history <oid> --remote <remote> --remote-branch <branch> --repo <repo> [--fixture-exceptions <file>] [--allow-large-binary <path>]
  git-finalize --resume-publish <oid> --repo <repo> [--fixture-exceptions <file>] [--reviewed-sensitive-source <file>]
  git-finalize --resume-initial-publish <oid> --remote <remote> --repo <repo> [--allow-test-fixture <path>]
  git-finalize --resume-existing-history-publish <oid> --remote <remote> --remote-branch <branch> --repo <repo>
  git-finalize --sync-published-branch <oid> --source-remote <src> --remote <dst> --remote-branch <branch> --expected-target-oid <oid> --repo <repo>
  git-finalize --publish-integration-candidate <oid> --repo <candidate> --lease-id <id> --run-id <run> [--summary]
  git-finalize --retire-remote-branch <branch> --remote <remote> --integrated-into <ref> --expected-remote-oid <oid> --expected-integrated-oid <oid> --retirement-plan-id <64hex> --repo <repo>
  git-finalize --retire-local-branch <branch> --remote <remote> --integrated-into <ref> --expected-local-oid <oid> --expected-integrated-oid <oid> --retirement-plan-id <64hex> --repo <repo>
  git-finalize --repo-plan --repo <repo> --gitea-url <url> --owner <owner> --repo-name <name> --visibility <private|public> [--summary]
  git-finalize --repo-ensure --repo <repo> --gitea-url <url> --owner <owner> --repo-name <name> --visibility <private|public> [--add-origin] [--summary]

Options:
  --mode <normal|verify-only|commit-only>   Execution mode
  --repo <path>                             Absolute or resolved Git worktree root
  --message <text>, -m <text>               Commit message
  --remote <name>                           Target remote name
  --remote-branch <name>                    Target remote branch name
  --source-remote <name>                    Source remote for --sync-published-branch
  --expected-target-oid <oid>               Expected target remote OID for sync
  --snapshot <64hex>                        Snapshot Runner diff-audit ID (initial-publish only)
  --allow-test-fixture <path>               Exact synthetic test fixture path under tests/
  --fixture-exceptions <file>               Exact reviewed fixture exception manifest JSON
  --allow-large-binary <path>               Exact binary blob override (5 MiB .. 25 MiB)
  --reviewed-sensitive-source <file>        External reviewed sensitive source manifest JSON
  --merge-parent <oid>                      Second parent OID for resolved two-parent merge
  --summary                                 Emit single-line deterministic JSON summary
  --diagnostics                             Include bounded runtime and scanner diagnostics in summary
  --dry-run                                 Validate without mutating Git state or remotes
  --trailer-agent <name>                    Add Assisted-by AI provenance Git trailer
  --trace-ref <uri>                         Add Agent-Trace-Ref Git trailer
  --agent-trace                             Write Agent Trace JSON note to refs/notes/agent-trace
  --scanner <gitleaks|trufflehog|osv|sarif-cmd>  Run pluggable SARIF 2.1.0 external scanner
  --sarif-cmd <exe>                         Executable for --scanner sarif-cmd
  --opa-policy <path>                       Optional OPA Rego policy file or directory
  --opa-query <query>                       OPA evaluation query (default: data.git_finalizer.delivery.deny)
  --ci-verified-at <timestamp>              CI verification timestamp metadata for branch retirement
  --repository-id <id>                      Controller repository identity metadata
  --allocation-id <id>                      Controller allocation identity metadata
  --task-key <key>                          Controller task key metadata
  --authority-key <key>                     Controller authority key metadata
  --worktree-path <path>                    Controller worktree path metadata
  --role <role>                             Controller worktree role metadata
  --version, -V                             Print version and exit
  --help, -h                                Print this help message and exit
"""


def parse_cli(argv: list[str]) -> ParsedCLI:
    """Parse CLI arguments left-to-right in a single state-aware pass."""
    parsed = ParsedCLI(raw_argv=list(argv))
    exclusive_actions: list[str] = []
    explicit_mode_flag = False
    seen_flags: set[str] = set()
    repeatable_flags = {"--allow-test-fixture", "--scanner"}
    stray_positionals: list[str] = []
    option_tokens_before_sep: list[str] = []
    stray_error_only = False

    idx = 0
    total = len(argv)
    while idx < total:
        token = argv[idx]
        if token == "--":
            parsed.paths.extend(argv[idx + 1 :])
            break

        flag = token
        inline_value: str | None = None
        if token.startswith("--") and "=" in token:
            flag, _, inline_value = token.partition("=")

        if flag in {"--version", "-V"}:
            if inline_value is not None:
                parsed.parse_error = f"选项 {flag} 不接受参数值"
            parsed.dispatch = "version"
            option_tokens_before_sep.append(token)
            idx += 1
            continue
        if flag in {"--help", "-h"}:
            if inline_value is not None:
                parsed.parse_error = f"选项 {flag} 不接受参数值"
            parsed.dispatch = "help"
            option_tokens_before_sep.append(token)
            idx += 1
            continue

        if flag in BOOLEAN_FLAGS:
            if inline_value is not None:
                parsed.parse_error = f"选项 {flag} 不接受参数值"
            if flag in seen_flags and parsed.parse_error is None:
                parsed.parse_error = f"选项 {flag} 只允许指定一次 ({flag} may be supplied once)"
            seen_flags.add(flag)
            option_tokens_before_sep.append(token)
            if flag == "--summary":
                parsed.summary = True
            elif flag == "--diagnostics":
                parsed.diagnostics = True
            elif flag == "--dry-run":
                parsed.dry_run = True
            elif flag == "--initial-publish":
                exclusive_actions.append("initial-publish")
                parsed.mode = "initial"
                parsed.action = "initial-publish"
            elif flag == "--initial-commit-only":
                exclusive_actions.append("initial-commit-only")
                parsed.mode = "initial-commit-only"
                parsed.action = "initial-commit-only"
            elif flag == "--initial-branch-publish":
                exclusive_actions.append("initial-branch-publish")
                parsed.mode = "initial_branch"
                parsed.action = "initial-branch-publish"
            elif flag in {"--repo-plan", "--repo-ensure"}:
                exclusive_actions.append(flag.lstrip("-"))
            elif flag == "--ci-required":
                parsed.ci_required = True
            elif flag == "--agent-trace":
                parsed.agent_trace = True
            idx += 1
            continue

        if flag in VALUE_FLAGS:
            canon_flag = "--message" if flag == "-m" else flag
            if canon_flag not in repeatable_flags:
                if canon_flag in seen_flags and parsed.parse_error is None:
                    parsed.parse_error = f"选项 {flag} 只允许指定一次 ({flag} may be supplied once)"
                seen_flags.add(canon_flag)
            if inline_value is not None:
                value = inline_value
                step = 1
                option_tokens_before_sep.append(token)
            else:
                if idx + 1 >= total:
                    parsed.parse_error = f"选项 {flag} 缺少参数值"
                    break
                value = argv[idx + 1]
                step = 2
                option_tokens_before_sep.extend([token, value])

            if flag == "--mode":
                explicit_mode_flag = True
                parsed.mode = value
                parsed.action = value
            elif flag == "--repo":
                parsed.repo = value
            elif flag in {"--message", "-m"}:
                parsed.message = value
            elif flag == "--remote":
                parsed.remote = value
            elif flag == "--remote-branch":
                parsed.remote_branch = value
            elif flag == "--source-remote":
                parsed.source_remote = value
            elif flag == "--expected-target-oid":
                parsed.expected_target_oid = value
            elif flag == "--snapshot":
                parsed.snapshot = value
            elif flag == "--allow-test-fixture":
                parsed.allow_test_fixtures.append(value)
            elif flag == "--fixture-exceptions":
                parsed.fixture_exceptions = value
            elif flag == "--allow-large-binary":
                parsed.allow_large_binary = value
            elif flag == "--reviewed-sensitive-source":
                parsed.reviewed_sensitive_source = value
            elif flag == "--merge-parent":
                parsed.merge_parent = value
            elif flag == "--publish-existing-branch":
                exclusive_actions.append("publish-existing-branch")
                parsed.mode = "publish_existing_branch"
                parsed.action = "publish-existing-branch"
                parsed.head_oid = value
            elif flag == "--publish-existing-history":
                exclusive_actions.append("publish-existing-history")
                parsed.mode = "publish_existing_history"
                parsed.action = "publish-existing-history"
                parsed.head_oid = value
            elif flag == "--resume-existing-history-publish":
                exclusive_actions.append("resume-existing-history-publish")
                parsed.mode = "resume"
                parsed.action = "resume-existing-history-publish"
                parsed.head_oid = value
            elif flag == "--resume-publish":
                exclusive_actions.append("resume-publish")
                parsed.mode = "resume"
                parsed.action = "resume-publish"
                parsed.head_oid = value
            elif flag == "--resume-initial-publish":
                exclusive_actions.append("resume-initial-publish")
                parsed.mode = "resume"
                parsed.action = "resume-initial-publish"
                parsed.head_oid = value
            elif flag == "--sync-published-branch":
                exclusive_actions.append("sync-published-branch")
                parsed.mode = "sync_published_branch"
                parsed.action = "sync-published-branch"
                parsed.head_oid = value
            elif flag == "--publish-integration-candidate":
                exclusive_actions.append("publish-integration-candidate")
            elif flag == "--retire-remote-branch":
                exclusive_actions.append("retire-remote-branch")
                parsed.mode = "retire_remote_branch"
                parsed.action = "retire-remote-branch"
                parsed.retire_branch = value
            elif flag == "--retire-local-branch":
                exclusive_actions.append("retire-local-branch")
                parsed.mode = "retire_local_branch"
                parsed.action = "retire-local-branch"
                parsed.retire_branch = value
            elif flag == "--expected-remote-oid":
                parsed.expected_remote_oid = value
            elif flag == "--expected-local-oid":
                parsed.expected_local_oid = value
            elif flag == "--expected-integrated-oid":
                parsed.expected_integrated_oid = value
            elif flag == "--integrated-into":
                parsed.integrated_into = value
            elif flag == "--retirement-plan-id":
                parsed.retirement_plan_id = value
            elif flag == "--ci-status":
                parsed.ci_status = value
            elif flag == "--ci-commit-oid":
                parsed.ci_commit_oid = value
            elif flag == "--ci-verification-source":
                parsed.ci_verification_source = value
                parsed.ci_verification_source_seen = True
            elif flag == "--ci-verified-at":
                parsed.ci_verified_at = value
            elif flag == "--repository-id":
                parsed.repository_id = value
            elif flag == "--allocation-id":
                parsed.allocation_id = value
            elif flag == "--task-key":
                parsed.task_key = value
            elif flag == "--authority-key":
                parsed.authority_key = value
            elif flag == "--worktree-path":
                parsed.worktree_path = value
            elif flag == "--role":
                parsed.role = value
            elif flag == "--trailer-agent":
                parsed.trailer_agent = value
            elif flag == "--trace-ref":
                parsed.trace_ref = value
            elif flag == "--scanner":
                for item in value.split(","):
                    cleaned = item.strip()
                    if cleaned:
                        parsed.scanners.append(cleaned)
            elif flag == "--sarif-cmd":
                parsed.sarif_cmd = value
            elif flag == "--opa-policy":
                parsed.opa_policy = value
            elif flag == "--opa-query":
                parsed.opa_query = value
            idx += step
            continue

        if token.startswith("-"):
            parsed.unknown_flags.append(token)
            if parsed.parse_error is None:
                parsed.parse_error = f"不支持的选项: {token}"
        else:
            parsed.unknown_flags.append(token)
            stray_positionals.append(token)
            if parsed.parse_error is None:
                parsed.parse_error = f"位置参数必须放在 `--` 之后: {token}"
                stray_error_only = True
        idx += 1

    if parsed.dispatch in {"version", "help"}:
        if parsed.summary:
            parsed.dispatch = "core"
            _set_cli_error(
                parsed,
                "--version/--help 不能与 --summary 混用",
                issue="metadata_with_summary",
                suggested_argv=_strip_flags(argv, {"--summary"}),
                note="Remove --summary when requesting --version or --help.",
            )
        return parsed

    if parsed.diagnostics and not parsed.summary and parsed.parse_error is None:
        _set_cli_error(
            parsed,
            "--diagnostics requires --summary",
            issue="diagnostics_requires_summary",
            suggested_argv=["--summary", *argv],
            note="Add --summary alongside --diagnostics.",
        )
        return parsed

    if "repo-plan" in exclusive_actions or "repo-ensure" in exclusive_actions:
        if len(exclusive_actions) > 1 or explicit_mode_flag:
            parsed.parse_error = "--repo-plan/--repo-ensure 不能与其他动作选项混用"
        else:
            parsed.dispatch = "bootstrap"
        return parsed

    if "publish-integration-candidate" in exclusive_actions:
        if len(exclusive_actions) > 1 or explicit_mode_flag:
            parsed.parse_error = "--publish-integration-candidate 不能与其他动作选项混用"
        else:
            parsed.dispatch = "integration_publish"
        return parsed

    if stray_error_only and stray_positionals:
        path_taking_actions = {
            "normal",
            "verify-only",
            "commit-only",
            "initial-publish",
            "initial-commit-only",
            "initial-branch-publish",
        }
        first_tok = stray_positionals[0]
        if parsed.action in path_taking_actions:
            suggested = [*option_tokens_before_sep, "--", *stray_positionals, *parsed.paths]
            _set_cli_error(
                parsed,
                f"位置参数必须放在 `--` 之后: {first_tok}（修正: {_format_command_hint(suggested)}）",
                issue="missing_double_dash_before_paths",
                suggested_argv=suggested,
                note="Place `--` before explicit repository-relative file paths.",
            )
        else:
            suggested = list(option_tokens_before_sep)
            _set_cli_error(
                parsed,
                f"位置参数必须放在 `--` 之后（且 --{parsed.action} 不接受文件列表）: {first_tok}（修正: {_format_command_hint(suggested)}）",
                issue="paths_disallowed_for_mode",
                suggested_argv=suggested,
                note=f"--{parsed.action} operates on existing commits/refs and does not take file paths.",
            )
        return parsed

    if parsed.parse_error:
        return parsed

    if len(exclusive_actions) > 1:
        if "resume-initial-publish" in exclusive_actions and "initial-publish" in exclusive_actions:
            _set_cli_error(
                parsed,
                "--resume-initial-publish 不接受 --initial-publish",
                issue="conflicting_actions",
                suggested_argv=_strip_flags(argv, {"--initial-publish"}),
                note="Use --resume-initial-publish alone to resume an existing root commit.",
            )
        else:
            parsed.parse_error = "互斥模式选项不能同时指定"
        return parsed

    if exclusive_actions and explicit_mode_flag:
        _set_cli_error(
            parsed,
            "--mode 不能与互斥模式选项同时指定",
            issue="mode_conflicts_with_action",
            suggested_argv=_strip_flags(argv, {"--mode"}, value_flags={"--mode"}),
            note="Remove --mode when invoking a dedicated action flag.",
        )
        return parsed

    if parsed.action not in {
        "normal",
        "verify-only",
        "commit-only",
        "initial-publish",
        "initial-commit-only",
        "initial-branch-publish",
        "publish-existing-branch",
        "publish-existing-history",
        "resume-publish",
        "resume-initial-publish",
        "resume-existing-history-publish",
        "sync-published-branch",
        "retire-remote-branch",
        "retire-local-branch",
    }:
        parsed.parse_error = f"--mode 仅支持 commit-only 或 verify-only: {parsed.mode}"
        return parsed

    if parsed.action == "initial-publish" and not parsed.remote:
        _set_cli_error(
            parsed,
            "--initial-publish 必须指定 --remote",
            issue="missing_remote",
            suggested_argv=[*option_tokens_before_sep, "--remote", "origin", *(["--", *parsed.paths] if parsed.paths else [])],
            note="Pass --remote <remote> (for example --remote origin) with --initial-publish.",
        )
        return parsed

    if parsed.action in {
        "initial-branch-publish",
        "publish-existing-branch",
        "publish-existing-history",
        "resume-initial-publish",
        "resume-existing-history-publish",
        "sync-published-branch",
        "retire-remote-branch",
        "retire-local-branch",
    } and not parsed.remote:
        _set_cli_error(
            parsed,
            f"--{parsed.action} 必须指定 --remote",
            issue="missing_remote",
            suggested_argv=[*option_tokens_before_sep, "--remote", "origin", *(["--", *parsed.paths] if parsed.paths else [])],
            note=f"Pass --remote <remote> with --{parsed.action}.",
        )
        return parsed

    if parsed.action in {"normal", "commit-only", "verify-only", "initial-commit-only"} and parsed.remote is not None:
        suggested = _strip_flags(argv, {"--remote"}, value_flags={"--remote"})
        _set_cli_error(
            parsed,
            "--remote 只允许与 --initial-publish 或 --initial-branch-publish 一起使用（当前模式请移除 --remote，或改用 --initial-branch-publish / --publish-existing-branch）",
            issue="remote_disallowed_for_mode",
            suggested_argv=suggested,
            note="Normal mode uses the branch's configured upstream; remove --remote or use --publish-existing-branch.",
        )
        return parsed

    if parsed.action in {"publish-existing-branch", "publish-existing-history", "resume-existing-history-publish"}:
        if not parsed.remote_branch:
            _set_cli_error(
                parsed,
                f"--{parsed.action} 必须指定 --remote-branch",
                issue="missing_remote_branch",
                suggested_argv=[*option_tokens_before_sep, "--remote-branch", "<branch>"],
                note=f"Pass --remote-branch <branch> with --{parsed.action}.",
            )
            return parsed
        if parsed.message is not None:
            _set_cli_error(
                parsed,
                f"--{parsed.action} 不接受 --message",
                issue="message_disallowed_for_mode",
                suggested_argv=_strip_flags(argv, {"--message", "-m"}, value_flags={"--message", "-m"}),
                note=f"--{parsed.action} publishes existing commits without creating a new commit.",
            )
            return parsed
        if parsed.dry_run:
            _set_cli_error(
                parsed,
                f"--{parsed.action} 不支持 --dry-run",
                issue="dry_run_unsupported_for_mode",
                suggested_argv=_strip_flags(argv, {"--dry-run"}),
                note=f"Remove --dry-run when running --{parsed.action}.",
            )
            return parsed
        if parsed.paths and not (parsed.action == "publish-existing-branch" and parsed.reviewed_sensitive_source is not None):
            _set_cli_error(
                parsed,
                f"--{parsed.action} 不接受文件列表或 -- 分隔符",
                issue="paths_disallowed_for_mode",
                suggested_argv=_strip_paths_after_separator(argv),
                note=f"Remove `-- <paths>` when publishing existing commits with --{parsed.action}.",
            )
            return parsed

    if parsed.action == "resume-publish":
        if parsed.remote is not None:
            _set_cli_error(
                parsed,
                "--resume-publish 不接受 --remote",
                issue="remote_disallowed_for_mode",
                suggested_argv=_strip_flags(argv, {"--remote"}, value_flags={"--remote"}),
                note="--resume-publish pushes to the branch's configured upstream.",
            )
            return parsed
        if parsed.message is not None:
            _set_cli_error(
                parsed,
                "--resume-publish 不接受 --message",
                issue="message_disallowed_for_mode",
                suggested_argv=_strip_flags(argv, {"--message", "-m"}, value_flags={"--message", "-m"}),
                note="--resume-publish publishes existing commits without creating a new commit.",
            )
            return parsed
        if parsed.reviewed_sensitive_source is not None:
            if not parsed.paths:
                parsed.parse_error = "reviewed resume publication 要求 exact review scope 文件列表"
                return parsed
        elif parsed.paths:
            _set_cli_error(
                parsed,
                "--resume-publish 不接受文件列表",
                issue="paths_disallowed_for_mode",
                suggested_argv=_strip_paths_after_separator(argv),
                note="Remove `-- <paths>` when resuming publication of existing commits.",
            )
            return parsed
        if parsed.dry_run:
            _set_cli_error(
                parsed,
                "--resume-publish 不支持 --dry-run",
                issue="dry_run_unsupported_for_mode",
                suggested_argv=_strip_flags(argv, {"--dry-run"}),
                note="Remove --dry-run when running --resume-publish.",
            )
            return parsed

    if parsed.action == "resume-initial-publish":
        if parsed.message is not None:
            _set_cli_error(
                parsed,
                "--resume-initial-publish 不接受 --message",
                issue="message_disallowed_for_mode",
                suggested_argv=_strip_flags(argv, {"--message", "-m"}, value_flags={"--message", "-m"}),
                note="--resume-initial-publish publishes the existing root commit.",
            )
            return parsed
        if parsed.paths:
            _set_cli_error(
                parsed,
                "--resume-initial-publish 不接受文件列表",
                issue="paths_disallowed_for_mode",
                suggested_argv=_strip_paths_after_separator(argv),
                note="Remove `-- <paths>` when resuming initial root commit publication.",
            )
            return parsed
        if parsed.dry_run:
            _set_cli_error(
                parsed,
                "--resume-initial-publish 不支持 --dry-run",
                issue="dry_run_unsupported_for_mode",
                suggested_argv=_strip_flags(argv, {"--dry-run"}),
                note="Remove --dry-run when running --resume-initial-publish.",
            )
            return parsed
        if len(parsed.allow_test_fixtures) != len(set(parsed.allow_test_fixtures)):
            parsed.parse_error = "duplicate root resume test fixture path"
            return parsed

    if parsed.action in {"retire-remote-branch", "retire-local-branch"}:
        if parsed.ci_required and parsed.ci_status in (None, "", "NOT_REQUIRED"):
            _set_cli_error(
                parsed,
                "--ci-required 必须指定 --ci-status（示例: --ci-required --ci-status SUCCESS --ci-commit-oid <oid> --ci-verification-source tool_authenticated）",
                issue="missing_ci_status",
                suggested_argv=[*option_tokens_before_sep, "--ci-status", "SUCCESS", "--ci-commit-oid", "<integrated-oid>", "--ci-verification-source", "tool_authenticated"],
                note="Provide --ci-status SUCCESS, --ci-commit-oid, and --ci-verification-source alongside --ci-required.",
            )
            return parsed
        has_active_ci_evidence = (
            parsed.ci_status not in (None, "NOT_REQUIRED")
            or parsed.ci_verification_source not in (None, "not_required")
            or bool(parsed.ci_commit_oid)
            or bool(parsed.ci_verified_at)
        )
        if not parsed.ci_required and has_active_ci_evidence:
            ci_flags = {"--ci-status", "--ci-commit-oid", "--ci-verification-source", "--ci-verified-at"}
            suggested = (
                _strip_flags(argv, ci_flags, value_flags=ci_flags)
                if parsed.action == "retire-local-branch"
                else [*option_tokens_before_sep, "--ci-required"]
            )
            _set_cli_error(
                parsed,
                "未启用 --ci-required 时 CI evidence 必须为 not_required（若需校验 CI 请补加 --ci-required，否则移除 --ci-* 参数）",
                issue="ci_evidence_without_ci_required",
                suggested_argv=suggested,
                note="Add --ci-required to enforce CI evidence, or remove --ci-* flags when CI is not required.",
            )
            return parsed

    if parsed.snapshot is not None:
        if parsed.action != "initial-publish":
            suggested = _strip_flags(argv, {"--snapshot"}, value_flags={"--snapshot"})
            _set_cli_error(
                parsed,
                f"--snapshot 只允许与 --initial-publish 一起使用（当前模式 {parsed.action} 请移除 --snapshot；如需在 commit trailer 记录引用请改用 --trace-ref）",
                issue="snapshot_disallowed_for_mode",
                suggested_argv=suggested,
                note="Remove --snapshot (valid only with --initial-publish on an unborn repo); use --trace-ref to record a provenance trailer.",
            )
            return parsed
        if len(parsed.snapshot) != 64 or not all(ch in "0123456789abcdef" for ch in parsed.snapshot):
            parsed.parse_error = "--snapshot 要求 64 位小写十六进制 ID"
            return parsed

    if parsed.action == "sync-published-branch" and (
        parsed.message is not None
        or parsed.allow_test_fixtures
        or parsed.paths
        or parsed.merge_parent
        or parsed.snapshot
        or parsed.reviewed_sensitive_source
    ):
        suggested = _strip_paths_after_separator(
            _strip_flags(
                argv,
                {"--message", "-m", "--allow-test-fixture", "--merge-parent", "--snapshot", "--reviewed-sensitive-source"},
                value_flags={"--message", "-m", "--allow-test-fixture", "--merge-parent", "--snapshot", "--reviewed-sensitive-source"},
            )
        )
        _set_cli_error(
            parsed,
            "--sync-published-branch 不接受提交内容、fixture override 或路径参数",
            issue="disallowed_flags_for_sync",
            suggested_argv=suggested,
            note="Remove commit/path/review flags from --sync-published-branch.",
        )
        return parsed

    if parsed.action in {"retire-remote-branch", "retire-local-branch"} and (
        parsed.message is not None
        or parsed.allow_test_fixtures
        or parsed.fixture_exceptions
        or parsed.allow_large_binary
        or parsed.reviewed_sensitive_source
        or parsed.merge_parent
        or parsed.paths
        or parsed.snapshot
    ):
        disallowed = {
            "--message",
            "-m",
            "--allow-test-fixture",
            "--fixture-exceptions",
            "--allow-large-binary",
            "--reviewed-sensitive-source",
            "--merge-parent",
            "--snapshot",
        }
        suggested = _strip_paths_after_separator(_strip_flags(argv, disallowed, value_flags=disallowed))
        _set_cli_error(
            parsed,
            f"--{parsed.action} 不接受提交内容、exception、review 或路径参数",
            issue="disallowed_flags_for_retirement",
            suggested_argv=suggested,
            note=f"Remove commit/path/exception flags from --{parsed.action}.",
        )
        return parsed

    if (
        parsed.action in {"normal", "commit-only", "initial-publish", "initial-commit-only", "initial-branch-publish"}
        and not parsed.dry_run
        and parsed.message is None
    ):
        # Detect common mistake: `--` placed before `-m` / `--message`.
        misplaced_idx = next(
            (i for i, tok in enumerate(parsed.paths) if tok in {"-m", "--message"} or tok.startswith("--message=")),
            None,
        )
        if misplaced_idx is not None:
            tok = parsed.paths[misplaced_idx]
            remaining_paths = list(parsed.paths)
            moved_flags: list[str]
            if tok.startswith("--message="):
                moved_flags = [remaining_paths.pop(misplaced_idx)]
            elif misplaced_idx + 1 < len(remaining_paths):
                moved_flags = [remaining_paths.pop(misplaced_idx), remaining_paths.pop(misplaced_idx)]
            else:
                moved_flags = [remaining_paths.pop(misplaced_idx), "<message>"]
            suggested = [*option_tokens_before_sep, *moved_flags, "--", *remaining_paths]
            _set_cli_error(
                parsed,
                f"缺少 --message（检测到 {tok} 位于 `--` 之后；请把 --message/-m 移到 `--` 之前）",
                issue="message_flag_after_double_dash",
                suggested_argv=suggested,
                note="Move --message/-m before the `--` path separator.",
            )
            return parsed
        suggested = [*option_tokens_before_sep, "--message", "<commit-message>", "--", *parsed.paths]
        _set_cli_error(
            parsed,
            "缺少 --message/-m 提交说明",
            issue="missing_message",
            suggested_argv=suggested,
            note="Supply --message <text> (or -m <text>) before `--`.",
        )
        return parsed

    return parsed
