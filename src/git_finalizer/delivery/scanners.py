"""Pluggable external scanner SARIF 2.1.0 orchestration (`gitleaks`, `trufflehog`, `osv`, `sarif-cmd`)."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
from typing import Any

from git_finalizer.delivery.policy import DeliveryPolicy
from git_finalizer.errors import ErrorCode, FinalizerError

SUPPORTED_SCANNERS = frozenset({"gitleaks", "trufflehog", "osv", "sarif-cmd"})
MAX_SARIF_BYTES = 16 * 1024 * 1024
BLOCKING_LEVELS = frozenset({"error", "warning", "none", ""})


def _resolve_scanner_argv(
    scanner: str,
    target_dir: Path,
    report_path: Path,
    policy: DeliveryPolicy,
    sarif_cmd: str | None = None,
) -> list[str]:
    if scanner == "gitleaks":
        exe = shutil.which("gitleaks")
        if not exe:
            raise FinalizerError(
                ErrorCode.EXTERNAL_SCANNER_FAILED,
                "已请求 --scanner gitleaks，但 PATH 中未找到 gitleaks",
                final_phase="local_validation",
            )
        return [
            exe,
            "dir",
            str(target_dir),
            "--no-banner",
            "--exit-code",
            "0",
            "--report-format",
            "sarif",
            "--report-path",
            str(report_path),
        ]
    if scanner == "trufflehog":
        exe = shutil.which("trufflehog")
        if not exe:
            raise FinalizerError(
                ErrorCode.EXTERNAL_SCANNER_FAILED,
                "已请求 --scanner trufflehog，但 PATH 中未找到 trufflehog",
                final_phase="local_validation",
            )
        return [exe, "filesystem", str(target_dir), "--no-update", "--json"]
    if scanner == "osv":
        exe = shutil.which("osv-scanner") or shutil.which("osv")
        if not exe:
            raise FinalizerError(
                ErrorCode.EXTERNAL_SCANNER_FAILED,
                "已请求 --scanner osv，但 PATH 中未找到 osv-scanner",
                final_phase="local_validation",
            )
        return [
            exe,
            "scan",
            "source",
            "-r",
            str(target_dir),
            "--format",
            "sarif",
            "--output",
            str(report_path),
        ]
    if scanner == "sarif-cmd":
        if sarif_cmd:
            resolved = shutil.which(sarif_cmd) or sarif_cmd
            exe_path = Path(resolved)
            if not exe_path.is_file() or not os.access(exe_path, os.X_OK):
                raise FinalizerError(
                    ErrorCode.EXTERNAL_SCANNER_FAILED,
                    f"--sarif-cmd 可执行文件不可用: {sarif_cmd}",
                    final_phase="cli",
                )
            return [str(exe_path), str(target_dir), str(report_path)]
        if policy.sarif_cmd_argv:
            first = shutil.which(policy.sarif_cmd_argv[0]) or policy.sarif_cmd_argv[0]
            if not Path(first).is_file() or not os.access(first, os.X_OK):
                raise FinalizerError(
                    ErrorCode.EXTERNAL_SCANNER_FAILED,
                    "policy [scanners.sarif_cmd] 可执行文件不可用",
                    final_phase="cli",
                )
            return [
                str(first),
                *[
                    arg.replace("{target_dir}", str(target_dir)).replace(
                        "{report_path}", str(report_path)
                    )
                    for arg in policy.sarif_cmd_argv[1:]
                ],
            ]
        raise FinalizerError(
            ErrorCode.EXTERNAL_SCANNER_FAILED,
            "使用 --scanner sarif-cmd 时必须提供 --sarif-cmd 或 .agents/delivery-policy.toml [scanners.sarif_cmd]",
            final_phase="cli",
        )
    raise FinalizerError(
        ErrorCode.CLI_INVALID_ARGS,
        f"不支持的 --scanner 值: {scanner}",
        final_phase="cli",
    )


def parse_sarif_findings(raw_bytes: bytes, scanner: str) -> list[dict[str, Any]]:
    """Parse SARIF 2.1.0 JSON without retaining raw secret snippets."""
    if len(raw_bytes) > MAX_SARIF_BYTES:
        raise FinalizerError(
            ErrorCode.EXTERNAL_SCANNER_FAILED,
            f"外部扫描器 {scanner} 输出超过 SARIF 体积上限",
            status="failed",
            final_phase="local_validation",
        )
    try:
        document = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalizerError(
            ErrorCode.EXTERNAL_SCANNER_FAILED,
            f"外部扫描器 {scanner} 未返回合法 SARIF JSON: {exc}",
            status="failed",
            final_phase="local_validation",
        ) from exc
    if not isinstance(document, dict) or str(document.get("version", "")) != "2.1.0":
        raise FinalizerError(
            ErrorCode.EXTERNAL_SCANNER_FAILED,
            f"外部扫描器 {scanner} 仅支持 SARIF 2.1.0 输出",
            status="failed",
            final_phase="local_validation",
        )
    runs = document.get("runs")
    if not isinstance(runs, list):
        raise FinalizerError(
            ErrorCode.EXTERNAL_SCANNER_FAILED,
            f"外部扫描器 {scanner} 缺少 SARIF runs 列表",
            status="failed",
            final_phase="local_validation",
        )
    findings: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        results = run.get("results")
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, dict):
                continue
            suppressions = item.get("suppressions")
            if isinstance(suppressions, list) and suppressions:
                continue
            kind = str(item.get("kind", "fail"))
            if kind in {"pass", "notApplicable", "informational"}:
                continue
            level = str(item.get("level", "error")).lower()
            if level not in {"error", "warning"}:
                continue
            rule_id = str(item.get("ruleId") or "sarif-finding")
            path_str = _extract_sarif_path(item)
            line_no = _extract_sarif_line(item)
            findings.append(
                {
                    "scanner": scanner,
                    "rule_id": rule_id[:128],
                    "level": level,
                    "path": path_str,
                    "line": line_no,
                }
            )
    return findings


def _extract_sarif_path(item: dict[str, Any]) -> str | None:
    locations = item.get("locations")
    if not isinstance(locations, list) or not locations:
        return None
    phys = locations[0].get("physicalLocation") if isinstance(locations[0], dict) else None
    artifact = phys.get("artifactLocation") if isinstance(phys, dict) else None
    uri = artifact.get("uri") if isinstance(artifact, dict) else None
    return str(uri) if isinstance(uri, str) and uri else None


def _extract_sarif_line(item: dict[str, Any]) -> int | None:
    locations = item.get("locations")
    if not isinstance(locations, list) or not locations:
        return None
    phys = locations[0].get("physicalLocation") if isinstance(locations[0], dict) else None
    region = phys.get("region") if isinstance(phys, dict) else None
    line = region.get("startLine") if isinstance(region, dict) else None
    return int(line) if isinstance(line, int) and line > 0 else None


def _parse_trufflehog_ndjson(raw_bytes: bytes) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for raw_line in raw_bytes.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FinalizerError(
                ErrorCode.EXTERNAL_SCANNER_FAILED,
                f"trufflehog JSON 输出解析失败: {exc}",
                status="failed",
                final_phase="local_validation",
            ) from exc
        if not isinstance(entry, dict) or "DetectorName" not in entry:
            continue
        meta = entry.get("SourceMetadata", {}).get("Data", {}).get("Filesystem", {})
        findings.append(
            {
                "scanner": "trufflehog",
                "rule_id": str(entry.get("DetectorName") or "trufflehog"),
                "level": "error",
                "path": str(meta.get("file")) if meta.get("file") else None,
                "line": int(meta["line"]) if isinstance(meta.get("line"), int) else None,
            }
        )
    return findings


def run_external_scanners(
    candidates: dict[str, bytes],
    scanners: list[str],
    policy: DeliveryPolicy,
    *,
    sarif_cmd: str | None = None,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """Materialize validated candidates into a fresh temp tree and run configured SARIF scanners."""
    if not scanners or not candidates:
        return []
    reports: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="gf-ext-scan-") as temporary:
        root = Path(temporary)
        target_dir = root / "tree"
        target_dir.mkdir(mode=0o700)
        for rel, data in candidates.items():
            posix = PurePosixPath(rel)
            if posix.is_absolute() or ".." in posix.parts:
                raise FinalizerError(
                    ErrorCode.SCOPE_INVALID,
                    f"外部扫描器拒绝非法路径: {rel}",
                    final_phase="local_validation",
                )
            dest = target_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

        all_findings: list[dict[str, Any]] = []
        for idx, scanner in enumerate(scanners):
            if scanner not in SUPPORTED_SCANNERS:
                raise FinalizerError(
                    ErrorCode.CLI_INVALID_ARGS,
                    f"不支持的外部扫描器: {scanner}",
                    final_phase="cli",
                )
            report_path = root / f"report-{idx}.sarif.json"
            argv = _resolve_scanner_argv(scanner, target_dir, report_path, policy, sarif_cmd=sarif_cmd)
            try:
                completed = subprocess.run(
                    argv,
                    capture_output=True,
                    timeout=timeout,
                    env={**os.environ, "LC_ALL": "C"},
                    close_fds=True,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise FinalizerError(
                    ErrorCode.EXTERNAL_SCANNER_FAILED,
                    f"外部扫描器 {scanner} 执行失败: {exc}",
                    status="failed",
                    final_phase="local_validation",
                ) from exc

            if scanner == "trufflehog":
                if completed.returncode not in (0, 183):
                    raise FinalizerError(
                        ErrorCode.EXTERNAL_SCANNER_FAILED,
                        f"trufflehog 返回失败退出码 {completed.returncode}",
                        status="failed",
                        final_phase="local_validation",
                    )
                findings = _parse_trufflehog_ndjson(completed.stdout)
            else:
                if completed.returncode != 0 and not (
                    scanner == "osv" and completed.returncode == 1 and report_path.is_file()
                ) and not (
                    scanner == "sarif-cmd" and report_path.is_file()
                ):
                    raise FinalizerError(
                        ErrorCode.EXTERNAL_SCANNER_FAILED,
                        f"外部扫描器 {scanner} 返回失败退出码 {completed.returncode}",
                        status="failed",
                        final_phase="local_validation",
                    )
                raw_sarif = report_path.read_bytes() if report_path.is_file() else completed.stdout
                findings = parse_sarif_findings(raw_sarif, scanner)

            all_findings.extend(findings)
            reports.append({"scanner": scanner, "findings_count": len(findings)})

        if all_findings:
            first = all_findings[0]
            summary_msg = f"{first['scanner']}({first['rule_id']})"
            if first.get("path"):
                summary_msg += f" at {first['path']}"
            raise FinalizerError(
                ErrorCode.EXTERNAL_SCANNER_BLOCKED,
                f"外部扫描器发现安全风险 ({len(all_findings)} 项): {summary_msg}",
                final_phase="local_validation",
                diagnostics={
                    "schema_version": 1,
                    "external_scanners": all_findings[:10],
                },
            )
    return reports
