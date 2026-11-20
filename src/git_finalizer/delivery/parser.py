"""State-aware single-pass CLI parser for Git Finalizer.

Prevents flag hijacking via option values (e.g. `--message --repo-plan`,
`--message --summary`, `--run-id --summary`) and prevents `--version`/`--help`
combined with `--summary` from emitting a fabricated success summary.
"""

from __future__ import annotations

import dataclasses

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
            idx += 1
            continue
        if flag in {"--help", "-h"}:
            if inline_value is not None:
                parsed.parse_error = f"选项 {flag} 不接受参数值"
            parsed.dispatch = "help"
            idx += 1
            continue

        if flag in BOOLEAN_FLAGS:
            if inline_value is not None:
                parsed.parse_error = f"选项 {flag} 不接受参数值"
            if flag in seen_flags and parsed.parse_error is None:
                parsed.parse_error = f"选项 {flag} 只允许指定一次 ({flag} may be supplied once)"
            seen_flags.add(flag)
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
            else:
                if idx + 1 >= total:
                    parsed.parse_error = f"选项 {flag} 缺少参数值"
                    break
                value = argv[idx + 1]
                step = 2

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
            if parsed.parse_error is None:
                parsed.parse_error = f"位置参数必须放在 `--` 之后: {token}"
        idx += 1

    if parsed.dispatch in {"version", "help"}:
        if parsed.summary:
            parsed.dispatch = "core"
            parsed.parse_error = "--version/--help 不能与 --summary 混用"
        return parsed

    if parsed.diagnostics and not parsed.summary and parsed.parse_error is None:
        parsed.parse_error = "--diagnostics requires --summary"
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

    if parsed.parse_error:
        return parsed

    if len(exclusive_actions) > 1:
        if "resume-initial-publish" in exclusive_actions and "initial-publish" in exclusive_actions:
            parsed.parse_error = "--resume-initial-publish 不接受 --initial-publish"
        else:
            parsed.parse_error = "互斥模式选项不能同时指定"
        return parsed

    if exclusive_actions and explicit_mode_flag:
        parsed.parse_error = "--mode 不能与互斥模式选项同时指定"
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
        parsed.parse_error = "--initial-publish 必须指定 --remote"
        return parsed

    if parsed.action in {"initial-branch-publish", "publish-existing-branch", "publish-existing-history", "resume-initial-publish", "resume-existing-history-publish", "sync-published-branch", "retire-remote-branch", "retire-local-branch"} and not parsed.remote:
        parsed.parse_error = f"--{parsed.action} 必须指定 --remote"
        return parsed

    if parsed.action in {"normal", "commit-only", "verify-only", "initial-commit-only"} and parsed.remote is not None:
        parsed.parse_error = "--remote 只允许与 --initial-publish 或 --initial-branch-publish 一起使用"
        return parsed

    if parsed.action == "resume-publish":
        if parsed.remote is not None:
            parsed.parse_error = "--resume-publish 不接受 --remote"
            return parsed
        if parsed.message is not None:
            parsed.parse_error = "--resume-publish 不接受 --message"
            return parsed
        if parsed.reviewed_sensitive_source is not None:
            if not parsed.paths:
                parsed.parse_error = "reviewed resume publication 要求 exact review scope 文件列表"
                return parsed
        elif parsed.paths:
            parsed.parse_error = "--resume-publish 不接受文件列表"
            return parsed
        if parsed.dry_run:
            parsed.parse_error = "--resume-publish 不支持 --dry-run"
            return parsed

    if parsed.action == "resume-initial-publish":
        if parsed.message is not None:
            parsed.parse_error = "--resume-initial-publish 不接受 --message"
            return parsed
        if parsed.paths:
            parsed.parse_error = "--resume-initial-publish 不接受文件列表"
            return parsed
        if parsed.dry_run:
            parsed.parse_error = "--resume-initial-publish 不支持 --dry-run"
            return parsed
        if len(parsed.allow_test_fixtures) != len(set(parsed.allow_test_fixtures)):
            parsed.parse_error = "duplicate root resume test fixture path"
            return parsed

    if parsed.action in {"retire-remote-branch", "retire-local-branch"} and parsed.ci_required and parsed.ci_status in (None, "", "NOT_REQUIRED"):
        parsed.parse_error = "--ci-required 必须指定 --ci-status"
        return parsed

    if parsed.snapshot is not None:
        if parsed.action != "initial-publish":
            parsed.parse_error = "--snapshot 只允许与 --initial-publish 一起使用"
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
        parsed.parse_error = "--sync-published-branch 不接受提交内容、fixture override 或路径参数"
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
        parsed.parse_error = f"--{parsed.action} 不接受提交内容、exception、review 或路径参数"
        return parsed

    return parsed
