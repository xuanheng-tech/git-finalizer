"""Core delivery orchestration engine replacing the legacy Bash state machine."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
from typing import Any

from git_finalizer.delivery import (
    policy as policy_mod,
)
from git_finalizer.delivery import (
    repo_state,
    retirement_plan,
    snapshot_verify,
    source_review,
    validation,
)
from git_finalizer.delivery import (
    trailers as trailer_mod,
)
from git_finalizer.delivery.parser import ParsedCLI
from git_finalizer.delivery.runtime import SummaryState
from git_finalizer.errors import ErrorCode, FinalizerError
from git_finalizer.git_io import (
    isolated_staging_env,
    parse_nul_records,
    remote_git_timeout,
    run_git,
    validate_branch_name,
    validate_oid,
    validate_remote_name,
)

FULL_DEFAULT_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _emit(line: str, *, quiet: bool) -> None:
    if not quiet:
        print(line, flush=True)


def _run_remote_push(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Execute remote git push, surfacing timeouts as non-zero returncode so callers record uncertainty."""
    try:
        return run_git(repo, *args, timeout=remote_git_timeout())
    except FinalizerError as exc:
        if exc.code == ErrorCode.REMOTE_UNAVAILABLE:
            return subprocess.CompletedProcess(["git", *args], 124, b"", exc.message.encode("utf-8"))
        raise


def create_summary(cli: ParsedCLI, *, entrypoint: Path | None = None) -> SummaryState:
    return SummaryState(
        mode=cli.mode,
        status="failed",
        final_phase="preflight",
        exit_code=1,
        repository=str(Path(cli.repo).resolve()) if cli.repo else "",
        repository_id=cli.repository_id,
        allocation_id=cli.allocation_id,
        task_key=cli.task_key,
        authority_key=cli.authority_key,
        worktree_path=cli.worktree_path,
        role=cli.role,
        requested_path_count=len(cli.paths),
        dry_run=cli.dry_run,
        diagnostics_requested=cli.diagnostics,
        entrypoint=entrypoint,
    )


def _normalize_heads_ref(raw: str, flag: str) -> tuple[str, str]:
    value = raw.strip()
    if value.startswith("refs/heads/"):
        branch = value.removeprefix("refs/heads/")
        ref = value
    elif value.startswith("refs/"):
        raise FinalizerError(ErrorCode.CLI_INVALID_ARGS, f"{flag} 必须是 branch 名或 refs/heads/ ref: {raw}", final_phase="cli")
    else:
        branch = value
        ref = f"refs/heads/{branch}"
    validate_branch_name(branch)
    return branch, ref


def _normalize_integration_ref(raw: str, remote: str) -> tuple[str, str]:
    value = raw.strip()
    if value.startswith("refs/heads/"):
        branch = value.removeprefix("refs/heads/")
        validate_branch_name(branch)
        return branch, value
    if value.startswith("refs/remotes/"):
        rest = value.removeprefix("refs/remotes/")
        rem, sep, branch = rest.partition("/")
        if not sep or rem != remote:
            raise FinalizerError(
                ErrorCode.CLI_INVALID_ARGS,
                f"--integrated-into remote prefix 必须匹配 --remote {remote}",
                final_phase="cli",
            )
        validate_branch_name(branch)
        return branch, f"refs/heads/{branch}"
    if value.startswith("refs/"):
        raise FinalizerError(ErrorCode.CLI_INVALID_ARGS, f"--integrated-into 非法 ref: {raw}", final_phase="cli")
    if value.startswith(f"{remote}/"):
        branch = value.removeprefix(f"{remote}/")
        validate_branch_name(branch)
        return branch, f"refs/heads/{branch}"
    if "/" in value:
        # Either branch name with `/` (e.g. release/v1) or conflicting foreign remote prefix.
        validate_branch_name(value)
        return value, f"refs/heads/{value}"
    validate_branch_name(value)
    return value, f"refs/heads/{value}"


def _show_ref_hash(repo: Path, ref: str, *, label: str) -> str | None:
    """Fail-closed wrapper around `git show-ref --verify`: `--quiet` returns 1 cleanly on missing/unborn refs."""
    chk = run_git(repo, "show-ref", "--verify", "--quiet", "--", ref)
    if chk.returncode == 1:
        return None
    if chk.returncode != 0:
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            f"无法验证 {label} ({ref}) 引用状态 (exit {chk.returncode})",
            status="failed",
        )
    res = run_git(repo, "show-ref", "--verify", "--hash", "--", ref)
    if res.returncode == 0:
        oid = res.stdout.decode("ascii", errors="strict").strip()
        if not oid:
            raise FinalizerError(ErrorCode.REPO_INVALID, f"{label} 返回空 OID", status="failed")
        return oid
    raise FinalizerError(
        ErrorCode.REPO_INVALID,
        f"无法验证 {label} ({ref}) 引用状态 (exit {res.returncode})",
        status="failed",
    )


def _git_config_get_all(repo: Path, key: str) -> list[str]:
    """Fail-closed `git config --null --get-all` wrapper (exit 1 -> empty, >=2 -> error)."""
    res = run_git(repo, "config", "--null", "--get-all", key)
    if res.returncode == 1:
        return []
    if res.returncode != 0:
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            f"读取 Git 配置 {key} 失败 (exit {res.returncode})",
            status="failed",
        )
    return parse_nul_records(res.stdout)


def _classify_branch_upstream(
    repo: Path, branch: str, selected_remote: str, expected_merge_ref: str
) -> tuple[str, str | None, str | None]:
    """Return `(state, oid, error)` in {'none', 'exact_resolved', 'exact_unresolved', 'conflicting'}."""
    remote_vals = _git_config_get_all(repo, f"branch.{branch}.remote")
    merge_vals = _git_config_get_all(repo, f"branch.{branch}.merge")
    if not remote_vals and not merge_vals:
        return "none", None, None
    if (
        len(remote_vals) != 1
        or len(merge_vals) != 1
        or remote_vals[0] != selected_remote
        or merge_vals[0] != expected_merge_ref
    ):
        return "conflicting", None, "upstream 配置与 selected remote/branch 冲突或含糊"
    rev = run_git(repo, "rev-parse", "--verify", "@{upstream}^{commit}")
    if rev.returncode == 0:
        return "exact_resolved", rev.stdout.decode("ascii", errors="strict").strip(), None
    return "exact_unresolved", None, None


def _ls_remote_refs(repo: Path, remote: str, *ref_patterns: str) -> tuple[dict[str, str], str | None]:
    """Query live remote refs and optional `HEAD` symbolic ref cleanly."""
    validate_remote_name(remote)
    args = ["ls-remote", "--quiet", "--symref" if not ref_patterns else "--refs", "--", remote, *ref_patterns]
    if not ref_patterns:
        args = ["ls-remote", "--quiet", "--refs", "--", remote]
    completed = run_git(repo, *args, timeout=remote_git_timeout())
    if completed.returncode != 0:
        raise FinalizerError(
            ErrorCode.REMOTE_UNAVAILABLE,
            f"无法查询远端 {remote} refs (exit {completed.returncode})",
            final_phase="remote_preflight",
        )
    refs: dict[str, str] = {}
    for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise FinalizerError(
                ErrorCode.REMOTE_UNAVAILABLE,
                f"远端 {remote} ls-remote 输出格式异常",
                final_phase="remote_preflight",
            )
        oid, refname = parts
        if refname in refs:
            raise FinalizerError(
                ErrorCode.REMOTE_UNAVAILABLE,
                f"远端 {remote} 返回重复 ref: {refname}",
                final_phase="remote_preflight",
            )
        refs[refname] = oid
    return refs, None


def _query_remote_default_branch(repo: Path, remote: str) -> str:
    validate_remote_name(remote)
    res = run_git(repo, "ls-remote", "--symref", "--quiet", "--", remote, "HEAD", timeout=remote_git_timeout())
    if res.returncode != 0:
        raise FinalizerError(ErrorCode.REMOTE_UNAVAILABLE, f"无法查询 remote default branch (exit {res.returncode})")
    default_ref: str | None = None
    for line in res.stdout.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) == 2 and parts[0].startswith("ref: refs/heads/") and parts[1] == "HEAD":
            if default_ref is not None:
                raise FinalizerError(ErrorCode.REMOTE_UNAVAILABLE, "remote default branch 返回重复 symbolic ref")
            default_ref = parts[0].removeprefix("ref: ")
    if not default_ref:
        raise FinalizerError(ErrorCode.REMOTE_UNAVAILABLE, "无法安全确定 remote default branch；fail closed")
    return default_ref


def _fetch_remote(
    repo: Path,
    remote: str,
    *refspecs: str,
    prune: bool = False,
    no_write_fetch_head: bool = False,
    phase: str = "remote_preflight",
    phase_label: str = "",
) -> None:
    validate_remote_name(remote)
    args = ["fetch", "--no-tags", "--no-recurse-submodules"]
    if prune:
        args.append("--prune")
    if no_write_fetch_head:
        args.append("--no-write-fetch-head")
    args.extend(["--", remote, *refspecs])
    res = run_git(repo, *args, timeout=remote_git_timeout())
    if res.returncode != 0:
        err_code = ErrorCode.REMOTE_UNAVAILABLE if phase == "remote_preflight" else ErrorCode.POST_VERIFY_FAILED
        raise FinalizerError(
            err_code,
            f"{phase_label}git fetch 失败 ({remote}, exit {res.returncode})",
            final_phase=phase,
            status="failed" if phase in {"post_verify", "push"} else "blocked",
        )


def _snapshot_tags(repo: Path) -> str:
    res = run_git(repo, "for-each-ref", "--format=%(objectname)\t%(refname)", "refs/tags")
    if res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, "无法记录本地 refs/tags 基线", status="failed")
    return res.stdout.decode("utf-8", errors="strict")


def _snapshot_local_state(repo: Path) -> dict[str, Any]:
    sym = run_git(repo, "symbolic-ref", "--quiet", "HEAD")
    if sym.returncode != 0:
        raise FinalizerError(ErrorCode.DETACHED_HEAD, "要求 symbolic attached HEAD branch")
    head_res = run_git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    if head_res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, "无法解析 HEAD commit")
    idx_res = run_git(repo, "ls-files", "--stage")
    if idx_res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, "无法读取 index snapshot")
    st_res = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if st_res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, "无法读取 worktree snapshot")
    cnt_res = run_git(repo, "rev-list", "--count", "HEAD")
    if cnt_res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, "无法统计 commit count")
    return {
        "symbolic_head": sym.stdout.decode("utf-8", errors="strict").strip(),
        "head": head_res.stdout.decode("ascii", errors="strict").strip(),
        "index": idx_res.stdout.decode("utf-8", errors="replace"),
        "status": st_res.stdout.decode("utf-8", errors="replace"),
        "commit_count": cnt_res.stdout.decode("ascii", errors="strict").strip(),
        "tags": _snapshot_tags(repo),
        "other_heads": _snapshot_other_local_heads(repo, excluded_ref=None),
    }


def _snapshot_other_local_heads(repo: Path, excluded_ref: str | None) -> str:
    res = run_git(repo, "for-each-ref", "--format=%(objectname)\t%(refname)", "refs/heads")
    if res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, "无法枚举 refs/heads", status="failed")
    lines = [
        ln
        for ln in res.stdout.decode("utf-8", errors="strict").splitlines()
        if ln and (excluded_ref is None or ln.split("\t", 1)[1] != excluded_ref)
    ]
    return "\n".join(lines)


def _assert_local_state_unchanged(
    repo: Path,
    baseline: dict[str, Any],
    *,
    phase: str = "post_verify",
    failure_status: str = "failed",
    next_action: str = "inspect_failure",
    commit_created: bool = False,
    commit_sha: str | None = None,
    push_executed: bool = False,
    push_result: str = "not_run",
    context_label: str = "验证/发布",
) -> None:
    sym = run_git(repo, "symbolic-ref", "--quiet", "HEAD")
    head = run_git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    idx = run_git(repo, "ls-files", "--stage")
    st = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    cnt = run_git(repo, "rev-list", "--count", "HEAD")
    tags = _snapshot_tags(repo)
    checks = [
        (sym.returncode == 0 and sym.stdout.decode("utf-8", errors="replace").strip() == baseline["symbolic_head"], "symbolic HEAD 发生变化"),
        (head.returncode == 0 and head.stdout.decode("ascii", errors="replace").strip() == baseline["head"], "HEAD commit 发生变化"),
        (idx.returncode == 0 and idx.stdout.decode("utf-8", errors="replace") == baseline["index"], f"index 在 {context_label} 期间发生变化"),
        (st.returncode == 0 and st.stdout.decode("utf-8", errors="replace") == baseline["status"], f"worktree 在 {context_label} 期间发生变化"),
        (cnt.returncode == 0 and cnt.stdout.decode("ascii", errors="replace").strip() == baseline["commit_count"], "commit history 发生变化"),
        (tags == baseline["tags"], "refs/tags 在操作期间发生变化"),
    ]
    for ok, msg in checks:
        if not ok:
            raise FinalizerError(
                ErrorCode.POST_VERIFY_FAILED if phase == "post_verify" else ErrorCode.REPO_INVALID,
                msg,
                status=failure_status,
                final_phase=phase,
                next_action=next_action,
                commit_created=commit_created,
                commit_sha=commit_sha,
                push_executed=push_executed,
                push_result=push_result,
            )


def _require_clean_repo_state(repo: Path, info: repo_state.RepoInfo) -> None:
    diff_c = run_git(repo, "diff", "--cached", "--quiet")
    if diff_c.returncode != 0:
        raise FinalizerError(ErrorCode.INDEX_UNCLEAN, "该模式要求 index/staged 状态干净", final_phase="preflight")
    _, _, wt_state = repo_state.working_tree_dirty_status(repo)
    if wt_state != "clean":
        raise FinalizerError(ErrorCode.WORKING_TREE_DIRTY, "该模式要求 worktree 完全干净（包括 untracked 文件）", final_phase="preflight")
    if not info.branch:
        raise FinalizerError(ErrorCode.DETACHED_HEAD, "该模式要求 symbolic attached branch", final_phase="preflight")
    if info.is_unborn or not info.head_oid:
        raise FinalizerError(ErrorCode.UNBORN_REPO, "该模式要求已有可解析 HEAD commit", final_phase="preflight")


def _check_normal_remote_state(
    repo: Path,
    remote: str,
    target_branch: str,
    *,
    phase_label: str,
    phase_name: str = "remote_preflight",
    commit_created: bool = False,
    commit_sha: str | None = None,
) -> tuple[str, str, int, int, str]:
    """Fetch `remote`, query live `refs/heads/<target_branch>`, return `(local_oid, upstream_oid, ahead, behind, remote_oid)`."""
    _fetch_remote(repo, remote, phase=phase_name, phase_label=phase_label)
    target_ref = f"refs/heads/{target_branch}"
    refs, _ = _ls_remote_refs(repo, remote, target_ref)
    if target_ref not in refs:
        raise FinalizerError(
            ErrorCode.REMOTE_MISSING if phase_name == "remote_preflight" else ErrorCode.POST_VERIFY_FAILED,
            f"{phase_label}远端 branch 不存在: {target_ref}",
            status="failed" if commit_created else "blocked",
            final_phase=phase_name,
            next_action="inspect_failure_with_local_commit_retained" if commit_created else "resolve_blocker_and_retry",
            commit_created=commit_created,
            commit_sha=commit_sha,
        )
    remote_oid = refs[target_ref]
    validate_oid(remote_oid, "remote branch OID")
    up_res = run_git(repo, "rev-parse", "--verify", "@{upstream}^{commit}")
    if up_res.returncode != 0:
        raise FinalizerError(
            ErrorCode.UPSTREAM_CONFLICT,
            f"{phase_label}无法解析 @{{upstream}}",
            status="failed" if commit_created else "blocked",
            final_phase=phase_name,
            next_action="inspect_failure_with_local_commit_retained" if commit_created else "resolve_blocker_and_retry",
            commit_created=commit_created,
            commit_sha=commit_sha,
        )
    upstream_oid = up_res.stdout.decode("ascii", errors="strict").strip()
    if upstream_oid != remote_oid:
        raise FinalizerError(
            ErrorCode.REMOTE_DIVERGED,
            f"{phase_label}fetch 后 @{{upstream}} 与远端 branch OID 不一致",
            status="failed" if commit_created else "blocked",
            final_phase=phase_name,
            next_action="inspect_failure_with_local_commit_retained" if commit_created else "resolve_blocker_and_retry",
            commit_created=commit_created,
            commit_sha=commit_sha,
        )
    head_res = run_git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    if head_res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, f"{phase_label}无法解析 HEAD commit", status="failed", final_phase=phase_name)
    local_oid = head_res.stdout.decode("ascii", errors="strict").strip()
    cnt_res = run_git(repo, "rev-list", "--left-right", "--count", f"{local_oid}...{upstream_oid}")
    if cnt_res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, f"{phase_label}无法计算 ahead/behind", status="failed", final_phase=phase_name)
    ahead_s, behind_s = cnt_res.stdout.decode("ascii", errors="strict").strip().split()
    ahead, behind = int(ahead_s), int(behind_s)
    if behind > 0:
        msg = (
            f"{phase_label}本地已与 upstream 分叉（HEAD...upstream ahead/behind: {ahead}/{behind}）"
            if ahead > 0
            else f"{phase_label}本地落后 upstream（HEAD...upstream ahead/behind: {ahead}/{behind}）"
        )
        raise FinalizerError(
            ErrorCode.REMOTE_DIVERGED if ahead > 0 else ErrorCode.REMOTE_BEHIND,
            msg,
            status="failed" if commit_created else "blocked",
            final_phase=phase_name,
            next_action="inspect_failure_with_local_commit_retained" if commit_created else "resolve_blocker_and_retry",
            commit_created=commit_created,
            commit_sha=commit_sha,
        )
    return local_oid, upstream_oid, ahead, behind, remote_oid


def _check_initial_target(
    repo: Path, info: repo_state.RepoInfo, remote_arg: str | None, *, require_remote: bool
) -> tuple[str, str | None]:
    if not info.branch:
        raise FinalizerError(ErrorCode.DETACHED_HEAD, "initial root commit 要求 symbolic attached branch")
    if not info.is_unborn:
        raise FinalizerError(ErrorCode.REPO_INVALID, "initial root commit 只接受尚无 commit 的 unborn 仓库")
    any_commit = run_git(repo, "rev-list", "--all", "--max-count=1")
    if any_commit.returncode != 0 or any_commit.stdout.strip():
        raise FinalizerError(ErrorCode.REPO_INVALID, "initial root commit 不接受已有可解析 commit 的仓库")
    branch_ref = f"refs/heads/{info.branch}"
    if _show_ref_hash(repo, branch_ref, label="initial branch ref") is not None:
        raise FinalizerError(ErrorCode.REPO_INVALID, "initial root commit 只接受 branch ref 尚不存在的 unborn 仓库")
    ls = run_git(repo, "ls-files", "--stage")
    if ls.returncode != 0 or ls.stdout.strip():
        raise FinalizerError(ErrorCode.INDEX_UNCLEAN, "initial 模式不允许预先存在 index 或 staged 内容")
    if not require_remote:
        return branch_ref, None
    remote = validate_remote_name(remote_arg or "")
    repo_state.validate_remote_endpoints(repo, remote)
    up_state, _, up_err = _classify_branch_upstream(repo, info.branch, remote, branch_ref)
    if up_state == "exact_resolved":
        raise FinalizerError(ErrorCode.UPSTREAM_CONFLICT, "initial-publish 不接受可解析 upstream")
    if up_state == "conflicting":
        raise FinalizerError(ErrorCode.UPSTREAM_CONFLICT, up_err or "upstream 配置冲突")
    return branch_ref, remote


def _check_initial_worktree_scope(repo: Path, allowed_set: set[str]) -> None:
    res = run_git(repo, "--literal-pathspecs", "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, "无法读取 initial root worktree 状态")
    for entry in parse_nul_records(res.stdout):
        if len(entry) < 4:
            raise FinalizerError(ErrorCode.REPO_INVALID, "无法解析 initial root worktree 状态")
        rel = entry[3:]
        if rel not in allowed_set:
            raise FinalizerError(ErrorCode.SCOPE_INVALID, f"initial root 存在显式范围外改动: {rel}")


def _retained_staged_deletions(repo: Path, paths: list[str], *, is_unborn: bool) -> set[str]:
    if is_unborn:
        return set()
    retained: set[str] = set()
    for rel in paths:
        d = run_git(
            repo,
            "--literal-pathspecs",
            "diff",
            "--cached",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--quiet",
            "--diff-filter=D",
            "HEAD",
            "--",
            rel,
        )
        if d.returncode == 1:
            target = repo / rel
            if (not target.exists() and not target.is_symlink()) or (
                run_git(repo, "check-ignore", "--quiet", "--no-index", "--", rel).returncode == 0
            ):
                retained.add(rel)
    return retained


def _unpublished_commits(repo: Path, expected_oid: str, exclude_oids: list[str]) -> list[str]:
    args = ["rev-list", "--reverse", "--topo-order", expected_oid]
    unique_excludes = list(dict.fromkeys(oid for oid in exclude_oids if oid))
    for ex in unique_excludes:
        chk = run_git(repo, "cat-file", "-e", f"{ex}^{{commit}}")
        if chk.returncode != 0:
            raise FinalizerError(ErrorCode.REPO_INVALID, f"无法解析远端基准 commit: {ex}", final_phase="remote_preflight")
    if unique_excludes:
        args.extend(["--not", *unique_excludes])
    res = run_git(repo, *args)
    if res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, "无法计算待发布 commit 集合", final_phase="remote_preflight")
    return [ln.strip() for ln in res.stdout.decode("ascii", errors="strict").splitlines() if ln.strip()]


def _emit_scope_banner(cli: ParsedCLI, repo: Path, info: repo_state.RepoInfo) -> None:
    if cli.summary:
        return
    from git_finalizer import __version__

    status_res = run_git(repo, "status", "--short")
    status_text = status_res.stdout.decode("utf-8", errors="replace").strip() if status_res.returncode == 0 else ""
    verify_only = cli.action == "verify-only"
    remotes_text = ""
    if not verify_only:
        rem_res = run_git(repo, "remote")
        remotes_text = rem_res.stdout.decode("utf-8", errors="replace").strip() if rem_res.returncode == 0 else ""

    print(f"\n== Git Finalizer v{__version__}: 提交前状态 ==", flush=True)
    print("$ git branch --show-current", flush=True)
    print(info.branch or "(detached)", flush=True)
    print("$ git rev-parse HEAD", flush=True)
    print(info.head_oid or "(unborn HEAD)", flush=True)
    print("$ git status --short", flush=True)
    print(status_text or "(clean)", flush=True)
    print("$ git remote", flush=True)
    print("(skipped by verify-only)" if verify_only else (remotes_text or "(none)"), flush=True)
    print("$ git rev-parse --abbrev-ref --symbolic-full-name @{upstream}", flush=True)
    print("(skipped by verify-only)" if verify_only else (info.upstream or "(none)"), flush=True)
    staged_res = run_git(repo, "ls-files", "--stage")
    staged_count = len(staged_res.stdout.splitlines()) if staged_res.returncode == 0 else 0
    print(f"index entries: {staged_count}", flush=True)

    print("\n== 显式范围 ==", flush=True)
    print(f"repo: {repo}", flush=True)
    if cli.action in {"verify-only", "commit-only", "initial-commit-only"}:
        print(f"mode: {cli.action}", flush=True)
    elif cli.action == "initial-publish":
        print("mode: initial-publish", flush=True)
        print(f"remote: {cli.remote}", flush=True)
        if cli.snapshot:
            print(f"snapshot: {cli.snapshot}", flush=True)
    elif cli.action == "initial-branch-publish":
        print("mode: initial-branch-publish", flush=True)
        print(f"remote: {cli.remote}", flush=True)
        print(f"remote branch: {cli.remote_branch}", flush=True)
    else:
        print(f"mode: {cli.action}", flush=True)
    print(f"branch: {info.branch or '(detached)'}", flush=True)
    print(f"head before: {info.head_oid or '(unborn)'}", flush=True)
    print(f"requested paths ({len(cli.paths)}):", flush=True)
    for rel in cli.paths:
        print(f"  - {rel}", flush=True)


def _run_dry_run_commit_flow(
    cli: ParsedCLI, info: repo_state.RepoInfo, policy: policy_mod.DeliveryPolicy, summary: SummaryState
) -> tuple[int, SummaryState]:
    repo = info.repo
    paths = repo_state.validate_explicit_paths(repo, cli.paths, is_unborn=info.is_unborn)
    summary.requested_path_count = len(paths)
    _, _, wt_before = repo_state.working_tree_dirty_status(repo)
    summary.worktree_before_state = wt_before
    summary.branch = info.branch
    summary.head_before = info.head_oid
    _emit_scope_banner(cli, repo, info)

    # Target checks in dry-run mirror real mode rules without mutating anything.
    if cli.action == "initial-commit-only":
        _check_initial_target(repo, info, None, require_remote=False)
        _check_initial_worktree_scope(repo, set(paths))
    elif cli.action == "initial-publish":
        branch_ref, remote = _check_initial_target(repo, info, cli.remote, require_remote=True)
        _check_initial_worktree_scope(repo, set(paths))
        summary.upstream = f"{remote}/{info.branch}"
        summary.warnings.append("dry-run did not query the remote empty-state")
        _emit("  - dry-run 未执行 ls-remote；未实时核验远端是否完全为空。", quiet=cli.summary)
    elif cli.action == "initial-branch-publish":
        if not info.branch or info.is_unborn:
            raise FinalizerError(ErrorCode.REPO_INVALID, "initial-branch-publish 要求已有 attached branch commit")
        if repo_state.is_protected_branch(info.branch, policy):
            raise FinalizerError(ErrorCode.BRANCH_PROTECTED, f"initial-branch-publish 拒绝受保护分支: {info.branch}")
        if cli.remote_branch != info.branch:
            raise FinalizerError(ErrorCode.CLI_INVALID_ARGS, "本地 branch 名必须与 --remote-branch 完全一致")
        remote = validate_remote_name(cli.remote or "")
        repo_state.validate_remote_endpoints(repo, remote)
        summary.upstream = f"{remote}/{info.branch}"
        summary.warnings.append("dry-run did not query the explicit remote branch")
        _emit("  - dry-run 未执行 ls-remote；未实时核验显式远端目标 branch 是否不存在。", quiet=cli.summary)
    elif cli.action == "commit-only":
        if not info.branch:
            raise FinalizerError(ErrorCode.DETACHED_HEAD, "detached HEAD 拒绝 commit")
    else:
        # normal
        if info.is_unborn or not info.branch:
            raise FinalizerError(
                ErrorCode.UNBORN_REPO if info.is_unborn else ErrorCode.DETACHED_HEAD,
                "正常模式拒绝 unborn HEAD（要求已有非空 attached branch）"
                if info.is_unborn
                else "正常模式要求已有非空 attached branch",
            )
        remote, target_branch = repo_state.resolve_configured_upstream(repo, info.branch)
        summary.upstream = f"{remote}/{target_branch}"
        summary.warnings.append("dry-run did not fetch or verify remote ahead/behind")
        _emit("  - dry-run 未执行 fetch；未实时核验远端 branch 或 ahead/behind。", quiet=cli.summary)

    trailers = trailer_mod.build_provenance_trailers(
        trailer_agent=cli.trailer_agent,
        trace_ref=cli.trace_ref,
        snapshot_id=cli.snapshot,
    )
    policy_mod.enforce_declarative_policy(
        policy,
        branch=info.branch,
        requested_paths=paths,
        trailers=trailers,
        active_scanners=list(cli.scanners),
        mutating_commit=False,
    )

    with isolated_staging_env(repo, info.git_dir, info.common_dir) as (iso_env, _):
        outcome = validation.validate_staged_candidate(
            repo,
            info,
            iso_env,
            paths,
            cli,
            policy,
            specs=[],
            local_only=True,
        )
    summary.warnings.extend(outcome.warnings)
    summary.reviewed_sensitive_sources.extend(outcome.reviewed_sensitive_sources)
    if (
        not outcome.warnings
        and "no static risk found; tests and review remain caller responsibilities" not in summary.warnings
    ):
        summary.warnings.append("no static risk found; tests and review remain caller responsibilities")
    summary.status = "success"
    summary.exit_code = 0
    summary.final_phase = "preflight"
    summary.mode_state = "dry_run_complete"
    summary.next_action = "run_without_dry_run_after_review"
    if not cli.summary:
        print("\nDRY-RUN 完成：未执行 git add、git commit、git fetch 或 git push。", flush=True)
    return 0, summary


def _run_commit_delivery(
    cli: ParsedCLI, summary: SummaryState
) -> tuple[int, SummaryState]:
    mode_map = {
        "verify-only": "verify-only",
        "commit-only": "commit-only",
        "initial-commit-only": "initial-commit-only",
        "initial-publish": "initial",
        "initial-branch-publish": "initial_branch",
        "normal": "normal",
    }
    summary.mode = mode_map.get(cli.action, "normal")
    summary.final_phase = "preflight"
    require_attached = True
    info = repo_state.inspect_repository(
        cli.repo,
        merge_parent_arg=cli.merge_parent,
        mode=cli.action,
        require_attached=require_attached,
    )
    repo = info.repo
    summary.repository = str(repo)
    policy = policy_mod.load_delivery_policy(repo)

    if cli.dry_run:
        return _run_dry_run_commit_flow(cli, info, policy, summary)

    paths = repo_state.validate_explicit_paths(repo, cli.paths, is_unborn=info.is_unborn)
    summary.requested_path_count = len(paths)
    summary.branch = info.branch
    summary.head_before = info.head_oid
    _emit_scope_banner(cli, repo, info)

    local_only = cli.action in {"verify-only", "commit-only", "initial-commit-only"}
    remote_name: str | None = None
    target_branch: str | None = None
    remote_refs_before: dict[str, str] = {}
    local_tags_before: str = ""
    local_head_before_commit = info.head_oid

    # 1. Resolve target & run read-only remote preflight BEFORE mutating anything.
    if cli.action == "initial-commit-only":
        branch_ref, _ = _check_initial_target(repo, info, None, require_remote=False)
        _check_initial_worktree_scope(repo, set(paths))
        summary.remote_conclusion = "skipped_by_initial_commit_only"
    elif cli.action == "initial-publish":
        branch_ref, remote_name = _check_initial_target(repo, info, cli.remote, require_remote=True)
        _check_initial_worktree_scope(repo, set(paths))
        summary.upstream = f"{remote_name}/{info.branch}"
        if cli.snapshot:
            summary.snapshot_verification = "pending"
            snapshot_verify.verify_snapshot_evidence(cli.snapshot, repo, "worktree", paths)
            summary.snapshot_verification = "worktree_verified"

    _, _, wt_before = repo_state.working_tree_dirty_status(repo)
    summary.worktree_before_state = wt_before

    if cli.action in {"initial-commit-only", "initial-publish"}:
        pass
    elif cli.action == "initial-branch-publish":
        if not info.branch or info.is_unborn or not info.head_oid:
            raise FinalizerError(ErrorCode.REPO_INVALID, "initial-branch-publish 要求已有 attached branch commit")
        if repo_state.is_protected_branch(info.branch, policy):
            raise FinalizerError(ErrorCode.BRANCH_PROTECTED, f"initial-branch-publish 拒绝受保护分支: {info.branch}")
        if cli.remote_branch != info.branch:
            raise FinalizerError(ErrorCode.CLI_INVALID_ARGS, "本地 branch 名必须与 --remote-branch 完全一致")
        remote_name = validate_remote_name(cli.remote or "")
        repo_state.validate_remote_endpoints(repo, remote_name)
        branch_ref = f"refs/heads/{info.branch}"
        summary.upstream = f"{remote_name}/{info.branch}"
    elif cli.action == "commit-only":
        if not info.branch:
            raise FinalizerError(ErrorCode.DETACHED_HEAD, "detached HEAD 拒绝 commit")
        summary.remote_conclusion = "skipped_by_commit_only"
    elif cli.action == "verify-only":
        if not info.branch:
            raise FinalizerError(ErrorCode.DETACHED_HEAD, "detached HEAD 拒绝 verify-only")
        summary.remote_conclusion = "skipped_by_verify_only"
    else:
        # normal
        if not cli.summary:
            print("\n== Commit 前 fetch 与 fast-forward 检查 ==", flush=True)
            print("$ git rev-parse --abbrev-ref --symbolic-full-name @{upstream}", flush=True)
            print(info.upstream or "(none)", flush=True)
        if info.is_unborn or not info.head_oid or not info.branch:
            raise FinalizerError(
                ErrorCode.UNBORN_REPO if info.is_unborn else ErrorCode.DETACHED_HEAD,
                "正常模式拒绝 unborn HEAD（要求已有非空 attached branch）"
                if info.is_unborn
                else "正常模式要求已有非空 attached branch",
            )
        remote_name, target_branch = repo_state.resolve_configured_upstream(repo, info.branch)
        summary.upstream = f"{remote_name}/{target_branch}"

    trailers = trailer_mod.build_provenance_trailers(
        trailer_agent=cli.trailer_agent,
        trace_ref=cli.trace_ref,
        snapshot_id=cli.snapshot,
    )
    policy_mod.enforce_declarative_policy(
        policy,
        branch=info.branch,
        requested_paths=paths,
        trailers=trailers,
        active_scanners=list(cli.scanners),
        mutating_commit=(cli.action != "verify-only"),
    )

    specs: list[validation.ExceptionSpec] = []
    if cli.fixture_exceptions:
        if cli.action in {"verify-only", "commit-only"}:
            try:
                remote_url = repo_state.validate_remote_endpoints(repo, "origin")
            except FinalizerError as exc:
                raise FinalizerError(
                    ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
                    "fixture exception 文件无效；未执行 push",
                    final_phase="preflight",
                ) from exc
        elif cli.action == "initial-commit-only":
            raise FinalizerError(
                ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
                "fixture exception 文件无效；未执行 push",
                final_phase="preflight",
            )
        else:
            remote_url = repo_state.validate_remote_endpoints(repo, remote_name or "")
        specs = validation.load_fixture_exceptions(
            cli.fixture_exceptions,
            repo=repo,
            remote_url=remote_url,
            local_only=(cli.action in {"verify-only", "commit-only"}),
            phase="worktree",
            head_ref="HEAD",
            baseline_ref=None if info.is_unborn else "HEAD",
            final_phase="preflight",
        )

    # 2. Isolated staging & full local validation BEFORE mutating `.git/index` OR `.git/objects`.
    summary.final_phase = "local_validation"
    idx_before_res = run_git(repo, "ls-files", "--stage")
    idx_before = idx_before_res.stdout.decode("utf-8", errors="replace") if idx_before_res.returncode == 0 else ""

    with isolated_staging_env(repo, info.git_dir, info.common_dir) as (iso_env, _):
        outcome = validation.validate_staged_candidate(
            repo,
            info,
            iso_env,
            paths,
            cli,
            policy,
            specs=specs,
            local_only=local_only,
        )
    summary.warnings.extend(outcome.warnings)
    summary.fixture_exceptions.extend(outcome.fixture_exceptions)
    summary.reviewed_sensitive_sources.extend(outcome.reviewed_sensitive_sources)
    summary.external_scanners = list(outcome.external_scanners)
    summary.policy_verdict = outcome.policy_verdict

    if cli.action == "verify-only":
        idx_after_res = run_git(repo, "ls-files", "--stage")
        idx_after = idx_after_res.stdout.decode("utf-8", errors="replace") if idx_after_res.returncode == 0 else ""
        _, _, wt_after = repo_state.working_tree_dirty_status(repo)
        head_after_res = run_git(repo, "rev-parse", "--verify", "HEAD^{commit}")
        head_after = head_after_res.stdout.decode("ascii", errors="strict").strip() if head_after_res.returncode == 0 else None
        if head_after != info.head_oid or idx_after != idx_before or wt_after != wt_before:
            raise FinalizerError(
                ErrorCode.POST_VERIFY_FAILED,
                "verify-only 验证期间仓库状态发生变化",
                status="failed",
                final_phase="local_validation",
            )
        summary.head_after = head_after
        summary.head_unchanged = True
        summary.index_unchanged = True
        summary.worktree_state = wt_after
        summary.worktree_unchanged = True
        summary.mode_state = "passed"
        summary.commit_result = "skipped_by_verify_only"
        summary.push_result = "skipped_by_verify_only"
        summary.post_verify = "skipped_by_verify_only"
        summary.remote_conclusion = "skipped_by_verify_only"
        summary.completed_through = "local_validation"
        summary.status = "success"
        summary.exit_code = 0
        summary.next_action = "none"
        if not cli.summary:
            status_short = run_git(repo, "status", "--short").stdout.decode("utf-8", errors="replace").strip()
            for line in (
                "\n== Verify-only 结果 ==",
                "mode: verify-only",
                "local validation: passed",
                "commit: skipped (--mode verify-only)",
                "push: skipped (--mode verify-only)",
                "remote verification: skipped (--mode verify-only)",
                f"HEAD before: {info.head_oid}",
                f"HEAD after: {head_after}",
                "HEAD unchanged: yes",
                "index unchanged: yes",
                f"worktree before: {wt_before}",
                f"worktree after: {wt_after}",
                "worktree unchanged: yes",
                "$ git status --short",
                status_short or "(clean)",
            ):
                print(line, flush=True)
        _emit("Git Finalizer 完成：本地验证通过；未执行 add、commit、push 或远端验证。", quiet=cli.summary)
        return 0, summary

    # 3. Remote preflight for remote-publishing modes BEFORE mutating real `.git/index`.
    if cli.action == "initial-publish":
        summary.final_phase = "remote_preflight"
        assert remote_name is not None
        refs, _ = _ls_remote_refs(repo, remote_name)
        if refs:
            raise FinalizerError(ErrorCode.REMOTE_DIVERGED, "远端不是完全空 remote；未执行新增 git add、commit 或 push", final_phase="remote_preflight")
        summary.remote_conclusion = "empty_remote_verified"
    elif cli.action == "initial-branch-publish":
        summary.final_phase = "remote_preflight"
        assert remote_name is not None and info.branch is not None and info.head_oid is not None
        branch_ref = f"refs/heads/{info.branch}"
        remote_refs_before, _ = _ls_remote_refs(repo, remote_name)
        if branch_ref in remote_refs_before:
            raise FinalizerError(ErrorCode.REMOTE_DIVERGED, f"远端目标 branch 已存在: {branch_ref}", final_phase="remote_preflight")
        local_tags_before = _snapshot_tags(repo)
        _fetch_remote(repo, remote_name, phase="remote_preflight")
        remote_refs_after, _ = _ls_remote_refs(repo, remote_name)
        if remote_refs_after != remote_refs_before:
            raise FinalizerError(ErrorCode.REMOTE_DIVERGED, "fetch 期间远端 refs 发生变化；未执行 commit", final_phase="remote_preflight")
        head_oids = [oid for ref, oid in remote_refs_before.items() if ref.startswith("refs/heads/")]
        range_commits = _unpublished_commits(repo, info.head_oid, head_oids)
        if range_commits:
            rng_out = validation.validate_commit_range(repo, info, range_commits, cli, policy, specs=specs, local_only=False)
            summary.fixture_exceptions.extend(rng_out.fixture_exceptions)
            summary.warnings.extend(rng_out.warnings)
        summary.remote_conclusion = "target_absent_verified"
    elif cli.action == "normal":
        summary.final_phase = "remote_preflight"
        assert remote_name is not None and target_branch is not None
        local_oid, upstream_oid, ahead, _, _ = _check_normal_remote_state(repo, remote_name, target_branch, phase_label="commit 前 ")
        if ahead > 0:
            if cli.fixture_exceptions:
                raise FinalizerError(
                    ErrorCode.CLI_INVALID_ARGS,
                    "normal fixture review 要求 HEAD 与 upstream 对齐；existing commits 使用 resume-publish",
                    final_phase="remote_preflight",
                )
            range_commits = _unpublished_commits(repo, local_oid, [upstream_oid])
            if range_commits:
                rng_out = validation.validate_commit_range(repo, info, range_commits, cli, policy, specs=specs, local_only=False)
                summary.warnings.extend(rng_out.warnings)

    # 4. Stage validated paths onto real `.git/index` and assert exact write-tree equality (TOCTOU guard).
    summary.final_phase = "staging"
    retained = _retained_staged_deletions(repo, paths, is_unborn=info.is_unborn)
    for rel_del in sorted(retained):
        _emit(f"保留已暂存删除及本地忽略副本: {rel_del}", quiet=cli.summary)
    paths_to_add = [p for p in paths if p not in retained]
    if paths_to_add:
        add_res = run_git(repo, "--literal-pathspecs", "add", "-A", "--", *paths_to_add)
        if add_res.returncode != 0:
            raise FinalizerError(ErrorCode.INDEX_UNCLEAN, "git add 失败；未执行 commit 或 push", status="failed", final_phase="staging")

    # Verify staged scope matches candidate validation exact tree OID.
    tree_res = run_git(repo, "write-tree")
    if tree_res.returncode != 0:
        raise FinalizerError(ErrorCode.INDEX_UNCLEAN, "无法生成已验证 index tree", status="failed", final_phase="staging")
    expected_tree_oid = tree_res.stdout.decode("ascii", errors="strict").strip()
    if outcome.tree_oid and expected_tree_oid != outcome.tree_oid:
        raise FinalizerError(
            ErrorCode.INDEX_UNCLEAN,
            "真实 staging tree 与隔离候选 tree 不一致（工作区在验证后发生修改）；未执行 commit",
            status="failed",
            final_phase="staging",
        )
    if cli.action == "initial-publish" and cli.snapshot:
        snapshot_verify.verify_snapshot_evidence(cli.snapshot, repo, "index", paths)
        summary.snapshot_verification = "index_verified"

    # 5. Commit with optional AI provenance trailers.
    summary.final_phase = "commit"
    assert cli.message is not None
    commit_message = trailer_mod.format_commit_message_with_trailers(repo, cli.message, trailers)
    if not cli.summary:
        diff_ns = run_git(repo, "--literal-pathspecs", "diff", "--cached", "--name-status", "--no-renames")
        print("\n== 显式暂存 ==", flush=True)
        print("$ git diff --cached --name-status --no-renames", flush=True)
        print(diff_ns.stdout.decode("utf-8", errors="replace").strip() or "(empty)", flush=True)
        print("$ git diff --cached --check", flush=True)
        print("(passed)", flush=True)
        print("\n== Commit ==", flush=True)
        print(f"$ git commit -m <explicit message> (branch={info.branch}, paths={len(paths)})", flush=True)
    sym_before = run_git(repo, "symbolic-ref", "--quiet", "HEAD").stdout.decode("utf-8", errors="strict").strip()
    commit_res = run_git(repo, "commit", "-m", commit_message)
    if not cli.summary:
        out_txt = commit_res.stdout.decode("utf-8", errors="replace").strip()
        if out_txt:
            print(out_txt, flush=True)

    head_after_commit = run_git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    commit_hash = head_after_commit.stdout.decode("ascii", errors="strict").strip() if head_after_commit.returncode == 0 else None
    if not commit_hash or commit_hash == local_head_before_commit:
        raise FinalizerError(
            ErrorCode.COMMIT_FAILED,
            f"git commit 失败，未创建 commit: {commit_res.stderr.decode('utf-8', errors='replace').strip()}",
            status="failed",
            final_phase="commit",
        )

    summary.commit_created = True
    summary.commit_sha = commit_hash
    summary.completed_through = "commit"

    def _fail_created_commit(code: str, msg: str, phase: str = "post_commit_verify") -> FinalizerError:
        if cli.action == "initial-publish":
            summary.mode_state = "commit_retained"
            summary.resume_required = True
            summary.resume_oid = commit_hash
            summary.resume_remote = remote_name
            return FinalizerError(
                code,
                msg,
                status="failed",
                final_phase=phase,
                next_action="resume_initial_publish",
                commit_created=True,
                commit_sha=commit_hash,
                resume={"head_oid": commit_hash, "remote": remote_name},
            )
        if cli.action == "initial-commit-only":
            summary.mode_state = "commit_retained"
            summary.push_result = "skipped_by_initial_commit_only"
            summary.post_verify = "skipped_by_initial_commit_only"
            summary.remote_conclusion = "skipped_by_initial_commit_only"
            return FinalizerError(
                code,
                msg,
                status="failed",
                final_phase=phase,
                next_action="inspect_local_root_commit",
                commit_created=True,
                commit_sha=commit_hash,
            )
        if cli.action == "initial-branch-publish":
            summary.mode_state = "commit_retained"
            return FinalizerError(
                code,
                msg,
                status="failed",
                final_phase=phase,
                next_action="inspect_initial_branch_publish_failure",
                commit_created=True,
                commit_sha=commit_hash,
            )
        return FinalizerError(
            code,
            msg,
            status="failed",
            final_phase=phase,
            next_action="inspect_failure_with_local_commit_retained",
            commit_created=True,
            commit_sha=commit_hash,
        )

    if commit_res.returncode != 0:
        raise _fail_created_commit(
            ErrorCode.HOOK_FAILED,
            "git commit 返回失败但 HEAD 已改变；保留本地 commit，未执行 push",
            phase="commit",
        )

    # 6. Post-commit structural, tree, parent, scope, and baseline-aware reviewed-source verification.
    summary.final_phase = "post_commit_verify"
    sym_after = run_git(repo, "symbolic-ref", "--quiet", "HEAD")
    if sym_after.returncode != 0 or sym_after.stdout.decode("utf-8", errors="strict").strip() != sym_before:
        raise _fail_created_commit(ErrorCode.POST_VERIFY_FAILED, "commit 后 attached branch 发生变化或 detached")

    expected_parents = [commit_hash]
    if local_head_before_commit:
        expected_parents.append(local_head_before_commit)
    if info.merge_parent:
        expected_parents.append(info.merge_parent)
    parents_res = run_git(repo, "rev-list", "--parents", "-n", "1", commit_hash)
    if parents_res.returncode != 0 or parents_res.stdout.decode("ascii", errors="strict").strip().split() != expected_parents:
        raise _fail_created_commit(ErrorCode.POST_VERIFY_FAILED, "commit parents 与已验证 HEAD 不一致；未执行 push")

    ct_res = run_git(repo, "rev-parse", f"{commit_hash}^{{tree}}")
    if ct_res.returncode != 0 or ct_res.stdout.decode("ascii", errors="strict").strip() != expected_tree_oid:
        raise _fail_created_commit(ErrorCode.POST_VERIFY_FAILED, "commit tree 与已验证 index tree 不一致；检查 commit hooks，未执行 push")

    diff_t = run_git(repo, "diff-tree", "--root", "--diff-merges=first-parent", "--no-commit-id", "--name-only", "-r", "--no-renames", "-z", commit_hash)
    if diff_t.returncode != 0:
        raise _fail_created_commit(ErrorCode.POST_VERIFY_FAILED, "无法读取 commit 文件范围；保留本地 commit，未执行 push")
    committed_paths = parse_nul_records(diff_t.stdout)
    extra_committed = [p for p in committed_paths if p not in set(paths)]
    if extra_committed:
        raise _fail_created_commit(ErrorCode.SCOPE_INVALID, f"commit 包含超出显式范围的文件: {', '.join(extra_committed)}")

    if cli.reviewed_sensitive_source:
        # P0 FIX (v1.9.9 regression): verify created commit blobs against first parent (or None on root commit).
        summary.reviewed_sensitive_sources = source_review.validate_review_document(
            review_file=cli.reviewed_sensitive_source,
            repo=repo,
            common_dir=info.common_dir,
            repository_id=cli.repository_id,
            allocation_id=cli.allocation_id,
            task_key=cli.task_key,
            authority_key=cli.authority_key,
            worktree_path=cli.worktree_path,
            requested_paths=paths,
            stage="head",
            allowed_extensions=policy.allow_reviewed_extensions,
            commit_ref=commit_hash,
            baseline_ref=local_head_before_commit,
        )
        for rec in summary.reviewed_sensitive_sources:
            rec["content_scan"] = "passed"

    if cli.agent_trace:
        summary.agent_trace_note = trailer_mod.write_agent_trace_note(
            repo,
            commit_hash,
            paths,
            trailer_agent=cli.trailer_agent,
            trace_ref=cli.trace_ref,
            snapshot_id=cli.snapshot,
            task_key=cli.task_key,
            allocation_id=cli.allocation_id,
        )

    if cli.action == "initial-publish" and cli.snapshot:
        snapshot_verify.verify_snapshot_evidence(cli.snapshot, repo, "head", paths)
        summary.snapshot_verification = "passed"

    _emit(f"commit hash: {commit_hash}", quiet=cli.summary)

    if cli.action in {"commit-only", "initial-commit-only"}:
        skip_tag = "initial_commit_only" if cli.action == "initial-commit-only" else "commit_only"
        _, _, wt_final = repo_state.working_tree_dirty_status(repo)
        if cli.action == "initial-commit-only":
            if wt_final != "clean" or run_git(repo, "diff", "--cached", "--quiet").returncode != 0:
                raise _fail_created_commit(ErrorCode.POST_VERIFY_FAILED, "root commit 后 worktree 或 index 不干净")
            summary.mode_state = "committed"
        summary.worktree_state = wt_final
        summary.push_result = f"skipped_by_{skip_tag}"
        summary.post_verify = f"skipped_by_{skip_tag}"
        summary.remote_conclusion = f"skipped_by_{skip_tag}"
        summary.status = "success"
        summary.exit_code = 0
        summary.next_action = "none"
        if not cli.summary:
            skip_display = "--initial-commit-only" if cli.action == "initial-commit-only" else "--mode commit-only"
            result_mode = "initial-commit-only" if cli.action == "initial-commit-only" else "commit-only"
            status_short = run_git(repo, "status", "--short").stdout.decode("utf-8", errors="replace").strip()
            for line in (
                f"mode: {result_mode}",
                f"commit hash: {commit_hash}",
                f"push: skipped ({skip_display})",
                f"remote verification: skipped ({skip_display})",
                "$ git status --short",
                status_short or "(clean)",
            ):
                print(line, flush=True)
        return 0, summary

    # 7. Push & post-push verification per publishing mode.
    if cli.action == "initial-publish":
        assert remote_name is not None and info.branch is not None
        branch_ref = f"refs/heads/{info.branch}"
        pre_push_refs, _ = _ls_remote_refs(repo, remote_name)
        if pre_push_refs:
            raise _fail_created_commit(
                ErrorCode.REMOTE_DIVERGED,
                "commit 后远端不是完全空 remote；保留本地 root commit，未执行 push",
                phase="post_commit_verify",
            )
        summary.remote_conclusion = "empty_remote_reverified"
        summary.final_phase = "push"
        summary.push_executed = True
        summary.push_branch_refspec_only = True
        summary.push_follow_tags_requested = False
        if not cli.summary:
            print("\n== Root commit 后远端空状态复查 ==", flush=True)
            print(f"$ git ls-remote -- {remote_name}", flush=True)
            print("(verified empty)", flush=True)
            print("\n== Initial push ==", flush=True)
            print(f"$ git push --no-follow-tags --set-upstream -- {remote_name} HEAD:{branch_ref}", flush=True)
        push_res = _run_remote_push(
            repo,
            "push",
            "--no-follow-tags",
            "--set-upstream",
            "--",
            remote_name,
            f"HEAD:{branch_ref}",
        )
        if not cli.summary and push_res.returncode == 0:
            for chunk in (push_res.stdout, push_res.stderr):
                txt = chunk.decode("utf-8", errors="replace").strip()
                if txt:
                    print(txt, flush=True)
        if push_res.returncode != 0:
            summary.push_result = "uncertain"
            raise _fail_created_commit(ErrorCode.PUSH_FAILED, "initial push 失败或结果无法确认", phase="push")
        summary.push_result = "succeeded"
        summary.mode_state = "pushed"
        summary.final_phase = "post_verify"
        up_state, up_oid, up_err = _classify_branch_upstream(repo, info.branch, remote_name, branch_ref)
        remote_refs, _ = _ls_remote_refs(repo, remote_name)
        if len(remote_refs) != 1 or remote_refs.get(branch_ref) != commit_hash:
            raise _fail_created_commit(ErrorCode.POST_VERIFY_FAILED, "push 后远端 ref 集合不是唯一目标 branch", phase="post_verify")
        if up_state != "exact_resolved" or up_oid != commit_hash:
            raise _fail_created_commit(ErrorCode.POST_VERIFY_FAILED, up_err or "initial push 后 upstream 与远端 OID 未对齐", phase="post_verify")
        _, _, wt_final = repo_state.working_tree_dirty_status(repo)
        if wt_final != "clean":
            raise _fail_created_commit(ErrorCode.POST_VERIFY_FAILED, "initial push 后 worktree 不干净", phase="post_verify")
        summary.post_verify = "passed"
        summary.remote_conclusion = "published_and_upstream_aligned"
        summary.mode_state = "verified"
        if not cli.summary:
            for line in (
                "\n== Initial push 后验证 ==",
                "$ git rev-parse HEAD",
                commit_hash,
                "$ git rev-parse @{upstream}",
                up_oid,
                f"$ git ls-remote --heads -- {remote_name}",
                f"{remote_refs.get(branch_ref)}\t{branch_ref}",
                f"branch: {info.branch}",
                f"remote: {remote_name}",
                f"upstream: {summary.upstream}",
                "local branch, tags, and Git config: verified",
                "worktree state: clean",
                "post-verify: passed",
            ):
                print(line, flush=True)
    elif cli.action == "initial-branch-publish":
        assert remote_name is not None and info.branch is not None
        branch_ref = f"refs/heads/{info.branch}"
        live_refs, _ = _ls_remote_refs(repo, remote_name)
        if branch_ref in live_refs:
            raise _fail_created_commit(ErrorCode.REMOTE_DIVERGED, "远端目标 branch 已在 push 前出现")
        if live_refs != remote_refs_before:
            raise _fail_created_commit(ErrorCode.REMOTE_DIVERGED, "commit 后远端 refs 发生变化，拒绝 initial-branch push")
        summary.remote_conclusion = "target_absent_reverified"
        summary.final_phase = "push"
        summary.push_executed = True
        summary.push_branch_refspec_only = True
        summary.push_follow_tags_requested = False
        push_res = _run_remote_push(
            repo,
            "push",
            "--no-follow-tags",
            "--set-upstream",
            "--",
            remote_name,
            f"HEAD:{branch_ref}",
        )
        if push_res.returncode != 0:
            summary.push_result = "uncertain"
            raise _fail_created_commit(ErrorCode.PUSH_FAILED, "initial branch push 失败或结果无法确认", phase="push")
        summary.push_result = "succeeded"
        summary.mode_state = "pushed"
        summary.final_phase = "post_verify"
        post_refs, _ = _ls_remote_refs(repo, remote_name)
        other_post = {k: v for k, v in post_refs.items() if k != branch_ref}
        if post_refs.get(branch_ref) != commit_hash or other_post != remote_refs_before or _snapshot_tags(repo) != local_tags_before:
            raise _fail_created_commit(ErrorCode.POST_VERIFY_FAILED, "push 后远端 branch 或非目标 refs/tags 校验失败", phase="post_verify")
        up_state, up_oid, up_err = _classify_branch_upstream(repo, info.branch, remote_name, branch_ref)
        if up_state != "exact_resolved" or up_oid != commit_hash:
            raise _fail_created_commit(ErrorCode.POST_VERIFY_FAILED, up_err or "push 后 upstream 未对齐", phase="post_verify")
        summary.post_verify = "passed"
        summary.remote_conclusion = "published_and_upstream_aligned"
        summary.mode_state = "verified"
        if not cli.summary:
            for line in (
                "\n== 显式暂存 ==",
                f"staged paths: {', '.join(paths)}",
                "\n== Commit ==",
                f"commit hash: {commit_hash}",
                "\n== Initial push ==",
                f"$ git push --no-follow-tags --set-upstream -- {remote_name} HEAD:{branch_ref}",
                "\n== Initial push 后验证 ==",
                f"branch: {info.branch}",
                f"remote: {remote_name}",
                f"head: {commit_hash}",
                f"remote ref ({branch_ref}): {post_refs.get(branch_ref)}",
                f"upstream: {summary.upstream}",
                "local branch, tags, and Git config: verified",
                "post-verify: passed",
            ):
                print(line, flush=True)
    else:
        # normal
        assert remote_name is not None and target_branch is not None
        if not cli.summary:
            print("\n== Commit 后 fetch 与 fast-forward 检查 ==", flush=True)
            print(f"$ git fetch --prune -- {remote_name}", flush=True)
        _check_normal_remote_state(
            repo,
            remote_name,
            target_branch,
            phase_label="commit 后 ",
            phase_name="post_commit_verify",
            commit_created=True,
            commit_sha=commit_hash,
        )
        summary.final_phase = "push"
        summary.push_executed = True
        summary.push_branch_refspec_only = True
        summary.push_follow_tags_requested = False
        if not cli.summary:
            print("\n== Push ==", flush=True)
            print(f"$ git push --no-follow-tags -- {remote_name} HEAD:refs/heads/{target_branch}", flush=True)
        push_res = _run_remote_push(
            repo,
            "push",
            "--no-follow-tags",
            "--",
            remote_name,
            f"HEAD:refs/heads/{target_branch}",
        )
        if not cli.summary and push_res.returncode == 0:
            for chunk in (push_res.stdout, push_res.stderr):
                txt = chunk.decode("utf-8", errors="replace").strip()
                if txt:
                    print(txt, flush=True)
        if push_res.returncode != 0:
            summary.push_result = "uncertain"
            raise _fail_created_commit(ErrorCode.PUSH_FAILED, "git push 失败", phase="push")
        summary.push_result = "succeeded"
        summary.final_phase = "post_verify"
        local_after, upstream_after, ahead_after, behind_after, remote_after = _check_normal_remote_state(
            repo,
            remote_name,
            target_branch,
            phase_label="push 后 ",
            phase_name="post_verify",
            commit_created=True,
            commit_sha=commit_hash,
        )
        if local_after != commit_hash or upstream_after != commit_hash or remote_after != commit_hash or ahead_after != 0 or behind_after != 0:
            raise _fail_created_commit(ErrorCode.POST_VERIFY_FAILED, "push 后本地 HEAD 与远端 branch / upstream 未对齐", phase="post_verify")
        summary.post_verify = "passed"
        if not cli.summary:
            for line in (
                "\n== Push 后验证 ==",
                "$ git rev-parse HEAD",
                local_after,
                "$ git rev-parse @{upstream}",
                upstream_after,
                f"remote branch OID: {remote_after}",
                f"ahead/behind: {ahead_after}\t{behind_after}",
                "$ git status --short",
                "(clean)",
                "post-verify: passed",
            ):
                print(line, flush=True)

    summary.completed_through = "post_verify"
    summary.status = "success"
    summary.exit_code = 0
    summary.next_action = "none"
    _emit("\nGit Finalizer 完成：commit 与 push 已验证。", quiet=cli.summary)
    return 0, summary


def _run_publish_existing_branch_or_history(cli: ParsedCLI, summary: SummaryState) -> tuple[int, SummaryState]:
    is_history = cli.action in {"publish-existing-history", "resume-existing-history-publish"}
    is_resume_history = cli.action == "resume-existing-history-publish"
    summary.mode = "resume" if is_resume_history else ("publish_existing_history" if is_history else "publish_existing_branch")
    summary.commit_reused = True
    summary.commit_result = "reused"
    if is_resume_history:
        summary.resume_publish_kind = "existing_history_first"
        summary.recovery_start = "existing_history_first_publish"

    info = repo_state.inspect_repository(cli.repo, merge_parent_arg=None, mode=cli.action, require_attached=True)
    repo = info.repo
    summary.repository = str(repo)
    _require_clean_repo_state(repo, info)
    policy = policy_mod.load_delivery_policy(repo)
    assert info.branch is not None and info.head_oid is not None

    expected_oid = validate_oid(cli.head_oid or "", "expected HEAD")
    if expected_oid != info.head_oid:
        raise FinalizerError(ErrorCode.REPO_INVALID, "当前 HEAD 与 expected HEAD 不一致")
    if not is_history and repo_state.is_protected_branch(info.branch, policy):
        raise FinalizerError(ErrorCode.BRANCH_PROTECTED, f"publish-existing-branch 拒绝受保护分支: {info.branch}")
    if cli.remote_branch != info.branch:
        raise FinalizerError(ErrorCode.CLI_INVALID_ARGS, "本地 branch 名必须与 --remote-branch 完全一致")
    remote = validate_remote_name(cli.remote or "")
    remote_url = repo_state.validate_remote_endpoints(repo, remote)
    branch_ref = f"refs/heads/{info.branch}"

    up_state, up_oid, up_err = _classify_branch_upstream(repo, info.branch, remote, branch_ref)
    if not is_history and up_state != "none":
        raise FinalizerError(ErrorCode.UPSTREAM_CONFLICT, "publish-existing-branch 只接受尚未配置 upstream 的本地 branch")
    if is_history and not is_resume_history and up_state not in {"none", "exact_unresolved"}:
        raise FinalizerError(ErrorCode.UPSTREAM_CONFLICT, "existing-history first publication 只接受无 upstream 或精确未解析 upstream")
    if is_resume_history and (up_state == "conflicting" or (up_state == "exact_resolved" and up_oid != expected_oid)):
        raise FinalizerError(ErrorCode.UPSTREAM_CONFLICT, up_err or "existing-history resume upstream 与 expected target 不一致")

    summary.branch = info.branch
    summary.upstream = f"{remote}/{info.branch}"
    summary.commit_sha = expected_oid
    summary.head_before = expected_oid
    summary.worktree_before_state = "clean"
    summary.push_target = f"{remote}:{branch_ref}"

    baseline = _snapshot_local_state(repo)
    specs = (
        validation.load_fixture_exceptions(
            cli.fixture_exceptions,
            repo=repo,
            remote_url=remote_url,
            local_only=False,
            phase="history",
            head_ref=expected_oid,
            final_phase="preflight",
        )
        if cli.fixture_exceptions
        else []
    )
    reviewed_blobs: dict[str, str] | None = None
    if cli.reviewed_sensitive_source:
        summary.reviewed_sensitive_sources = source_review.validate_review_document(
            review_file=cli.reviewed_sensitive_source,
            repo=repo,
            common_dir=info.common_dir,
            repository_id=cli.repository_id,
            allocation_id=cli.allocation_id,
            task_key=cli.task_key,
            authority_key=cli.authority_key,
            worktree_path=cli.worktree_path,
            requested_paths=cli.paths,
            stage="head",
            allowed_extensions=policy.allow_reviewed_extensions,
            commit_ref=expected_oid,
            baseline_ref=f"{expected_oid}^1" if run_git(repo, "rev-parse", "--verify", f"{expected_oid}^1").returncode == 0 else None,
        )
        for rec in summary.reviewed_sensitive_sources:
            rec["content_scan"] = "passed"
        reviewed_blobs = source_review.reviewed_blob_map(repo, summary.reviewed_sensitive_sources, commit_ref=expected_oid)

    summary.final_phase = "remote_preflight"
    refs_before, _ = _ls_remote_refs(repo, remote)
    if is_history:
        if not is_resume_history and refs_before:
            raise FinalizerError(ErrorCode.REMOTE_DIVERGED, "远端不是完全空 repository（包含非目标用户 Git refs）；existing-history first publication 已阻断", final_phase="remote_preflight")
        if is_resume_history and refs_before and refs_before != {branch_ref: expected_oid}:
            raise FinalizerError(ErrorCode.REMOTE_DIVERGED, "远端包含冲突或意外分支，拒绝 resume-existing-history-publish", final_phase="remote_preflight")
    elif branch_ref in refs_before:
        raise FinalizerError(ErrorCode.REMOTE_DIVERGED, f"远端目标 branch 已存在: {branch_ref}", final_phase="remote_preflight")

    _fetch_remote(repo, remote, phase="remote_preflight")
    refs_after_fetch, _ = _ls_remote_refs(repo, remote)
    if refs_after_fetch != refs_before:
        raise FinalizerError(ErrorCode.REMOTE_DIVERGED, "fetch 期间远端 refs 发生变化；未执行 push", final_phase="remote_preflight")
    _assert_local_state_unchanged(repo, baseline, phase="remote_preflight")

    remote_heads = [oid for ref, oid in refs_before.items() if ref.startswith("refs/heads/")] if not is_history else []
    commits = _unpublished_commits(repo, expected_oid, remote_heads)
    summary.publish_commit_count = len(commits)
    summary.publish_range = (
        f"{expected_oid} --not captured:{remote}/heads" if not is_history else f"{len(commits)} commits up to {expected_oid}"
    )
    if is_history and not commits:
        raise FinalizerError(ErrorCode.REPO_INVALID, "existing-history publication 要求至少一个本地 commit", final_phase="remote_preflight")
    if commits:
        rng_out = validation.validate_commit_range(
            repo,
            info,
            commits,
            cli,
            policy,
            specs=specs,
            local_only=False,
            reviewed_blobs=reviewed_blobs,
        )
        summary.fixture_exceptions.extend(rng_out.fixture_exceptions)
        summary.warnings.extend(rng_out.warnings)
    elif specs:
        raise FinalizerError(ErrorCode.FIXTURE_EXCEPTION_BLOCKED, "fixture exception 未匹配待发布历史中的 exact path/blob/detector", final_phase="remote_preflight")

    _assert_local_state_unchanged(repo, baseline, phase="remote_preflight")
    summary.completed_through = "preflight"
    summary.remote_conclusion = (
        "target_absent_reverified"
        if not is_history
        else ("empty_or_expected_target_reverified" if is_resume_history else "empty_repository_reverified")
    )

    summary.final_phase = "push"
    summary.push_executed = True
    summary.push_branch_refspec_only = True
    summary.push_follow_tags_requested = False
    push_res = _run_remote_push(repo, "push", "--no-follow-tags", "--set-upstream", "--", remote, f"HEAD:{branch_ref}")
    if push_res.returncode != 0:
        summary.push_result = "uncertain"
        if is_history:
            summary.resume_required = True
            summary.resume_oid = expected_oid
            summary.resume_remote = remote
            raise FinalizerError(
                ErrorCode.PUSH_FAILED,
                "existing-history push 失败或结果无法确认",
                status="failed",
                final_phase="push",
                next_action="resume_existing_history_publish",
                push_executed=True,
                push_result="uncertain",
                resume={"head_oid": expected_oid, "remote": remote},
            )
        raise FinalizerError(
            ErrorCode.PUSH_FAILED,
            "existing branch push 失败或结果无法确认",
            status="failed",
            final_phase="push",
            next_action="inspect_existing_branch_publish_failure",
            push_executed=True,
            push_result="uncertain",
        )
    summary.push_result = "succeeded"
    summary.completed_through = "push"
    summary.mode_state = "pushed"

    summary.final_phase = "post_verify"
    post_refs, _ = _ls_remote_refs(repo, remote)
    expected_other = {k: v for k, v in refs_before.items() if k != branch_ref}
    actual_other = {k: v for k, v in post_refs.items() if k != branch_ref}
    if post_refs.get(branch_ref) != expected_oid or actual_other != expected_other:
        msg = (
            "push 后远端目标 branch 与 HEAD 不一致"
            if post_refs.get(branch_ref) != expected_oid
            else "push 后远端 refs 校验失败"
        )
        raise FinalizerError(
            ErrorCode.POST_VERIFY_FAILED,
            msg,
            status="failed",
            final_phase="post_verify",
            next_action="resume_existing_history_publish" if is_history else "inspect_existing_branch_publish_failure",
            push_executed=True,
            push_result="succeeded",
        )
    up_state_after, up_oid_after, _ = _classify_branch_upstream(repo, info.branch, remote, branch_ref)
    if up_state_after != "exact_resolved" or up_oid_after != expected_oid:
        raise FinalizerError(ErrorCode.POST_VERIFY_FAILED, "push 后 upstream 未对齐", status="failed", final_phase="post_verify", push_executed=True, push_result="succeeded")
    _assert_local_state_unchanged(
        repo,
        baseline,
        phase="post_verify",
        next_action="resume_existing_history_publish" if is_history else "inspect_existing_branch_publish_failure",
        push_executed=True,
        push_result="succeeded",
        context_label="existing-history publish" if is_history else "existing-branch publish",
    )

    summary.head_after = expected_oid
    summary.head_unchanged = True
    summary.index_unchanged = True
    summary.worktree_unchanged = True
    summary.worktree_state = "clean"
    summary.final_head = expected_oid
    summary.final_remote_oid = expected_oid
    summary.post_verify = "passed"
    summary.remote_conclusion = "published_and_upstream_aligned"
    summary.completed_through = "post_verify"
    summary.mode_state = "verified"
    summary.status = "success"
    summary.exit_code = 0
    summary.next_action = "none"
    return 0, summary


def _run_resume_publish_or_initial(cli: ParsedCLI, summary: SummaryState) -> tuple[int, SummaryState]:
    is_initial = cli.action == "resume-initial-publish"
    summary.mode = "resume"
    summary.mode_state = "recovering"
    summary.commit_reused = True
    summary.commit_result = "reused"
    info = repo_state.inspect_repository(cli.repo, merge_parent_arg=None, mode=cli.action, require_attached=True)
    repo = info.repo
    summary.repository = str(repo)
    if info.is_unborn or not info.head_oid:
        raise FinalizerError(ErrorCode.UNBORN_REPO, "当前 HEAD 无法解析为 commit", final_phase="preflight")
    _require_clean_repo_state(repo, info)
    policy = policy_mod.load_delivery_policy(repo)
    assert info.branch is not None and info.head_oid is not None

    expected_oid = validate_oid(cli.head_oid or "", "expected HEAD")
    if expected_oid != info.head_oid:
        raise FinalizerError(ErrorCode.REPO_INVALID, "当前 HEAD 与 expected HEAD 不一致")

    summary.branch = info.branch
    summary.commit_sha = expected_oid
    summary.head_before = expected_oid
    summary.worktree_before_state = "clean"
    summary.push_branch_refspec_only = True
    summary.push_follow_tags_requested = False

    baseline = _snapshot_local_state(repo)
    branch_ref = f"refs/heads/{info.branch}"

    if is_initial:
        remote = validate_remote_name(cli.remote or "")
        remote_url = repo_state.validate_remote_endpoints(repo, remote)
        summary.resume_publish_kind = "initial_root"
        summary.upstream = f"{remote}/{info.branch}"
        summary.push_target = f"{remote}:{branch_ref}"
        parents_res = run_git(repo, "rev-list", "--parents", "-n", "1", expected_oid)
        if parents_res.returncode != 0 or parents_res.stdout.decode("ascii").strip().split() != [expected_oid]:
            raise FinalizerError(ErrorCode.REPO_INVALID, "resume-initial-publish 要求 HEAD 为单一 root commit")

        for fixture_rel in cli.allow_test_fixtures:
            if not fixture_rel.startswith("tests/") or ".." in Path(fixture_rel).parts:
                raise FinalizerError(ErrorCode.SCOPE_INVALID, f"allow-test-fixture 只接受 tests/ 路径: {fixture_rel}")
            lt = run_git(repo, "--literal-pathspecs", "ls-tree", "-z", expected_oid, "--", fixture_rel)
            recs = parse_nul_records(lt.stdout) if lt.returncode == 0 else []
            if len(recs) != 1 or "\t" not in recs[0]:
                raise FinalizerError(ErrorCode.SCOPE_INVALID, f"allow-test-fixture 必须指向一个 regular root-commit blob: {fixture_rel}")
            meta, _, found_p = recs[0].partition("\t")
            mp = meta.split()
            if found_p != fixture_rel or len(mp) != 3 or mp[1] != "blob" or mp[0] not in {"100644", "100755"}:
                raise FinalizerError(ErrorCode.SCOPE_INVALID, f"allow-test-fixture 必须指向一个 regular root-commit blob: {fixture_rel}")

        specs = (
            validation.load_fixture_exceptions(
                cli.fixture_exceptions,
                repo=repo,
                remote_url=remote_url,
                local_only=False,
                phase="history",
                head_ref=expected_oid,
                final_phase="preflight",
            )
            if cli.fixture_exceptions
            else []
        )
        reviewed_blobs_init: dict[str, str] | None = None
        if cli.reviewed_sensitive_source:
            summary.reviewed_sensitive_sources = source_review.validate_review_document(
                review_file=cli.reviewed_sensitive_source,
                repo=repo,
                common_dir=info.common_dir,
                repository_id=cli.repository_id,
                allocation_id=cli.allocation_id,
                task_key=cli.task_key,
                authority_key=cli.authority_key,
                worktree_path=cli.worktree_path,
                requested_paths=cli.paths,
                stage="head",
                allowed_extensions=policy.allow_reviewed_extensions,
                commit_ref=expected_oid,
                baseline_ref=None,
            )
            for rec in summary.reviewed_sensitive_sources:
                rec["content_scan"] = "passed"
            reviewed_blobs_init = source_review.reviewed_blob_map(repo, summary.reviewed_sensitive_sources, commit_ref=expected_oid)
        summary.publish_commit_count = 1
        summary.publish_range = expected_oid
        rng_out = validation.validate_commit_range(
            repo,
            info,
            [expected_oid],
            cli,
            policy,
            specs=specs,
            local_only=False,
            reviewed_blobs=reviewed_blobs_init,
        )
        summary.fixture_exceptions.extend(rng_out.fixture_exceptions)
        summary.warnings.extend(rng_out.warnings)
        summary.completed_through = "preflight"

        up_state, up_oid, up_err = _classify_branch_upstream(repo, info.branch, remote, branch_ref)
        if up_state == "conflicting" or (up_state == "exact_resolved" and up_oid != expected_oid):
            raise FinalizerError(ErrorCode.UPSTREAM_CONFLICT, up_err or "upstream commit 与 expected HEAD 不一致", final_phase="remote_preflight")

        def _classify_init_remote(refs_map: dict[str, str]) -> tuple[str, str]:
            if not refs_map:
                return "A", ""
            if refs_map == {branch_ref: expected_oid}:
                return "B", ""
            if len(refs_map) > 1:
                return "C", "远端存在额外或含糊的 refs"
            ref_k, oid_v = next(iter(refs_map.items()))
            if ref_k == branch_ref:
                return "C", f"远端目标 branch 指向其他 OID ({oid_v})"
            return "C", f"远端存在非目标 ref ({ref_k})"

        refs, _ = _ls_remote_refs(repo, remote)
        r_state, r_err = _classify_init_remote(refs)
        need_push = False
        if r_state == "A":
            summary.recovery_start = "empty_remote"
            summary.remote_conclusion = "empty_remote"
            need_push = True
        elif r_state == "B":
            summary.remote_conclusion = "expected_branch_present"
            if up_state != "exact_resolved" or up_oid != expected_oid:
                summary.recovery_start = "published_without_upstream"
                need_push = True
            else:
                summary.recovery_start = "already_published"
        else:
            summary.recovery_start = "conflicting_remote"
            summary.remote_conclusion = "conflicting_or_ambiguous"
            raise FinalizerError(ErrorCode.REMOTE_DIVERGED, f"远端处于冲突或含糊状态；拒绝恢复: {r_err}", final_phase="remote_preflight")
        summary.completed_through = "remote_classification"

        if need_push:
            summary.final_phase = "push"
            summary.push_executed = True
            push_res = _run_remote_push(repo, "push", "--no-follow-tags", "--set-upstream", "--", remote, f"HEAD:{branch_ref}")
            if push_res.returncode != 0:
                summary.push_result = "uncertain"
                refs_chk, _ = _ls_remote_refs(repo, remote)
                c_state, c_err = _classify_init_remote(refs_chk)
                if c_state == "A":
                    summary.resume_required = True
                    summary.resume_oid = expected_oid
                    summary.resume_remote = remote
                    raise FinalizerError(
                        ErrorCode.PUSH_FAILED,
                        "resume push 失败且远端仍为空",
                        status="failed",
                        final_phase="push",
                        next_action="retry_same_resume",
                        push_executed=True,
                        push_result="uncertain",
                        resume={"head_oid": expected_oid, "remote": remote},
                    )
                if c_state == "C":
                    raise FinalizerError(ErrorCode.REMOTE_DIVERGED, f"resume push 后远端进入冲突或含糊状态: {c_err}", status="failed", final_phase="push", push_executed=True, push_result="uncertain")
                # c_state == "B": remote has expected_oid; check upstream and run bounded up-to-date second push if missing!
                u_s, u_o, _ = _classify_branch_upstream(repo, info.branch, remote, branch_ref)
                if u_s != "exact_resolved" or u_o != expected_oid:
                    _emit("\n== Resume up-to-date push ==", quiet=cli.summary)
                    second_res = _run_remote_push(repo, "push", "--no-follow-tags", "--set-upstream", "--", remote, f"HEAD:{branch_ref}")
                    if second_res.returncode != 0:
                        refs_chk2, _ = _ls_remote_refs(repo, remote)
                        c_state2, c_err2 = _classify_init_remote(refs_chk2)
                        if c_state2 == "A":
                            summary.resume_required = True
                            summary.resume_oid = expected_oid
                            summary.resume_remote = remote
                            raise FinalizerError(ErrorCode.PUSH_FAILED, "up-to-date push 失败且远端仍为空", status="failed", final_phase="push", next_action="retry_same_resume", push_executed=True, push_result="uncertain", resume={"head_oid": expected_oid, "remote": remote})
                        if c_state2 == "C":
                            raise FinalizerError(ErrorCode.REMOTE_DIVERGED, f"up-to-date push 后远端冲突或含糊: {c_err2}", status="failed", final_phase="push", push_executed=True, push_result="uncertain")
                        u_s2, u_o2, _ = _classify_branch_upstream(repo, info.branch, remote, branch_ref)
                        if u_s2 != "exact_resolved" or u_o2 != expected_oid:
                            summary.resume_required = True
                            summary.resume_oid = expected_oid
                            summary.resume_remote = remote
                            raise FinalizerError(ErrorCode.PUSH_FAILED, "远端已正确但本地 upstream 尚未建立", status="failed", final_phase="push", next_action="retry_same_resume", push_executed=True, push_result="uncertain", resume={"head_oid": expected_oid, "remote": remote})
                summary.push_result = "succeeded"
            else:
                summary.push_result = "succeeded"
        summary.completed_through = "push"
        summary.final_phase = "post_verify"
        refs_final, _ = _ls_remote_refs(repo, remote)
        up_after, up_oid_after, _ = _classify_branch_upstream(repo, info.branch, remote, branch_ref)
        if refs_final != {branch_ref: expected_oid} or up_after != "exact_resolved" or up_oid_after != expected_oid:
            raise FinalizerError(ErrorCode.POST_VERIFY_FAILED, "resume-initial-publish 验证失败", status="failed", final_phase="post_verify")
        _assert_local_state_unchanged(repo, baseline, phase="post_verify", push_executed=need_push, push_result=summary.push_result)
    else:
        parents_chk = run_git(repo, "rev-list", "--parents", "-n", "1", expected_oid)
        if parents_chk.returncode != 0 or len(parents_chk.stdout.decode("ascii").strip().split()) < 2:
            raise FinalizerError(
                ErrorCode.REPO_INVALID,
                f"resume-publish 要求非 root commit（root commit 请使用 --resume-initial-publish {expected_oid}）",
                final_phase="preflight",
            )
        summary.resume_publish_kind = "existing_commits"
        summary.recovery_start = "local_ahead_commits"
        remote, target_branch = repo_state.resolve_upstream_for_resume(repo, info.branch)
        remote_url = repo_state.validate_remote_endpoints(repo, remote)
        target_ref = f"refs/heads/{target_branch}"
        summary.upstream = f"{remote}/{target_branch}"
        summary.push_target = f"{remote}:{target_ref}"

        summary.final_phase = "remote_preflight"
        local_oid, upstream_oid, ahead, behind, _ = _check_normal_remote_state(repo, remote, target_branch, phase_label="resume-publish 前 ")
        if local_oid != expected_oid:
            raise FinalizerError(ErrorCode.REPO_INVALID, "resume-publish 预检后 当前 HEAD 与 expected HEAD 不一致")
        if ahead < 1:
            raise FinalizerError(ErrorCode.REPO_INVALID, "resume-publish 要求当前分支至少 ahead configured upstream 1 个 commit")
        commits = _unpublished_commits(repo, local_oid, [upstream_oid])
        summary.publish_ahead = ahead
        summary.publish_behind = behind
        summary.publish_commit_count = len(commits)
        summary.publish_range = f"{upstream_oid}..{local_oid}"
        specs = (
            validation.load_fixture_exceptions(
                cli.fixture_exceptions,
                repo=repo,
                remote_url=remote_url,
                local_only=False,
                phase="history",
                head_ref=expected_oid,
                baseline_ref=upstream_oid,
                final_phase="preflight",
            )
            if cli.fixture_exceptions
            else []
        )
        reviewed_blobs_res: dict[str, str] | None = None
        if cli.reviewed_sensitive_source:
            summary.reviewed_sensitive_sources = source_review.validate_review_document(
                review_file=cli.reviewed_sensitive_source,
                repo=repo,
                common_dir=info.common_dir,
                repository_id=cli.repository_id,
                allocation_id=cli.allocation_id,
                task_key=cli.task_key,
                authority_key=cli.authority_key,
                worktree_path=cli.worktree_path,
                requested_paths=cli.paths,
                stage="head",
                allowed_extensions=policy.allow_reviewed_extensions,
                commit_ref=expected_oid,
                baseline_ref=f"{expected_oid}^1" if run_git(repo, "rev-parse", "--verify", f"{expected_oid}^1").returncode == 0 else None,
            )
            for rec in summary.reviewed_sensitive_sources:
                rec["content_scan"] = "passed"
            reviewed_blobs_res = source_review.reviewed_blob_map(repo, summary.reviewed_sensitive_sources, commit_ref=expected_oid)
        rng_out = validation.validate_commit_range(
            repo,
            info,
            commits,
            cli,
            policy,
            specs=specs,
            local_only=False,
            reviewed_blobs=reviewed_blobs_res,
        )
        summary.fixture_exceptions.extend(rng_out.fixture_exceptions)
        summary.warnings.extend(rng_out.warnings)
        summary.completed_through = "preflight"
        summary.remote_conclusion = "fast_forward_publish_ready"

        summary.final_phase = "push"
        summary.push_executed = True
        summary.push_branch_refspec_only = True
        summary.push_follow_tags_requested = False
        push_res = _run_remote_push(repo, "push", "--no-follow-tags", remote, f"HEAD:{target_ref}")
        if push_res.returncode != 0:
            summary.push_result = "uncertain"
            summary.resume_required = True
            summary.resume_oid = expected_oid
            summary.resume_remote = remote
            raise FinalizerError(
                ErrorCode.PUSH_FAILED,
                "resume-publish push 失败或远端仍未包含待发布 commits",
                status="failed",
                final_phase="push",
                next_action="retry_same_resume",
                push_executed=True,
                push_result="uncertain",
                resume={"head_oid": expected_oid, "remote": remote},
            )
        summary.push_result = "succeeded"
        summary.completed_through = "push"
        summary.final_phase = "post_verify"
        l_after, u_after, ah_after, bh_after, r_after = _check_normal_remote_state(repo, remote, target_branch, phase_label="resume-publish 后 ", phase_name="post_verify")
        if l_after != expected_oid or u_after != expected_oid or r_after != expected_oid or ah_after != 0 or bh_after != 0:
            raise FinalizerError(ErrorCode.POST_VERIFY_FAILED, "resume-publish 验证后未对齐", status="failed", final_phase="post_verify")
        _assert_local_state_unchanged(repo, baseline, phase="post_verify", push_executed=True, push_result="succeeded")

    summary.head_after = expected_oid
    summary.head_unchanged = True
    summary.index_unchanged = True
    summary.worktree_unchanged = True
    summary.worktree_state = "clean"
    summary.final_head = expected_oid
    summary.final_remote_oid = expected_oid
    summary.post_verify = "passed"
    summary.remote_conclusion = "published_and_upstream_aligned"
    summary.completed_through = "post_verify"
    summary.mode_state = "verified"
    summary.status = "success"
    summary.exit_code = 0
    summary.next_action = "none"
    if not cli.summary and is_initial:
        for line in (
            "\n== Resume initial publish ==",
            f"repo: {repo}",
            "mode: resume-initial-publish",
            "publish kind: initial_root",
            f"branch: {info.branch}",
            f"expected HEAD: {expected_oid}",
            f"remote: {remote}",
            f"target ref: {branch_ref}",
            f"snapshot: {cli.snapshot or '(none)'}",
            f"recovery start: {summary.recovery_start or 'unknown'}",
            f"remote conclusion: {summary.remote_conclusion or 'unknown'}",
            "\n== Existing commit safety check ==",
            f"$ git rev-list --reverse {expected_oid}",
            f"root commit count: {summary.publish_commit_count}",
            f"publish range: {summary.publish_range or expected_oid}",
            "range safety checks: passed (all commits validated against clean-tree content & binary limits)",
            "snapshot verification: " + (summary.snapshot_verification or "not_requested"),
            "\n== Resume push ==",
            f"$ git push --no-follow-tags --set-upstream -- {remote} HEAD:{branch_ref}",
            f"push executed: {summary.push_executed}",
            f"push result: {summary.push_result}",
            "push branch_refspec_only: true",
            "push follow_tags_requested: false",
            "\n== Resume 最终验证 ==",
            f"$ git ls-remote --heads -- {remote}",
            f"head: {expected_oid}",
            f"remote ref ({branch_ref}): {expected_oid}",
            f"upstream: {summary.upstream or '(none)'}",
            f"$ git rev-parse --verify {branch_ref}",
            f"$ git rev-list --left-right --count HEAD...{summary.upstream or '@{upstream}'}",
            "0\t0",
            "local branch, tags, and Git config: unchanged",
            "index unchanged: true",
            "worktree state: clean",
            "post-verify: passed",
            "\nGit Finalizer 完成：initial publish 恢复已验证。",
        ):
            print(line, flush=True)
    elif not cli.summary:
        for line in (
            "\n== Resume existing commits ==",
            "mode: resume-publish",
            f"publish kind: {summary.resume_publish_kind}",
            f"pre-publish ahead/behind: {summary.publish_ahead}/{summary.publish_behind}",
            f"commit count: {summary.publish_commit_count}",
            "\n== Existing commit range 安全检查 ==",
            "range safety checks: passed",
            "\n== Resume publish 后验证 ==",
            "local branch, tags, and Git config: unchanged",
            "post-verify: passed",
            "\nGit Finalizer 完成：既有本地 commit range 已发布并完成远端验证。",
        ):
            print(line, flush=True)
    return 0, summary


def _run_sync_published_branch(cli: ParsedCLI, summary: SummaryState) -> tuple[int, SummaryState]:
    summary.mode = "sync_published_branch"
    summary.commit_reused = True
    summary.commit_result = "reused"
    info = repo_state.inspect_repository(cli.repo, merge_parent_arg=None, mode=cli.action, require_attached=True)
    repo = info.repo
    summary.repository = str(repo)
    _require_clean_repo_state(repo, info)
    policy = policy_mod.load_delivery_policy(repo)
    assert info.branch is not None and info.head_oid is not None

    published_oid = validate_oid(cli.head_oid or "", "published OID")
    expected_target_oid = validate_oid(cli.expected_target_oid or "", "expected target OID")
    _, branch_ref = _normalize_heads_ref(cli.remote_branch or "", "--remote-branch")
    source_remote = validate_remote_name(cli.source_remote or "")
    target_remote = validate_remote_name(cli.remote or "")
    if source_remote == target_remote:
        raise FinalizerError(ErrorCode.CLI_INVALID_ARGS, "sync source and target remote must differ", final_phase="cli")
    src_url = repo_state.validate_remote_endpoints(repo, source_remote)
    tgt_url = repo_state.validate_remote_endpoints(repo, target_remote)
    if src_url == tgt_url:
        raise FinalizerError(ErrorCode.CLI_INVALID_ARGS, "sync source and target endpoints must differ", final_phase="cli")

    summary.branch = info.branch
    summary.upstream = info.upstream
    summary.commit_sha = published_oid
    summary.head_before = info.head_oid
    summary.worktree_before_state = "clean"
    summary.sync_source_remote = source_remote
    summary.sync_expected_target_oid = expected_target_oid
    summary.push_target = f"{target_remote}:{branch_ref}"
    summary.push_branch_refspec_only = True
    summary.push_follow_tags_requested = False

    upstream_cfg_before = (
        tuple(_git_config_get_all(repo, f"branch.{info.branch}.remote")),
        tuple(_git_config_get_all(repo, f"branch.{info.branch}.merge")),
    )
    baseline = _snapshot_local_state(repo)
    summary.final_phase = "remote_preflight"
    src_refs, _ = _ls_remote_refs(repo, source_remote, branch_ref)
    if src_refs.get(branch_ref) != published_oid:
        raise FinalizerError(ErrorCode.REMOTE_DIVERGED, "sync source branch does not equal the exact published OID", final_phase="remote_preflight")
    tgt_refs, _ = _ls_remote_refs(repo, target_remote, branch_ref)
    target_before = tgt_refs.get(branch_ref)
    if target_before is None:
        raise FinalizerError(ErrorCode.REMOTE_MISSING, f"sync target branch 不存在: {branch_ref}", final_phase="remote_preflight")

    if target_before != published_oid:
        if target_before != expected_target_oid:
            raise FinalizerError(ErrorCode.REMOTE_DIVERGED, "sync live target differs from expected target OID", final_phase="remote_preflight")
        _fetch_remote(repo, source_remote, branch_ref, no_write_fetch_head=True, phase="remote_preflight")
        _fetch_remote(repo, target_remote, branch_ref, no_write_fetch_head=True, phase="remote_preflight")
        # Re-verify source, target, and local upstream config didn't drift during fetch before push.
        live_src, _ = _ls_remote_refs(repo, source_remote, branch_ref)
        live_tgt, _ = _ls_remote_refs(repo, target_remote, branch_ref)
        upstream_cfg_after = (
            tuple(_git_config_get_all(repo, f"branch.{info.branch}.remote")),
            tuple(_git_config_get_all(repo, f"branch.{info.branch}.merge")),
        )
        if live_src.get(branch_ref) != published_oid or live_tgt.get(branch_ref) != target_before or upstream_cfg_after != upstream_cfg_before:
            raise FinalizerError(ErrorCode.REMOTE_DIVERGED, "fetch 期间 source/target/upstream 发生漂移；未执行 push", final_phase="remote_preflight")
        anc = run_git(repo, "merge-base", "--is-ancestor", target_before, published_oid)
        if anc.returncode != 0:
            raise FinalizerError(
                ErrorCode.REMOTE_DIVERGED,
                "sync target is not an ancestor of the published source; no force or merge is allowed",
                final_phase="remote_preflight",
            )
        commits = _unpublished_commits(repo, published_oid, [target_before])
        summary.publish_commit_count = len(commits)
        summary.publish_range = f"{target_before}..{published_oid}"
        specs = (
            validation.load_fixture_exceptions(
                cli.fixture_exceptions,
                repo=repo,
                remote_url=tgt_url,
                local_only=False,
                phase="history",
                head_ref=published_oid,
                baseline_ref=target_before,
                final_phase="preflight",
            )
            if cli.fixture_exceptions
            else []
        )
        rng_out = validation.validate_commit_range(repo, info, commits, cli, policy, specs=specs, local_only=False)
        summary.fixture_exceptions.extend(rng_out.fixture_exceptions)
        summary.warnings.extend(rng_out.warnings)
        _assert_local_state_unchanged(repo, baseline, phase="remote_preflight")
        summary.completed_through = "preflight"
        summary.remote_conclusion = "published_source_fast_forward_ready"

        if cli.dry_run:
            summary.head_after = info.head_oid
            summary.head_unchanged = True
            summary.index_unchanged = True
            summary.worktree_unchanged = True
            summary.worktree_state = "clean"
            summary.final_head = info.head_oid
            summary.push_result = "skipped_by_dry_run"
            summary.post_verify = "skipped_by_dry_run"
            summary.status = "success"
            summary.exit_code = 0
            summary.next_action = "run_without_dry_run_after_review"
            return 0, summary

        summary.final_phase = "push"
        summary.push_executed = True
        summary.push_branch_refspec_only = True
        summary.push_follow_tags_requested = False
        push_res = _run_remote_push(repo, "push", "--no-follow-tags", "--", target_remote, f"{published_oid}:{branch_ref}")
        if push_res.returncode != 0:
            summary.push_result = "uncertain"
            post_check, _ = _ls_remote_refs(repo, target_remote, branch_ref)
            if post_check.get(branch_ref) == published_oid:
                summary.push_result = "confirmed_after_uncertain"
            else:
                raise FinalizerError(ErrorCode.SYNC_FAILED, "sync push 失败且远端结果未对齐", status="failed", final_phase="push", push_executed=True, push_result="uncertain")
        else:
            summary.push_result = "succeeded"
    else:
        if cli.fixture_exceptions:
            raise FinalizerError(ErrorCode.FIXTURE_EXCEPTION_BLOCKED, "already-aligned sync does not consume fixture exceptions", final_phase="remote_preflight")
        summary.push_result = "not_needed_already_aligned"

    summary.final_phase = "post_verify"
    post_src, _ = _ls_remote_refs(repo, source_remote, branch_ref)
    post_tgt, _ = _ls_remote_refs(repo, target_remote, branch_ref)
    if post_src.get(branch_ref) != published_oid or post_tgt.get(branch_ref) != published_oid:
        raise FinalizerError(ErrorCode.POST_VERIFY_FAILED, "post-verify: source 与 target live OID 不一致", status="failed", final_phase="post_verify")
    _assert_local_state_unchanged(repo, baseline, phase="post_verify", push_executed=summary.push_executed, push_result=summary.push_result)

    summary.head_after = info.head_oid
    summary.head_unchanged = True
    summary.index_unchanged = True
    summary.worktree_unchanged = True
    summary.worktree_state = "clean"
    summary.final_head = info.head_oid
    summary.final_remote_oid = published_oid
    summary.post_verify = "passed"
    summary.completed_through = "post_verify"
    summary.remote_conclusion = "source_and_target_exactly_aligned"
    summary.status = "success"
    summary.exit_code = 0
    summary.next_action = "none"
    return 0, summary


def _run_retire_branch(cli: ParsedCLI, summary: SummaryState) -> tuple[int, SummaryState]:
    is_local = cli.action == "retire-local-branch"
    op_name = "local" if is_local else "remote"
    summary.mode = "retire_local_branch" if is_local else "retire_remote_branch"
    summary.retirement_operation = "dry_run" if cli.dry_run else "delete"

    if is_local and not cli.retirement_plan_id:
        raise FinalizerError(
            ErrorCode.CLI_INVALID_ARGS,
            "retire-local-branch 必须提供 Controller --retirement-plan-id（通过 worktree-controller branch-retirement-plan 获取）",
            final_phase="cli",
        )
    if cli.retirement_plan_id is not None and not retirement_plan.PLAN_ID_RE.fullmatch(cli.retirement_plan_id):
        raise FinalizerError(
            ErrorCode.CLI_INVALID_ARGS,
            "--retirement-plan-id 要求 64 位小写十六进制 ID",
            final_phase="cli",
        )

    info = repo_state.inspect_repository(cli.repo, merge_parent_arg=None, mode=cli.action, require_attached=True)
    repo = info.repo
    summary.repository = str(repo)
    policy = policy_mod.load_delivery_policy(repo)
    assert info.branch is not None and info.head_oid is not None
    summary.branch = info.branch
    summary.head_before = info.head_oid

    raw_branch = cli.retire_branch or ""
    branch, feature_ref = _normalize_heads_ref(raw_branch, f"--retire-{op_name}-branch")
    if repo_state.is_protected_branch(branch, policy):
        raise FinalizerError(ErrorCode.BRANCH_PROTECTED, f"retire-{op_name}-branch 拒绝受保护分支: {branch}")

    remote = validate_remote_name(cli.remote or "")
    summary.retirement_remote = remote
    summary.retirement_branch = branch
    summary.retirement_ref = feature_ref

    raw_expected = cli.expected_local_oid if is_local else cli.expected_remote_oid
    expected_oid = validate_oid(raw_expected or "", f"expected_{op_name}_oid")
    summary.retirement_expected_oid = expected_oid

    expected_integrated_oid = (
        validate_oid(cli.expected_integrated_oid, "expected_integrated_oid")
        if cli.expected_integrated_oid
        else None
    )
    if is_local and not expected_integrated_oid:
        raise FinalizerError(ErrorCode.CLI_INVALID_ARGS, "retire-local-branch 要求 --expected-integrated-oid", final_phase="cli")

    int_branch, integration_ref = _normalize_integration_ref(cli.integrated_into or "main", remote)
    summary.retirement_integrated_ref = integration_ref
    if feature_ref == integration_ref:
        raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, "feature ref 与 integration target 不能相同")

    # CI verification evidence validation
    summary.retirement_ci_required = cli.ci_required
    summary.retirement_ci_status = cli.ci_status
    summary.retirement_ci_commit_oid = cli.ci_commit_oid
    summary.retirement_ci_source = cli.ci_verification_source
    summary.retirement_ci_verified_at = cli.ci_verified_at

    summary.retirement_plan_id = cli.retirement_plan_id

    plan_args: argparse.Namespace | None = None
    if cli.retirement_plan_id:
        plan_args = argparse.Namespace(
            repo=str(repo),
            plan_id=cli.retirement_plan_id,
            operation=op_name,
            branch=branch,
            remote=remote,
            integrated_into=cli.integrated_into or "main",
            expected_oid=expected_oid,
            expected_integrated_oid=expected_integrated_oid or "",
            recover=not cli.dry_run,
        )
        try:
            retirement_plan.verify_capabilities(repo)
            retirement_plan.preflight(plan_args)
        except retirement_plan.RetirementBlocked as exc:
            raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, str(exc), final_phase="preflight") from exc

    _require_clean_repo_state(repo, info)
    summary.worktree_before_state = "clean"
    baseline = _snapshot_local_state(repo)
    other_local_before = _snapshot_other_local_heads(repo, excluded_ref=feature_ref)

    repo_state.validate_remote_endpoints(repo, remote)
    remote_default_ref = _query_remote_default_branch(repo, remote)
    if feature_ref == remote_default_ref:
        raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, f"retire-{op_name}-branch 拒绝 remote default branch: {branch}")

    summary.final_phase = "remote_preflight"
    _fetch_remote(repo, remote, prune=True, phase="remote_preflight")
    live_refs, _ = _ls_remote_refs(repo, remote, feature_ref, integration_ref)
    if integration_ref not in live_refs:
        raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, "live remote integration target 不存在", final_phase="remote_preflight")
    live_int_oid = live_refs[integration_ref]
    if expected_integrated_oid and live_int_oid != expected_integrated_oid:
        raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, "live integration target OID 与 expected_integrated_oid 不一致", final_phase="remote_preflight")
    summary.retirement_integrated_oid = live_int_oid

    if plan_args is not None and not plan_args.expected_integrated_oid:
        plan_args.expected_integrated_oid = live_int_oid

    if cli.ci_required:
        if cli.ci_status != "SUCCESS" or cli.ci_verification_source not in {"tool_authenticated", "human_authenticated_ui"}:
            raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, "repository policy 要求 CI SUCCESS (effective evidence required)", final_phase="remote_preflight")
        ci_oid = validate_oid(cli.ci_commit_oid or "", "ci_commit_oid")
        if ci_oid != live_int_oid:
            raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, "ci_commit_oid 与 live integration target OID 不一致", final_phase="remote_preflight")
    elif cli.ci_status not in (None, "NOT_REQUIRED") or cli.ci_verification_source not in (None, "not_required") or cli.ci_commit_oid or cli.ci_verified_at:
        raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, "未启用 --ci-required 时 CI evidence 必须为 not_required", final_phase="remote_preflight")

    # Verify ancestry against integration target & check no local worktree / local-only commits block retirement.
    live_remote_feature_oid = live_refs.get(feature_ref)
    if is_local and live_remote_feature_oid and live_remote_feature_oid != expected_oid:
        raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, "live remote feature OID 与 expected_local_oid 不一致", final_phase="remote_preflight")
    if not is_local and live_remote_feature_oid and live_remote_feature_oid != expected_oid:
        raise FinalizerError(
            ErrorCode.RETIREMENT_BLOCKED,
            f"live remote feature OID 与 expected_remote_oid 不一致（与 remote feature OID 不一致: {expected_oid}）",
            final_phase="remote_preflight",
        )

    if live_remote_feature_oid or is_local or cli.retirement_plan_id:
        anc_res = run_git(repo, "merge-base", "--is-ancestor", expected_oid, live_int_oid)
        if anc_res.returncode != 0:
            raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, "expected feature OID 不是 integration target OID 的 ancestor", final_phase="remote_preflight")
        summary.retirement_ancestry_verified = True

    wt_list = run_git(repo, "worktree", "list", "--porcelain")
    if wt_list.returncode != 0 or f"branch {feature_ref}" in wt_list.stdout.decode("utf-8", errors="replace").splitlines():
        raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, "目标 branch 仍被一个 local worktree checkout", final_phase="remote_preflight")

    # Verify local ref does not contain local-only commits or mismatch expected OID.
    local_oid = _show_ref_hash(repo, feature_ref, label="local feature ref")
    if local_oid is not None:
        ahead_local = int(run_git(repo, "rev-list", "--count", f"{expected_oid}..{feature_ref}").stdout.decode("ascii").strip() or "0")
        if ahead_local > 0 or local_oid != expected_oid:
            raise FinalizerError(
                ErrorCode.RETIREMENT_BLOCKED,
                "local feature branch 存在 local-only commit，或 local branch OID 与 remote feature OID 不一致",
                final_phase="remote_preflight",
            )

    # Reject if another local branch uses `refs/remotes/<remote>/<branch>` as upstream.
    target_tracking = f"refs/remotes/{remote}/{branch}"
    up_refs_out = run_git(repo, "for-each-ref", "--format=%(refname)\t%(upstream)", "refs/heads")
    for line in up_refs_out.stdout.decode("utf-8", errors="replace").splitlines():
        b_ref, _, u_ref = line.partition("\t")
        if u_ref == target_tracking and b_ref != feature_ref:
            raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, "其他 local branch 仍把目标 remote ref 作为 upstream", final_phase="remote_preflight")

    already_absent = (local_oid is None) if is_local else (live_remote_feature_oid is None)
    if already_absent:
        if plan_args is not None and not cli.dry_run:
            try:
                res_doc = retirement_plan.execute(plan_args)
                summary.retirement_receipt_id = res_doc.get("receipt_id")
                summary.retirement_lease_bound = bool(res_doc.get("expected_oid_lease_bound", False))
            except retirement_plan.RetirementBlocked as exc:
                raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, str(exc), status="failed", final_phase="post_verify") from exc
        else:
            summary.retirement_lease_bound = False
        summary.retirement_result = "ALREADY_ABSENT_VERIFIED"
        summary.retirement_local_absent_after = True if is_local else summary.retirement_local_absent_after
        summary.retirement_remote_absent_after = True if not is_local else (live_remote_feature_oid is None)
        summary.remote_conclusion = "local_branch_already_absent_verified" if is_local else "already_absent_verified"
        summary.post_verify = "passed"
        summary.mode_state = "verified"
        summary.completed_through = "post_verify"
        summary.status = "success"
        summary.exit_code = 0
        summary.next_action = "none"
        return 0, summary

    if cli.dry_run:
        _assert_local_state_unchanged(repo, baseline, phase="preflight")
        summary.retirement_local_absent_after = False if is_local else None
        summary.retirement_remote_absent_after = False if not is_local else (live_remote_feature_oid is None)
        summary.retirement_result = "RETIREMENT_PREFLIGHT_PASSED"
        summary.remote_conclusion = "retirement_preflight_passed"
        summary.post_verify = "skipped_by_dry_run"
        summary.mode_state = "dry_run_complete"
        summary.completed_through = "preflight"
        summary.status = "success"
        summary.exit_code = 0
        summary.next_action = (
            "run_without_dry_run_after_review" if is_local else "publish_install_verify_then_run_without_dry_run"
        )
        return 0, summary

    # Execute CAS delete (Controller-authorized if --retirement-plan-id, else direct Git CAS).
    summary.final_phase = "local_mutation" if is_local else "remote_mutation"
    summary.push_branch_refspec_only = True if not is_local else None
    summary.push_follow_tags_requested = False if not is_local else None
    if plan_args is not None:
        prog: dict[str, Any] = {
            "mutation_attempted": False,
            "controller_complete": False,
            "authorization_id": None,
            "expected_oid_lease_bound": False,
        }
        try:
            exec_res = retirement_plan.execute(plan_args, progress=prog)
        except retirement_plan.RetirementBlocked as exc:
            summary.retirement_lease_bound = bool(prog.get("expected_oid_lease_bound"))
            summary.retirement_result = f"{'LOCAL' if is_local else 'REMOTE'}_DELETE_UNVERIFIED"
            raise FinalizerError(
                ErrorCode.RETIREMENT_BLOCKED,
                str(exc),
                status="failed" if prog.get("mutation_attempted") else "blocked",
                final_phase=summary.final_phase,
                next_action=exc.next_action or "inspect_remote_state_then_rerun_same_retirement",
            ) from exc
        summary.retirement_lease_bound = bool(exec_res.get("expected_oid_lease_bound", True))
        summary.retirement_receipt_id = exec_res.get("receipt_id")
        if not is_local:
            summary.push_executed = bool(exec_res.get("mutation_attempted", True))
            summary.push_result = "succeeded"
    else:
        summary.retirement_lease_bound = True
        if is_local:
            del_res = run_git(repo, "update-ref", "-d", feature_ref, expected_oid)
            if del_res.returncode != 0:
                summary.retirement_result = "LOCAL_DELETE_UNVERIFIED"
                raise FinalizerError(ErrorCode.RETIREMENT_BLOCKED, "lease-bound local update-ref 删除被拒绝", status="failed", final_phase="local_mutation")
        else:
            summary.push_executed = True
            del_res = _run_remote_push(
                repo,
                "push",
                "--no-follow-tags",
                f"--force-with-lease={feature_ref}:{expected_oid}",
                "--",
                remote,
                f":{feature_ref}",
            )
            if del_res.returncode != 0:
                summary.push_result = "uncertain"
                summary.retirement_result = "REMOTE_DELETE_UNVERIFIED"
                raise FinalizerError(
                    ErrorCode.RETIREMENT_BLOCKED,
                    "lease-bound remote delete 被拒绝或结果无法验证",
                    status="failed",
                    final_phase="remote_mutation",
                    next_action="inspect_remote_state_then_rerun_same_retirement",
                    push_executed=True,
                    push_result="uncertain",
                )
            summary.push_result = "succeeded"

    summary.final_phase = "post_verify"
    summary.retirement_result = "LOCAL_DELETE_UNVERIFIED" if is_local else "REMOTE_DELETE_UNVERIFIED"
    _fetch_remote(repo, remote, prune=True, phase="post_verify")
    post_live, _ = _ls_remote_refs(repo, remote, feature_ref, integration_ref)
    if post_live.get(integration_ref) != live_int_oid:
        raise FinalizerError(ErrorCode.POST_VERIFY_FAILED, "post-delete integration target 缺失或 OID 发生变化", status="failed", final_phase="post_verify")
    if is_local:
        if _show_ref_hash(repo, feature_ref, label="post-delete local ref") is not None:
            raise FinalizerError(ErrorCode.POST_VERIFY_FAILED, "post-delete local feature ref 仍存在", status="failed", final_phase="post_verify")
        if _snapshot_other_local_heads(repo, excluded_ref=feature_ref) != other_local_before:
            raise FinalizerError(ErrorCode.POST_VERIFY_FAILED, "retirement 前后其他 local branch refs 发生变化", status="failed", final_phase="post_verify")
        summary.retirement_local_absent_after = True
        summary.retirement_remote_absent_after = feature_ref not in post_live
    else:
        if feature_ref in post_live or _show_ref_hash(repo, target_tracking, label="post-delete tracking ref") is not None:
            raise FinalizerError(ErrorCode.POST_VERIFY_FAILED, "post-delete remote 或 tracking feature ref 仍存在", status="failed", final_phase="post_verify")
        summary.retirement_remote_absent_after = True
        summary.retirement_remote_tracking_absent = True
    _assert_local_state_unchanged(repo, baseline, phase="post_verify")

    summary.head_after = info.head_oid
    summary.head_unchanged = True
    summary.index_unchanged = True
    summary.worktree_unchanged = True
    summary.worktree_state = "clean"
    summary.retirement_tags_unchanged = True
    summary.post_verify = "passed"
    summary.retirement_result = "LOCAL_BRANCH_RETIRED_VERIFIED" if is_local else "REMOTE_BRANCH_RETIRED_VERIFIED"
    summary.remote_conclusion = "local_branch_retired_verified" if is_local else "remote_branch_retired_verified"
    summary.completed_through = "post_verify"
    summary.mode_state = "verified"
    summary.status = "success"
    summary.exit_code = 0
    summary.next_action = "none"
    return 0, summary


def run_delivery(
    cli: ParsedCLI,
    *,
    entrypoint: Path | None = None,
    summary: SummaryState | None = None,
) -> tuple[int, SummaryState]:
    """Execute one parsed delivery operation through its dedicated workflow."""
    summary_state = summary if summary is not None else create_summary(cli, entrypoint=entrypoint)
    if cli.action in {"publish-existing-branch", "publish-existing-history", "resume-existing-history-publish"}:
        rc, out = _run_publish_existing_branch_or_history(cli, summary_state)
    elif cli.action in {"resume-publish", "resume-initial-publish"}:
        rc, out = _run_resume_publish_or_initial(cli, summary_state)
    elif cli.action == "sync-published-branch":
        rc, out = _run_sync_published_branch(cli, summary_state)
    elif cli.action in {"retire-local-branch", "retire-remote-branch"}:
        rc, out = _run_retire_branch(cli, summary_state)
    else:
        rc, out = _run_commit_delivery(cli, summary_state)
    if rc == 0:
        out.final_phase = "complete"
    return rc, out
