# GF-INTEGRATION-ADAPTER: gitea-repository-bootstrap (single site: Gitea REST API v1 plan/ensure client)
"""Plan or idempotently ensure an empty Gitea repository over HTTPS (or localhost)."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from git_finalizer import __version__
from git_finalizer.errors import ErrorCode
from git_finalizer.git_io import validate_remote_name

SUMMARY_SCHEMA_VERSION = 1
VERSION = __version__
DEFAULT_HTTP_TIMEOUT = 15.0
GIT_TIMEOUT_SECONDS = 30.0
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class Credential:
    username: str
    password: str


@dataclass(frozen=True)
class Target:
    repo: Path
    gitea_url: str
    owner: str
    repo_name: str
    visibility: str
    remote: str
    ensure: bool
    add_origin: bool
    allow_insecure_http: bool
    remote_scheme: str
    timeout: float
    summary: bool
    profile: str | None = None
    description: str = ""
    repository_id: str | None = None
    allocation_id: str | None = None
    task_key: str | None = None
    authority_key: str | None = None
    worktree_path: str | None = None
    role: str | None = None


class BootstrapFailure(RuntimeError):
    def __init__(
        self,
        decision: str,
        message: str,
        *,
        status: str = "blocked",
        error_code: str = ErrorCode.BOOTSTRAP_BLOCKED,
        exit_code: int = 2,
        bootstrap_executed: bool = False,
        owner_kind: str | None = None,
    ) -> None:
        self.decision = decision
        self.message = message
        self.status = status
        self.error_code = error_code
        self.exit_code = exit_code
        self.bootstrap_executed = bootstrap_executed
        self.owner_kind = owner_kind
        super().__init__(message)


def _git(
    repo: Path,
    *arguments: str,
    input_text: str | None = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            close_fds=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapFailure(
            "UNKNOWN",
            f"Git credential/config command timed out ({arguments[0]})",
            status="failed",
            error_code=ErrorCode.BOOTSTRAP_FAILED,
            exit_code=3,
        ) from exc


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    lowered = host.lower()
    if lowered in {"localhost", "::1"}:
        return True
    try:
        addr = ipaddress.ip_address(lowered.strip("[]"))
        return addr.is_loopback
    except ValueError:
        return False


def _validate_gitea_url(raw_url: str, *, allow_insecure_http: bool) -> tuple[str, str, int | None]:
    try:
        parsed = urlparse(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise BootstrapFailure("BLOCK_INVALID_CONFIG", f"invalid gitea URL: {exc}") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(c in raw_url for c in ("\n", "\r", "\0"))
    ):
        raise BootstrapFailure("BLOCK_INVALID_CONFIG", "gitea URL is invalid or contains embedded credentials")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname) and not allow_insecure_http:
        raise BootstrapFailure(
            "BLOCK_INVALID_CONFIG",
            "plaintext http:// is forbidden for non-loopback Gitea hosts unless --allow-insecure-http is passed",
        )
    normalized = raw_url.rstrip("/")
    return normalized, parsed.hostname, port


def _target(arguments: argparse.Namespace) -> Target:
    if not math.isfinite(arguments.timeout) or arguments.timeout <= 0 or arguments.timeout > 120:
        raise BootstrapFailure("BLOCK_INVALID_CONFIG", "timeout must be finite between 0 and 120 seconds")
    if not OWNER_RE.fullmatch(arguments.owner):
        raise BootstrapFailure("BLOCK_INVALID_CONFIG", "owner name is invalid")
    if not REPO_NAME_RE.fullmatch(arguments.repo_name) or arguments.repo_name.endswith(".git"):
        raise BootstrapFailure("BLOCK_INVALID_CONFIG", "repository name is invalid")
    try:
        remote = validate_remote_name(arguments.remote)
    except Exception as exc:
        raise BootstrapFailure("BLOCK_INVALID_CONFIG", "remote name is invalid") from exc
    normalized_url, _, _ = _validate_gitea_url(
        arguments.gitea_url, allow_insecure_http=getattr(arguments, "allow_insecure_http", False)
    )
    return Target(
        repo=Path(arguments.repo).resolve(),
        gitea_url=normalized_url,
        owner=arguments.owner,
        repo_name=arguments.repo_name,
        visibility=arguments.visibility,
        remote=remote,
        ensure=bool(getattr(arguments, "repo_ensure", False)),
        add_origin=bool(getattr(arguments, "add_origin", False)),
        allow_insecure_http=bool(getattr(arguments, "allow_insecure_http", False)),
        remote_scheme=str(getattr(arguments, "remote_scheme", "auto")),
        timeout=float(arguments.timeout),
        summary=bool(getattr(arguments, "summary", False)),
        profile=getattr(arguments, "gitea_profile", None),
        description=str(getattr(arguments, "description", "") or ""),
        repository_id=getattr(arguments, "repository_id", None),
        allocation_id=getattr(arguments, "allocation_id", None),
        task_key=getattr(arguments, "task_key", None),
        authority_key=getattr(arguments, "authority_key", None),
        worktree_path=getattr(arguments, "worktree_path", None),
        role=getattr(arguments, "role", None),
    )


def resolve_credential(target: Target) -> Credential:
    parsed = urlparse(target.gitea_url)
    request = f"protocol={parsed.scheme}\nhost={parsed.netloc}\n\n"
    completed = _git(target.repo, "credential", "fill", input_text=request, timeout=target.timeout)
    if completed.returncode != 0:
        raise BootstrapFailure("BLOCK_PERMISSION", "unable to resolve Git credential for target Gitea host")
    fields: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            fields[key] = val
    user = fields.get("username", "")
    pw = fields.get("password", "")
    if not pw:
        raise BootstrapFailure("BLOCK_PERMISSION", "Git credential helper returned an empty token/password")
    return Credential(username=user, password=pw)


def _api_request(
    target: Target,
    credential: Credential,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    url = f"{target.gitea_url}/api/v1{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    token_pair = base64.b64encode(f"{credential.username}:{credential.password}".encode()).decode("ascii")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Basic {token_pair}",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, method=method, headers=headers)
    # Bypass HTTP proxy for loopback hosts; keep normal opener otherwise.
    opener = build_opener(ProxyHandler({})) if _is_loopback_host(urlparse(url).hostname) else build_opener()
    try:
        with opener.open(req, timeout=target.timeout) as resp:
            raw = resp.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
            return resp.status, parsed
    except HTTPError as err:
        raw = err.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            parsed = {}
        return err.code, parsed
    except (URLError, TimeoutError, ValueError, UnicodeDecodeError) as exc:
        raise BootstrapFailure(
            "UNKNOWN",
            f"Gitea API error or invalid JSON response: {exc.__class__.__name__}",
            status="failed",
            error_code=ErrorCode.BOOTSTRAP_FAILED,
            exit_code=3,
        ) from exc


def _redact_url(raw_url: str) -> str:
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return "<invalid-url>"
    if parsed.username is not None or parsed.password is not None:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return parsed._replace(netloc=f"<redacted>@{host}{port}").geturl()
    return raw_url


def _expected_remote_urls(target: Target, repo_data: dict[str, Any] | None = None) -> tuple[str, ...]:
    https_url = f"{target.gitea_url}/{target.owner}/{target.repo_name}.git"
    parsed = urlparse(target.gitea_url)
    host = parsed.hostname or ""
    ssh_scp = f"git@{host}:{target.owner}/{target.repo_name}.git"
    ssh_scheme = f"ssh://git@{host}/{target.owner}/{target.repo_name}.git"
    candidates = [https_url, ssh_scp, ssh_scheme]
    if repo_data:
        for key in ("clone_url", "ssh_url"):
            val = repo_data.get(key)
            if isinstance(val, str) and val and val not in candidates:
                candidates.append(val)
    return tuple(candidates)


def _check_existing_remote(target: Target, repo_data: dict[str, Any] | None = None) -> tuple[str, str | None]:
    res = _git(target.repo, "remote", "get-url", target.remote)
    if res.returncode != 0:
        return "missing", None
    configured = res.stdout.strip()
    allowed = _expected_remote_urls(target, repo_data)
    if configured in allowed:
        return "correct", _redact_url(configured)
    raise BootstrapFailure(
        "BLOCK_REMOTE_MISMATCH",
        f"configured remote {target.remote} points to unexpected URL: {_redact_url(configured)}",
    )


def _preferred_remote_url(target: Target, repo_data: dict[str, Any] | None) -> str:
    if target.remote_scheme == "ssh":
        if repo_data and isinstance(repo_data.get("ssh_url"), str) and repo_data["ssh_url"]:
            return str(repo_data["ssh_url"])
        host = urlparse(target.gitea_url).hostname or ""
        return f"git@{host}:{target.owner}/{target.repo_name}.git"
    return f"{target.gitea_url}/{target.owner}/{target.repo_name}.git"


def _resolve_owner_kind(target: Target, credential: Credential) -> str:
    status, me = _api_request(target, credential, "GET", "/user")
    if status in (401, 403):
        raise BootstrapFailure("BLOCK_PERMISSION", "authentication failed against Gitea /api/v1/user")
    if status != 200 or not isinstance(me, dict):
        raise BootstrapFailure("UNKNOWN", f"unexpected status {status} from /user", status="failed", error_code=ErrorCode.BOOTSTRAP_FAILED, exit_code=3)
    if me.get("login") == target.owner:
        return "user"

    page = 1
    while page <= 50:
        st, orgs = _api_request(target, credential, "GET", f"/user/orgs?page={page}&limit=50")
        if st in (401, 403):
            raise BootstrapFailure("BLOCK_PERMISSION", "permission denied querying Gitea organizations")
        if st != 200 or not isinstance(orgs, list):
            raise BootstrapFailure("UNKNOWN", "unexpected organization list payload", status="failed", error_code=ErrorCode.BOOTSTRAP_FAILED, exit_code=3)
        if not orgs:
            break
        for org in orgs:
            if isinstance(org, dict) and (org.get("username") == target.owner or org.get("login") == target.owner):
                return "organization"
        page += 1

    # Confirm whether org exists at all vs missing.
    org_st, _ = _api_request(target, credential, "GET", f"/orgs/{quote(target.owner)}")
    if org_st in (401, 403):
        raise BootstrapFailure("BLOCK_PERMISSION", f"permission denied for organization {target.owner}")
    raise BootstrapFailure("BLOCK_INVALID_OWNER", f"unknown or inaccessible owner {target.owner}")


def execute(arguments: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    mode = "repo-ensure" if getattr(arguments, "repo_ensure", False) else "repo-plan"
    bootstrap_executed = False
    owner_kind: str | None = None
    target: Target | None = None
    repo_data: dict[str, Any] | None = None
    remote_state = "unknown"
    remote_url: str | None = None
    reason = ""
    try:
        target = _target(arguments)
        credential = resolve_credential(target)

        st, repo_data = _api_request(
            target,
            credential,
            "GET",
            f"/repos/{quote(target.owner)}/{quote(target.repo_name)}",
        )
        if st in (401, 403):
            raise BootstrapFailure("BLOCK_PERMISSION", "permission denied inspecting target repository")
        if st == 200 and isinstance(repo_data, dict):
            want_private = target.visibility == "private"
            if bool(repo_data.get("private")) != want_private:
                raise BootstrapFailure("BLOCK_REMOTE_MISMATCH", "existing repository visibility mismatch")
            if repo_data.get("empty") is False:
                raise BootstrapFailure("BLOCK_REMOTE_MISMATCH", "existing repository is not empty")
            decision = "ALREADY_EXISTS_MATCH"
            reason = "target repository already exists and is verified empty"
            bootstrap_outcome = "already_exists_verified"
            owner_kind = "organization" if repo_data.get("owner", {}).get("is_organization") else "user"
            remote_state, remote_url = _check_existing_remote(target, repo_data)
        elif st == 404:
            owner_kind = _resolve_owner_kind(target, credential)
            remote_state, remote_url = _check_existing_remote(target)
            if not target.ensure:
                decision = "CREATE_ALLOWED"
                reason = "target repository does not exist yet and may be created"
                bootstrap_outcome = "planned"
                repo_data = None
            else:
                create_path = (
                    "/user/repos"
                    if owner_kind == "user"
                    else f"/orgs/{quote(target.owner)}/repos"
                )
                post_st, created = _api_request(
                    target,
                    credential,
                    "POST",
                    create_path,
                    {
                        "name": target.repo_name,
                        "private": target.visibility == "private",
                        "auto_init": False,
                        **({"description": target.description} if target.description else {}),
                    },
                )
                if post_st in (401, 403):
                    raise BootstrapFailure("BLOCK_PERMISSION", "permission denied creating repository", owner_kind=owner_kind)
                if post_st not in (200, 201) or not isinstance(created, dict):
                    raise BootstrapFailure(
                        "UNKNOWN",
                        f"repository creation returned status {post_st}",
                        status="failed",
                        error_code=ErrorCode.BOOTSTRAP_FAILED,
                        exit_code=3,
                        owner_kind=owner_kind,
                    )
                bootstrap_executed = True
                repo_data = created
                decision = "CREATED_EMPTY_VERIFIED"
                reason = "created and verified empty target repository"
                bootstrap_outcome = "created_empty_verified"
        else:
            raise BootstrapFailure(
                "UNKNOWN",
                f"unexpected repository lookup status {st}",
                status="failed",
                error_code=ErrorCode.BOOTSTRAP_FAILED,
                exit_code=3,
            )

        remote_outcome = "not_run" if mode == "repo-plan" else "unchanged"
        if target.ensure and target.add_origin and remote_state == "missing":
            preferred = _preferred_remote_url(target, repo_data)
            added = _git(target.repo, "remote", "add", target.remote, preferred)
            if added.returncode != 0:
                if bootstrap_executed:
                    raise BootstrapFailure(
                        "UNKNOWN",
                        f"remote changed before origin update; git remote add {target.remote} failed after creating repository",
                        status="failed",
                        error_code=ErrorCode.BOOTSTRAP_FAILED,
                        exit_code=3,
                        bootstrap_executed=True,
                        owner_kind=owner_kind,
                    )
                raise BootstrapFailure("BLOCK_INVALID_CONFIG", f"git remote add {target.remote} failed")
            verified = _git(target.repo, "remote", "get-url", target.remote)
            if verified.returncode != 0 or verified.stdout.strip() != preferred:
                if bootstrap_executed:
                    raise BootstrapFailure(
                        "UNKNOWN",
                        "remote changed before origin update; configured remote could not be verified",
                        status="failed",
                        error_code=ErrorCode.BOOTSTRAP_FAILED,
                        exit_code=3,
                        bootstrap_executed=True,
                        owner_kind=owner_kind,
                    )
                raise BootstrapFailure("BLOCK_REMOTE_MISMATCH", "remote URL mismatch after configuration")
            remote_state = "correct"
            remote_outcome = "added_verified"
            remote_url = _redact_url(preferred)
        elif target.ensure and target.add_origin and remote_state == "correct":
            remote_outcome = "already_correct"

        repo_id_val = repo_data.get("id") if isinstance(repo_data, dict) else None
        gitea_repo_id = repo_id_val if isinstance(repo_id_val, (int, str)) and not isinstance(repo_id_val, bool) else None
        receipt = {
            "summary_schema_version": SUMMARY_SCHEMA_VERSION,
            "finalizer_version": VERSION,
            "operation": "repository-bootstrap",
            "mode": mode,
            "status": "success",
            "decision": decision,
            "reason": reason,
            "repository": str(target.repo),
            "repository_id": target.repository_id,
            "allocation_id": target.allocation_id,
            "task_key": target.task_key,
            "authority_key": target.authority_key,
            "worktree_path": target.worktree_path,
            "role": target.role,
            "target": {
                "profile": target.profile,
                "base_url": target.gitea_url,
                "owner": target.owner,
                "repository": target.repo_name,
                "visibility": target.visibility,
            },
            "bootstrap": {
                "executed": bootstrap_executed,
                "outcome": bootstrap_outcome,
                "gitea_repository_id": gitea_repo_id,
                "owner_kind": owner_kind,
            },
            "remote": {
                "name": target.remote,
                "state": remote_state,
                "outcome": remote_outcome,
                "url": remote_url,
                "expected_url": _redact_url(_preferred_remote_url(target, repo_data)),
            },
            "publication": {"executed": False, "outcome": "not_run"},
            "warnings": [],
            "errors": [],
        }
        return 0, receipt
    except BootstrapFailure as exc:
        decision = exc.decision
        receipt = {
            "summary_schema_version": SUMMARY_SCHEMA_VERSION,
            "finalizer_version": VERSION,
            "operation": "repository-bootstrap",
            "mode": mode,
            "status": exc.status,
            "decision": decision,
            "reason": exc.message,
            "error_code": exc.error_code,
            "repository": str(target.repo) if target else str(arguments.repo),
            "repository_id": target.repository_id if target else getattr(arguments, "repository_id", None),
            "allocation_id": target.allocation_id if target else getattr(arguments, "allocation_id", None),
            "task_key": target.task_key if target else getattr(arguments, "task_key", None),
            "authority_key": target.authority_key if target else getattr(arguments, "authority_key", None),
            "worktree_path": target.worktree_path if target else getattr(arguments, "worktree_path", None),
            "role": target.role if target else getattr(arguments, "role", None),
            "target": {
                "profile": target.profile if target else getattr(arguments, "gitea_profile", None),
                "base_url": target.gitea_url if target else None,
                "owner": target.owner if target else getattr(arguments, "owner", None),
                "repository": target.repo_name if target else getattr(arguments, "repo_name", None),
                "visibility": target.visibility if target else getattr(arguments, "visibility", None),
            },
            "bootstrap": {
                "executed": exc.bootstrap_executed or bootstrap_executed,
                "outcome": "created_before_failure" if exc.bootstrap_executed else "not_executed",
                "gitea_repository_id": None,
                "owner_kind": exc.owner_kind or owner_kind,
            },
            "remote": {
                "name": getattr(arguments, "remote", "origin"),
                "state": "unknown",
                "outcome": "unchanged",
                "url": None,
                "expected_url": None,
            },
            "publication": {"executed": False, "outcome": "not_run"},
            "warnings": [],
            "errors": [{"code": decision, "message": exc.message}],
        }
        return (2 if decision.startswith("BLOCK_") else 3), receipt


def render_human(receipt: dict[str, Any]) -> str:
    return (
        f"Git Finalizer {receipt['mode']}: status={receipt['status']} "
        f"decision={receipt['decision']} bootstrap={receipt['bootstrap']['outcome']}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="git-finalize")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--repo-plan", action="store_true")
    group.add_argument("--repo-ensure", action="store_true")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--gitea-url", required=True)
    parser.add_argument("--gitea-profile")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo-name", required=True)
    parser.add_argument("--visibility", choices=["private", "public"], required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--add-origin", action="store_true")
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument("--remote-scheme", choices=["auto", "https", "ssh"], default="auto")
    parser.add_argument("--timeout", type=float, default=DEFAULT_HTTP_TIMEOUT)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--repository-id")
    parser.add_argument("--allocation-id")
    parser.add_argument("--task-key")
    parser.add_argument("--authority-key")
    parser.add_argument("--worktree-path")
    parser.add_argument("--role")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    code, receipt = execute(arguments)
    if arguments.summary:
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    else:
        stream = sys.stdout if code == 0 else sys.stderr
        print(render_human(receipt), file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
