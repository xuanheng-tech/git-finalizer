#!/usr/bin/env python3
"""Run one explicitly escalated Git Finalizer call on the host.

Codex 0.145.0 keeps shell calls inside the permission-profile sandbox when the
profile contains denied reads, even when the raw tool call requests escalation.
This fail-closed hook restores the already-approved Finalizer boundary without
granting host execution to any other command.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess
import sys
from typing import Any


FINALIZER = "/home/hsd/bin/codex-git-finalize"
FINALIZER_NAME = "codex-git-finalize"
RULES_FILE = Path("/home/hsd/.codex/rules/default.rules")
ALLOW_RULE = (
    'prefix_rule(pattern=["/home/hsd/bin/codex-git-finalize"], decision="allow")'
)
TRANSCRIPT_ROOT = Path("/home/hsd/.codex/sessions")
FINALIZER_TIMEOUT_SECONDS = 300
MAX_CONTEXT_CHARS = 12_000
TOOL_USE_ID_PATTERN = re.compile(
    r"^exec-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
SHELL_CONTROL_TOKENS = {"&", "&&", "(", ")", ";", "<", "<<", ">", ">>", "|", "||"}


class BridgeError(Exception):
    """An invariant failed before the Finalizer could run."""


def emit_hook_output(
    *,
    decision: str,
    reason: str | None = None,
    updated_command: str | None = None,
    additional_context: str | None = None,
) -> None:
    output: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
    }
    if reason is not None:
        output["permissionDecisionReason"] = reason
    if updated_command is not None:
        output["updatedInput"] = {"command": updated_command}
    if additional_context is not None:
        output["additionalContext"] = additional_context
    print(json.dumps({"hookSpecificOutput": output}, ensure_ascii=False))


def resolve_transcript(raw_path: object) -> Path:
    if not isinstance(raw_path, str):
        raise BridgeError("会话 transcript 不可用")

    path = Path(raw_path)
    if path.is_symlink():
        raise BridgeError("会话 transcript 不得是符号链接")

    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(TRANSCRIPT_ROOT.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise BridgeError("会话 transcript 不在受信任的 sessions 目录") from exc

    if not resolved.is_file() or resolved.stat().st_uid != os.getuid():
        raise BridgeError("会话 transcript 不是当前用户拥有的普通文件")
    return resolved


def load_transcript_arguments(
    raw_path: object,
    call_id: object,
) -> dict[str, Any] | None:
    if not isinstance(raw_path, str) or not isinstance(call_id, str):
        return None
    try:
        transcript = resolve_transcript(raw_path)
    except BridgeError:
        return None

    matched: dict[str, Any] | None = None
    with transcript.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if (
                payload.get("type") != "function_call"
                or payload.get("name") != "exec_command"
                or payload.get("call_id") != call_id
            ):
                continue
            encoded = payload.get("arguments")
            if not isinstance(encoded, str):
                raise BridgeError("transcript 中的 exec_command 参数格式异常")
            try:
                candidate = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise BridgeError("transcript 中的 exec_command 参数不是 JSON") from exc
            if not isinstance(candidate, dict):
                raise BridgeError("transcript 中的 exec_command 参数不是对象")
            if matched is not None and candidate != matched:
                raise BridgeError("transcript 中存在冲突的 exec_command 参数")
            matched = candidate
    return matched


def _explicit_paths(values: list[str]) -> tuple[str, ...]:
    if not values:
        raise BridgeError("Finalizer 必须在 -- 后显式列出文件路径")
    normalized: list[str] = []
    for value in values:
        path = PurePosixPath(value)
        if (
            not value
            or value == "."
            or value.startswith("-")
            or value in SHELL_CONTROL_TOKENS
            or "\\" in value
            or any(character in value for character in ("\x00", "\n", "\r"))
            or path.is_absolute()
            or value != path.as_posix()
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise BridgeError("Finalizer 显式文件路径不安全或不规范")
        normalized.append(value)
    if len(normalized) != len(set(normalized)):
        raise BridgeError("Finalizer 显式文件路径不得重复")
    return tuple(normalized)


def parse_direct_finalizer(
    raw_command: object,
) -> tuple[list[str], str, tuple[str, ...], bool]:
    if not isinstance(raw_command, str):
        raise BridgeError("PreToolUse tool_input 缺少 command")
    try:
        argv = shlex.split(raw_command, posix=True)
    except ValueError as exc:
        raise BridgeError("Finalizer shell 命令无法安全解析") from exc
    if not argv or argv[0] != FINALIZER:
        raise BridgeError(
            "Finalizer 必须以固定绝对路径作为直接命令，不能经 shell、env 或管道包装"
        )
    delimiters = [index for index, value in enumerate(argv) if value == "--"]
    if len(delimiters) > 1:
        raise BridgeError("Finalizer 最多只能包含一个 -- 文件分隔符")
    if delimiters:
        delimiter = delimiters[0]
        options = argv[1:delimiter]
        path_values = argv[delimiter + 1 :]
    else:
        options = argv[1:]
        path_values = []
    parsed: dict[str, str] = {}
    dry_run = False
    summary = False
    initial_publish = False
    initial_branch_publish = False
    index = 0
    while index < len(options):
        option = options[index]
        if option == "--dry-run":
            if dry_run:
                raise BridgeError("Finalizer --dry-run 不得重复")
            dry_run = True
            index += 1
            continue
        if option == "--summary":
            if summary:
                raise BridgeError("Finalizer --summary 不得重复")
            summary = True
            index += 1
            continue
        if option == "--initial-publish":
            if initial_publish:
                raise BridgeError("Finalizer --initial-publish 不得重复")
            initial_publish = True
            index += 1
            continue
        if option == "--initial-branch-publish":
            if initial_branch_publish:
                raise BridgeError("Finalizer --initial-branch-publish 不得重复")
            initial_branch_publish = True
            index += 1
            continue
        if option not in (
            "--message",
            "--mode",
            "--remote",
            "--remote-branch",
            "--repo",
            "--resume-initial-publish",
            "--resume-publish",
            "--snapshot",
        ) or index + 1 >= len(options):
            raise BridgeError("Finalizer 包含未授权参数或缺少参数值")
        if option in parsed:
            raise BridgeError("Finalizer 参数不得重复")
        parsed[option] = options[index + 1]
        index += 2

    mode = parsed.get("--mode")
    if mode is not None and mode not in ("commit-only", "verify-only"):
        raise BridgeError("Finalizer --mode 仅支持 commit-only 或 verify-only")
    verify_only = mode == "verify-only"
    resume_initial_oid = parsed.get("--resume-initial-publish")
    resume_publish_oid = parsed.get("--resume-publish")
    if resume_initial_oid is not None and resume_publish_oid is not None:
        raise BridgeError("Finalizer resume 发布入口只能选择一个")
    resume = resume_initial_oid is not None or resume_publish_oid is not None
    if resume:
        if delimiters:
            raise BridgeError("Finalizer resume 模式不接受 -- 分隔符或文件路径")
        paths: tuple[str, ...] = ()
    else:
        if len(delimiters) != 1:
            raise BridgeError("Finalizer 必须且只能包含一个 -- 文件分隔符")
        paths = _explicit_paths(path_values)

    required = {"--repo"} if verify_only or resume else {"--message", "--repo"}
    if not required.issubset(parsed):
        if verify_only:
            raise BridgeError("Finalizer verify-only 必须显式提供 --repo")
        if resume:
            raise BridgeError("Finalizer resume 必须显式提供 --repo")
        raise BridgeError("Finalizer 必须显式提供 --repo 和 --message")
    if mode in ("commit-only", "verify-only") and (
        initial_publish or initial_branch_publish
    ):
        raise BridgeError(f"Finalizer --mode {mode} 不接受其他发布模式")
    if verify_only:
        if "--message" in parsed:
            raise BridgeError("Finalizer --mode verify-only 不接受 --message")
        if dry_run:
            raise BridgeError("Finalizer --mode verify-only 不接受 --dry-run")
    if resume:
        resume_oid = resume_initial_oid or resume_publish_oid
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", resume_oid or "") is None:
            raise BridgeError("Finalizer resume 要求完整小写十六进制 HEAD OID")
        if mode is not None or initial_publish or initial_branch_publish:
            raise BridgeError("Finalizer resume 不接受其他 mode 或发布入口")
        if "--message" in parsed:
            raise BridgeError("Finalizer resume 不接受 --message")
        if dry_run:
            raise BridgeError("Finalizer resume 不接受 --dry-run")
        if "--snapshot" in parsed or "--remote-branch" in parsed:
            raise BridgeError("Finalizer resume 不接受 snapshot 或 remote-branch")
    if initial_publish and initial_branch_publish:
        raise BridgeError("Finalizer 发布模式只能选择一个")
    if resume_initial_oid is not None:
        remote = parsed.get("--remote")
        if remote is None:
            raise BridgeError("Finalizer root resume 必须显式提供 --remote")
        if (
            not remote
            or remote.startswith("-")
            or any(character in remote for character in ("\x00", "\n", "\r", "\t"))
        ):
            raise BridgeError("Finalizer --remote 不是有效的 remote 名")
    elif resume_publish_oid is not None:
        if "--remote" in parsed:
            raise BridgeError(
                "Finalizer --resume-publish 从 configured upstream 推导目标，不接受 --remote"
            )
    elif initial_publish:
        remote = parsed.get("--remote")
        if remote is None:
            raise BridgeError("Finalizer --initial-publish 必须显式提供 --remote")
        if (
            not remote
            or remote.startswith("-")
            or any(character in remote for character in ("\x00", "\n", "\r", "\t"))
        ):
            raise BridgeError("Finalizer --remote 不是有效的 remote 名")
        snapshot = parsed.get("--snapshot")
        if snapshot is not None and re.fullmatch(r"[0-9a-f]{64}", snapshot) is None:
            raise BridgeError("Finalizer --snapshot 不是有效的 Snapshot Runner ID")
        if "--remote-branch" in parsed:
            raise BridgeError("Finalizer --initial-publish 不接受 --remote-branch")
    elif initial_branch_publish:
        remote = parsed.get("--remote")
        remote_branch = parsed.get("--remote-branch")
        if remote is None or remote_branch is None:
            raise BridgeError(
                "Finalizer --initial-branch-publish 必须显式提供 --remote 和 --remote-branch"
            )
        if (
            not remote
            or remote.startswith("-")
            or any(character in remote for character in ("\x00", "\n", "\r", "\t"))
        ):
            raise BridgeError("Finalizer --remote 不是有效的 remote 名")
        if (
            not remote_branch
            or remote_branch.startswith("-")
            or any(
                character in remote_branch for character in ("\x00", "\n", "\r", "\t")
            )
        ):
            raise BridgeError("Finalizer --remote-branch 不是有效的 branch 名")
        if "--snapshot" in parsed:
            raise BridgeError("Finalizer --initial-branch-publish 不接受 --snapshot")
    elif "--remote" in parsed:
        raise BridgeError(
            "Finalizer --remote 只允许与 --initial-publish 或 --initial-branch-publish 一起使用"
        )
    elif "--remote-branch" in parsed:
        raise BridgeError(
            "Finalizer --remote-branch 只允许与 --initial-branch-publish 一起使用"
        )
    elif "--snapshot" in parsed:
        raise BridgeError("Finalizer --snapshot 只允许与 --initial-publish 一起使用")
    message = parsed.get("--message")
    if message is not None and (
        not message
        or any(character in message for character in ("\x00", "\n", "\r"))
    ):
        raise BridgeError("Finalizer --message 不得为空或包含换行")
    return argv, parsed["--repo"], paths, dry_run


def resolve_repo(raw_repo: object) -> Path:
    if not isinstance(raw_repo, str):
        raise BridgeError("Finalizer --repo 缺少路径")
    repo_arg = Path(raw_repo)
    if not repo_arg.is_absolute():
        raise BridgeError("Finalizer --repo 必须是绝对路径")

    try:
        repo = repo_arg.resolve(strict=True)
    except OSError as exc:
        raise BridgeError("Finalizer repo 无法解析") from exc
    if not repo.is_dir():
        raise BridgeError("Finalizer repo 不是目录")
    return repo


def validate_hook_event(event: object) -> tuple[str, list[str], Path, bool]:
    if not isinstance(event, dict):
        raise BridgeError("PreToolUse payload 必须是对象")
    if event.get("hook_event_name") != "PreToolUse":
        raise BridgeError("hook event 不是 PreToolUse")
    if event.get("tool_name") != "Bash":
        raise BridgeError("Finalizer 只能从真实观测到的 Bash hook 工具调用")
    if event.get("permission_mode") != "default":
        raise BridgeError("PreToolUse permission_mode 与已验证结构不一致")
    tool_use_id = event.get("tool_use_id")
    if (
        not isinstance(tool_use_id, str)
        or TOOL_USE_ID_PATTERN.fullmatch(tool_use_id) is None
    ):
        raise BridgeError("PreToolUse tool_use_id 与已验证结构不一致")

    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict) or set(tool_input) != {"command"}:
        raise BridgeError("PreToolUse tool_input 必须只包含 command")
    public_command = tool_input.get("command")
    if not isinstance(public_command, str):
        raise BridgeError("PreToolUse tool_input 缺少 command")
    argv, raw_repo, _paths, dry_run = parse_direct_finalizer(public_command)
    repo = resolve_repo(raw_repo)
    return public_command, argv, repo, dry_run


def cross_check_transcript(
    raw_arguments: dict[str, Any] | None,
    *,
    public_command: str,
    repo: Path,
) -> None:
    if raw_arguments is None:
        return
    if raw_arguments.get("cmd") != public_command:
        raise BridgeError("transcript command 与 PreToolUse payload 不一致")
    raw_workdir = raw_arguments.get("workdir")
    if not isinstance(raw_workdir, str):
        raise BridgeError("transcript workdir 缺失")
    try:
        transcript_workdir = Path(raw_workdir).resolve(strict=True)
    except OSError as exc:
        raise BridgeError("transcript workdir 无法解析") from exc
    if transcript_workdir != repo:
        raise BridgeError("transcript workdir 与 Finalizer --repo 不一致")
    if raw_arguments.get("sandbox_permissions") != "require_escalated":
        raise BridgeError("transcript 中的 Finalizer 调用未请求 require_escalated")


def require_existing_allow_rule() -> None:
    try:
        rules = {
            line.strip() for line in RULES_FILE.read_text(encoding="utf-8").splitlines()
        }
    except OSError as exc:
        raise BridgeError("无法核对既有 Git Finalizer allow rule") from exc
    if ALLOW_RULE not in rules:
        raise BridgeError("既有 Git Finalizer allow rule 不存在或已改变")


def redact_and_bound(output: str) -> str:
    output = re.sub(
        r"(https?://)[^/@\s]+@",
        r"\1[redacted]@",
        output,
    )
    output = re.sub(
        r"([?&](?:access_token|auth|credential|password|secret|token)=)[^&\s]+",
        r"\1[redacted]",
        output,
        flags=re.IGNORECASE,
    )
    output = re.sub(
        r"(github_pat_|gh[pousr]_|sk-(?:proj-)?|xox[baprs]-)[A-Za-z0-9_.-]{8,}",
        r"\1[redacted]",
        output,
    )
    output = output.strip() or "<no output>"
    if len(output) <= MAX_CONTEXT_CHARS:
        return output
    half = MAX_CONTEXT_CHARS // 2
    omitted = len(output) - (half * 2)
    return (
        output[:half]
        + f"\n... <bridge omitted {omitted} characters> ...\n"
        + output[-half:]
    )


def run_finalizer(argv: list[str], repo: Path) -> tuple[int, str]:
    process = subprocess.Popen(
        argv,
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=FINALIZER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
        return 124, output + "\nERROR: Git Finalizer host bridge timed out"
    return process.returncode, output


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        emit_hook_output(
            decision="deny",
            reason="Git Finalizer host bridge 无法解析 hook 输入",
        )
        return

    tool_input = event.get("tool_input") if isinstance(event, dict) else None
    if not isinstance(tool_input, dict):
        emit_hook_output(decision="deny", reason="PreToolUse tool_input 缺失或格式异常")
        return
    public_command = tool_input.get("command")
    if not isinstance(public_command, str):
        emit_hook_output(
            decision="deny", reason="PreToolUse tool_input.command 缺失或格式异常"
        )
        return
    if FINALIZER_NAME not in public_command:
        return

    try:
        public_command, argv, repo, dry_run = validate_hook_event(event)
        raw_arguments = load_transcript_arguments(
            event.get("transcript_path"),
            event.get("tool_use_id"),
        )
        cross_check_transcript(
            raw_arguments,
            public_command=public_command,
            repo=repo,
        )
        if dry_run:
            return
        require_existing_allow_rule()
        returncode, output = run_finalizer(argv, repo)
        bounded_output = redact_and_bound(output)
    except BridgeError as exc:
        emit_hook_output(decision="deny", reason=str(exc))
        return
    except Exception as exc:  # Fail closed without exposing a traceback to the model.
        emit_hook_output(
            decision="deny",
            reason=f"Git Finalizer host bridge 内部失败：{type(exc).__name__}",
        )
        return

    if returncode == 0:
        context = (
            "Git Finalizer host bridge 已在 sandbox 外原样执行一次完整命令；"
            "原 Bash 输入已改写为 /usr/bin/true，不得重跑。\n"
            f"FINALIZER_HOST_EXIT={returncode}\n{bounded_output}"
        )
        emit_hook_output(
            decision="allow",
            updated_command="/usr/bin/true",
            additional_context=context,
        )
    else:
        context = (
            "Git Finalizer host bridge 已在 sandbox 外原样执行一次完整命令；"
            "原 Bash 输入已由 hook 阻断，未进入 sandbox，不得重跑。\n"
            f"FINALIZER_HOST_EXIT={returncode}\n{bounded_output}"
        )
        emit_hook_output(
            decision="deny",
            reason=context,
        )


if __name__ == "__main__":
    main()
