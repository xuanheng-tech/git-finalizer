# GF-INTEGRATION-ADAPTER: worktree-controller-integration-publication (single site: Controller publication session & lease-bound push verification)
"""Execute lease-bound integration candidate publication through Worktree Controller's public session protocol."""

from __future__ import annotations

import argparse
from contextlib import suppress
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import time
from typing import Any
import uuid

from git_finalizer import __version__
from git_finalizer.errors import ErrorCode
from git_finalizer.json_io import loads_unique

SUMMARY_SCHEMA_VERSION = 1
VERSION = __version__
MODE = "integration_candidate_publish"
CONTRACT = "integration-publication-session/v1"
FULL_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
LINE_BYTES = 16 * 1024
RESPONSE_TIMEOUT_SECONDS = 30.0
GIT_LOCAL_TIMEOUT = 30.0
GIT_REMOTE_TIMEOUT = 120.0
NEXT_ACTION = "read_remote_fact_and_reenter_controller_publish_gate"


class PublishError(RuntimeError):
    """Fail-closed publication blocker preserving attempted remote state."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = ErrorCode.INTEGRATION_PUBLISH_BLOCKED,
        exit_code: int = 1,
        next_action: str = NEXT_ACTION,
        final_phase: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.exit_code = exit_code
        self.next_action = next_action
        self.final_phase = final_phase
        super().__init__(message)


class PublicationProgress:
    """Structured progress receipt distinguishing blocked vs remote-attempted state."""

    def __init__(self, *, candidate_oid: str, lease_id: str, run_id: str) -> None:
        self.phase = "preflight"
        self.candidate_oid = candidate_oid
        self.lease_id = lease_id
        self.run_id = run_id
        self.identity: dict[str, Any] | None = None
        self.repository_id: str | None = None
        self.allocation_id: str | None = None
        self.target_ref: str | None = None
        self.expected_main_oid: str | None = None
        self.remote: str | None = None
        self.mutation_attempted = False
        self.push_returncode: int | None = None
        self.remote_verified = False
        self.remote_oid: str | None = None
        self.controller_completed = False
        self.record_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "phase": self.phase,
            "candidate_oid": self.candidate_oid,
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "identity": self.identity,
            "repository_id": self.repository_id,
            "allocation_id": self.allocation_id,
            "target_ref": self.target_ref,
            "expected_main_oid": self.expected_main_oid,
            "remote": self.remote,
            "mutation_attempted": self.mutation_attempted,
            "push_returncode": self.push_returncode,
            "remote_verified": self.remote_verified,
            "remote_oid": self.remote_oid,
            "controller_completed": self.controller_completed,
            "record_id": self.record_id,
        }


def run_git(repo: Path, *arguments: str, timeout: float = GIT_LOCAL_TIMEOUT) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        close_fds=True,
        check=False,
    )


def controller_program() -> str:
    found = shutil.which("worktree-controller")
    if not found:
        raise PublishError("Worktree Controller is required for integration candidate publication")
    return found


def validate_oid(value: str, label: str) -> str:
    if FULL_OID_RE.fullmatch(value) is None:
        raise PublishError(f"invalid {label}: {value!r}")
    return value


def validate_safe_remote(remote: str) -> str:
    if not remote or remote.startswith("-") or any(c.isspace() for c in remote) or ".." in remote:
        raise PublishError(f"Controller publication identity invalid remote name: {remote!r}")
    return remote


def validate_target_ref(target_ref: str) -> str:
    if not target_ref.startswith("refs/heads/") or len(target_ref) <= len("refs/heads/") or ".." in target_ref:
        raise PublishError(f"unsupported target ref: {target_ref!r}")
    return target_ref


def verify_controller_capabilities(controller: str, repo: Path) -> None:
    try:
        completed = subprocess.run(
            [controller, "capabilities", "--repo", str(repo), "--json"],
            capture_output=True,
            text=True,
            timeout=RESPONSE_TIMEOUT_SECONDS,
            close_fds=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PublishError(f"Controller capabilities failed: {exc}") from exc
    if completed.returncode != 0:
        raise PublishError("Controller capabilities refused or failed")
    try:
        payload = loads_unique(completed.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PublishError("Controller capabilities returned invalid JSON") from exc
    decision = payload.get("decision") if isinstance(payload, dict) else None
    if not isinstance(decision, dict):
        raise PublishError("Controller capabilities verdict is invalid")
    if payload.get("errors") not in (None, []):
        raise PublishError("Controller capabilities returned errors")
    cap_ver = decision.get("integration_publication_execution_version")
    if type(cap_ver) is not int or cap_ver != 1:
        raise PublishError("Controller lacks required public integration publication protocol")


class ControllerChannel:
    """Bounded line-oriented JSON-line session speaking integration-publication-session/v1."""

    def __init__(self, controller: str, repo: Path, lease_id: str, run_id: str) -> None:
        self.process = subprocess.Popen(
            [
                controller,
                "publication-execution-session",
                "--repo",
                str(repo),
                "--lease-id",
                lease_id,
                "--run-id",
                run_id,
                "--json",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            close_fds=True,
        )

    def close(self) -> None:
        if self.process.stdin and not self.process.stdin.closed:
            with suppress(OSError):
                self.process.stdin.close()
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2.0)
        for stream in (self.process.stdout, self.process.stderr):
            if stream and not stream.closed:
                stream.close()

    def read_message(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        fd = self.process.stdout.fileno()
        deadline = time.monotonic() + RESPONSE_TIMEOUT_SECONDS
        buf = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PublishError(
                    "Controller publication session timed out",
                    error_code=ErrorCode.INTEGRATION_PUBLISH_FAILED,
                )
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise PublishError(
                    "Controller publication session timed out",
                    error_code=ErrorCode.INTEGRATION_PUBLISH_FAILED,
                )
            chunk = os.read(fd, 1)
            if not chunk:
                raise PublishError("Controller publication session closed unexpectedly")
            if chunk == b"\n":
                break
            buf.extend(chunk)
            if len(buf) > LINE_BYTES:
                raise PublishError("Controller publication response exceeded size budget")
        try:
            payload = loads_unique(bytes(buf))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PublishError(f"Controller publication returned invalid JSON ({exc})") from exc
        if not isinstance(payload, dict):
            raise PublishError("Controller publication response must be a JSON object")
        return payload

    def send_json(self, message: dict[str, Any]) -> dict[str, Any]:
        assert self.process.stdin is not None
        line = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except OSError as exc:
            raise PublishError("failed writing to Controller publication session") from exc
        return self.read_message()


def remote_head_oid(repo: Path, remote: str, target_ref: str) -> str | None:
    completed = run_git(repo, "ls-remote", "--exit-code", "--refs", "--", remote, target_ref, timeout=GIT_REMOTE_TIMEOUT)
    if completed.returncode == 2:
        return None
    if completed.returncode != 0:
        raise PublishError(f"failed querying remote ref {target_ref}")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    parts = lines[0].split()
    if len(parts) != 2 or parts[1] != target_ref:
        raise PublishError("ambiguous ls-remote response")
    return validate_oid(parts[0], "remote OID")


def _validate_phase_message(
    message: dict[str, Any],
    *,
    expected_phase: str,
    expected_lease: str,
    expected_run: str,
    expected_candidate: str,
) -> dict[str, Any]:
    if type(message.get("schema_version")) is not int or message["schema_version"] != 1:
        raise PublishError("Controller publication schema_version mismatch")
    if message.get("contract") != CONTRACT:
        raise PublishError("Controller publication contract mismatch")
    phase = message.get("phase")
    if phase == "blocked":
        code = message.get("code") or "CONTROLLER_BLOCKED"
        reason = message.get("reason") or "public protocol refused execution"
        raise PublishError(f"Controller publication blocked ({code}): {reason}")
    if phase != expected_phase:
        raise PublishError(f"Controller publication phase drift: expected {expected_phase}, got {phase}")

    identity = message.get("identity")
    if not isinstance(identity, dict):
        raise PublishError("Controller publication message missing identity")
    if (
        type(identity.get("record_version")) is not int
        or identity.get("lease_id") != expected_lease
        or identity.get("run_id") != expected_run
        or identity.get("candidate_oid") != expected_candidate
    ):
        raise PublishError("Controller publication identity drift detected")
    validate_safe_remote(str(identity.get("remote") or ""))
    validate_target_ref(str(identity.get("target_ref") or ""))
    validate_oid(str(identity.get("expected_main_oid") or ""), "expected_main_oid")
    expires_raw = identity.get("expires_at")
    if not isinstance(expires_raw, str):
        raise PublishError("Controller lease expiry is missing")
    try:
        if datetime.fromisoformat(expires_raw).astimezone(UTC) <= datetime.now(UTC):
            raise PublishError("Controller publication lease has expired")
    except ValueError as exc:
        raise PublishError("Controller lease expiry is malformed") from exc
    if expected_phase in {"verified", "authorized"}:
        action = message.get("action")
        if not isinstance(action, str) or action not in {"PUSH_CANDIDATE", "RECOVER_ALREADY_PUBLISHED"}:
            raise PublishError("Controller publication action is invalid")
    return identity


def publish(arguments: argparse.Namespace, progress: PublicationProgress | None = None) -> dict[str, Any]:
    repo = Path(arguments.repo).resolve()
    candidate_oid = validate_oid(arguments.publish_integration_candidate, "candidate OID")
    lease_id = arguments.lease_id
    run_id = arguments.run_id
    prog = progress or PublicationProgress(candidate_oid=candidate_oid, lease_id=lease_id, run_id=run_id)

    controller = controller_program()
    verify_controller_capabilities(controller, repo)

    head_res = run_git(repo, "rev-parse", "--verify", "HEAD")
    if head_res.returncode != 0 or head_res.stdout.strip() != candidate_oid:
        raise PublishError("candidate worktree HEAD does not match requested candidate OID")

    channel = ControllerChannel(controller, repo, lease_id, run_id)
    try:
        prog.phase = "verify"
        verified = channel.read_message()
        identity = _validate_phase_message(
            verified,
            expected_phase="verified",
            expected_lease=lease_id,
            expected_run=run_id,
            expected_candidate=candidate_oid,
        )
        remote = str(identity["remote"])
        target_ref = str(identity["target_ref"])
        expected_main_oid = str(identity["expected_main_oid"])
        prog.identity = dict(identity)
        prog.remote = remote
        prog.target_ref = target_ref
        prog.expected_main_oid = expected_main_oid
        prog.repository_id = str(identity["repository_id"])
        prog.allocation_id = str(identity["allocation_id"])

        prog.phase = "authorize"
        authorized = channel.send_json(
            {"schema_version": 1, "contract": CONTRACT, "action": "authorize"}
        )
        auth_identity = _validate_phase_message(
            authorized,
            expected_phase="authorized",
            expected_lease=lease_id,
            expected_run=run_id,
            expected_candidate=candidate_oid,
        )
        if auth_identity != identity:
            raise PublishError("Controller authorization identity differs from verified identity")

        action = str(authorized["action"])
        record_id = str(uuid.uuid4())
        prog.record_id = record_id

        if action == "RECOVER_ALREADY_PUBLISHED":
            live_before = remote_head_oid(repo, remote, target_ref)
            if live_before != candidate_oid:
                raise PublishError("Controller reported RECOVER_ALREADY_PUBLISHED but remote differs")
            prog.remote_oid = live_before
            prog.remote_verified = True
            result_push = "already_published_recovered"
        else:
            live_before = remote_head_oid(repo, remote, target_ref)
            if live_before == candidate_oid:
                prog.remote_oid = live_before
                prog.remote_verified = True
                result_push = "already_published_recovered"
            else:
                if live_before != expected_main_oid:
                    raise PublishError("remote main drifted from expected_main_oid before push")
                prog.phase = "push"
                prog.mutation_attempted = True
                try:
                    push = run_git(
                        repo,
                        "push",
                        "--no-follow-tags",
                        f"--force-with-lease={target_ref}:{expected_main_oid}",
                        "--",
                        remote,
                        f"{candidate_oid}:{target_ref}",
                        timeout=GIT_REMOTE_TIMEOUT,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise PublishError(
                        f"integration push timed out after {GIT_REMOTE_TIMEOUT}s; remote state uncertain",
                        final_phase="push",
                        next_action="recheck_remote_and_recover",
                    ) from exc
                prog.push_returncode = push.returncode
                if push.returncode != 0:
                    prog.remote_oid = remote_head_oid(repo, remote, target_ref)
                    raise PublishError("integration push failed")
                result_push = "published"

            prog.phase = "remote_verify"
            live_after = remote_head_oid(repo, remote, target_ref)
            if live_after != candidate_oid:
                raise PublishError("remote verification failed: remote OID does not match candidate")
            prog.remote_oid = live_after
            prog.remote_verified = True

        prog.phase = "controller_complete"
        completed = channel.send_json(
            {
                "schema_version": 1,
                "contract": CONTRACT,
                "action": "complete",
                "result": {
                    "record_id": record_id,
                    "remote_oid": prog.remote_oid,
                    "push_result": result_push,
                },
            }
        )
        _validate_phase_message(
            completed,
            expected_phase="completed",
            expected_lease=lease_id,
            expected_run=run_id,
            expected_candidate=candidate_oid,
        )
        if completed.get("record_id") != record_id:
            raise PublishError("Controller completion record_id mismatch")
        prog.controller_completed = True
        prog.phase = "complete"

        return {
            "summary_schema_version": SUMMARY_SCHEMA_VERSION,
            "finalizer_version": VERSION,
            "mode": MODE,
            "status": "success",
            "final_phase": "complete",
            "reason": None,
            "next_action": "none",
            "repository_id": prog.repository_id,
            "allocation_id": prog.allocation_id,
            "lease_id": lease_id,
            "run_id": run_id,
            "remote": remote,
            "target_ref": target_ref,
            "expected_main_oid": expected_main_oid,
            "candidate_oid": candidate_oid,
            "record_id": record_id,
            "push": {
                "executed": prog.mutation_attempted,
                "result": result_push,
                "branch_refspec_only": True,
                "follow_tags_requested": False,
            },
            "remote_verify": {"status": "verified", "oid": prog.remote_oid},
            "controller_execution": {
                "contract": CONTRACT,
                "completion": "completed",
                "lifecycle_completion_required": True,
            },
            "progress": prog.to_dict(),
        }
    finally:
        channel.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="git-finalize")
    parser.add_argument("--publish-integration-candidate", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--summary", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    prog = PublicationProgress(
        candidate_oid=arguments.publish_integration_candidate,
        lease_id=arguments.lease_id,
        run_id=arguments.run_id,
    )
    try:
        receipt = publish(arguments, progress=prog)
        if arguments.summary:
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        else:
            print(f"Git Finalizer integration publication complete: {receipt['candidate_oid']}")
        return 0
    except PublishError as exc:
        if arguments.summary:
            blocked = {
                "summary_schema_version": SUMMARY_SCHEMA_VERSION,
                "finalizer_version": VERSION,
                "mode": MODE,
                "status": "blocked",
                "final_phase": exc.final_phase or prog.phase,
                "error_code": exc.error_code,
                "reason": str(exc),
                "next_action": exc.next_action,
                "progress": prog.to_dict(),
            }
            print(json.dumps(blocked, ensure_ascii=False, sort_keys=True))
        else:
            print(f"ERROR [{exc.error_code}]: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code) from None


if __name__ == "__main__":
    raise SystemExit(main())
