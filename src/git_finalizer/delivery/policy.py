"""Declarative delivery policy (`.agents/delivery-policy.toml`) and optional OPA/Rego hook."""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
from typing import Any

from git_finalizer.errors import ErrorCode, FinalizerError

POLICY_REL_PATH = Path(".agents/delivery-policy.toml")
DEFAULT_LARGE_BINARY_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_ALLOWED_BINARY_BYTES = 25 * 1024 * 1024
ALLOWED_POLICY_KEYS = frozenset(
    {
        "schema_version",
        "protected_branches",
        "extra_protected_branches",
        "max_file_bytes",
        "max_large_binary_bytes",
        "require_trailers",
        "require_scanners",
        "forbid_paths",
        "allow_reviewed_extensions",
        "scanners",
    }
)


@dataclasses.dataclass(frozen=True)
class DeliveryPolicy:
    source: str | None = None
    extra_protected_branches: tuple[str, ...] = ()
    max_file_bytes: int = DEFAULT_LARGE_BINARY_BYTES
    max_large_binary_bytes: int = DEFAULT_MAX_ALLOWED_BINARY_BYTES
    require_trailers: tuple[str, ...] = ()
    require_scanners: tuple[str, ...] = ()
    forbid_paths: tuple[str, ...] = ()
    allow_reviewed_extensions: tuple[str, ...] = (".py", ".css", ".json")
    sarif_cmd_argv: tuple[str, ...] = ()


def _string_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    if key not in data:
        return ()
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            f"delivery-policy.toml 字段 {key} 必须是非空字符串数组",
            final_phase="preflight",
        )
    return tuple(item.strip() for item in value)


def _positive_int(data: dict[str, Any], key: str, default: int, *, maximum: int) -> int:
    if key not in data:
        return default
    value = data[key]
    if type(value) is not int or value <= 0 or value > maximum:
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            f"delivery-policy.toml 字段 {key} 必须是 1..{maximum} 整数",
            final_phase="preflight",
        )
    return value


def load_delivery_policy(repo: Path) -> DeliveryPolicy:
    """Load `.agents/delivery-policy.toml` when present, failing closed on invalid syntax/types."""
    policy_path = repo / POLICY_REL_PATH
    if not policy_path.exists() and not policy_path.is_symlink():
        return DeliveryPolicy()
    if policy_path.is_symlink() or not policy_path.is_file():
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            ".agents/delivery-policy.toml 必须是普通文件",
            final_phase="preflight",
        )
    try:
        raw = policy_path.read_bytes()
        if len(raw) > 256 * 1024:
            raise ValueError("policy file exceeds 256 KiB")
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            f"无法解析 .agents/delivery-policy.toml: {exc}",
            final_phase="preflight",
        ) from exc
    if not isinstance(data, dict):
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            ".agents/delivery-policy.toml 顶层必须是 TOML table",
            final_phase="preflight",
        )
    unknown = set(data) - ALLOWED_POLICY_KEYS
    if unknown:
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            f".agents/delivery-policy.toml 包含未识别字段: {', '.join(sorted(unknown))}",
            final_phase="preflight",
        )
    schema_version = data.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            ".agents/delivery-policy.toml 需要 schema_version = 1",
            final_phase="preflight",
        )
    extra_branches = _string_list(data, "protected_branches") + _string_list(
        data, "extra_protected_branches"
    )
    max_file_bytes = _positive_int(
        data, "max_file_bytes", DEFAULT_LARGE_BINARY_BYTES, maximum=DEFAULT_MAX_ALLOWED_BINARY_BYTES
    )
    max_large_binary_bytes = _positive_int(
        data,
        "max_large_binary_bytes",
        DEFAULT_MAX_ALLOWED_BINARY_BYTES,
        maximum=DEFAULT_MAX_ALLOWED_BINARY_BYTES,
    )
    if max_large_binary_bytes < max_file_bytes:
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            "delivery-policy.toml 的 max_large_binary_bytes 不得小于 max_file_bytes",
            final_phase="preflight",
        )
    extensions = _string_list(data, "allow_reviewed_extensions") or (".py", ".css", ".json")
    for ext in extensions:
        if not ext.startswith(".") or len(ext) < 2:
            raise FinalizerError(
                ErrorCode.POLICY_DENIED,
                f"delivery-policy.toml 非法扩展名配置: {ext!r}",
                final_phase="preflight",
            )
    sarif_cmd_argv: tuple[str, ...] = ()
    scanners_cfg = data.get("scanners")
    if scanners_cfg is not None:
        if not isinstance(scanners_cfg, dict):
            raise FinalizerError(
                ErrorCode.POLICY_DENIED,
                "delivery-policy.toml [scanners] 必须是 table",
                final_phase="preflight",
            )
        sarif_cfg = scanners_cfg.get("sarif_cmd")
        if sarif_cfg is not None:
            if not isinstance(sarif_cfg, dict):
                raise FinalizerError(
                    ErrorCode.POLICY_DENIED,
                    "delivery-policy.toml [scanners.sarif_cmd] 必须是 table",
                    final_phase="preflight",
                )
            sarif_cmd_argv = _string_list(sarif_cfg, "argv")

    return DeliveryPolicy(
        source=str(POLICY_REL_PATH),
        extra_protected_branches=extra_branches,
        max_file_bytes=max_file_bytes,
        max_large_binary_bytes=max_large_binary_bytes,
        require_trailers=_string_list(data, "require_trailers"),
        require_scanners=_string_list(data, "require_scanners"),
        forbid_paths=_string_list(data, "forbid_paths"),
        allow_reviewed_extensions=extensions,
        sarif_cmd_argv=sarif_cmd_argv,
    )


def enforce_declarative_policy(
    policy: DeliveryPolicy,
    *,
    branch: str | None,
    requested_paths: list[str],
    trailers: list[tuple[str, str]],
    active_scanners: list[str],
    mutating_commit: bool,
) -> None:
    """Enforce declarative TOML rules deterministically before index/repo writes."""
    if branch:
        for pattern in policy.extra_protected_branches:
            if fnmatch.fnmatchcase(branch, pattern):
                raise FinalizerError(
                    ErrorCode.BRANCH_PROTECTED,
                    f"delivery-policy.toml 禁止操作受保护分支: {branch}",
                    final_phase="preflight",
                )
    for rel in requested_paths:
        for pattern in policy.forbid_paths:
            if fnmatch.fnmatchcase(rel, pattern):
                raise FinalizerError(
                    ErrorCode.POLICY_DENIED,
                    f"delivery-policy.toml 禁止提交路径: {rel}",
                    final_phase="local_validation",
                )
    if mutating_commit and policy.require_trailers:
        present = {key for key, _ in trailers}
        missing = [req for req in policy.require_trailers if req not in present]
        if missing:
            raise FinalizerError(
                ErrorCode.POLICY_DENIED,
                f"delivery-policy.toml 要求提供 trailers: {', '.join(missing)}",
                final_phase="preflight",
            )
    if policy.require_scanners:
        missing_scanners = [s for s in policy.require_scanners if s not in active_scanners]
        if missing_scanners:
            raise FinalizerError(
                ErrorCode.POLICY_DENIED,
                f"delivery-policy.toml 要求启用扫描器: {', '.join(missing_scanners)}",
                final_phase="preflight",
            )


def evaluate_opa_policy(
    opa_policy_path: Path,
    *,
    input_document: dict[str, Any],
    query: str = "data.git_finalizer.delivery.deny",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Invoke `opa eval` fail-closed when `--opa-policy` is explicitly requested."""
    if not opa_policy_path.exists():
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            f"OPA policy 路径不存在: {opa_policy_path}",
            final_phase="preflight",
        )
    opa_bin = shutil.which("opa")
    if not opa_bin:
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            "已指定 --opa-policy，但 PATH 中未找到 opa 可执行文件",
            final_phase="preflight",
        )
    # Keep deterministic JSON eval and parse result directly.
    cmd = [
        opa_bin,
        "eval",
        "--format",
        "json",
        "--data",
        str(opa_policy_path),
        "--stdin-input",
        query,
    ]
    payload = json.dumps(input_document, ensure_ascii=False, sort_keys=True)
    try:
        completed = subprocess.run(
            cmd,
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
            close_fds=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            f"OPA policy 执行失败: {exc}",
            final_phase="preflight",
            status="failed",
        ) from exc
    if completed.returncode != 0:
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            f"OPA policy 评估返回非零退出码 ({completed.returncode})",
            final_phase="preflight",
            status="failed",
        )
    try:
        parsed = json.loads(completed.stdout)
        expressions = parsed["result"][0]["expressions"]
        value = expressions[0]["value"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            "OPA policy 输出不是有效判定 JSON",
            final_phase="preflight",
            status="failed",
        ) from exc
    if isinstance(value, bool):
        if not value:
            raise FinalizerError(
                ErrorCode.POLICY_DENIED,
                "OPA policy 拒绝了本次交付",
                final_phase="local_validation",
            )
    elif isinstance(value, (list, set)):
        denials = [str(item) for item in value if item]
        if denials:
            raise FinalizerError(
                ErrorCode.POLICY_DENIED,
                f"OPA policy 拒绝了本次交付: {'; '.join(denials[:3])}",
                final_phase="local_validation",
            )
    else:
        raise FinalizerError(
            ErrorCode.POLICY_DENIED,
            "OPA policy 判定值必须是布尔值或拒绝消息列表",
            final_phase="local_validation",
            status="failed",
        )
    return {"policy_path": str(opa_policy_path), "decision": "allow"}
