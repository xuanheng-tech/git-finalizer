#!/usr/bin/env python3
"""Explicit Gitea repository planning and empty-repository bootstrap."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# GF-INTEGRATION-ADAPTER: gitea-repository-bootstrap (single site: API surface)
VERSION = "1.4.0"
SUMMARY_SCHEMA_VERSION = 1
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
PLAN_DECISIONS = frozenset(
    {
        "CREATE_ALLOWED",
        "ALREADY_EXISTS_MATCH",
        "BLOCK_REMOTE_MISMATCH",
        "BLOCK_PERMISSION",
        "BLOCK_INVALID_OWNER",
        "BLOCK_INVALID_CONFIG",
        "UNKNOWN",
    }
)


class BootstrapError(RuntimeError):
    def __init__(self, decision: str, message: str) -> None:
        super().__init__(message)
        self.decision = decision


@dataclass(frozen=True)
class Credential:
    username: str
    password: str


@dataclass(frozen=True)
class Target:
    local_repo: Path
    base_url: str
    api_url: str
    owner: str
    repo_name: str
    visibility: str
    description: str
    remote: str
    expected_remote_url: str
    profile: str | None
    add_origin: bool
    timeout: float


@dataclass(frozen=True)
class PlanResult:
    decision: str
    reason: str
    owner_kind: str | None
    remote_state: str
    remote_url: str | None
    repository: dict[str, Any] | None


def _git(repo: Path, *arguments: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
    }
    return subprocess.run(
        ["git", "-C", os.fspath(repo), *arguments],
        input=input_text,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _repository_root(path: Path) -> Path:
    try:
        requested = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise BootstrapError("BLOCK_INVALID_CONFIG", "repository path is unavailable") from exc
    result = _git(requested, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise BootstrapError("BLOCK_INVALID_CONFIG", "path is not a Git worktree")
    try:
        root = Path(result.stdout.strip()).resolve(strict=True)
    except OSError as exc:
        raise BootstrapError("BLOCK_INVALID_CONFIG", "Git repository root is unavailable") from exc
    if root != requested:
        raise BootstrapError("BLOCK_INVALID_CONFIG", "--repo must be the worktree root")
    return root


def _normalize_base_url(value: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise BootstrapError("BLOCK_INVALID_CONFIG", "Gitea URL must be a credential-free HTTP(S) URL")
    path = parsed.path.rstrip("/")
    base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return base, f"{base}/api/v1"


def _target(arguments: argparse.Namespace) -> Target:
    if NAME_PATTERN.fullmatch(arguments.owner) is None:
        raise BootstrapError("BLOCK_INVALID_OWNER", "owner name is invalid")
    if NAME_PATTERN.fullmatch(arguments.repo_name) is None:
        raise BootstrapError("BLOCK_INVALID_CONFIG", "repository name is invalid")
    if not arguments.remote or arguments.remote.startswith("-") or any(
        character.isspace() for character in arguments.remote
    ):
        raise BootstrapError("BLOCK_INVALID_CONFIG", "remote name is invalid")
    if arguments.timeout <= 0 or arguments.timeout > 60:
        raise BootstrapError("BLOCK_INVALID_CONFIG", "timeout must be within (0, 60] seconds")
    base_url, api_url = _normalize_base_url(arguments.gitea_url)
    owner = urllib.parse.quote(arguments.owner, safe="")
    repo = urllib.parse.quote(arguments.repo_name, safe="")
    expected_remote = f"{base_url}/{owner}/{repo}.git"
    return Target(
        local_repo=_repository_root(arguments.repo),
        base_url=base_url,
        api_url=api_url,
        owner=arguments.owner,
        repo_name=arguments.repo_name,
        visibility=arguments.visibility,
        description=arguments.description,
        remote=arguments.remote,
        expected_remote_url=expected_remote,
        profile=arguments.gitea_profile,
        add_origin=arguments.add_origin,
        timeout=arguments.timeout,
    )


def resolve_credential(target: Target) -> Credential:
    parsed = urllib.parse.urlsplit(target.base_url)
    lines = [f"protocol={parsed.scheme}", f"host={parsed.netloc}"]
    if parsed.path.strip("/"):
        lines.append(f"path={parsed.path.strip('/')}")
    result = _git(target.local_repo, "credential", "fill", input_text="\n".join(lines) + "\n\n")
    if result.returncode != 0:
        raise BootstrapError("BLOCK_PERMISSION", "Git credential resolution failed")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"username", "password"}:
            values[key] = value
    if not values.get("username") or not values.get("password"):
        raise BootstrapError("BLOCK_PERMISSION", "Git credential authority returned no usable credential")
    return Credential(username=values["username"], password=values["password"])


class GiteaClient:
    def __init__(self, target: Target, credential: Credential) -> None:
        self.target = target
        token = base64.b64encode(
            f"{credential.username}:{credential.password}".encode("utf-8")
        ).decode("ascii")
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {token}",
            "User-Agent": f"git-finalize/{VERSION}",
        }
        self.opener = urllib.request.build_opener(_NoRedirect())

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        allowed_statuses: frozenset[int] = frozenset({200}),
    ) -> tuple[int, Any]:
        body = None
        headers = dict(self.headers)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.target.api_url}{path}", data=body, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=self.target.timeout) as response:
                status = response.status
                raw = response.read(1_048_577)
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read(1_048_577)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BootstrapError("UNKNOWN", "Gitea API request failed or timed out") from exc
        if status not in allowed_statuses:
            if status in {401, 403}:
                raise BootstrapError("BLOCK_PERMISSION", "Gitea denied the requested capability")
            raise BootstrapError("UNKNOWN", f"Gitea API returned unexpected HTTP status {status}")
        if len(raw) > 1_048_576:
            raise BootstrapError("UNKNOWN", "Gitea API response exceeded the safety bound")
        if not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BootstrapError("UNKNOWN", "Gitea API returned invalid JSON") from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: urllib.request.Request,
        _file_pointer: Any,
        _code: int,
        _message: str,
        _headers: Any,
        _new_url: str,
    ) -> None:
        return None


def _remote_state(target: Target) -> tuple[str, str | None]:
    fetch = _git(target.local_repo, "remote", "get-url", "--all", target.remote)
    if fetch.returncode != 0:
        if fetch.returncode == 2:
            return "missing", None
        raise BootstrapError("BLOCK_INVALID_CONFIG", "cannot inspect configured remote")
    fetch_urls = [line for line in fetch.stdout.splitlines() if line]
    push = _git(target.local_repo, "remote", "get-url", "--push", "--all", target.remote)
    if push.returncode != 0:
        raise BootstrapError("BLOCK_INVALID_CONFIG", "cannot inspect remote push URL")
    push_urls = [line for line in push.stdout.splitlines() if line]
    if fetch_urls != [target.expected_remote_url] or push_urls != [target.expected_remote_url]:
        observed = fetch_urls[0] if len(fetch_urls) == 1 else None
        return "conflicting", observed
    return "correct", target.expected_remote_url


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BootstrapError("UNKNOWN", f"Gitea returned an invalid {context} object")
    return value


def _list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise BootstrapError("UNKNOWN", f"Gitea returned an invalid {context} list")
    return value


def _owner_kind(client: GiteaClient, target: Target) -> str:
    _, user_raw = client.request("GET", "/user")
    user = _object(user_raw, "authenticated user")
    login = user.get("login")
    if not isinstance(login, str) or not login:
        raise BootstrapError("UNKNOWN", "Gitea user response omitted login")
    if login.casefold() == target.owner.casefold():
        return "personal"
    encoded_owner = urllib.parse.quote(target.owner, safe="")
    status, org_raw = client.request(
        "GET", f"/orgs/{encoded_owner}", allowed_statuses=frozenset({200, 404})
    )
    if status == 404:
        raise BootstrapError("BLOCK_INVALID_OWNER", "owner is neither the current user nor an organization")
    _object(org_raw, "organization")
    _, organizations_raw = client.request("GET", "/user/orgs")
    organizations = _list(organizations_raw, "organization membership")
    memberships = {
        str(item.get("username") or item.get("name") or "").casefold()
        for item in organizations
        if isinstance(item, dict)
    }
    if target.owner.casefold() not in memberships:
        raise BootstrapError("BLOCK_PERMISSION", "credential lacks organization repository capability")
    return "organization"


def _repository_matches(repository: dict[str, Any], target: Target) -> bool:
    owner = repository.get("owner")
    owner_name = owner.get("login") if isinstance(owner, dict) else None
    expected_private = target.visibility == "private"
    return (
        repository.get("name") == target.repo_name
        and isinstance(owner_name, str)
        and owner_name.casefold() == target.owner.casefold()
        and repository.get("private") is expected_private
    )


def make_plan(target: Target, client: GiteaClient) -> PlanResult:
    remote_state, remote_url = _remote_state(target)
    if remote_state == "conflicting":
        return PlanResult(
            "BLOCK_REMOTE_MISMATCH",
            "configured remote does not match the requested Gitea repository",
            None,
            remote_state,
            remote_url,
            None,
        )
    owner_kind = _owner_kind(client, target)
    owner = urllib.parse.quote(target.owner, safe="")
    name = urllib.parse.quote(target.repo_name, safe="")
    status, repository_raw = client.request(
        "GET", f"/repos/{owner}/{name}", allowed_statuses=frozenset({200, 404})
    )
    if status == 404:
        return PlanResult(
            "CREATE_ALLOWED",
            "target repository is absent and owner capability is available",
            owner_kind,
            remote_state,
            remote_url,
            None,
        )
    repository = _object(repository_raw, "repository")
    if not _repository_matches(repository, target):
        return PlanResult(
            "BLOCK_REMOTE_MISMATCH",
            "existing repository does not match owner, name, or visibility",
            owner_kind,
            remote_state,
            remote_url,
            repository,
        )
    return PlanResult(
        "ALREADY_EXISTS_MATCH",
        "existing repository matches the requested target",
        owner_kind,
        remote_state,
        remote_url,
        repository,
    )


def _create_repository(target: Target, client: GiteaClient, owner_kind: str) -> dict[str, Any]:
    owner = urllib.parse.quote(target.owner, safe="")
    endpoint = "/user/repos" if owner_kind == "personal" else f"/orgs/{owner}/repos"
    _, created_raw = client.request(
        "POST",
        endpoint,
        payload={
            "name": target.repo_name,
            "description": target.description,
            "private": target.visibility == "private",
            "auto_init": False,
        },
        allowed_statuses=frozenset({201}),
    )
    created = _object(created_raw, "created repository")
    if not _repository_matches(created, target):
        raise BootstrapError("UNKNOWN", "created repository response does not match the request")
    return created


def _verify_repository(target: Target, client: GiteaClient, *, require_empty: bool) -> dict[str, Any]:
    owner = urllib.parse.quote(target.owner, safe="")
    name = urllib.parse.quote(target.repo_name, safe="")
    _, repository_raw = client.request("GET", f"/repos/{owner}/{name}")
    repository = _object(repository_raw, "repository verification")
    if not _repository_matches(repository, target):
        raise BootstrapError("UNKNOWN", "post-create repository verification mismatched")
    if require_empty and repository.get("empty") is not True:
        raise BootstrapError("UNKNOWN", "new repository is not verified empty")
    return repository


def _add_origin(target: Target) -> str:
    state, _ = _remote_state(target)
    if state == "correct":
        return "already_correct"
    if state != "missing":
        raise BootstrapError("BLOCK_REMOTE_MISMATCH", "remote changed before origin update")
    result = _git(
        target.local_repo,
        "remote",
        "add",
        target.remote,
        target.expected_remote_url,
    )
    if result.returncode != 0:
        raise BootstrapError("UNKNOWN", "repository exists but adding origin failed")
    verified, _ = _remote_state(target)
    if verified != "correct":
        raise BootstrapError("UNKNOWN", "origin post-update verification failed")
    return "added_verified"


def _repository_id(repository: dict[str, Any] | None) -> int | str | None:
    if repository is None:
        return None
    value = repository.get("id")
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        return value
    return None


def _receipt(
    *,
    arguments: argparse.Namespace,
    target: Target | None,
    status: str,
    decision: str,
    reason: str,
    plan: PlanResult | None,
    bootstrap_executed: bool,
    bootstrap_outcome: str,
    repository: dict[str, Any] | None,
    origin_outcome: str,
) -> dict[str, Any]:
    return {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "finalizer_version": VERSION,
        "operation": "repository-bootstrap",
        "mode": "repo-ensure" if arguments.repo_ensure else "repo-plan",
        "status": status,
        "decision": decision,
        "reason": reason,
        "repository": str(target.local_repo) if target else str(arguments.repo),
        "repository_id": arguments.repository_id,
        "allocation_id": arguments.allocation_id,
        "task_key": arguments.task_key,
        "authority_key": arguments.authority_key,
        "worktree_path": arguments.worktree_path,
        "role": arguments.role,
        "target": {
            "profile": target.profile if target else arguments.gitea_profile,
            "base_url": target.base_url if target else None,
            "owner": target.owner if target else arguments.owner,
            "repository": target.repo_name if target else arguments.repo_name,
            "visibility": target.visibility if target else arguments.visibility,
        },
        "remote": {
            "name": target.remote if target else arguments.remote,
            "state": plan.remote_state if plan else "unknown",
            "url": plan.remote_url if plan else None,
            "expected_url": target.expected_remote_url if target else None,
            "outcome": origin_outcome,
        },
        "bootstrap": {
            "executed": bootstrap_executed,
            "outcome": bootstrap_outcome,
            "gitea_repository_id": _repository_id(repository),
            "owner_kind": plan.owner_kind if plan else None,
        },
        "publication": {"executed": False, "outcome": "not_run"},
        "warnings": [],
        "errors": [] if status == "success" else [{"code": decision, "message": reason}],
    }


def execute(arguments: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    target: Target | None = None
    plan: PlanResult | None = None
    bootstrap_executed = False
    bootstrap_outcome = "not_run"
    origin_outcome = "not_run"
    repository: dict[str, Any] | None = None
    try:
        target = _target(arguments)
        credential = resolve_credential(target)
        client = GiteaClient(target, credential)
        plan = make_plan(target, client)
        repository = plan.repository
        if plan.decision not in {"CREATE_ALLOWED", "ALREADY_EXISTS_MATCH"}:
            raise BootstrapError(plan.decision, plan.reason)
        if arguments.repo_plan:
            receipt = _receipt(
                arguments=arguments,
                target=target,
                status="success",
                decision=plan.decision,
                reason=plan.reason,
                plan=plan,
                bootstrap_executed=False,
                bootstrap_outcome="planned",
                repository=repository,
                origin_outcome="not_run",
            )
            return 0, receipt
        if plan.decision == "CREATE_ALLOWED":
            if plan.owner_kind is None:
                raise BootstrapError("UNKNOWN", "owner capability was not resolved")
            bootstrap_executed = True
            _create_repository(target, client, plan.owner_kind)
            repository = _verify_repository(target, client, require_empty=True)
            bootstrap_outcome = "created_empty_verified"
        else:
            repository = _verify_repository(target, client, require_empty=False)
            bootstrap_outcome = "already_exists_verified"
        if target.add_origin and plan.remote_state == "missing":
            origin_outcome = _add_origin(target)
        elif plan.remote_state == "correct":
            origin_outcome = "already_correct"
        else:
            origin_outcome = "left_missing"
        receipt = _receipt(
            arguments=arguments,
            target=target,
            status="success",
            decision="ALREADY_EXISTS_MATCH" if not bootstrap_executed else "CREATE_ALLOWED",
            reason="repository bootstrap verified; publication was not run",
            plan=plan,
            bootstrap_executed=bootstrap_executed,
            bootstrap_outcome=bootstrap_outcome,
            repository=repository,
            origin_outcome=origin_outcome,
        )
        return 0, receipt
    except BootstrapError as exc:
        decision = exc.decision if exc.decision in PLAN_DECISIONS else "UNKNOWN"
        receipt = _receipt(
            arguments=arguments,
            target=target,
            status="blocked" if decision.startswith("BLOCK_") else "failed",
            decision=decision,
            reason=str(exc),
            plan=plan,
            bootstrap_executed=bootstrap_executed,
            bootstrap_outcome=bootstrap_outcome,
            repository=repository,
            origin_outcome=origin_outcome,
        )
        return (2 if decision.startswith("BLOCK_") else 3), receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-finalize", description="Plan or ensure one empty Gitea repository"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--repo-plan", action="store_true")
    mode.add_argument("--repo-ensure", action="store_true")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--gitea-url", required=True)
    parser.add_argument("--gitea-profile")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo-name", required=True)
    parser.add_argument("--visibility", choices=["private", "public"], required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--add-origin", action="store_true")
    parser.add_argument("--timeout", type=float, default=10.0, help=argparse.SUPPRESS)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--repository-id")
    parser.add_argument("--allocation-id")
    parser.add_argument("--task-key")
    parser.add_argument("--authority-key")
    parser.add_argument("--worktree-path")
    parser.add_argument("--role")
    return parser


def render_human(receipt: dict[str, Any]) -> str:
    lines = [
        f"Decision: {receipt['decision']}",
        f"Reason: {receipt['reason']}",
        f"Repository: {receipt['repository']}",
        f"Target: {receipt['target']['owner']}/{receipt['target']['repository']}",
        f"Bootstrap: {receipt['bootstrap']['outcome']}",
        f"Origin: {receipt['remote']['outcome']}",
        "Publication: not run",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    exit_code, receipt = execute(arguments)
    if arguments.summary:
        print(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")))
    else:
        stream = sys.stdout if exit_code == 0 else sys.stderr
        print(render_human(receipt), file=stream)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
