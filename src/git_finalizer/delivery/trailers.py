"""AI provenance Git trailers and optional Agent Trace Git Notes (`refs/notes/agent-trace`)."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import re
import tempfile
import uuid

from git_finalizer import TOOL_NAME, __version__
from git_finalizer.errors import ErrorCode, FinalizerError
from git_finalizer.git_io import run_git, validate_oid
from git_finalizer.json_io import sha256_file

AGENT_TRACE_REF = "refs/notes/agent-trace"
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _validate_trailer_value(value: str, flag: str) -> str:
    stripped = value.strip()
    if not stripped or len(stripped) > 512 or CONTROL_CHARS_RE.search(stripped):
        raise FinalizerError(
            ErrorCode.CLI_INVALID_ARGS,
            f"{flag} 值非法（禁止空值、换行或控制字符）",
            final_phase="cli",
        )
    return stripped


def build_provenance_trailers(
    *,
    trailer_agent: str | None = None,
    trace_ref: str | None = None,
    snapshot_id: str | None = None,
) -> list[tuple[str, str]]:
    """Return validated `(key, value)` Git trailer pairs for commit formatting."""
    trailers: list[tuple[str, str]] = []
    if trailer_agent is not None:
        trailers.append(("Assisted-by", _validate_trailer_value(trailer_agent, "--trailer-agent")))
    if snapshot_id is not None:
        validate_oid(snapshot_id, "snapshot ID")
        trailers.append(("Snapshot-Evidence-Id", snapshot_id))
    if trace_ref is not None:
        trailers.append(("Agent-Trace-Ref", _validate_trailer_value(trace_ref, "--trace-ref")))
    return trailers


def format_commit_message_with_trailers(
    repo: Path,
    message: str,
    trailers: list[tuple[str, str]],
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Apply canonical RFC-822 trailers using `git interpret-trailers`."""
    if not trailers:
        return message
    args = ["interpret-trailers", "--if-exists", "addIfDifferent"]
    for key, value in trailers:
        args.extend(["--trailer", f"{key}: {value}"])
    completed = run_git(
        repo,
        *args,
        text=False,
        env=env,
        input_data=(message.rstrip("\n") + "\n").encode("utf-8"),
    )
    if completed.returncode != 0:
        raise FinalizerError(
            ErrorCode.COMMIT_FAILED,
            "无法使用 git interpret-trailers 写入 AI provenance trailers",
            status="failed",
            final_phase="commit",
        )
    return completed.stdout.decode("utf-8", errors="strict")


def write_agent_trace_note(
    repo: Path,
    commit_sha: str,
    paths: list[str],
    *,
    trailer_agent: str | None = None,
    trace_ref: str | None = None,
    snapshot_id: str | None = None,
    task_key: str | None = None,
    allocation_id: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Attach an Agent Trace JSON record as a Git note under `refs/notes/agent-trace`."""
    validate_oid(commit_sha, "commit OID")
    file_records: list[dict[str, object]] = []
    for rel in sorted(dict.fromkeys(paths)):
        target = repo / rel
        entry: dict[str, object] = {"path": rel}
        if target.exists() and not target.is_symlink() and target.is_file():
            entry["sha256"] = sha256_file(target)
        else:
            entry["status"] = "deleted_or_nonregular"
        file_records.append(entry)

    record: dict[str, object] = {
        "schema": "https://agent-trace.dev/v0/record.schema.json",
        "version": "0.1.0",
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"git-finalizer:{commit_sha}:{','.join(sorted(paths))}")),
        "timestamp": datetime.now(UTC).isoformat(),
        "vcs": {
            "type": "git",
            "revision": commit_sha,
        },
        "tool": {
            "name": TOOL_NAME,
            "version": __version__,
        },
        "files": file_records,
    }
    metadata: dict[str, object] = {}
    if trailer_agent:
        metadata["agent"] = _validate_trailer_value(trailer_agent, "--trailer-agent")
    if trace_ref:
        metadata["trace_ref"] = _validate_trailer_value(trace_ref, "--trace-ref")
    if snapshot_id:
        metadata["snapshot_evidence_id"] = snapshot_id
    if task_key:
        metadata["task_key"] = task_key
    if allocation_id:
        metadata["allocation_id"] = allocation_id
    if metadata:
        record["metadata"] = metadata

    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(prefix="gf-agent-trace-", suffix=".json", delete=True) as tmp:
        tmp.write(payload)
        tmp.flush()
        completed = run_git(
            repo,
            "notes",
            "--ref",
            AGENT_TRACE_REF,
            "add",
            "-f",
            "-F",
            tmp.name,
            commit_sha,
            env=env,
        )
    if completed.returncode != 0:
        raise FinalizerError(
            ErrorCode.COMMIT_FAILED,
            "写入 Agent Trace Git Note 失败",
            status="failed",
            final_phase="commit",
            commit_created=True,
            commit_sha=commit_sha,
        )
    rev = run_git(repo, "notes", "--ref", AGENT_TRACE_REF, "list", commit_sha, env=env)
    note_oid = rev.stdout.decode("ascii", errors="replace").strip() if rev.returncode == 0 else None
    return {
        "ref": AGENT_TRACE_REF,
        "recorded": True,
        "note_oid": note_oid or None,
    }
