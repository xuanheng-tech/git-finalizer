# GF-INTEGRATION-ADAPTER: worktree-controller-retirement (single site: Controller retirement verify/authorize/complete client)
"""Verify, authorize, CAS-delete, and complete/record branch retirements via Worktree Controller."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import uuid

from git_finalizer import __version__
from git_finalizer.errors import VALID_ERROR_CODE, ErrorCode
from git_finalizer.git_io import FULL_OID_RE, clean_git_env, validate_remote_name
from git_finalizer.json_io import loads_unique

VERSION = __version__
COMMAND_TIMEOUT = 60.0
GIT_REMOTE_TIMEOUT = 120.0
PLAN_ID_RE = re.compile(r"^[0-9a-f]{64}$")
EXECUTOR_KINDS = ("local", "remote")
RETIREMENT_CONTRACT = "branch-retirement-execution/v1"
RETIREMENT_RESULT_CONTRACT = "retirement-execution-result/v1"


class RetirementBlocked(RuntimeError):
    """Fail-closed blocker emitted before or around CAS retirement execution."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = ErrorCode.RETIREMENT_BLOCKED,
        next_action: str = "resolve_blocker_and_retry",
        mutation_attempted: bool = False,
        expected_oid_lease_bound: bool = False,
        controller_complete: bool = False,
        result_code: str | None = None,
        receipt_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.error_code = error_code
        self.next_action = next_action
        self.mutation_attempted = mutation_attempted
        self.expected_oid_lease_bound = expected_oid_lease_bound
        self.controller_complete = controller_complete
        self.result_code = result_code
        self.receipt_id = receipt_id
        self.details = dict(details) if details is not None else None
        super().__init__(message)


def replan_command(
    repo: Path | str,
    branch: str,
    remote: str,
    integrated_into: str,
) -> str:
    return shlex.join(
        [
            "worktree-controller",
            "branch-retirement-plan",
            "--repo",
            str(Path(repo).resolve()),
            "--branch",
            branch,
            "--remote",
            remote,
            "--integrated-into",
            integrated_into,
            "--json",
        ]
    )


def _extract_controller_error_codes(stdout: str) -> list[str]:
    """Extract only validated uppercase error code tokens from Controller JSON; never forward prose."""
    if not stdout or not stdout.strip():
        return []
    try:
        payload = loads_unique(stdout)
    except (ValueError, UnicodeDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return []
    codes: list[str] = []
    for item in errors[:8]:
        if isinstance(item, dict):
            code = item.get("code")
            if isinstance(code, str) and VALID_ERROR_CODE.fullmatch(code) and code not in codes:
                codes.append(code)
    return codes


def git_run(
    repo: Path,
    *arguments: str,
    mutation: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    eff_timeout = timeout or (GIT_REMOTE_TIMEOUT if mutation or "ls-remote" in arguments or "fetch" in arguments else COMMAND_TIMEOUT)
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            timeout=eff_timeout,
            env=clean_git_env(),
            close_fds=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RetirementBlocked(f"Git inspection failed: {exc}") from exc


def controller_program() -> str:
    found = shutil.which("worktree-controller")
    if not found:
        raise RetirementBlocked(
            "Worktree Controller is required for branch retirement",
            error_code=ErrorCode.CONTROLLER_UNAVAILABLE,
        )
    return found


def controller_call(repo: Path, command: str, *arguments: str) -> dict[str, Any]:
    program = controller_program()
    try:
        completed = subprocess.run(
            [program, command, "--repo", str(repo), *arguments, "--json"],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            close_fds=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RetirementBlocked(
            f"Controller {command} refused or failed (timed out)",
            error_code=ErrorCode.CONTROLLER_UNAVAILABLE,
            details={"controller_command": command, "controller_error_codes": ["TIMEOUT"]},
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RetirementBlocked(
            f"Controller {command} refused or failed: {exc}",
            error_code=ErrorCode.CONTROLLER_UNAVAILABLE,
            details={"controller_command": command},
        ) from exc
    if completed.returncode != 0 or not completed.stdout.strip():
        err_codes = _extract_controller_error_codes(completed.stdout)
        if err_codes:
            err_code = (
                ErrorCode.RETIREMENT_BLOCKED
                if any(c in {"UNKNOWN_RETIREMENT_PLAN", "INVALID_PLAN_ID", "RETIREMENT_EXECUTION_ACTIVE"} for c in err_codes)
                else ErrorCode.CONTROLLER_UNAVAILABLE
            )
            raise RetirementBlocked(
                f"Controller {command} refused or failed (codes={','.join(err_codes)})",
                error_code=err_code,
                details={"controller_command": command, "controller_error_codes": err_codes},
            )
        raise RetirementBlocked(
            f"Controller {command} refused or failed",
            error_code=ErrorCode.CONTROLLER_UNAVAILABLE,
            details={"controller_command": command},
        )
    try:
        payload = loads_unique(completed.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RetirementBlocked("Controller returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("errors"):
        err_codes = _extract_controller_error_codes(completed.stdout)
        if err_codes:
            raise RetirementBlocked(
                f"Controller {command} refused or failed (codes={','.join(err_codes)})",
                details={"controller_command": command, "controller_error_codes": err_codes},
            )
        raise RetirementBlocked("Controller returned invalid or error payload")
    decision = payload.get("decision")
    if not isinstance(decision, dict):
        raise RetirementBlocked("Controller decision missing")
    return decision


def check_controller_namespace(repo: Path) -> None:
    """Block branch retirement if repository retains legacy `codex-worktree` metadata without `worktree-controller/v1`."""
    res = git_run(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if res.returncode != 0:
        return
    common_dir = Path(res.stdout.strip())
    if (common_dir / "codex-worktree" / "v1").is_dir() and not (common_dir / "worktree-controller" / "v1").is_dir():
        raise RetirementBlocked(
            "检测到 legacy codex-worktree namespace；retirement 需先经 WC 治理激活 worktree-controller/v1"
        )


def verify_capabilities(repo: Path) -> None:
    check_controller_namespace(repo)
    decision = controller_call(repo, "capabilities")
    for key in (
        "branch_retirement_version",
        "branch_retirement_verify_version",
        "branch_retirement_execution_version",
    ):
        value = decision.get(key)
        if type(value) is not int or value < 1:
            raise RetirementBlocked(
                f"Controller lacks required public retirement protocols ({key})"
            )


def normalize_integrated_ref(integrated_into: str, remote: str) -> str:
    if integrated_into.startswith(("refs/heads/", "refs/remotes/")):
        return integrated_into
    if integrated_into.startswith(f"{remote}/"):
        return f"refs/remotes/{integrated_into}"
    return f"refs/remotes/{remote}/{integrated_into}"


def request_id(arguments: argparse.Namespace) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"git-finalizer-retirement/{arguments.plan_id}/{arguments.operation}",
        )
    )


def current_holder() -> dict[str, Any]:
    pid = os.getpid()
    stat_path = Path(f"/proc/{pid}/stat")
    boot_path = Path("/proc/sys/kernel/random/boot_id")
    if stat_path.is_file() and boot_path.is_file():
        raw = stat_path.read_text(encoding="ascii")
        start_ticks = int(raw[raw.rfind(")") + 2 :].split()[19])
        boot_id = boot_path.read_text(encoding="ascii").strip()
        return {"pid": pid, "start_ticks": start_ticks, "boot_id": boot_id}
    return {"pid": pid}


def holder_alive(holder: Any) -> bool:
    if not isinstance(holder, dict):
        return False
    pid = holder.get("pid")
    if type(pid) is not int or pid <= 0:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    boot_path = Path("/proc/sys/kernel/random/boot_id")
    if stat_path.is_file() and boot_path.is_file():
        boot_id_expected = holder.get("boot_id")
        start_ticks_expected = holder.get("start_ticks")
        if type(start_ticks_expected) is not int or start_ticks_expected <= 0:
            return False
        try:
            boot_id = boot_path.read_text(encoding="ascii").strip()
            if boot_id_expected != boot_id:
                return False
            raw = stat_path.read_text(encoding="ascii")
            ticks = int(raw[raw.rfind(")") + 2 :].split()[19])
            return ticks == start_ticks_expected
        except (OSError, ValueError, IndexError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _validate_holder_shape(holder: Any) -> None:
    if not isinstance(holder, dict) or type(holder.get("pid")) is not int or holder["pid"] <= 0:
        raise RetirementBlocked("invalid execution holder identity")
    if (
        Path(f"/proc/{holder['pid']}").parent.exists()
        and "start_ticks" in holder
        and (
            type(holder.get("start_ticks")) is not int
            or holder["start_ticks"] <= 0
            or not isinstance(holder.get("boot_id"), str)
        )
    ):
        raise RetirementBlocked("invalid Linux execution holder metadata")


def identity_matches(
    arguments: argparse.Namespace,
    doc: dict[str, Any],
    *,
    grant: bool = False,
    status_only: bool = False,
    check_expiry: bool = True,
) -> None:
    replan = replan_command(arguments.repo, arguments.branch, arguments.remote, arguments.integrated_into)
    if status_only:
        if type(doc.get("schema_version")) is not int or doc["schema_version"] != 1:
            raise RetirementBlocked("Controller plan status schema_version mismatch")
        mismatched: list[str] = []
        if doc.get("plan_id") != arguments.plan_id:
            mismatched.append("plan_id")
        if doc.get("branch") != arguments.branch:
            mismatched.append("branch")
        if doc.get("remote") != arguments.remote:
            mismatched.append("remote")
        if doc.get("expected_oid") != arguments.expected_oid:
            mismatched.append("expected_oid")
        if mismatched:
            raise RetirementBlocked(
                f"Controller retirement identity differs (fields={','.join(mismatched)}; create a fresh plan: {replan})",
                details={"mismatched_fields": mismatched, "replan_command": replan},
            )
        expected_int = normalize_integrated_ref(arguments.integrated_into, arguments.remote)
        if doc.get("integrated_ref") != expected_int:
            raise RetirementBlocked(
                f"Controller plan integration ref differs: {doc.get('integrated_ref')} != {expected_int}",
                details={"mismatched_fields": ["integrated_ref"], "replan_command": replan},
            )
        exp_int_oid = doc.get("expected_integrated_oid")
        if not isinstance(exp_int_oid, str) or FULL_OID_RE.fullmatch(exp_int_oid) is None:
            raise RetirementBlocked(
                f"Controller retirement identity differs (fields=expected_integrated_oid_invalid; create a fresh plan: {replan})",
                details={"mismatched_fields": ["expected_integrated_oid_invalid"], "replan_command": replan},
            )
        if arguments.expected_integrated_oid and exp_int_oid != arguments.expected_integrated_oid:
            raise RetirementBlocked(
                f"Controller retirement identity differs (fields=expected_integrated_oid; create a fresh plan: {replan})",
                details={"mismatched_fields": ["expected_integrated_oid"], "replan_command": replan},
            )
        return
    if grant:
        if type(doc.get("schema_version")) is not int or doc["schema_version"] != 1:
            raise RetirementBlocked("authorization schema_version mismatch")
        if doc.get("contract") != RETIREMENT_CONTRACT:
            raise RetirementBlocked("authorization contract mismatch")
        if doc.get("plan_id") != arguments.plan_id or doc.get("operation") != arguments.operation:
            raise RetirementBlocked("authorization plan/operation mismatch")
        if doc.get("branch") != arguments.branch:
            raise RetirementBlocked("authorization branch mismatch")
        if doc.get("expected_oid") != arguments.expected_oid or doc.get("expected_integrated_oid") != arguments.expected_integrated_oid:
            raise RetirementBlocked("authorization OID mismatch")
        expected_ref = (
            f"refs/heads/{arguments.branch}"
            if arguments.operation == "local"
            else f"refs/remotes/{arguments.remote}/{arguments.branch}"
        )
        if doc.get("ref") != expected_ref:
            raise RetirementBlocked("authorization ref mismatch")
        expected_int = normalize_integrated_ref(arguments.integrated_into, arguments.remote)
        if doc.get("integrated_ref") != expected_int:
            raise RetirementBlocked(f"authorization integrated_ref mismatch: {doc.get('integrated_ref')}")
        exp_raw = doc.get("expires_at")
        if not isinstance(exp_raw, str):
            raise RetirementBlocked("authorization expires_at missing")
        try:
            expired = datetime.fromisoformat(exp_raw).astimezone(UTC) <= datetime.now(UTC)
            if check_expiry and expired:
                raise RetirementBlocked("authorization grant has expired")
        except ValueError as exc:
            raise RetirementBlocked("authorization expires_at malformed") from exc
        _validate_holder_shape(doc.get("holder"))
    else:
        rid = doc.get("receipt_id")
        if not isinstance(rid, str) or PLAN_ID_RE.fullmatch(rid) is None:
            raise RetirementBlocked("Controller receipt_id is invalid")
        if (
            doc.get("plan_id") != arguments.plan_id
            or doc.get("operation") != arguments.operation
            or doc.get("branch") != arguments.branch
            or doc.get("expected_oid") != arguments.expected_oid
            or doc.get("expected_integrated_oid") != arguments.expected_integrated_oid
        ):
            raise RetirementBlocked("Controller receipt identity mismatch")


def _ref_present(repo: Path, arguments: argparse.Namespace) -> bool:
    if arguments.operation == "local":
        completed = git_run(repo, "show-ref", "--verify", "--quiet", "--", f"refs/heads/{arguments.branch}")
        if completed.returncode == 1:
            return False
        if completed.returncode != 0:
            raise RetirementBlocked("unable to verify local branch ref cleanly")
        return True
    ref = f"refs/heads/{arguments.branch}"
    completed = git_run(repo, "ls-remote", "--heads", "--", arguments.remote, ref)
    if completed.returncode != 0:
        raise RetirementBlocked(
            "unable to query remote branch ref cleanly",
            next_action="inspect_remote_state_then_rerun_same_retirement",
        )
    return bool(completed.stdout.strip())


def preflight(arguments: argparse.Namespace) -> dict[str, Any]:
    repo = Path(arguments.repo).resolve()
    replan = replan_command(repo, arguments.branch, arguments.remote, arguments.integrated_into)
    try:
        status = controller_call(
            repo,
            "branch-retirement-status",
            "--plan-id",
            arguments.plan_id,
        )
    except RetirementBlocked as exc:
        err_codes = (exc.details or {}).get("controller_error_codes") or []
        if "UNKNOWN_RETIREMENT_PLAN" in err_codes:
            details = dict(exc.details or {})
            details["replan_command"] = replan
            raise RetirementBlocked(
                f"{exc}; create a fresh plan: {replan}",
                error_code=exc.error_code,
                details=details,
            ) from exc
        raise
    status_code = str(status.get("code") or "")
    current = status.get("current")
    status_blockers = list(current.get("blockers") or []) if isinstance(current, dict) else []
    if status_code in {"BRANCH_RETIREMENT_DRIFTED", "BRANCH_RETIREMENT_PARTIAL_DRIFTED"} or "PLAN_STATE_DRIFT" in status_blockers:
        raise RetirementBlocked(
            f"Controller retirement verdict blocks ({status_code}; blockers={status_blockers}): plan is stale; create a fresh plan: {replan}",
            details={
                "controller_code": status_code,
                "blockers": status_blockers,
                "completed_operations": list(status.get("completed_operations") or []),
                "replan_command": replan,
            },
        )
    if not isinstance(current, dict):
        raise RetirementBlocked("Controller branch-retirement-status returned invalid current payload")
    identity_matches(arguments, current, status_only=True)

    decision = controller_call(
        repo,
        "branch-retirement-verify",
        "--plan-id",
        arguments.plan_id,
        "--operation",
        arguments.operation,
    )
    code = str(decision.get("code") or "")
    blockers = list(decision.get("blockers") or [])
    if code in {"BRANCH_RETIREMENT_DRIFTED", "RETIREMENT_STALE"} or "PLAN_STATE_DRIFT" in blockers:
        raise RetirementBlocked(
            f"Controller retirement verdict blocks ({code}; blockers={blockers}): plan is stale; create a fresh plan: {replan}",
            details={
                "controller_code": code,
                "blockers": blockers,
                "replan_command": replan,
            },
        )
    reval = decision.get("minimum_revalidation")
    if isinstance(reval, dict):
        expected_int = normalize_integrated_ref(arguments.integrated_into, arguments.remote)
        mismatched: list[str] = []
        if reval.get("plan_id") != arguments.plan_id:
            mismatched.append("plan_id")
        if reval.get("operation") != arguments.operation:
            mismatched.append("operation")
        if reval.get("branch") != arguments.branch:
            mismatched.append("branch")
        if reval.get("expected_oid") != arguments.expected_oid:
            mismatched.append("expected_oid")
        if reval.get("integrated_ref") != expected_int:
            mismatched.append("integrated_ref")
        if arguments.expected_integrated_oid and reval.get("expected_integrated_oid") != arguments.expected_integrated_oid:
            mismatched.append("expected_integrated_oid")
        if mismatched:
            raise RetirementBlocked(
                f"Controller retirement identity differs (minimum_revalidation; fields={','.join(mismatched)}; create a fresh plan: {replan})",
                details={"mismatched_fields": mismatched, "replan_command": replan},
            )
    elif code == "RETIREMENT_EXECUTABLE":
        raise RetirementBlocked("Controller branch-retirement-verify missing minimum_revalidation")
    if code == "RETIREMENT_OPERATION_COMPLETE" and (
        not isinstance(reval, dict) or reval.get("ref_state") != "ABSENT"
    ):
        raise RetirementBlocked("Controller already records a consumed receipt while its ref remains")
    if code in {"RETIREMENT_EXECUTABLE", "RETIREMENT_OPERATION_COMPLETE"} and not blockers:
        return decision
    if code == "RETIREMENT_BLOCKED" and blockers == ["ABSENT_UNATTESTED"]:
        return decision
    if (
        getattr(arguments, "recover", False)
        and "RETIREMENT_AUTHORIZATION_ACTIVE" in blockers
        and set(blockers) <= {"RETIREMENT_AUTHORIZATION_ACTIVE", "ABSENT_UNATTESTED"}
    ):
        return decision
    raise RetirementBlocked(
        f"Controller retirement verdict blocks ({code}; blockers={blockers})",
        details={"controller_code": code, "blockers": blockers, "replan_command": replan},
    )


def _write_result_file(directory: Path, payload: dict[str, Any]) -> Path:
    path = directory / "result.json"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _complete_authorization(
    repo: Path,
    arguments: argparse.Namespace,
    grant: dict[str, Any],
    *,
    outcome: str,
    progress: dict[str, Any],
) -> dict[str, Any]:
    result_doc = {
        "schema_version": 1,
        "contract": RETIREMENT_RESULT_CONTRACT,
        "plan_id": arguments.plan_id,
        "operation": arguments.operation,
        "kind": "CAS_DELETE",
        "outcome": outcome,
        "ref": grant["ref"],
        "expected_oid": arguments.expected_oid,
        "integrated_ref": grant["integrated_ref"],
        "expected_integrated_oid": arguments.expected_integrated_oid,
        "executor": {"name": "git-finalizer", "version": VERSION},
    }
    with tempfile.TemporaryDirectory(prefix="gf-retire-res-") as temporary:
        result_file = _write_result_file(Path(temporary), result_doc)
        resp = controller_call(
            repo,
            "branch-retirement-complete",
            "--authorization-id",
            str(grant["authorization_id"]),
            "--token",
            str(grant.get("token", "")),
            "--result-file",
            str(result_file),
        )
    code = resp.get("code")
    if code == "RETIREMENT_EXECUTION_ABORTED":
        raise RetirementBlocked(
            "Controller aborted retirement execution because ref remained present",
            mutation_attempted=bool(progress.get("mutation_attempted")),
            expected_oid_lease_bound=bool(progress.get("expected_oid_lease_bound")),
        )
    if code != "RETIREMENT_EXECUTION_COMPLETE":
        raise RetirementBlocked(
            f"Controller refused completion (authorization {grant['authorization_id']}, {code})",
            mutation_attempted=bool(progress.get("mutation_attempted")),
            expected_oid_lease_bound=bool(progress.get("expected_oid_lease_bound")),
            result_code="LOCAL_DELETE_UNVERIFIED" if arguments.operation == "local" else "REMOTE_DELETE_UNVERIFIED",
        )
    receipt = resp.get("receipt")
    if not isinstance(receipt, dict):
        raise RetirementBlocked(
            "Controller completion missing receipt",
            mutation_attempted=bool(progress.get("mutation_attempted")),
            expected_oid_lease_bound=bool(progress.get("expected_oid_lease_bound")),
        )
    identity_matches(arguments, receipt, grant=False)
    return receipt


def execute(
    arguments: argparse.Namespace,
    *,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repo = Path(arguments.repo).resolve()
    validate_remote_name(arguments.remote)
    if PLAN_ID_RE.fullmatch(arguments.plan_id) is None:
        raise RetirementBlocked("plan-id 必须是 64 位小写十六进制", error_code=ErrorCode.CLI_INVALID_ARGS)
    for val, label in (
        (arguments.expected_oid, "expected-oid"),
        (arguments.expected_integrated_oid, "expected-integrated-oid"),
    ):
        if FULL_OID_RE.fullmatch(val) is None:
            raise RetirementBlocked(f"{label} 必须是完整 OID", error_code=ErrorCode.CLI_INVALID_ARGS)

    prog = progress if progress is not None else {
        "mutation_attempted": False,
        "controller_complete": False,
        "authorization_id": None,
        "expected_oid_lease_bound": False,
    }
    req_id = request_id(arguments)

    holder_arg = f"ancestor:{Path(sys.executable).resolve(strict=True).name}"
    auth = controller_call(
        repo,
        "branch-retirement-authorize",
        "--plan-id",
        arguments.plan_id,
        "--operation",
        arguments.operation,
        "--request-id",
        req_id,
        "--holder",
        holder_arg,
        "--client-id",
        "git-finalizer",
        "--run-id",
        req_id,
    )
    auth_code = str(auth.get("code") or "")
    grant = auth.get("authorization")

    if auth_code == "RETIREMENT_AUTHORIZED" and isinstance(grant, dict):
        # Validate holder shape before deciding foreign/dead abandonment.
        _validate_holder_shape(grant.get("holder"))
        # Detect replay/previous unfinished execution with another holder!
        if grant.get("holder") != current_holder() and _holder_is_foreign(grant.get("holder")):
            if holder_alive(grant.get("holder")):
                raise RetirementBlocked("Controller retirement verdict blocks: another retirement executor is active")
            if _ref_present(repo, arguments):
                controller_call(
                    repo,
                    "branch-retirement-executions",
                    "--action",
                    "abandon",
                    "--authorization-id",
                    str(grant["authorization_id"]),
                    "--token",
                    str(grant.get("token", "")),
                    "--reason",
                    "stale dead executor",
                )
                auth = controller_call(
                    repo,
                    "branch-retirement-authorize",
                    "--plan-id",
                    arguments.plan_id,
                    "--operation",
                    arguments.operation,
                    "--request-id",
                    req_id,
                    "--holder",
                    holder_arg,
                    "--client-id",
                    "git-finalizer",
                    "--run-id",
                    req_id,
                )
                if auth.get("code") != "RETIREMENT_AUTHORIZED" or not isinstance(auth.get("authorization"), dict):
                    raise RetirementBlocked("Controller retirement verdict blocks after abandoning stale execution")
                grant = auth["authorization"]
            else:
                identity_matches(arguments, grant, grant=True, check_expiry=False)
                receipt = _complete_authorization(
                    repo, arguments, grant, outcome="REF_ALREADY_ABSENT", progress=prog
                )
                prog["controller_complete"] = True
                prog["receipt_id"] = receipt["receipt_id"]
                return {
                    "result": "ALREADY_ABSENT_VERIFIED",
                    "receipt_id": str(receipt["receipt_id"]),
                    "mutation_attempted": False,
                    "controller_complete": True,
                    "expected_oid_lease_bound": False,
                }

        present = _ref_present(repo, arguments)
        identity_matches(arguments, grant, grant=True, check_expiry=present)
        if not present:
            receipt = _complete_authorization(
                repo, arguments, grant, outcome="REF_ALREADY_ABSENT", progress=prog
            )
            prog["controller_complete"] = True
            prog["receipt_id"] = receipt["receipt_id"]
            return {
                "result": "ALREADY_ABSENT_VERIFIED",
                "receipt_id": str(receipt["receipt_id"]),
                "mutation_attempted": False,
                "controller_complete": True,
                "expected_oid_lease_bound": False,
            }

        prog["mutation_attempted"] = True
        prog["expected_oid_lease_bound"] = True
        prog["authorization_id"] = grant["authorization_id"]
        if arguments.operation == "local":
            del_res = git_run(repo, "update-ref", "-d", str(grant["ref"]), str(grant["expected_oid"]), mutation=True)
            if del_res.returncode != 0:
                _complete_authorization(repo, arguments, grant, outcome="REF_STILL_PRESENT", progress=prog)
                raise RetirementBlocked(
                    "local CAS delete failed",
                    mutation_attempted=True,
                    expected_oid_lease_bound=True,
                )
        else:
            del_res = git_run(
                repo,
                "push",
                "--no-follow-tags",
                "--no-verify",
                f"--force-with-lease=refs/heads/{arguments.branch}:{grant['expected_oid']}",
                "--",
                arguments.remote,
                f":refs/heads/{arguments.branch}",
                mutation=True,
            )
            if del_res.returncode != 0:
                raise RetirementBlocked(
                    "remote CAS delete failed or uncertain",
                    next_action="inspect_remote_state_then_rerun_same_retirement",
                    mutation_attempted=True,
                    expected_oid_lease_bound=True,
                )
            fetch_res = git_run(
                repo,
                "fetch",
                "--prune",
                "--",
                arguments.remote,
                f"+refs/heads/*:refs/remotes/{arguments.remote}/*",
            )
            if fetch_res.returncode != 0:
                raise RetirementBlocked(
                    "remote fetch/prune failed after retirement",
                    next_action="inspect_remote_state_then_rerun_same_retirement",
                    mutation_attempted=True,
                    expected_oid_lease_bound=True,
                )

        if _ref_present(repo, arguments):
            fail_code = "LOCAL_DELETE_UNVERIFIED" if arguments.operation == "local" else "REMOTE_DELETE_UNVERIFIED"
            raise RetirementBlocked(
                "branch ref still exists after CAS delete",
                result_code=fail_code,
                mutation_attempted=True,
                expected_oid_lease_bound=True,
            )
        receipt = _complete_authorization(
            repo, arguments, grant, outcome="REF_REMOVED_AT_EXPECTED_OID", progress=prog
        )
        prog["controller_complete"] = True
        prog["receipt_id"] = receipt["receipt_id"]
        return {
            "result": "LOCAL_BRANCH_RETIRED_VERIFIED" if arguments.operation == "local" else "REMOTE_BRANCH_RETIRED_VERIFIED",
            "receipt_id": str(receipt["receipt_id"]),
            "mutation_attempted": True,
            "controller_complete": True,
            "expected_oid_lease_bound": True,
        }

    # Not authorized directly: check verify / already completed / unattested absence.
    verify_dec = controller_call(
        repo,
        "branch-retirement-verify",
        "--plan-id",
        arguments.plan_id,
        "--operation",
        arguments.operation,
    )
    receipt = verify_dec.get("receipt")
    ref_exists = _ref_present(repo, arguments)
    if isinstance(receipt, dict) and receipt:
        identity_matches(arguments, receipt, grant=False)
        if ref_exists:
            raise RetirementBlocked(
                "Controller already records a consumed receipt while the Git ref is still present"
            )
        prog["controller_complete"] = True
        prog["receipt_id"] = receipt["receipt_id"]
        return {
            "result": "ALREADY_ABSENT_VERIFIED",
            "receipt_id": str(receipt["receipt_id"]),
            "mutation_attempted": False,
            "controller_complete": True,
            "expected_oid_lease_bound": False,
        }

    blockers = auth.get("blockers") or verify_dec.get("blockers") or []
    if not ref_exists and "ABSENT_UNATTESTED" in blockers:
        record_doc = {
            "schema_version": 1,
            "contract": RETIREMENT_RESULT_CONTRACT,
            "plan_id": arguments.plan_id,
            "operation": arguments.operation,
            "kind": "ATTEST_ABSENT",
            "outcome": "REF_ALREADY_ABSENT",
            "ref": (
                f"refs/heads/{arguments.branch}"
                if arguments.operation == "local"
                else f"refs/remotes/{arguments.remote}/{arguments.branch}"
            ),
            "expected_oid": arguments.expected_oid,
            "integrated_ref": normalize_integrated_ref(arguments.integrated_into, arguments.remote),
            "expected_integrated_oid": arguments.expected_integrated_oid,
            "executor": {"name": "git-finalizer", "version": VERSION},
        }
        with tempfile.TemporaryDirectory(prefix="gf-retire-rec-") as temporary:
            rfile = _write_result_file(Path(temporary), record_doc)
            rec = controller_call(
                repo,
                "branch-retirement-record",
                "--plan-id",
                arguments.plan_id,
                "--operation",
                arguments.operation,
                "--result-file",
                str(rfile),
            )
        rec_receipt = rec.get("receipt")
        if rec.get("code") != "BRANCH_RETIREMENT_RESULT_RECORDED" or not isinstance(rec_receipt, dict):
            raise RetirementBlocked("Controller refused recording absent branch")
        identity_matches(arguments, rec_receipt, grant=False)
        prog["controller_complete"] = True
        prog["receipt_id"] = rec_receipt["receipt_id"]
        return {
            "result": "ALREADY_ABSENT_VERIFIED",
            "receipt_id": str(rec_receipt["receipt_id"]),
            "mutation_attempted": False,
            "controller_complete": True,
            "expected_oid_lease_bound": False,
        }

    replan = replan_command(repo, arguments.branch, arguments.remote, arguments.integrated_into)
    if auth_code == "RETIREMENT_STALE" or "PLAN_STATE_DRIFT" in blockers:
        raise RetirementBlocked(
            f"Controller retirement verdict blocks ({auth_code}; blockers={blockers}): plan is stale; create a fresh plan: {replan}",
            details={"controller_code": auth_code, "blockers": list(blockers), "replan_command": replan},
        )
    raise RetirementBlocked(
        f"Controller retirement verdict blocks ({auth_code}; blockers={blockers})",
        details={"controller_code": auth_code, "blockers": list(blockers), "replan_command": replan},
    )


def _holder_is_foreign(holder: Any) -> bool:
    if not isinstance(holder, dict):
        return True
    cur = current_holder()
    return holder.get("pid") != cur.get("pid") or (
        "start_ticks" in cur and holder.get("start_ticks") != cur.get("start_ticks")
    )


def run_cli(arguments: argparse.Namespace) -> dict[str, Any]:
    verify_capabilities(Path(arguments.repo).resolve())
    preflight(arguments)
    return execute(arguments)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="git-finalize-retirement")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--operation", choices=EXECUTOR_KINDS, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--integrated-into", default="main")
    parser.add_argument("--expected-oid", required=True)
    parser.add_argument("--expected-integrated-oid", required=True)
    parser.add_argument("--recover", action="store_true", default=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        outcome = run_cli(arguments)
        print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
        return 0
    except RetirementBlocked as exc:
        print(f"retirement plan validation blocked: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    raise SystemExit(main())
