"""Local candidate and unpublished commit-range validation helpers."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from git_finalizer.delivery import (
    content_scan,
    patch_check,
    repo_state,
    scanners,
    source_review,
)
from git_finalizer.delivery import (
    policy as policy_mod,
)
from git_finalizer.delivery.parser import ParsedCLI
from git_finalizer.errors import ErrorCode, FinalizerError
from git_finalizer.git_io import (
    FULL_OID_RE,
    is_binary_bytes,
    parse_nul_records,
    read_baseline_blob,
    read_blob_bytes,
    read_blob_size,
    run_git,
)
from git_finalizer.json_io import loads_unique, sha256_bytes

MAX_FIXTURE_EXCEPTIONS = 16
BACKEND_TEST_PATH_RE = re.compile(r"^backend/tests/test_[A-Za-z0-9_]+\.py$")


@dataclasses.dataclass
class ExceptionSpec:
    path: str
    blob_oid: str
    sha256: str
    detector: str
    reason: str
    repo_meta: dict[str, Any] = dataclasses.field(default_factory=dict)
    raw_item: dict[str, Any] = dataclasses.field(default_factory=dict)
    consumed: bool = False

    def to_receipt(self) -> dict[str, Any]:
        approved = {"schema_version": 1, "repository": self.repo_meta, "exception": self.raw_item}
        encoded = json.dumps(approved, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = sha256_bytes(encoded)
        return {
            "exception_id": f"sha256:{digest}",
            "repository": self.repo_meta,
            **self.raw_item,
            "applied": True,
        }


@dataclasses.dataclass
class ValidationOutcome:
    tree_oid: str | None = None
    warnings: list[str] = dataclasses.field(default_factory=list)
    fixture_exceptions: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    reviewed_sensitive_sources: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    external_scanners: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    policy_verdict: dict[str, Any] | None = None


def _read_root_commit(repo: Path, ref_or_oid: str = "HEAD") -> str:
    res = run_git(repo, "rev-list", "--max-parents=0", ref_or_oid)
    if res.returncode != 0:
        raise FinalizerError(ErrorCode.REPO_INVALID, "无法确定仓库 root commit", final_phase="local_validation")
    roots = [line.strip() for line in res.stdout.decode("ascii").splitlines() if line.strip()]
    if len(roots) != 1 or FULL_OID_RE.fullmatch(roots[0]) is None:
        raise FinalizerError(ErrorCode.REPO_INVALID, "仓库存在多个或无效的 root commit", final_phase="local_validation")
    return roots[0]


def _detector_to_verdict(detector: str) -> str:
    for verdict, det in content_scan.VERDICT_DETECTOR.items():
        if det == detector:
            return verdict
    raise ValueError(f"unknown detector: {detector}")


def load_fixture_exceptions(
    path_str: str,
    *,
    repo: Path,
    remote_url: str,
    local_only: bool = False,
    phase: str = "history",
    head_ref: str = "HEAD",
    baseline_ref: str | None = None,
    final_phase: str = "preflight",
) -> list[ExceptionSpec]:
    def _reject(msg: str) -> FinalizerError:
        return FinalizerError(
            ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
            "fixture exception 文件无效；未执行 push",
            final_phase=final_phase,
        )

    try:
        p = Path(path_str)
        if not p.is_absolute() or str(p.resolve(strict=True)) != path_str or p.is_symlink() or not p.is_file():
            raise ValueError("expected canonical regular file")
        if p.stat().st_size > 65536:
            raise ValueError("exception file exceeds 64 KiB")
        raw = p.read_bytes()
        doc = loads_unique(raw)
        if not isinstance(doc, dict) or set(doc) != {"schema_version", "repository", "exceptions"}:
            raise ValueError("unknown or missing document fields")
        if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
            raise ValueError("unknown exception schema")

        repo_meta = doc["repository"]
        if not isinstance(repo_meta, dict) or set(repo_meta) != {"root_commit", "remote_url_sha256"}:
            raise ValueError("invalid repository identity")
        root_commit = _read_root_commit(repo, head_ref)
        if repo_meta["root_commit"] != root_commit or repo_meta["remote_url_sha256"] != sha256_bytes(remote_url.encode("utf-8")):
            raise ValueError("repository identity mismatch")

        raw_items = doc["exceptions"]
        if not isinstance(raw_items, list) or not (1 <= len(raw_items) <= MAX_FIXTURE_EXCEPTIONS):
            raise ValueError("expected 1..16 exceptions")

        seen_keys: set[tuple[str, str, str]] = set()
        specs: list[ExceptionSpec] = []
        for item in raw_items:
            if not isinstance(item, dict) or set(item) != {"path", "blob_oid", "sha256", "detector", "reason"}:
                raise ValueError("unknown or missing exception fields")
            if not all(type(v) is str for v in item.values()):
                raise ValueError("exception fields must be strings")
            rel = item["path"]
            oid = item["blob_oid"]
            sha = item["sha256"]
            detector = item["detector"]
            reason = item["reason"]
            posix = PurePosixPath(rel)
            backend_fixture = BACKEND_TEST_PATH_RE.fullmatch(rel) is not None
            if (
                not (backend_fixture or rel.startswith("tests/") or rel == "CHANGELOG.md" or rel.endswith(".py"))
                or len(rel) > 1024
                or posix.is_absolute()
                or str(posix) != rel
                or ".." in posix.parts
                or any(c in rel for c in "\x00\n\r\t*?[]\\")
            ):
                raise ValueError("invalid exception path")
            if local_only and not backend_fixture:
                raise ValueError("local mode only allows backend/tests/test_*.py")
            if backend_fixture and detector != "credential-assignment-v1":
                raise ValueError("backend test requires credential-assignment-v1")
            if (
                len(oid) != len(root_commit)
                or FULL_OID_RE.fullmatch(oid) is None
                or re.fullmatch(r"[0-9a-f]{64}", sha) is None
            ):
                raise ValueError("invalid content identity")
            if detector not in {"private-key-header-v1", "known-token-v1", "credential-assignment-v1"}:
                raise ValueError("unknown fixture detector")
            if not (0 < len(reason.strip()) <= 256) or any(c in reason for c in "\x00\n\r\t"):
                raise ValueError("invalid exception reason")
            key = (rel, oid, detector)
            if key in seen_keys:
                raise ValueError("duplicate exception")
            seen_keys.add(key)

            if phase == "worktree":
                candidate = repo / rel
                if (
                    candidate.is_symlink()
                    or not candidate.is_file()
                    or str(candidate.resolve(strict=True)) != str(candidate.resolve())
                    or candidate.stat().st_size > 16 * 1024 * 1024
                ):
                    raise ValueError("invalid canonical worktree file")
                ho = run_git(repo, "hash-object", "--no-filters", "--", rel)
                if ho.returncode != 0 or ho.stdout.decode("ascii").strip() != oid:
                    raise ValueError("working blob oid mismatch")
                content = candidate.read_bytes()
            else:
                content = read_blob_bytes(repo, oid, max_bytes=16 * 1024 * 1024)

            if sha256_bytes(content) != sha:
                raise ValueError("content sha256 mismatch")

            verdict_needed = _detector_to_verdict(detector)
            raw_findings = content_scan.findings(content)
            if raw_findings.get(verdict_needed, 0) <= 0:
                raise ValueError("approved detector did not match blob")

            if rel == "CHANGELOG.md":
                if detector != "private-key-header-v1" or not baseline_ref:
                    raise ValueError("changelog review requires baseline and private-key-header-v1")
                _, base_bytes = _baseline_blob(repo, baseline_ref, "CHANGELOG.md")
                if base_bytes is None or not content_scan.scan_changelog_header_preservation(content, base_bytes):
                    raise ValueError("changelog header validation failed")
            elif not backend_fixture and not rel.startswith("tests/"):
                if not rel.endswith(".py") or detector != "credential-assignment-v1" or not baseline_ref:
                    raise ValueError("prose review requires Python file, baseline, and credential-assignment-v1")
                _, base_bytes = _baseline_blob(repo, baseline_ref, rel)
                if base_bytes is None or not content_scan.scan_published_prose_reasons(content, base_bytes):
                    raise ValueError("published prose validation failed")

            specs.append(
                ExceptionSpec(
                    path=rel,
                    blob_oid=oid,
                    sha256=sha,
                    detector=detector,
                    reason=reason,
                    repo_meta=dict(repo_meta),
                    raw_item=dict(item),
                )
            )
    except (OSError, UnicodeDecodeError, ValueError, TypeError, FinalizerError) as exc:
        raise _reject(str(exc)) from exc
    return specs


def _baseline_blob(repo: Path, ref_expr: str, path: str, env: dict[str, str] | None = None) -> tuple[str | None, bytes | None]:
    return read_baseline_blob(repo, ref_expr, path, max_bytes=content_scan.MAX_SCAN_BYTES, env=env)


def _is_retained_staged_deletion(
    repo: Path,
    rel: str,
    *,
    is_unborn: bool,
    env: dict[str, str] | None = None,
) -> bool:
    """Return True when `rel` is a staged deletion whose local copy is absent or gitignored."""
    if is_unborn:
        return False
    is_del = (
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
            rel,
            env=env,
        ).returncode
        == 1
    )
    if not is_del:
        return False
    target = repo / rel
    return (not target.exists() and not target.is_symlink()) or (
        run_git(repo, "check-ignore", "--quiet", "--no-index", "--", rel, env=env).returncode == 0
    )


def _check_large_binary_policy(
    path: str,
    size: int,
    raw: bytes,
    *,
    allow_large_binary_path: str | None,
    policy: policy_mod.DeliveryPolicy,
) -> tuple[bool, str | None]:
    binary = is_binary_bytes(raw)
    if binary and size > policy.max_file_bytes:
        if size > policy.max_large_binary_bytes:
            raise FinalizerError(
                ErrorCode.BINARY_SIZE_EXCEEDED,
                f"大二进制例外目标超过 25 MiB 硬上限: {path}",
                final_phase="local_validation",
            )
        if allow_large_binary_path == path:
            return True, f"allow-large-binary override accepted ({path}, {size} bytes)"
        raise FinalizerError(
            ErrorCode.BINARY_SIZE_EXCEEDED,
            f"拒绝候选的大于 {policy.max_file_bytes // (1024 * 1024)} MiB 明显二进制文件: {path}",
            final_phase="local_validation",
        )
    if not binary and size > content_scan.MAX_SCAN_BYTES:
        raise FinalizerError(
            ErrorCode.BINARY_SIZE_EXCEEDED,
            f"文本文件超过扫描上限: {path}",
            final_phase="local_validation",
        )
    return False, None


def _apply_content_gate(
    *,
    repo: Path,
    path: str,
    raw: bytes,
    blob_oid: str | None,
    stage: str,
    baseline_ref: str | None,
    cli: ParsedCLI,
    specs: list[ExceptionSpec],
    local_only: bool,
    env: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    _, baseline_bytes = (
        _baseline_blob(repo, baseline_ref, path, env=env)
        if baseline_ref is not None
        else (None, None)
    )
    diag = content_scan.scan_diagnostic(raw, path=path, stage=stage, blob_oid=blob_oid, baseline=baseline_bytes)
    sha = sha256_bytes(raw)
    if diag is None:
        # Published files (e.g. prose *_REASONS unchanged from baseline) can match specs even when baseline subtraction yields CLEAR.
        matched_clear = [
            sp
            for sp in specs
            if sp.path == path and sp.sha256 == sha and (blob_oid is None or sp.blob_oid == blob_oid)
        ]
        if matched_clear:
            for sp in matched_clear:
                sp.consumed = True
            order = ("private-key-header-v1", "known-token-v1", "credential-assignment-v1")
            return [sp.to_receipt() for sp in sorted(matched_clear, key=lambda s: order.index(s.detector))], None
        return [], None

    if diag["verdict"] == "ERROR":
        raise FinalizerError(
            ErrorCode.CONTENT_SCAN_BLOCKED,
            f"{'staged ' if stage == 'index' else ''}内容分类失败: {path}",
            status="blocked" if stage != "index" else "failed",
            final_phase="local_validation" if stage != "index" else "staging",
            next_action="resolve_blocker_and_retry" if stage != "index" else "inspect_failure",
            scan_diagnostic=diag,
        )

    # Check --allow-test-fixture for tests/ paths (legacy blanket override in allowlisted modes)
    if (
        cli.allow_test_fixtures
        and path in cli.allow_test_fixtures
        and path.startswith("tests/")
        and cli.action in {"normal", "commit-only", "verify-only", "initial-publish", "resume-initial-publish", "initial-commit-only"}
    ):
        return [], f"root test fixture content override: {path}" if cli.action == "resume-initial-publish" else f"test fixture override: {path}"

    detector = diag["detector"]
    sha = diag["sha256"]
    matching = [
        sp
        for sp in specs
        if sp.path == path
        and sp.detector == detector
        and sp.sha256 == sha
        and (blob_oid is None or sp.blob_oid == blob_oid)
    ]
    if matching:
        # Enforce special rules for CHANGELOG.md & *_REASONS & local-only backend/tests!
        if path == "CHANGELOG.md":
            if detector != "private-key-header-v1" or baseline_bytes is None or not content_scan.scan_changelog_header_preservation(raw, baseline_bytes):
                raise FinalizerError(
                    ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
                    f"CHANGELOG.md 例外仅允许保留已有 inline-backtick header 且其余文本不变: {path}",
                    final_phase="local_validation",
                    scan_diagnostic=diag,
                )
        elif local_only and not BACKEND_TEST_PATH_RE.fullmatch(path):
            if cli.action in {"verify-only", "commit-only"}:
                raise FinalizerError(
                    ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
                    f"本地 verify-only/commit-only 不支持该路径的 fixture exception: {path}",
                    final_phase="local_validation",
                    scan_diagnostic=diag,
                )
        elif (
            path.endswith(".py")
            and not path.startswith(("tests/", "backend/tests/"))
            and (
                baseline_bytes is None
                or not content_scan.scan_published_prose_reasons(raw, baseline_bytes)
            )
        ):
            raise FinalizerError(
                ErrorCode.CONTENT_SCAN_BLOCKED,
                f"候选敏感文件内容命中规则 ({detector}): {path}",
                final_phase="local_validation",
                scan_diagnostic=diag,
            )

        # Verify all findings on this blob have matching approvals (`other_findings <= approved_verdicts`).
        all_specs_for_blob = [
            sp
            for sp in specs
            if sp.path == path and sp.sha256 == sha and (blob_oid is None or sp.blob_oid == blob_oid)
        ]
        other_findings = content_scan.findings(raw)
        if (
            baseline_bytes is not None
            and not BACKEND_TEST_PATH_RE.fullmatch(path)
            and not path.startswith("tests/")
            and path != "CHANGELOG.md"
        ):
            # Only published Python *_REASONS prose checks mask unchanged published prose separately via scan_published_prose_reasons.
            other_findings = {}
        approved_verdicts = {_detector_to_verdict(sp.detector) for sp in all_specs_for_blob}
        unapproved = set(other_findings) - approved_verdicts
        if not unapproved:
            for sp in all_specs_for_blob:
                sp.consumed = True
            return [sp.to_receipt() for sp in sorted(all_specs_for_blob, key=lambda s: ("private-key-header-v1", "known-token-v1", "credential-assignment-v1").index(s.detector))], None

    if detector == "known-token-v1":
        det_label = "高置信度 token/credential 内容签名"
    elif detector == "private-key-header-v1":
        det_label = "private key header (private-key-header-v1)"
    else:
        det_label = detector
    raise FinalizerError(
        ErrorCode.CONTENT_SCAN_BLOCKED,
        f"{'staged' if stage == 'index' else '候选'}敏感文件内容命中规则 ({det_label}): {path}（禁止硬编码敏感信息）",
        status="blocked" if stage != "index" else "failed",
        final_phase="local_validation" if stage != "index" else "staging",
        next_action="resolve_blocker_and_retry" if stage != "index" else "inspect_failure",
        scan_diagnostic=diag,
    )


def validate_staged_candidate(
    repo: Path,
    info: repo_state.RepoInfo,
    iso_env: dict[str, str],
    paths: list[str],
    cli: ParsedCLI,
    policy: policy_mod.DeliveryPolicy,
    *,
    specs: list[ExceptionSpec],
    local_only: bool,
) -> ValidationOutcome:
    """Run full pre-commit validation inside isolated temp GIT_INDEX_FILE & object dir."""
    outcome = ValidationOutcome()

    # Check source-review upfront at worktree stage if requested or sensitive path present
    sensitive_paths = [p for p in paths if source_review.is_sensitive_path(p)]
    if sensitive_paths and not cli.reviewed_sensitive_source:
        raise FinalizerError(
            ErrorCode.SOURCE_REVIEW_BLOCKED,
            f"敏感命名源码文件需要 --reviewed-sensitive-source: {sensitive_paths[0]}",
            final_phase="local_validation",
        )
    if cli.reviewed_sensitive_source and cli.allow_test_fixtures:
        raise FinalizerError(
            ErrorCode.CLI_INVALID_ARGS,
            "--reviewed-sensitive-source 不能与 --allow-test-fixture 混用",
            final_phase="cli",
        )
    if cli.reviewed_sensitive_source and cli.fixture_exceptions:
        raise FinalizerError(
            ErrorCode.CLI_INVALID_ARGS,
            "--reviewed-sensitive-source 不能与 --fixture-exceptions 混用",
            final_phase="cli",
        )
    if cli.allow_test_fixtures and cli.fixture_exceptions:
        raise FinalizerError(
            ErrorCode.CLI_INVALID_ARGS,
            "--allow-test-fixture 不能与 --fixture-exceptions 混用",
            final_phase="cli",
        )
    for f_path in cli.allow_test_fixtures:
        if f_path not in paths:
            raise FinalizerError(
                ErrorCode.SCOPE_INVALID,
                f"test fixture 不在本次显式文件范围内: {f_path}",
                final_phase="local_validation",
            )
    if cli.allow_large_binary and cli.allow_large_binary not in paths:
        raise FinalizerError(
            ErrorCode.SCOPE_INVALID,
            f"大二进制例外路径不在本次显式文件范围内: {cli.allow_large_binary}",
            final_phase="local_validation",
        )

    retained_deletions = {
        rel for rel in paths if _is_retained_staged_deletion(repo, rel, is_unborn=info.is_unborn, env=iso_env)
    }

    # Run worktree review path/digest checks upfront so summary carries `reviewed_sensitive_sources` even if secret scan blocks.
    worktree_review_receipts: list[dict[str, Any]] = []
    if cli.reviewed_sensitive_source:
        worktree_review_receipts = source_review.validate_review_document(
            review_file=cli.reviewed_sensitive_source,
            repo=repo,
            common_dir=info.common_dir,
            repository_id=cli.repository_id,
            allocation_id=cli.allocation_id,
            task_key=cli.task_key,
            authority_key=cli.authority_key,
            worktree_path=cli.worktree_path,
            requested_paths=paths,
            stage="worktree",
            stage_env=iso_env,
            allowed_extensions=policy.allow_reviewed_extensions,
            candidate_blobs=None,
            baseline_ref=None if info.is_unborn else "HEAD",
        )
        outcome.reviewed_sensitive_sources = worktree_review_receipts

    # 1. Worktree + Clean-filter candidate scan FIRST (before even running git add on isolated index)
    candidate_blobs: dict[str, bytes] = {}
    try:
        for rel in paths:
            if rel in retained_deletions:
                continue
            target = repo / rel
            if target.is_symlink() or not target.exists():
                continue
            raw_wt = target.read_bytes()
            _, w_msg = _check_large_binary_policy(
                rel, len(raw_wt), raw_wt, allow_large_binary_path=cli.allow_large_binary, policy=policy
            )
            if w_msg:
                outcome.warnings.append(w_msg)
            baseline_ref = None if info.is_unborn else "HEAD"
            wt_receipts, wt_warn = _apply_content_gate(
                repo=repo,
                path=rel,
                raw=raw_wt,
                blob_oid=None,
                stage="worktree",
                baseline_ref=baseline_ref,
                cli=cli,
                specs=specs,
                local_only=local_only,
                env=iso_env,
            )
            for rec in wt_receipts:
                if rec not in outcome.fixture_exceptions:
                    outcome.fixture_exceptions.append(rec)
            if wt_warn:
                outcome.warnings.append(wt_warn)

            cand_oid, cand_bytes = content_scan.hash_candidate_clean(repo, rel, target, env=iso_env)
            candidate_blobs[rel] = cand_bytes
            matching_specs = [s for s in specs if s.path == rel]
            if matching_specs:
                raw_hash_res = run_git(repo, "--literal-pathspecs", "hash-object", "--no-filters", "--", rel, env=iso_env)
                if raw_hash_res.returncode != 0:
                    raise FinalizerError(
                        ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
                        f"无法只读复核 local fixture raw blob: {rel}",
                        final_phase="local_validation",
                    )
                raw_oid = raw_hash_res.stdout.decode("ascii", errors="strict").strip()
                if all(s.blob_oid != raw_oid for s in matching_specs):
                    raise FinalizerError(
                        ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
                        f"reviewed fixture raw blob 已漂移: {rel}",
                        final_phase="local_validation",
                    )
                if cand_oid != raw_oid:
                    raise FinalizerError(
                        ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
                        f"clean filter 改变了已审查的 fixture 文件: {rel}",
                        final_phase="local_validation",
                    )
                # Also ensure any pre-existing staged change in real `.git/index` matches raw_oid cleanly.
                real_idx_diff = run_git(repo, "--literal-pathspecs", "diff", "--cached", "--name-only", "--no-renames", "-z", "--", rel)
                if real_idx_diff.returncode == 0 and real_idx_diff.stdout.strip(b"\x00"):
                    real_ls = run_git(repo, "--literal-pathspecs", "ls-files", "--stage", "-z", "--", rel)
                    real_entries = parse_nul_records(real_ls.stdout) if real_ls.returncode == 0 and real_ls.stdout.endswith(b"\x00") else []
                    if len(real_entries) != 1:
                        raise FinalizerError(
                            ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
                            f"local fixture review staged entry 无效: {rel}",
                            final_phase="local_validation",
                        )
                    idx_header = real_entries[0].partition("\t")[0].split()
                    if len(idx_header) < 2 or idx_header[0] not in {"100644", "100755"} or idx_header[1] != raw_oid:
                        raise FinalizerError(
                            ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
                            f"local fixture review staged blob 已漂移: {rel}",
                            final_phase="local_validation",
                        )
            _check_large_binary_policy(
                rel, len(cand_bytes), cand_bytes, allow_large_binary_path=cli.allow_large_binary, policy=policy
            )
            if cli.action == "verify-only":
                c_receipts, _ = _apply_content_gate(
                    repo=repo,
                    path=rel,
                    raw=cand_bytes,
                    blob_oid=cand_oid,
                    stage="candidate",
                    baseline_ref=baseline_ref,
                    cli=cli,
                    specs=specs,
                    local_only=local_only,
                    env=iso_env,
                )
                for rec in c_receipts:
                    if rec not in outcome.fixture_exceptions:
                        outcome.fixture_exceptions.append(rec)
    except FinalizerError as exc:
        if worktree_review_receipts and not exc.reviewed_sensitive_sources:
            exc.reviewed_sensitive_sources = [dict(r) for r in worktree_review_receipts]
        raise

    if cli.reviewed_sensitive_source:
        outcome.reviewed_sensitive_sources = source_review.validate_review_document(
            review_file=cli.reviewed_sensitive_source,
            repo=repo,
            common_dir=info.common_dir,
            repository_id=cli.repository_id,
            allocation_id=cli.allocation_id,
            task_key=cli.task_key,
            authority_key=cli.authority_key,
            worktree_path=cli.worktree_path,
            requested_paths=paths,
            stage="worktree",
            stage_env=iso_env,
            allowed_extensions=policy.allow_reviewed_extensions,
            candidate_blobs=candidate_blobs,
            baseline_ref=None if info.is_unborn else "HEAD",
        )

    # 2. Stage requested paths exclusively in isolated temporary GIT_INDEX_FILE + GIT_OBJECT_DIRECTORY
    #    Keep existing staged deletions untouched if an ignored copy exists locally.
    paths_to_stage = [rel for rel in paths if rel not in retained_deletions]

    # 2. Gather candidate diff without `git add` in verify-only; stage in isolated temp index otherwise.
    paths_to_stage = [rel for rel in paths if rel not in retained_deletions]
    staged_names: list[str] = []
    if cli.action == "verify-only":
        if retained_deletions:
            if info.is_unborn:
                raise FinalizerError(
                    ErrorCode.SCOPE_INVALID,
                    "unborn HEAD 不支持 staged deletion",
                    final_phase="local_validation",
                )
            diff_del = run_git(
                repo,
                "--literal-pathspecs",
                "diff",
                "--cached",
                "--name-only",
                "--no-renames",
                "-z",
                "--",
                *sorted(retained_deletions),
            )
            if diff_del.returncode != 0 or (diff_del.stdout and not diff_del.stdout.endswith(b"\x00")):
                raise FinalizerError(ErrorCode.INDEX_UNCLEAN, "无法读取 staged deletion 范围", final_phase="local_validation")
            staged_names.extend(parse_nul_records(diff_del.stdout))
        for rel in paths_to_stage:
            tracked = run_git(repo, "--literal-pathspecs", "ls-files", "--error-unmatch", "--", rel).returncode == 0
            if not tracked:
                ig = run_git(repo, "check-ignore", "--quiet", "--no-index", "--", rel)
                if ig.returncode == 0:
                    raise FinalizerError(
                        ErrorCode.SCOPE_INVALID,
                        f"ignored 文件无法进入提交范围: {rel}",
                        final_phase="local_validation",
                    )
                if ig.returncode != 1:
                    raise FinalizerError(
                        ErrorCode.INDEX_UNCLEAN,
                        f"无法验证 ignore 状态: {rel}",
                        final_phase="local_validation",
                    )
        if paths_to_stage:
            st = run_git(
                repo,
                "--literal-pathspecs",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--",
                *paths_to_stage,
            )
            if st.returncode != 0 or (st.stdout and not st.stdout.endswith(b"\x00")):
                raise FinalizerError(ErrorCode.INDEX_UNCLEAN, "无法读取候选变更范围", final_phase="local_validation")
            recs = parse_nul_records(st.stdout)
            r_idx = 0
            while r_idx < len(recs):
                rec = recs[r_idx]
                r_idx += 1
                if len(rec) >= 4:
                    staged_names.append(rec[3:])
                    if rec[0] in {"R", "C"} or rec[1] in {"R", "C"}:
                        r_idx += 1
        changed = bool(staged_names)
    else:
        if paths_to_stage:
            stage_res = run_git(repo, "--literal-pathspecs", "add", "-A", "--", *paths_to_stage, env=iso_env)
            if stage_res.returncode != 0:
                raise FinalizerError(
                    ErrorCode.INDEX_UNCLEAN,
                    f"在隔离暂存区暂存文件失败: {stage_res.stderr.decode('utf-8', errors='replace').strip()}",
                    final_phase="local_validation",
                )

        # 3. Verify non-empty staged change (unless dry-run explicitly warns on unchanged paths)
        if info.is_unborn:
            ls = run_git(repo, "--literal-pathspecs", "ls-files", "--cached", "-z", env=iso_env)
            if ls.returncode != 0 or (ls.stdout and not ls.stdout.endswith(b"\x00")):
                raise FinalizerError(ErrorCode.INDEX_UNCLEAN, "无法读取 staged 文件范围", final_phase="local_validation")
            staged_names = parse_nul_records(ls.stdout)
            changed = bool(staged_names)
        else:
            diff_names = run_git(repo, "--literal-pathspecs", "diff", "--cached", "--name-only", "--no-renames", "-z", env=iso_env)
            if diff_names.returncode != 0 or (diff_names.stdout and not diff_names.stdout.endswith(b"\x00")):
                raise FinalizerError(ErrorCode.INDEX_UNCLEAN, "无法读取 staged 文件范围", final_phase="local_validation")
            staged_names = parse_nul_records(diff_names.stdout)
            changed = bool(staged_names)

    staged_set = set(staged_names)
    if specs:
        invalid_specs = [
            s.path
            for s in specs
            if not s.consumed
            or s.path not in staged_set
            or (repo / s.path).is_symlink()
            or not (repo / s.path).is_file()
        ]
        if invalid_specs:
            raise FinalizerError(
                ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
                f"未生效或不在变更候选范围内的 --fixture-exceptions 项: {', '.join(invalid_specs)}",
                final_phase="local_validation",
            )

    if not changed:
        if cli.dry_run:
            outcome.warnings.extend([f"unchanged path: {p}" for p in paths])
            tree_res = run_git(repo, "write-tree", env=iso_env)
            outcome.tree_oid = tree_res.stdout.decode("ascii").strip() if tree_res.returncode == 0 else None
            return outcome
        raise FinalizerError(
            ErrorCode.SCOPE_INVALID,
            "显式路径范围内没有待提交的改动（diff 为空）",
            status="blocked" if cli.action == "verify-only" else "failed",
            final_phase="local_validation" if cli.action == "verify-only" else "staging",
            next_action="resolve_blocker_and_retry" if cli.action == "verify-only" else "inspect_failure",
        )

    # 4. Whitespace check (+ counted unified-diff .patch context exemption)
    diag_chunks: list[str] = []
    ws_failed = False
    if cli.action == "verify-only":
        tracked_verify: list[str] = []
        untracked_verify: list[str] = []
        for rel in paths_to_stage:
            if not info.is_unborn and run_git(repo, "--literal-pathspecs", "ls-files", "--error-unmatch", "--", rel).returncode == 0:
                tracked_verify.append(rel)
            else:
                untracked_verify.append(rel)
        if tracked_verify:
            ws_t = run_git(repo, "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--check", "HEAD", "--", *tracked_verify)
            if ws_t.returncode != 0:
                ws_failed = True
                diag_chunks.append(ws_t.stdout.decode("utf-8", errors="replace") + ws_t.stderr.decode("utf-8", errors="replace"))
        for rel in untracked_verify:
            target = repo / rel
            if target.is_symlink() or not target.is_file():
                continue
            ws_u = run_git(repo, "diff", "--no-index", "--no-ext-diff", "--no-textconv", "--check", "--", "/dev/null", str(target))
            raw_diag = ws_u.stdout.decode("utf-8", errors="replace") + ws_u.stderr.decode("utf-8", errors="replace")
            if ws_u.returncode not in (0, 1) or raw_diag.strip():
                ws_failed = True
                diag_chunks.append(raw_diag.replace(str(target), rel))
    else:
        ws = run_git(repo, "diff", "--cached", "--check", env=iso_env)
        if ws.returncode != 0:
            ws_failed = True
            diag_chunks.append(ws.stdout.decode("utf-8", errors="replace") + ws.stderr.decode("utf-8", errors="replace"))

    if ws_failed:
        diag_text = "\n".join(chunk for chunk in diag_chunks if chunk)
        # Check if every reported file is `.patch` and passes exact counted empty-context validation
        violating_files: set[str] = set()
        for line in diag_text.splitlines():
            if ":" in line:
                violating_files.add(line.split(":", 1)[0])
        if not violating_files:
            raise FinalizerError(
                ErrorCode.WHITESPACE_VIOLATION,
                "git diff --check 失败；staged diff 存在 whitespace 错误",
                final_phase="local_validation",
            )
        for vfile in violating_files:
            if not vfile.endswith(".patch"):
                raise FinalizerError(
                    ErrorCode.WHITESPACE_VIOLATION,
                    f"git diff --check 失败；存在 whitespace 错误: {vfile}",
                    final_phase="local_validation",
                )
            if cli.action == "verify-only":
                blob_raw = candidate_blobs.get(vfile)
                if blob_raw is None:
                    raise FinalizerError(ErrorCode.WHITESPACE_VIOLATION, f"git diff --check 失败；patch 候选不可读: {vfile}", final_phase="local_validation")
            else:
                ls = run_git(repo, "--literal-pathspecs", "ls-files", "--stage", "-z", "--", vfile, env=iso_env)
                entries = parse_nul_records(ls.stdout)
                if len(entries) != 1:
                    raise FinalizerError(ErrorCode.WHITESPACE_VIOLATION, f"git diff --check 失败；patch 文件暂存项异常: {vfile}", final_phase="local_validation")
                blob_oid = entries[0].partition("\t")[0].split()[1]
                blob_raw = read_blob_bytes(repo, blob_oid, max_bytes=patch_check.MAX_PATCH_BYTES + 1, env=iso_env)
            file_diags = "\n".join(ln for ln in diag_text.splitlines() if ln.startswith(f"{vfile}:"))
            if not patch_check.allow_patch_context_whitespace_only(repo, vfile, blob_raw, file_diags, env=iso_env):
                raise FinalizerError(
                    ErrorCode.WHITESPACE_VIOLATION,
                    f"git diff --check 失败；patch 文件包含无效或超限的 whitespace 错误: {vfile}",
                    final_phase="local_validation",
                )

    # 5. Staged blob size, unified NUL binary check, content scan & reviewed source check (skipped re-staging in verify-only)
    staged_blobs: dict[str, bytes] = dict(candidate_blobs) if cli.action == "verify-only" else {}
    if cli.action != "verify-only":
        ls_stage = run_git(repo, "--literal-pathspecs", "ls-files", "--stage", "-z", "--", *paths, env=iso_env)
        if ls_stage.returncode != 0 or (ls_stage.stdout and not ls_stage.stdout.endswith(b"\x00")):
            raise FinalizerError(ErrorCode.INDEX_UNCLEAN, "无法读取 staged 文件范围", final_phase="local_validation")
        for rec in parse_nul_records(ls_stage.stdout):
            meta, _, path = rec.partition("\t")
            mode, oid, stage_num = meta.split()
            if stage_num != "0" or mode not in {"100644", "100755", "120000"}:
                raise FinalizerError(ErrorCode.SCOPE_INVALID, f"拒绝非普通文件或冲突项: {path}", final_phase="local_validation")
            if mode == "120000":
                continue
            size = read_blob_size(repo, oid, env=iso_env)
            raw_blob = read_blob_bytes(repo, oid, max_bytes=max(content_scan.MAX_SCAN_BYTES, policy.max_large_binary_bytes), env=iso_env)
            staged_blobs[path] = raw_blob
            _check_large_binary_policy(
                path, size, raw_blob, allow_large_binary_path=cli.allow_large_binary, policy=policy
            )
            baseline_ref = None if info.is_unborn else "HEAD"
            try:
                idx_receipts, _ = _apply_content_gate(
                    repo=repo,
                    path=path,
                    raw=raw_blob,
                    blob_oid=oid,
                    stage="index",
                    baseline_ref=baseline_ref,
                    cli=cli,
                    specs=specs,
                    local_only=local_only,
                    env=iso_env,
                )
            except FinalizerError as exc:
                if worktree_review_receipts and not exc.reviewed_sensitive_sources:
                    exc.reviewed_sensitive_sources = [dict(r) for r in worktree_review_receipts]
                raise
            for r in idx_receipts:
                if r not in outcome.fixture_exceptions:
                    outcome.fixture_exceptions.append(r)

        if cli.reviewed_sensitive_source:
            outcome.reviewed_sensitive_sources = source_review.validate_review_document(
                review_file=cli.reviewed_sensitive_source,
                repo=repo,
                common_dir=info.common_dir,
                repository_id=cli.repository_id,
                allocation_id=cli.allocation_id,
                task_key=cli.task_key,
                authority_key=cli.authority_key,
                worktree_path=cli.worktree_path,
                requested_paths=paths,
                stage="index",
                stage_env=iso_env,
                allowed_extensions=policy.allow_reviewed_extensions,
                candidate_blobs=staged_blobs,
                baseline_ref=None if info.is_unborn else "HEAD",
            )
            for rec in outcome.reviewed_sensitive_sources:
                rec["content_scan"] = "passed"
    else:
        for rec in outcome.reviewed_sensitive_sources:
            rec["content_scan"] = "passed"

    # 6. External SARIF 2.1.0 scanners & OPA policy hook
    if cli.scanners:
        outcome.external_scanners = scanners.run_external_scanners(
            staged_blobs, cli.scanners, policy, sarif_cmd=cli.sarif_cmd
        )
    if cli.opa_policy:
        outcome.policy_verdict = policy_mod.evaluate_opa_policy(
            Path(cli.opa_policy),
            query=cli.opa_query,
            input_document={
                "mode": cli.mode,
                "action": cli.action,
                "branch": info.branch,
                "requested_paths": paths,
                "dry_run": cli.dry_run,
                "trailer_agent": cli.trailer_agent,
                "trace_ref": cli.trace_ref,
                "snapshot_id": cli.snapshot,
                "scanners": cli.scanners,
            },
        )

    tree_out = run_git(repo, "write-tree", env=iso_env)
    if tree_out.returncode == 0:
        outcome.tree_oid = tree_out.stdout.decode("ascii").strip()
    return outcome


def validate_commit_range(
    repo: Path,
    info: repo_state.RepoInfo,
    commits: list[str],
    cli: ParsedCLI,
    policy: policy_mod.DeliveryPolicy,
    *,
    specs: list[ExceptionSpec],
    local_only: bool = False,
    reviewed_blobs: dict[str, str] | None = None,
) -> ValidationOutcome:
    """Validate an unpublished commit range commit-by-commit with exact parent baseline diff scanning."""
    outcome = ValidationOutcome()
    reviewed_fixture_paths_seen: set[str] = set()
    head_large_binary_applied = False
    for commit_oid in commits:
        parents_res = run_git(repo, "rev-list", "--parents", "-n", "1", commit_oid)
        if parents_res.returncode != 0:
            raise FinalizerError(ErrorCode.REPO_INVALID, f"无法读取提交父节点: {commit_oid}", final_phase="local_validation")
        tokens = parents_res.stdout.decode("ascii").strip().split()
        first_parent = tokens[1] if len(tokens) >= 2 else None
        base_tree = first_parent if first_parent else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

        diff_tree = run_git(repo, "diff-tree", "--no-commit-id", "-r", "--name-status", "-z", base_tree, commit_oid)
        if diff_tree.returncode != 0 or (diff_tree.stdout and not diff_tree.stdout.endswith(b"\x00")):
            raise FinalizerError(ErrorCode.REPO_INVALID, f"无法读取待发布 commit 文件范围: {commit_oid}", final_phase="local_validation")
        records = parse_nul_records(diff_tree.stdout)
        idx = 0
        changed_paths: list[str] = []
        while idx < len(records):
            status_code = records[idx]
            idx += 1
            if status_code.startswith(("R", "C")):
                if idx + 1 >= len(records):
                    raise FinalizerError(ErrorCode.REPO_INVALID, f"无法读取待发布 commit 文件范围: {commit_oid}", final_phase="local_validation")
                _old = records[idx]
                new_p = records[idx + 1]
                idx += 2
                if not status_code.startswith("D"):
                    changed_paths.append(new_p)
            else:
                if idx >= len(records):
                    raise FinalizerError(ErrorCode.REPO_INVALID, f"无法读取待发布 commit 文件范围: {commit_oid}", final_phase="local_validation")
                p = records[idx]
                idx += 1
                if status_code != "D":
                    changed_paths.append(p)

        # Check whitespace on commit diff (allowing verified .patch files)
        chk = run_git(repo, "diff", "--check", base_tree, commit_oid)
        if chk.returncode != 0:
            diag_text = chk.stdout.decode("utf-8", errors="replace")
            violating_files: set[str] = set()
            for line in diag_text.splitlines():
                if ":" in line:
                    violating_files.add(line.split(":", 1)[0])
            if not violating_files:
                raise FinalizerError(
                    ErrorCode.WHITESPACE_VIOLATION,
                    f"历史提交存在 whitespace 错误 ({commit_oid[:12]}): diff",
                    final_phase="local_validation",
                )
            for vfile in violating_files:
                if not vfile.endswith(".patch"):
                    raise FinalizerError(
                        ErrorCode.WHITESPACE_VIOLATION,
                        f"历史提交存在 whitespace 错误 ({commit_oid[:12]}): {vfile}",
                        final_phase="local_validation",
                    )
                _, patch_blob = _baseline_blob(repo, commit_oid, vfile)
                file_diags = "\n".join(ln for ln in diag_text.splitlines() if ln.startswith(f"{vfile}:"))
                if patch_blob is None or not patch_check.allow_patch_context_whitespace_only(repo, vfile, patch_blob, file_diags):
                    raise FinalizerError(
                        ErrorCode.WHITESPACE_VIOLATION,
                        f"历史提交 patch whitespace 错误: {vfile}",
                        final_phase="local_validation",
                    )

        for rel in changed_paths:
            if not rel or rel.startswith("/") or ".." in PurePosixPath(rel).parts or rel == ".git" or rel.startswith(".git/"):
                raise FinalizerError(ErrorCode.SCOPE_INVALID, f"待发布 commit 包含异常文件路径: {rel}", final_phase="local_validation")
            ig = run_git(repo, "check-ignore", "--quiet", "--no-index", "--", rel)
            if ig.returncode == 0 and cli.action in {"resume-publish"}:
                raise FinalizerError(
                    ErrorCode.SCOPE_INVALID,
                    f"历史提交包含被 .gitignore 忽略的 ignored 文件: {rel}",
                    final_phase="local_validation",
                )
            ls = run_git(repo, "--literal-pathspecs", "ls-tree", "-z", commit_oid, "--", rel)
            if ls.returncode != 0 or (ls.stdout and not ls.stdout.endswith(b"\x00")):
                raise FinalizerError(ErrorCode.REPO_INVALID, f"无法读取待发布 commit 文件对象: {rel}", final_phase="local_validation")
            entries = parse_nul_records(ls.stdout)
            if len(entries) != 1:
                raise FinalizerError(ErrorCode.SCOPE_INVALID, f"提交条目非普通文件或异常对象: {rel}", final_phase="local_validation")
            meta, _, entry_path = entries[0].partition("\t")
            parts = meta.split()
            if entry_path != rel or len(parts) != 3:
                raise FinalizerError(ErrorCode.SCOPE_INVALID, f"提交条目非普通文件或异常对象: {rel}", final_phase="local_validation")
            mode, kind, blob_oid = parts
            if kind != "blob" or mode not in {"100644", "100755"} or FULL_OID_RE.fullmatch(blob_oid) is None:
                raise FinalizerError(ErrorCode.SCOPE_INVALID, f"历史提交包含非普通文件或异常对象: {rel} ({mode})", final_phase="local_validation")

            sensitive_reason = source_review.sensitive_path_reason(rel)
            if sensitive_reason:
                if not reviewed_blobs or rel not in reviewed_blobs:
                    raise FinalizerError(
                        ErrorCode.SOURCE_REVIEW_BLOCKED,
                        f"拒绝待发布敏感文件 {rel}: {sensitive_reason}",
                        final_phase="local_validation",
                    )
                if blob_oid != reviewed_blobs[rel]:
                    raise FinalizerError(
                        ErrorCode.SOURCE_REVIEW_BLOCKED,
                        f"reviewed source history blob mismatch: {rel}",
                        final_phase="local_validation",
                    )
            elif reviewed_blobs and rel in reviewed_blobs and blob_oid != reviewed_blobs[rel]:
                raise FinalizerError(
                    ErrorCode.SOURCE_REVIEW_BLOCKED,
                    f"reviewed source history blob mismatch: {rel}",
                    final_phase="local_validation",
                )

            size = read_blob_size(repo, blob_oid)
            raw = read_blob_bytes(repo, blob_oid, max_bytes=max(content_scan.MAX_SCAN_BYTES, policy.max_large_binary_bytes))
            is_head_commit = (commit_oid == info.head_oid)
            _, warn_msg = _check_large_binary_policy(
                rel,
                size,
                raw,
                allow_large_binary_path=(cli.allow_large_binary if is_head_commit else None),
                policy=policy,
            )
            if warn_msg:
                head_large_binary_applied = True
                if warn_msg not in outcome.warnings:
                    outcome.warnings.append(warn_msg)

            has_exact_spec = any(sp.path == rel and sp.blob_oid == blob_oid for sp in specs)
            if rel in reviewed_fixture_paths_seen and not has_exact_spec:
                raise FinalizerError(
                    ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
                    f"fixture exception path/blob identity mismatch: {rel}",
                    final_phase="local_validation",
                )

            rec_list, warn = _apply_content_gate(
                repo=repo,
                path=rel,
                raw=raw,
                blob_oid=blob_oid,
                stage="history",
                baseline_ref=first_parent,
                cli=cli,
                specs=specs,
                local_only=local_only,
            )
            if rec_list:
                reviewed_fixture_paths_seen.add(rel)
            for r in rec_list:
                if r not in outcome.fixture_exceptions:
                    outcome.fixture_exceptions.append(r)
            if warn and warn not in outcome.warnings:
                outcome.warnings.append(warn)

    # Reject unused --allow-large-binary
    if cli.allow_large_binary and not head_large_binary_applied:
        raise FinalizerError(
            ErrorCode.BINARY_SIZE_EXCEEDED,
            f"无效或未命中的 large binary history exception: {cli.allow_large_binary}",
            final_phase="local_validation",
        )

    # Reject unused fixture exceptions
    unused = [sp.path for sp in specs if not sp.consumed]
    if unused:
        raise FinalizerError(
            ErrorCode.FIXTURE_EXCEPTION_BLOCKED,
            f"未使用的 --fixture-exceptions 项（或历史已发布）: {', '.join(unused)}",
            final_phase="local_validation",
        )

    return outcome
