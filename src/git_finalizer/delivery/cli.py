"""Primary CLI entry point for `git-finalize`."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys

from git_finalizer import __version__
from git_finalizer.delivery import engine, integration_publish, repo_bootstrap
from git_finalizer.delivery.parser import ParsedCLI, parse_cli, usage_text
from git_finalizer.delivery.runtime import (
    SummaryState,
    execution_lock,
    render_fallback_error_summary,
)
from git_finalizer.errors import ErrorCode, FinalizerError


def _emit_summary_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _apply_error_to_summary(summary: SummaryState, exc: FinalizerError) -> SummaryState:
    summary.status = exc.status
    summary.final_phase = exc.final_phase
    summary.exit_code = exc.exit_code
    summary.error_code = exc.code
    summary.reason = exc.message
    summary.next_action = exc.next_action
    if exc.commit_created:
        summary.commit_created = True
    if exc.commit_sha:
        summary.commit_sha = exc.commit_sha
    if exc.push_executed:
        summary.push_executed = True
    if exc.push_result != "not_run":
        summary.push_result = exc.push_result
    for warning in exc.warnings:
        if warning not in summary.warnings:
            summary.warnings.append(warning)
    if exc.resume:
        summary.resume_required = True
        summary.resume_oid = str(exc.resume.get("head_oid") or "") or None
        summary.resume_remote = str(exc.resume.get("remote") or "") or None
    if exc.scan_diagnostic:
        summary.scan_diagnostic = dict(exc.scan_diagnostic)
    if exc.diagnostics:
        summary.extra_diagnostics = dict(exc.diagnostics)
    if exc.reviewed_sensitive_sources:
        summary.reviewed_sensitive_sources = [dict(x) for x in exc.reviewed_sensitive_sources]
    return summary


def _emit_human_diagnostics(diagnostics: dict[str, object] | None) -> None:
    if not isinstance(diagnostics, dict):
        return
    cli_hint = diagnostics.get("cli_hint")
    if isinstance(cli_hint, dict) and cli_hint.get("suggested_command"):
        print(f"建议命令: {cli_hint['suggested_command']}", file=sys.stderr)
    whitespace = diagnostics.get("whitespace")
    if isinstance(whitespace, dict):
        violations = whitespace.get("violations")
        if isinstance(violations, list):
            for item in violations:
                if isinstance(item, dict) and item.get("path"):
                    loc = f"{item['path']}:{item['line']}" if item.get("line") is not None else str(item["path"])
                    commit_tag = f" ({item['commit']})" if item.get("commit") else ""
                    print(f"  - {loc}: {item.get('reason', 'whitespace violation')}{commit_tag}", file=sys.stderr)
    retirement = diagnostics.get("retirement")
    if isinstance(retirement, dict) and retirement.get("replan_command"):
        print(f"恢复计划命令: {retirement['replan_command']}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    entrypoint = Path(sys.argv[0]) if sys.argv and sys.argv[0] else Path(__file__).resolve()
    want_summary = "--summary" in raw_argv
    cli: ParsedCLI | None = None
    summary: SummaryState | None = None

    try:
        with execution_lock(entrypoint):
            cli = parse_cli(raw_argv)
            if cli.dispatch == "version":
                print(f"git-finalize {__version__}")
                return 0
            if cli.dispatch == "help":
                print(usage_text())
                return 0

            summary = engine.create_summary(cli, entrypoint=entrypoint)
            if cli.parse_error:
                err = FinalizerError(
                    ErrorCode.CLI_INVALID_ARGS,
                    cli.parse_error,
                    final_phase="cli",
                    diagnostics=cli.parse_diagnostics,
                )
                if cli.summary:
                    _emit_summary_json(_apply_error_to_summary(summary, err).to_dict())
                else:
                    print(f"ERROR [{err.code}]: {err.message}", file=sys.stderr)
                    _emit_human_diagnostics(err.diagnostics)
                return err.exit_code

            if cli.dispatch == "bootstrap":
                return repo_bootstrap.main(raw_argv)
            if cli.dispatch == "integration_publish":
                return integration_publish.main(raw_argv)

            if cli.summary:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code, summary = engine.run_delivery(cli, entrypoint=entrypoint, summary=summary)
                _emit_summary_json(summary.to_dict())
                return code

            code, _summary = engine.run_delivery(cli, entrypoint=entrypoint, summary=summary)
            return code
    except FinalizerError as exc:
        if summary is not None and cli is not None and cli.summary:
            _emit_summary_json(_apply_error_to_summary(summary, exc).to_dict())
        elif want_summary or (cli is not None and cli.summary):
            print(
                render_fallback_error_summary(reason=exc.message, error_code=exc.code),
                flush=True,
            )
        else:
            print(f"ERROR [{exc.code}]: {exc.message}", file=sys.stderr)
            _emit_human_diagnostics(exc.diagnostics)
            if exc.commit_created and exc.commit_sha and cli is not None:
                if cli.action == "initial-branch-publish":
                    from git_finalizer.git_io import run_git

                    up_res = run_git(Path(cli.repo), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
                    obs_up = up_res.stdout.decode("utf-8", errors="replace").strip() if up_res.returncode == 0 else "(none)"
                    print(
                        f"本地提交 {exc.commit_sha} 已保留；未执行 pull、rebase、merge、reset 或 force push。",
                        file=sys.stderr,
                    )
                    print(f"显式目标: {cli.remote}/{cli.remote_branch}", file=sys.stderr)
                    print(f"当前 upstream: {obs_up}", file=sys.stderr)
                elif exc.resume and exc.next_action == "resume_initial_publish":
                    print(
                        f"本地 root commit {exc.resume.get('head_oid')} 已保留；不得重跑 --initial-publish。",
                        file=sys.stderr,
                    )
                    print(
                        f"恢复入口: git-finalize --resume-initial-publish {exc.resume.get('head_oid')} "
                        f"--remote {exc.resume.get('remote')} --repo {cli.repo}",
                        file=sys.stderr,
                    )
                else:
                    print(f"本地提交 {exc.commit_sha} 已保留", file=sys.stderr)
            elif exc.resume and exc.next_action == "resume_existing_history_publish" and cli is not None:
                print(
                    f"恢复入口: git-finalize --resume-existing-history-publish {exc.resume.get('head_oid')} "
                    f"--remote {exc.resume.get('remote')} --remote-branch {cli.remote_branch} --repo {cli.repo}",
                    file=sys.stderr,
                )
            elif exc.next_action == "retry_same_resume" and cli is not None:
                if cli.action == "resume-initial-publish":
                    print(
                        f"可安全再次运行同一恢复命令: git-finalize --resume-initial-publish {exc.resume.get('head_oid') if exc.resume else cli.head_oid} "
                        f"--remote {exc.resume.get('remote') if exc.resume else cli.remote} --repo {cli.repo}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"可安全再次运行同一恢复命令: git-finalize --resume-publish {exc.resume.get('head_oid') if exc.resume else cli.head_oid} --repo {cli.repo}",
                        file=sys.stderr,
                    )
        return exc.exit_code
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    except Exception as exc:
        if want_summary or (cli is not None and cli.summary):
            print(
                render_fallback_error_summary(
                    reason=f"internal error: {exc}",
                    error_code=ErrorCode.INTERNAL_ERROR,
                ),
                flush=True,
            )
        else:
            print(f"ERROR [{ErrorCode.INTERNAL_ERROR}]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
