"""Repository topology, branch state, protected refs, and remote endpoint verification."""

from __future__ import annotations

import dataclasses
from pathlib import Path, PurePosixPath
import posixpath
from urllib.parse import urlparse

from git_finalizer.delivery.policy import DeliveryPolicy
from git_finalizer.errors import ErrorCode, FinalizerError
from git_finalizer.git_io import (
    DEFAULT_REMOTE_GIT_TIMEOUT,
    parse_nul_records,
    run_git,
    validate_oid,
    validate_remote_name,
)

STATIC_PROTECTED_EXACT = frozenset(
    {"main", "master", "trunk", "production", "HEAD", "rescue", "archive", "recovery"}
)
STATIC_PROTECTED_PREFIXES = ("rescue/", "archive/", "recovery/")


@dataclasses.dataclass
class RepoInfo:
    repo: Path
    git_dir: Path
    common_dir: Path
    is_unborn: bool
    head_oid: str | None
    branch: str | None
    upstream: str | None
    merge_parent: str | None


def is_protected_branch(branch: str, policy: DeliveryPolicy | None = None) -> bool:
    if branch in STATIC_PROTECTED_EXACT or branch.startswith(STATIC_PROTECTED_PREFIXES):
        return True
    if policy is not None:
        import fnmatch

        for pattern in policy.extra_protected_branches:
            if fnmatch.fnmatchcase(branch, pattern):
                return True
    return False


def redact_endpoint(url: str) -> str:
    if "@" not in url:
        return url
    parsed = urlparse(url)
    if parsed.scheme and (parsed.username is not None or parsed.password is not None):
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return parsed._replace(netloc=f"<redacted>@{host}{port}").geturl()
    if "://" not in url and ":" in url and "@" in url:
        _, _, rest = url.partition("@")
        return f"<redacted>@{rest}"
    return url


def _pending_operation_check(
    repo: Path,
    git_dir: Path,
    merge_parent_arg: str | None,
    mode: str,
    head_oid: str | None = None,
) -> str | None:
    for marker in ("CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "BISECT_START", "BISECT_LOG"):
        p = git_dir / marker
        if p.exists() or p.is_symlink():
            raise FinalizerError(
                ErrorCode.REPO_INVALID,
                f"仓库存在未完成的 Git 操作（{marker}）",
                final_phase="preflight",
            )
    for directory in ("rebase-merge", "rebase-apply", "sequencer"):
        p = git_dir / directory
        if p.exists() or p.is_symlink():
            raise FinalizerError(
                ErrorCode.REPO_INVALID,
                f"仓库存在未完成的 Git 操作（{directory}）",
                final_phase="preflight",
            )
    merge_head_file = git_dir / "MERGE_HEAD"
    if merge_parent_arg is None:
        if merge_head_file.exists() or merge_head_file.is_symlink():
            raise FinalizerError(
                ErrorCode.REPO_INVALID,
                "仓库存在未完成的 Git 操作（MERGE_HEAD）；请先完成或中止合并，或仅在 verify-only/commit-only 下传 --merge-parent",
                final_phase="preflight",
            )
        return None

    if mode not in {"verify-only", "commit-only"}:
        raise FinalizerError(
            ErrorCode.CLI_INVALID_ARGS,
            "--merge-parent 仅允许配合 --mode verify-only 或 --mode commit-only 使用",
            final_phase="cli",
        )
    merge_parent_oid = validate_oid(merge_parent_arg, "--merge-parent")
    if head_oid and merge_parent_oid == head_oid:
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            "--merge-parent 不能与 HEAD 相同",
            final_phase="preflight",
        )
    if merge_head_file.is_symlink() or not merge_head_file.is_file():
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            "使用 --merge-parent 要求仓库处于有效的普通 MERGE_HEAD 状态",
            final_phase="preflight",
        )
    raw_merge = merge_head_file.read_bytes()
    if len(raw_merge) > 256 or raw_merge != f"{merge_parent_oid}\n".encode("ascii"):
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            "MERGE_HEAD 与传入的 --merge-parent 不一致或格式非法",
            final_phase="preflight",
        )
    parent_chk = run_git(repo, "cat-file", "-e", f"{merge_parent_oid}^{{commit}}")
    if parent_chk.returncode != 0:
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            "--merge-parent 不是有效 commit 对象",
            final_phase="preflight",
        )
    unmerged = run_git(repo, "ls-files", "--unmerged", "-z")
    if unmerged.returncode != 0 or unmerged.stdout:
        raise FinalizerError(
            ErrorCode.INDEX_UNCLEAN,
            "仓库仍存在未解决的冲突 unmerged index entries",
            final_phase="preflight",
        )
    return merge_parent_oid


def inspect_repository(
    repo_arg: str | None,
    *,
    merge_parent_arg: str | None = None,
    mode: str = "normal",
    require_attached: bool = True,
) -> RepoInfo:
    if not repo_arg:
        raise FinalizerError(
            ErrorCode.CLI_INVALID_ARGS,
            "必须通过 --repo 指定 Git worktree 根目录",
            final_phase="cli",
        )
    raw_repo = Path(repo_arg)
    if not raw_repo.exists():
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            f"--repo 不是 Git worktree: {repo_arg}",
            final_phase="preflight",
        )
    resolved_repo = raw_repo.resolve()
    inside = run_git(resolved_repo, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.decode("ascii", errors="replace").strip() != "true":
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            f"--repo 不是 Git worktree: {repo_arg}",
            final_phase="preflight",
        )
    top_res = run_git(resolved_repo, "rev-parse", "--show-toplevel")
    if top_res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, "无法解析 --show-toplevel", final_phase="preflight")
    top_resolved = Path(top_res.stdout.decode("utf-8", errors="strict").strip()).resolve()
    if top_resolved != resolved_repo:
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            f"--repo 必须是 Git worktree 根目录（实际根目录为 {top_resolved}）",
            final_phase="preflight",
        )
    dirs_res = run_git(resolved_repo, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    if dirs_res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, "无法解析 .git 目录", final_phase="preflight")
    dir_lines = dirs_res.stdout.decode("utf-8", errors="strict").splitlines()
    if len(dir_lines) != 2:
        raise FinalizerError(ErrorCode.REPO_INVALID, "git-dir / git-common-dir 输出异常", final_phase="preflight")
    git_dir = Path(dir_lines[0]).resolve()
    common_dir = Path(dir_lines[1]).resolve()

    sym = run_git(resolved_repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch: str | None = sym.stdout.decode("utf-8", errors="strict").strip() if sym.returncode == 0 else None
    if require_attached and not branch:
        raise FinalizerError(
            ErrorCode.DETACHED_HEAD,
            "要求 symbolic attached branch（当前处于 detached HEAD）",
            final_phase="preflight",
        )

    head_res = run_git(resolved_repo, "rev-parse", "--verify", "--quiet", "HEAD")
    if head_res.returncode == 0:
        is_unborn = False
        head_oid: str | None = head_res.stdout.decode("ascii").strip()
    elif head_res.returncode == 1 or head_res.returncode == 128:
        # Distinguish unborn HEAD from corrupt objects cleanly (`show-ref --verify --quiet` exits 1 when missing).
        ref_chk = run_git(resolved_repo, "show-ref", "--verify", "--quiet", "--", f"refs/heads/{branch}") if branch else None
        if ref_chk is not None and ref_chk.returncode == 0:
            raise FinalizerError(
                ErrorCode.REPO_INVALID,
                "HEAD 引用存在但对象已损坏",
                status="failed",
                final_phase="preflight",
            )
        if ref_chk is not None and ref_chk.returncode not in (0, 1):
            raise FinalizerError(
                ErrorCode.REPO_INVALID,
                "无法读取分支引用状态",
                status="failed",
                final_phase="preflight",
            )
        is_unborn = True
        head_oid = None
    else:
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            "读取 HEAD 失败",
            status="failed",
            final_phase="preflight",
        )

    merge_parent = _pending_operation_check(resolved_repo, git_dir, merge_parent_arg, mode, head_oid=head_oid)
    upstream = None
    if not is_unborn and branch:
        up_res = run_git(resolved_repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        if up_res.returncode == 0:
            upstream = up_res.stdout.decode("utf-8", errors="strict").strip() or None

    return RepoInfo(
        repo=resolved_repo,
        git_dir=git_dir,
        common_dir=common_dir,
        is_unborn=is_unborn,
        head_oid=head_oid,
        branch=branch,
        upstream=upstream,
        merge_parent=merge_parent,
    )


def resolve_configured_upstream(repo: Path, branch: str) -> tuple[str, str]:
    """Fail-closed upstream remote and branch resolution (fixes `git config || true` bugs)."""
    rem_cfg = run_git(repo, "config", "--get-all", f"branch.{branch}.remote")
    if rem_cfg.returncode == 1:
        raise FinalizerError(
            ErrorCode.UPSTREAM_CONFLICT,
            f"当前分支 {branch} 未配置 upstream remote",
            final_phase="preflight",
        )
    if rem_cfg.returncode != 0:
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            f"读取 branch.{branch}.remote 配置失败 (exit {rem_cfg.returncode})",
            status="failed",
            final_phase="preflight",
        )
    remotes = [ln.strip() for ln in rem_cfg.stdout.decode("utf-8").splitlines() if ln.strip()]
    if len(remotes) != 1 or remotes[0] == ".":
        raise FinalizerError(
            ErrorCode.UPSTREAM_CONFLICT,
            f"当前分支 {branch} 的 upstream remote 配置含糊或无效",
            final_phase="preflight",
        )
    remote = validate_remote_name(remotes[0])

    merge_cfg = run_git(repo, "config", "--get-all", f"branch.{branch}.merge")
    if merge_cfg.returncode == 1:
        raise FinalizerError(
            ErrorCode.UPSTREAM_CONFLICT,
            f"当前分支 {branch} 未配置 upstream merge ref",
            final_phase="preflight",
        )
    if merge_cfg.returncode != 0:
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            f"读取 branch.{branch}.merge 配置失败 (exit {merge_cfg.returncode})",
            status="failed",
            final_phase="preflight",
        )
    merges = [ln.strip() for ln in merge_cfg.stdout.decode("utf-8").splitlines() if ln.strip()]
    if len(merges) != 1 or not merges[0].startswith("refs/heads/"):
        raise FinalizerError(
            ErrorCode.UPSTREAM_CONFLICT,
            f"当前分支 {branch} 的 upstream merge ref 配置无效",
            final_phase="preflight",
        )
    target_branch = merges[0].removeprefix("refs/heads/")

    # Fail closed on conflicting pushRemote / pushDefault configuration.
    branch_push_remote: str | None = None
    bpr_res = run_git(repo, "config", "--null", "--get-all", f"branch.{branch}.pushRemote")
    if bpr_res.returncode not in (0, 1):
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            f"读取 branch.{branch}.pushRemote 配置失败 (exit {bpr_res.returncode})",
            status="failed",
            final_phase="preflight",
        )
    if bpr_res.returncode == 0:
        bpr_vals = parse_nul_records(bpr_res.stdout)
        if len(bpr_vals) != 1 or not bpr_vals[0].strip():
            raise FinalizerError(
                ErrorCode.UPSTREAM_CONFLICT,
                f"branch.{branch}.pushRemote 配置含糊，无法证明唯一 push 目标",
                final_phase="preflight",
            )
        branch_push_remote = bpr_vals[0].strip()

    default_push_remote: str | None = None
    dpr_res = run_git(repo, "config", "--null", "--get-all", "remote.pushDefault")
    if dpr_res.returncode not in (0, 1):
        raise FinalizerError(
            ErrorCode.REPO_INVALID,
            f"读取 remote.pushDefault 配置失败 (exit {dpr_res.returncode})",
            status="failed",
            final_phase="preflight",
        )
    if dpr_res.returncode == 0:
        dpr_vals = parse_nul_records(dpr_res.stdout)
        if len(dpr_vals) != 1 or not dpr_vals[0].strip():
            raise FinalizerError(
                ErrorCode.UPSTREAM_CONFLICT,
                "remote.pushDefault 配置含糊，无法证明唯一 push 目标",
                final_phase="preflight",
            )
        default_push_remote = dpr_vals[0].strip()

    effective_push_remote = branch_push_remote or default_push_remote or remote
    if effective_push_remote != remote:
        raise FinalizerError(
            ErrorCode.UPSTREAM_CONFLICT,
            f"实际 push remote 与 upstream remote 不一致 ({effective_push_remote} != {remote})",
            final_phase="preflight",
        )

    validate_remote_endpoints(repo, remote)
    return remote, target_branch


def resolve_upstream_for_resume(repo: Path, branch: str) -> tuple[str, str]:
    """Resolve existing upstream cleanly via `for-each-ref` + `rev-parse` without invoking `git config`."""
    head_ref = f"refs/heads/{branch}"
    fer = run_git(
        repo,
        "for-each-ref",
        "--format=%(upstream:short)\t%(upstream:remotename)\t%(upstream:remoteref)",
        head_ref,
    )
    if fer.returncode != 0:
        raise FinalizerError(
            ErrorCode.UPSTREAM_CONFLICT,
            "无法解析当前分支 configured upstream",
            final_phase="preflight",
        )
    lines = [ln for ln in fer.stdout.decode("utf-8", errors="replace").splitlines() if ln]
    if len(lines) != 1:
        raise FinalizerError(
            ErrorCode.UPSTREAM_CONFLICT,
            f"当前分支 {branch} 不存在唯一可解析的 upstream",
            final_phase="preflight",
        )
    parts = lines[0].split("\t")
    if len(parts) != 3:
        raise FinalizerError(ErrorCode.UPSTREAM_CONFLICT, "configured upstream 格式异常", final_phase="preflight")
    short_name, remote_raw, merge_ref = parts
    if not short_name or not remote_raw or remote_raw == "." or not merge_ref.startswith("refs/heads/"):
        raise FinalizerError(
            ErrorCode.UPSTREAM_CONFLICT,
            "configured upstream 不是可验证的远端 branch",
            final_phase="preflight",
        )
    remote = validate_remote_name(remote_raw)
    target_branch = merge_ref.removeprefix("refs/heads/")
    up_oid_res = run_git(repo, "rev-parse", "--verify", "@{upstream}^{commit}")
    full_ref = f"refs/remotes/{remote}/{target_branch}"
    tr_oid_res = run_git(repo, "rev-parse", "--verify", f"{full_ref}^{{commit}}")
    if (
        up_oid_res.returncode != 0
        or tr_oid_res.returncode != 0
        or up_oid_res.stdout.strip() != tr_oid_res.stdout.strip()
    ):
        raise FinalizerError(
            ErrorCode.UPSTREAM_CONFLICT,
            "configured upstream 无法解析或 tracking ref 不一致",
            final_phase="preflight",
        )
    validate_remote_endpoints(repo, remote)
    return remote, target_branch


def validate_remote_endpoints(repo: Path, remote: str) -> str:
    """Ensure `remote` exists with a single identical fetch/push URL."""
    validate_remote_name(remote)
    fetch = run_git(repo, "remote", "get-url", "--all", remote)
    if fetch.returncode != 0:
        raise FinalizerError(
            ErrorCode.REMOTE_MISSING,
            f"未配置指定 remote: {remote}",
            final_phase="preflight",
        )
    fetch_urls = [ln for ln in fetch.stdout.decode("utf-8").splitlines() if ln]
    if len(fetch_urls) != 1:
        raise FinalizerError(
            ErrorCode.REMOTE_MISSING,
            f"remote {remote} 包含多个 fetch endpoint 或未配置 URL",
            final_phase="preflight",
        )
    push = run_git(repo, "remote", "get-url", "--push", "--all", remote)
    if push.returncode != 0:
        raise FinalizerError(
            ErrorCode.REMOTE_MISSING,
            f"无法读取 remote {remote} 的 push endpoint",
            final_phase="preflight",
        )
    push_urls = [ln for ln in push.stdout.decode("utf-8").splitlines() if ln]
    if len(push_urls) != 1:
        raise FinalizerError(
            ErrorCode.REMOTE_MISSING,
            f"remote {remote} 包含多个 push endpoint",
            final_phase="preflight",
        )
    if fetch_urls[0] != push_urls[0]:
        raise FinalizerError(
            ErrorCode.UPSTREAM_CONFLICT,
            f"remote {remote} 的 fetch 与 push endpoint 不一致",
            final_phase="preflight",
        )
    return fetch_urls[0]


def list_remote_refs(repo: Path, remote: str) -> dict[str, str]:
    """Query `git ls-remote` safely with timeout and `--` separator."""
    validate_remote_name(remote)
    completed = run_git(repo, "ls-remote", "--", remote, timeout=DEFAULT_REMOTE_GIT_TIMEOUT)
    if completed.returncode != 0:
        raise FinalizerError(
            ErrorCode.REMOTE_UNAVAILABLE,
            f"无法访问远端 {remote}",
            final_phase="remote_verify",
        )
    refs: dict[str, str] = {}
    for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        oid, sep, ref = line.partition("\t")
        if not sep:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            oid, ref = parts
        refs[ref] = oid
    return refs


def _is_staged_deletion(
    repo: Path, rel_path: str, *, is_unborn: bool, env: dict[str, str] | None = None
) -> bool:
    if is_unborn:
        return False
    return (
        run_git(
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
            rel_path,
            env=env,
        ).returncode
        == 1
    )


def validate_explicit_paths(
    repo: Path,
    raw_paths: list[str],
    *,
    is_unborn: bool,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Validate explicit repository-relative paths, deduplicating against duplicates/symlinks/out-of-scope index."""
    if not raw_paths:
        raise FinalizerError(
            ErrorCode.SCOPE_INVALID,
            "必须在 `--` 后显式列出至少一个 repository-relative 文件路径",
            final_phase="cli",
        )

    repo_str = str(repo)
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        if not raw or raw in {".", ".."} or "\0" in raw or any(ch in raw for ch in ("\n", "\r", "\t")):
            raise FinalizerError(ErrorCode.SCOPE_INVALID, f"非法文件路径: {raw!r}", final_phase="preflight")

        # Support both repository-relative paths (`./a`, `dir/a`) and absolute paths inside `repo`.
        candidate_raw = raw if raw.startswith("/") else f"{repo_str}/{raw}"
        lexical = Path(posixpath.normpath(candidate_raw))
        if lexical == repo:
            raise FinalizerError(ErrorCode.SCOPE_INVALID, "不能把 repo 根目录作为提交文件", final_phase="preflight")
        try:
            rel_path = lexical.relative_to(repo).as_posix()
        except ValueError as exc:
            raise FinalizerError(ErrorCode.SCOPE_INVALID, f"文件不在 repo 内: {raw}", final_phase="preflight") from exc

        posix = PurePosixPath(rel_path)
        if not rel_path or rel_path in {".", ".."} or ".." in posix.parts or ".git" in posix.parts:
            raise FinalizerError(
                ErrorCode.SCOPE_INVALID,
                f"路径必须是 repository-relative 普通文件路径（禁止 Git 元数据）: {raw}",
                final_phase="preflight",
            )
        if rel_path in seen:
            raise FinalizerError(ErrorCode.SCOPE_INVALID, f"路径列表包含重复项: {rel_path}", final_phase="preflight")
        seen.add(rel_path)

        target = repo / rel_path
        if target.is_symlink():
            # Allow tracked/staged symlinks living inside `repo`, while still blocking directory traversal outside `repo`.
            try:
                target.parent.resolve(strict=False).relative_to(repo)
            except ValueError as exc:
                raise FinalizerError(
                    ErrorCode.SCOPE_INVALID,
                    f"symlink 父目录解析后不在 repo 内: {raw}",
                    final_phase="preflight",
                ) from exc
        else:
            try:
                resolved = target.resolve(strict=False)
                resolved.relative_to(repo)
            except (OSError, ValueError) as exc:
                raise FinalizerError(
                    ErrorCode.SCOPE_INVALID,
                    f"文件经符号链接解析后不在 repo 内: {raw}",
                    final_phase="preflight",
                ) from exc
            if target.is_dir():
                raise FinalizerError(ErrorCode.SCOPE_INVALID, f"只接受显式文件，不能指定目录: {raw}", final_phase="preflight")
            if not target.exists():
                tracked = run_git(repo, "--literal-pathspecs", "ls-files", "-z", "--", rel_path, env=env)
                staged_del = _is_staged_deletion(repo, rel_path, is_unborn=is_unborn, env=env)
                if (tracked.returncode != 0 or not parse_nul_records(tracked.stdout)) and not staged_del:
                    raise FinalizerError(ErrorCode.SCOPE_INVALID, f"文件不存在且不是 tracked deletion: {raw}", final_phase="preflight")
            elif not target.is_file():
                raise FinalizerError(ErrorCode.SCOPE_INVALID, f"目标路径不是普通文件: {raw}", final_phase="preflight")
            else:
                ig = run_git(repo, "check-ignore", "--quiet", "--no-index", "--", rel_path, env=env)
                if ig.returncode == 0:
                    tracked = run_git(repo, "--literal-pathspecs", "ls-files", "-z", "--", rel_path, env=env)
                    staged_del = _is_staged_deletion(repo, rel_path, is_unborn=is_unborn, env=env)
                    if not parse_nul_records(tracked.stdout) and not staged_del:
                        raise FinalizerError(
                            ErrorCode.SCOPE_INVALID,
                            f"禁止提交被 .gitignore 忽略的未跟踪文件: {rel_path}",
                            final_phase="preflight",
                        )
        cleaned.append(rel_path)

    # Reject any pre-existing staged changes outside the requested explicit path set.
    if is_unborn:
        staged_res = run_git(repo, "ls-files", "--cached", "-z", env=env)
        staged_paths = parse_nul_records(staged_res.stdout) if staged_res.returncode == 0 else []
    else:
        staged_res = run_git(repo, "diff", "--cached", "--name-only", "-z", env=env)
        if staged_res.returncode != 0:
            raise FinalizerError(ErrorCode.REPO_INVALID, "无法检查已有 staged index 状态", final_phase="preflight")
        staged_paths = parse_nul_records(staged_res.stdout)

    extra_staged = [p for p in staged_paths if p not in seen]
    if extra_staged:
        raise FinalizerError(
            ErrorCode.INDEX_UNCLEAN,
            f"index 中存在超出本次范围的已暂存改动: {', '.join(extra_staged[:5])}",
            final_phase="preflight",
        )
    return cleaned


def working_tree_dirty_status(repo: Path, env: dict[str, str] | None = None) -> tuple[bool, bool, str]:
    """Return `(dirty_worktree, dirty_index, status_short)`."""
    wt = run_git(repo, "status", "--porcelain=v1", "-z", env=env)
    if wt.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, "无法读取 Git 工作区状态", final_phase="preflight")
    records = parse_nul_records(wt.stdout)
    dirty_wt = False
    dirty_idx = False
    for rec in records:
        if len(rec) < 2:
            continue
        x, y = rec[0], rec[1]
        if x == "?" and y == "?":
            dirty_wt = True
        else:
            if x not in (" ", "!"):
                dirty_idx = True
            if y not in (" ", "!"):
                dirty_wt = True
    state_label = "dirty" if (dirty_wt or dirty_idx) else "clean"
    return dirty_wt, dirty_idx, state_label
