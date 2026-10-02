#!/usr/bin/env python3
"""Publish an exact integration candidate through Controller's public session."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import time
from typing import Any, Iterator, NoReturn, Sequence
import uuid

VERSION = "1.8.14"
OID = re.compile(r"[0-9a-f]{40}")
SAFE_REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
PROTOCOL = "integration-publication-session/v1"
MAX_FRAME_BYTES = 65536
RESPONSE_TIMEOUT_SECONDS = 180
GIT_TIMEOUT_SECONDS = 120
IDENTITY_FIELDS = {
    "repository_id", "allocation_id", "lease_id", "run_id", "remote", "target_ref",
    "candidate_oid", "expected_main_oid", "validation_evidence_id", "record_version", "expires_at",
}


class PublishError(Exception):
    """A failed publication gate; observe the remote before retrying."""


def run_git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never", "LC_ALL": "C",
    }
    try:
        result = subprocess.run(
            ("git", "-C", str(repo), *arguments), env=environment, check=False,
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishError("Git command timed out; read the live remote before retrying") from exc
    if check and result.returncode:
        raise PublishError(f"Git {arguments[0]} failed (exit {result.returncode})")
    return result


def require_text(value: object, field: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise PublishError(f"{field} is invalid")
    return value


def require_uuid(value: object, field: str) -> str:
    text = require_text(value, field, maximum=36)
    try:
        if str(uuid.UUID(text)) != text:
            raise ValueError
    except ValueError as exc:
        raise PublishError(f"{field} must be a canonical UUID") from exc
    return text


def require_oid(value: object, field: str) -> str:
    text = require_text(value, field, maximum=40)
    if OID.fullmatch(text) is None:
        raise PublishError(f"{field} must be a full commit OID")
    return text


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PublishError("Controller response has a duplicate JSON key")
        result[key] = value
    return result


def resolve_repo(argument: Path) -> Path:
    if not argument.is_absolute():
        raise PublishError("--repo must be an absolute integration checkout path")
    repo = argument.resolve(strict=True)
    root = Path(run_git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve(strict=True)
    if root != repo:
        raise PublishError("--repo must name the integration checkout root")
    return repo


# GF-INTEGRATION-ADAPTER: worktree-controller-integration-publication (single site)
def controller_program() -> str:
    program = shutil.which("worktree-controller")
    if program is None:
        raise PublishError("Worktree Controller is required for integration publication")
    try:
        result = subprocess.run(
            (program, "capabilities", "--json"), check=False, capture_output=True,
            text=True, stdin=subprocess.DEVNULL, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishError("Controller capabilities timed out") from exc
    if result.returncode or len(result.stdout.encode("utf-8")) > MAX_FRAME_BYTES:
        raise PublishError("Controller capabilities refused or failed")
    value = json.loads(result.stdout, object_pairs_hook=unique_object)
    decision = value.get("decision") if isinstance(value, dict) else None
    if (
        not isinstance(decision, dict)
        or type(decision.get("integration_publication_execution_version")) is not int
        or decision["integration_publication_execution_version"] != 1
    ):
        raise PublishError("Controller lacks the required public integration publication protocol")
    return program


class ControllerChannel:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.pending = bytearray()

    def read(self, phase: str) -> dict[str, Any]:
        assert self.process.stdout is not None
        deadline = time.monotonic() + RESPONSE_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise PublishError("Controller publication session timed out")
                chunk = os.read(self.process.stdout.fileno(), min(4096, MAX_FRAME_BYTES + 1 - len(self.pending)))
                if not chunk:
                    raise PublishError("Controller publication session ended before completion")
                self.pending.extend(chunk)
                if len(self.pending) > MAX_FRAME_BYTES:
                    raise PublishError("Controller publication response is too large")
        line, _, pending = self.pending.partition(b"\n")
        self.pending = bytearray(pending)
        value = json.loads(line.decode("utf-8"), object_pairs_hook=unique_object)
        if (
            not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("contract") != PROTOCOL
        ):
            raise PublishError("Controller publication response contract differs")
        if value.get("phase") == "blocked":
            code = require_text(value.get("code"), "Controller refusal code", maximum=128)
            reason = require_text(value.get("reason"), "Controller refusal reason")
            raise PublishError(f"Controller {code}: {reason}")
        if value.get("phase") != phase:
            raise PublishError("Controller publication response phase differs")
        return value

    def send(self, action: str, identity: dict[str, Any], **fields: Any) -> None:
        if self.process.poll() is not None:
            raise PublishError("Controller publication session is no longer active")
        assert self.process.stdin is not None
        document = {"schema_version": 1, "contract": PROTOCOL, "action": action, "identity": identity, **fields}
        self.process.stdin.write((json.dumps(document, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()


@contextmanager
def controller_session(program: str, repo: Path, candidate: str, lease_id: str, run_id: str) -> Iterator[ControllerChannel]:
    process = subprocess.Popen(
        (program, "publication-execution-session", "--repo", str(repo), "--candidate-oid", candidate,
         "--lease-id", lease_id, "--run-id", run_id, "--json"),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
    )
    try:
        yield ControllerChannel(process)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def validate_identity(value: object, repo: Path, candidate: str, lease_id: str, run_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != IDENTITY_FIELDS:
        raise PublishError("Controller publication identity fields differ")
    for field in ("repository_id", "allocation_id", "lease_id"):
        require_uuid(value[field], field)
    for field in ("candidate_oid", "expected_main_oid"):
        require_oid(value[field], field)
    for field in ("run_id", "validation_evidence_id", "expires_at"):
        require_text(value[field], field)
    if type(value["record_version"]) is not int or value["record_version"] < 1:
        raise PublishError("Controller record version is invalid")
    remote = require_text(value["remote"], "Controller remote", maximum=128)
    if SAFE_REMOTE.fullmatch(remote) is None:
        raise PublishError("Controller remote is unsafe")
    target = require_text(value["target_ref"], "Controller target ref")
    if not target.startswith("refs/heads/") or run_git(repo, "check-ref-format", target, check=False).returncode:
        raise PublishError("Controller target must be a branch ref")
    if (value["candidate_oid"], value["lease_id"], value["run_id"]) != (candidate, lease_id, run_id):
        raise PublishError("Controller publication identity differs from the request")
    return value


def same_identity(value: object, identity: dict[str, Any]) -> bool:
    return isinstance(value, dict) and set(value) == set(identity) and all(
        type(value[field]) is type(expected) and value[field] == expected
        for field, expected in identity.items()
    )


def validate_action(response: dict[str, Any], identity: dict[str, Any]) -> str:
    if not same_identity(response.get("identity"), identity):
        raise PublishError("Controller publication identity changed during session")
    action = response.get("action")
    expected = identity["candidate_oid"] if action == "RECOVER_ALREADY_PUBLISHED" else identity["expected_main_oid"]
    if (
        not isinstance(action, str)
        or action not in {"PUSH_CANDIDATE", "RECOVER_ALREADY_PUBLISHED"}
        or response.get("remote_oid") != expected
    ):
        raise PublishError("Controller publication authorization action differs")
    return action


def live_remote_oid(repo: Path, remote: str, target_ref: str) -> str:
    result = run_git(repo, "ls-remote", "--exit-code", "--refs", remote, target_ref, check=False)
    lines = result.stdout.splitlines()
    if result.returncode or len(lines) != 1:
        raise PublishError("live publication ref could not be verified")
    parts = lines[0].split("\t")
    if len(parts) != 2 or parts[1] != target_ref:
        raise PublishError("live publication ref response differs")
    return require_oid(parts[0], "live publication OID")


def publish(arguments: argparse.Namespace) -> dict[str, Any]:
    candidate = require_oid(arguments.publish_integration_candidate, "candidate argument")
    lease_id = require_uuid(arguments.lease_id, "lease argument")
    run_id = require_text(arguments.run_id, "run argument", maximum=256)
    repo = resolve_repo(arguments.repo)
    program = controller_program()
    push_executed = False
    record_id = f"git-finalizer-integration-{lease_id}-{candidate}"
    with controller_session(program, repo, candidate, lease_id, run_id) as channel:
        verified = channel.read("verified")
        identity = validate_identity(verified.get("identity"), repo, candidate, lease_id, run_id)
        validate_action(verified, identity)
        channel.send("authorize", identity)
        action = validate_action(channel.read("authorized"), identity)
        if channel.process.poll() is not None:
            raise PublishError("Controller authorization session ended before the mutation gate")
        remote, target_ref = identity["remote"], identity["target_ref"]
        if action == "PUSH_CANDIDATE":
            push = run_git(repo, "push", "--porcelain", "--no-follow-tags", remote, f"{candidate}:{target_ref}", check=False)
            push_executed = True
            if os.environ.get("GIT_FINALIZER_TEST_CRASH_AFTER_PUSH") == "1":
                os._exit(97)
            if push.returncode and live_remote_oid(repo, remote, target_ref) != candidate:
                raise PublishError(f"integration push failed (exit {push.returncode}); remote candidate was not verified")
            result = "published"
        else:
            result = "already_published_recovered"
        remote_oid = live_remote_oid(repo, remote, target_ref)
        if remote_oid != candidate:
            raise PublishError("remote verification does not equal the exact candidate OID")
        channel.send("complete", identity, result={
            "record_id": record_id, "executor": {"name": "git-finalizer", "version": VERSION},
            "push_executed": push_executed, "remote_oid": remote_oid,
        })
        completed = channel.read("completed")
        if (
            not same_identity(completed.get("identity"), identity) or completed.get("record_id") != record_id
            or completed.get("remote_verify") != {"status": "verified", "oid": candidate}
            or completed.get("lifecycle_completion_required") is not True
        ):
            raise PublishError("Controller publication completion receipt differs")
        if channel.process.wait(timeout=3) != 0:
            raise PublishError("Controller publication session failed after result verification")
    return {
        "summary_schema_version": 1, "finalizer_version": VERSION,
        "mode": "integration_candidate_publish", "status": "success", "final_phase": "remote_verified",
        "repository": str(repo), "repository_id": identity["repository_id"],
        "allocation_id": identity["allocation_id"], "lease_id": lease_id, "run_id": run_id,
        "target_ref": identity["target_ref"], "expected_main_oid": identity["expected_main_oid"],
        "candidate_oid": candidate, "record_id": record_id,
        "push": {"executed": push_executed, "result": result, "branch_refspec_only": True, "follow_tags_requested": False},
        "remote_verify": {"status": "verified", "oid": remote_oid},
        "controller_execution": {"contract": PROTOCOL, "lifecycle_completion_required": True},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--publish-integration-candidate", required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--summary", action="store_true")
    return parser


def fail(exc: Exception, *, summary: bool) -> NoReturn:
    if summary:
        print(json.dumps({
            "summary_schema_version": 1, "finalizer_version": VERSION, "mode": "integration_candidate_publish",
            "status": "blocked", "final_phase": "pre_publish_or_verify", "reason": str(exc)[:512],
            "next_action": "read_remote_fact_and_reenter_controller_publish_gate",
        }, ensure_ascii=False, separators=(",", ":")))
    else:
        print(f"ERROR: {exc}", file=sys.stderr)
    raise SystemExit(1)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    summary = "--summary" in raw
    try:
        arguments = build_parser().parse_args(raw)
        result = publish(arguments)
    except (PublishError, OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
        fail(exc, summary=summary)
    if arguments.summary:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        print(f"publication={result['push']['result']}")
        print(f"candidate_oid={result['candidate_oid']}")
        print(f"remote_verified={result['remote_verify']['oid']}")
        print(f"finalizer_record_id={result['record_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
