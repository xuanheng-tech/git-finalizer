#!/usr/bin/env python3
"""Validate and atomically stage versioned tool/binary Skill pairs."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Sequence


VERSION = "2.0.0"
SCHEMA_VERSION = 1
TOOL_SKILL_MANIFEST_SCHEMA_VERSION = 2
SKILL_NAME = "git-change-delivery"
COMPATIBILITY_SKILL_NAME = "three-tool-git-workflow"
COMPATIBILITY_BUNDLE_PATH = (
    Path("compatibility") / COMPATIBILITY_SKILL_NAME / "SKILL.md"
)
SKILL_PAYLOAD = (
    "SKILL.md",
    "quick_validate.py",
    "unpublished_queue.py",
    "references/context-loader.md",
    "references/git-finalizer.md",
    "references/snapshot-runner.md",
    "references/unpublished-queue.md",
)
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
FULL_GIT_OID = re.compile(r"[0-9a-f]{40,64}\Z")
SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
# Payload-tree digests of previously released canonical Skill lines that may
# legally sit in production before an upgrade. Mirrors deploy.py in the Skill
# bundle; tests/test_tool_contract.py locks the two registries equal. Never
# add a digest produced by local hand-editing. This module must keep exactly
# one x-dot-y-dot-z numeral (the VERSION constant) for bundled_sync_version.
RELEASED_CANONICAL_SKILL_SHA256 = frozenset(
    {
        # Provider-neutral payload of the first shipped canonical line.
        "ff6d5bea2807b2c884c2ec5bee441e5fe8060abd9d03c0520c57ce39fe37adb5",
        # Canonical payload carried by the two tagged releases of that line.
        "3c8679b6cfd6578da41007feeea43e7ff83e8152e3daea9ab9154055642a92e2",
        # Canonical payload carried by the merged live-policy release.
        "7b85ec9da739bd60f76736ae6352e642dbf880bbd089058cce4cf7a9c0b5c665",
    }
)


class SyncError(Exception):
    """A validation or safe activation condition was not met."""


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SyncError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SyncError(f"JSON root must be an object: {path}")
    if raw != canonical_json(value):
        raise SyncError(f"JSON is not deterministically serialized: {path}")
    return value


def write_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.write_bytes(canonical_json(value))
    path.chmod(mode)


def require_regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise SyncError(f"{label} is not a regular file: {path}")


def require_owned_directory(path: Path, *, create: bool = False) -> None:
    if create and not path.exists():
        path.mkdir(mode=0o700, parents=True)
    if path.is_symlink() or not path.is_dir():
        raise SyncError(f"directory is missing or unsafe: {path}")
    if path.stat().st_uid != os.getuid():
        raise SyncError(f"directory is not owned by current user: {path}")


def tree_sha256(root: Path, relative_paths: Sequence[str]) -> str:
    lines: list[str] = []
    for relative in relative_paths:
        path = root / relative
        require_regular(path, "Skill payload")
        lines.append(f"{sha256_file(path)}  {relative}\n")
    return sha256_bytes("".join(lines).encode())


def aggregate_hash(values: dict[str, str]) -> str:
    return sha256_bytes(canonical_json(values))


def run(
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def git(repo: Path, *arguments: str) -> str:
    result = run(("git", "-C", str(repo), *arguments))
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise SyncError(f"git {' '.join(arguments)} failed for {repo}: {detail}")
    return result.stdout.strip()


def git_head(repo: Path) -> str:
    head = git(repo, "rev-parse", "HEAD")
    if not FULL_GIT_OID.fullmatch(head):
        raise SyncError(f"repository HEAD is not a full object id: {repo}")
    return head


def git_dirty(repo: Path) -> bool:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = run(
        ("git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"),
        env=environment,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise SyncError(f"git status failed for {repo}: {detail}")
    return bool(result.stdout.strip())


def project_version(repo: Path, tool_name: str) -> tuple[str, tuple[str, ...]]:
    if tool_name in {"context-loader", "snapshot-runner"}:
        pyproject = repo / "pyproject.toml"
        require_regular(pyproject, "project metadata")
        with pyproject.open("rb") as stream:
            data = tomllib.load(stream)
        version = data.get("project", {}).get("version")
        scripts = data.get("project", {}).get("scripts", {})
        if not isinstance(version, str) or not isinstance(scripts, dict):
            raise SyncError(f"invalid project version/scripts: {pyproject}")
        return version, tuple(sorted(str(name) for name in scripts))

    manifest = read_json(repo / "tool_skill_manifest.json")
    entrypoints = validate_install_targets(manifest)
    executable = repo / entrypoints[0]
    require_regular(executable, "Git Finalizer executable")
    match = re.search(
        rb"(?m)^(?:readonly )?VERSION=['\"]([0-9]+\.[0-9]+\.[0-9]+)['\"]$",
        executable.read_bytes(),
    )
    if match is None:
        raise SyncError("cannot read Git Finalizer source version")
    return match.group(1).decode(), tuple(sorted(entrypoints))


class Sources:
    def __init__(
        self, source_root: Path, source_repos: dict[str, Path] | None = None
    ) -> None:
        self.source_root = source_root.resolve()
        self.source_repos = {
            name: path.resolve() for name, path in (source_repos or {}).items()
        }
        self.owner_repo = self.source_repos.get(
            "git-finalizer", self.source_root / "git-finalizer"
        )
        require_owned_directory(self.owner_repo)
        self.compatibility_path = self.owner_repo / "toolchain_compatibility.json"
        self.compatibility = read_json(self.compatibility_path)
        if self.compatibility.get("schema_version") != SCHEMA_VERSION:
            raise SyncError("unsupported toolchain compatibility schema_version")
        workflow_skill = self.compatibility.get("workflow_skill")
        if workflow_skill != {
            "name": SKILL_NAME,
            "contract_version": workflow_skill.get("contract_version")
            if isinstance(workflow_skill, dict) else None,
            "canonical_owner": "git-finalizer",
            "compatibility_shims": [COMPATIBILITY_SKILL_NAME],
        }:
            raise SyncError("toolchain workflow Skill binding is unrecognized")
        if workflow_skill["contract_version"] not in {1, 2}:
            raise SyncError("unsupported workflow Skill contract version")
        tools = self.compatibility.get("tools")
        if not isinstance(tools, dict) or not tools:
            raise SyncError("toolchain compatibility contract has no tools")
        self.tool_config = tools
        if set(self.source_repos) - set(tools):
            raise SyncError("source-repo names an unknown tool")
        self.skill_root = self.owner_repo / "skills" / SKILL_NAME
        self.compatibility_skill_root = (
            self.owner_repo / "skills" / COMPATIBILITY_SKILL_NAME
        )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.tool_config))

    def repo(self, tool: str) -> Path:
        config = self.tool_config.get(tool)
        if not isinstance(config, dict):
            raise SyncError(f"unknown tool: {tool}")
        if tool in self.source_repos:
            repo = self.source_repos[tool]
            require_owned_directory(repo)
            return repo
        source_directory = config.get("source_directory")
        if not isinstance(source_directory, str) or not SAFE_NAME.fullmatch(
            source_directory
        ):
            raise SyncError(f"unsafe source directory for {tool}")
        repo = (self.source_root / source_directory).resolve()
        if repo.parent != self.source_root:
            raise SyncError(f"source directory escapes source root for {tool}")
        require_owned_directory(repo)
        return repo

    def manifest_path(self, tool: str) -> Path:
        config = self.tool_config.get(tool, {})
        manifest_rel = config.get("skill_manifest")
        if manifest_rel:
            return (self.owner_repo / manifest_rel).resolve()
        repo = self.repo(tool)
        repo_manifest = repo / "tool_skill_manifest.json"
        if repo_manifest.exists():
            return repo_manifest
        internal_manifest = (
            self.owner_repo / "manifests" / tool / "tool_skill_manifest.json"
        )
        if internal_manifest.exists():
            return internal_manifest
        return repo_manifest


def validate_install_targets(
    manifest: dict[str, Any], *, allow_legacy: bool = False
) -> tuple[str, ...]:
    executable = manifest.get("executable")
    install_targets = manifest.get("install_targets")
    if not isinstance(executable, dict) or not isinstance(install_targets, dict):
        raise SyncError("manifest executable/install_targets must be objects")
    kind = executable.get("kind")
    if kind not in {"python_console_scripts", "source_files"}:
        raise SyncError(f"unsupported executable kind: {kind}")
    entrypoints = executable.get("entrypoints")
    stable_entries = install_targets.get("stable_entries")
    if (
        not isinstance(entrypoints, list)
        or not entrypoints
        or not isinstance(stable_entries, list)
        or tuple(entrypoints) != tuple(stable_entries)
    ):
        raise SyncError("manifest entrypoints and stable_entries must match")
    if any(
        not isinstance(name, str) or not SAFE_NAME.fullmatch(name)
        for name in entrypoints
    ):
        raise SyncError("manifest contains unsafe entrypoint")
    expected_skill = SKILL_NAME
    if allow_legacy and manifest.get("schema_version") == 1:
        expected_skill = COMPATIBILITY_SKILL_NAME
    if install_targets.get("skill_directory") != expected_skill:
        raise SyncError("manifest has unrecognized Skill install target")
    if kind == "source_files":
        paths = executable.get("paths")
        if not isinstance(paths, list) or not paths:
            raise SyncError("source_files executable requires paths")
        if any(
            not isinstance(path, str)
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            for path in paths
        ):
            raise SyncError("manifest contains unsafe executable source path")
        production_targets = executable.get("production_targets")
        if not isinstance(production_targets, dict) or set(production_targets) != set(
            paths
        ):
            raise SyncError(
                "source_files executable requires one production target per path"
            )
        for relative in production_targets.values():
            if (
                not isinstance(relative, str)
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or not Path(relative).parts
                or Path(relative).parts[0] not in {"bin", "hooks"}
            ):
                raise SyncError("manifest contains unsafe production target")
    elif "production_targets" in executable:
        raise SyncError("python_console_scripts must not declare production targets")
    return tuple(str(name) for name in entrypoints)


def validate_contract_shape(contract: dict[str, Any]) -> None:
    commands = contract.get("commands")
    operation_classes = contract.get("operation_classes")
    protected_operations = contract.get("protected_operations")
    if not isinstance(commands, list) or not commands:
        raise SyncError("public CLI contract must contain commands")
    if not isinstance(operation_classes, list) or not operation_classes:
        raise SyncError("public CLI contract must contain operation_classes")
    if not isinstance(protected_operations, list):
        raise SyncError("public CLI contract protected_operations must be a list")
    allowed_classes = {"read_only", "local_mutation", "remote_mutation"}
    if not set(operation_classes) <= allowed_classes:
        raise SyncError("public CLI contract has an unknown operation_class")
    names: set[str] = set()
    for command in commands:
        if not isinstance(command, dict):
            raise SyncError("public CLI contract command must be an object")
        name = command.get("name")
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name) or name in names:
            raise SyncError("public CLI contract command name is invalid or duplicated")
        names.add(name)
        if command.get("operation_class") not in operation_classes:
            raise SyncError("command operation_class is not declared")
        for key in ("flags", "preconditions", "result_statuses"):
            if not isinstance(command.get(key), list):
                raise SyncError(f"public CLI contract command {key} must be a list")


def validate_manifest_static_fields(
    manifest: dict[str, Any], *, allow_legacy: bool = False
) -> None:
    required = {
        "schema_version",
        "tool_name",
        "tool_version",
        "tool_commit",
        "skill_contract_version",
        "public_cli_contract_sha256",
        "canonical_skill_sha256",
        "compatible_toolchain_contract_version",
        "canonical_skill",
        "executable",
        "install_targets",
    }
    legacy = allow_legacy and manifest.get("schema_version") == 1
    if not legacy:
        required.add("compatibility_skill")
    if set(manifest) != required:
        raise SyncError("ToolSkillManifest fields do not match its schema")
    for key in ("public_cli_contract_sha256", "canonical_skill_sha256"):
        if not isinstance(manifest.get(key), str) or not HEX_SHA256.fullmatch(
            manifest[key]
        ):
            raise SyncError(f"ToolSkillManifest {key} is not SHA-256")
    expected_name = COMPATIBILITY_SKILL_NAME if legacy else SKILL_NAME
    canonical_skill = manifest.get("canonical_skill")
    if canonical_skill != {
        "owner": "git-finalizer",
        "path": f"skills/{expected_name}/SKILL.md",
    }:
        raise SyncError("ToolSkillManifest canonical Skill authority is unrecognized")
    if legacy:
        return
    compatibility_skill = manifest.get("compatibility_skill")
    if (
        not isinstance(compatibility_skill, dict)
        or set(compatibility_skill) != {"name", "path", "sha256"}
        or compatibility_skill.get("name") != COMPATIBILITY_SKILL_NAME
        or compatibility_skill.get("path")
        != f"skills/{COMPATIBILITY_SKILL_NAME}/SKILL.md"
    ):
        raise SyncError("ToolSkillManifest compatibility Skill binding is invalid")
    digest = compatibility_skill.get("sha256")
    if not isinstance(digest, str) or not HEX_SHA256.fullmatch(digest):
        raise SyncError("ToolSkillManifest compatibility Skill SHA is invalid")


def check_tool(sources: Sources, tool: str) -> dict[str, Any]:
    errors: list[str] = []
    repo = sources.repo(tool)
    manifest_path = sources.manifest_path(tool)
    contract_path = repo / "tool_cli_contract.json"
    try:
        manifest = read_json(manifest_path)
        contract = read_json(contract_path)
        if manifest.get("schema_version") != TOOL_SKILL_MANIFEST_SCHEMA_VERSION:
            raise SyncError("unsupported ToolSkillManifest schema_version")
        if contract.get("schema_version") != SCHEMA_VERSION:
            raise SyncError("unsupported public CLI contract schema_version")
        if manifest.get("tool_name") != tool or contract.get("tool_name") != tool:
            raise SyncError("tool_name mismatch")
        validate_manifest_static_fields(manifest)
        validate_contract_shape(contract)
        if manifest.get("tool_version") != contract.get("tool_version"):
            raise SyncError("tool version mismatch between manifest and CLI contract")
        source_version, source_entrypoints = project_version(repo, tool)
        if source_version != manifest.get("tool_version"):
            raise SyncError("source version does not match ToolSkillManifest")
        entrypoints = validate_install_targets(manifest)
        if tuple(sorted(entrypoints)) != source_entrypoints:
            raise SyncError("source public entrypoints do not match ToolSkillManifest")
        if manifest.get("tool_commit") != "@release":
            raise SyncError("canonical source manifest tool_commit must be @release")
        git_head(repo)
        contract_sha = sha256_file(contract_path)
        if manifest.get("public_cli_contract_sha256") != contract_sha:
            raise SyncError("public CLI contract SHA mismatch")
        skill_sha = tree_sha256(sources.skill_root, SKILL_PAYLOAD)
        if manifest.get("canonical_skill_sha256") != skill_sha:
            raise SyncError("canonical Skill payload SHA mismatch")
        compatibility_sha = sha256_file(sources.compatibility_skill_root / "SKILL.md")
        if manifest["compatibility_skill"]["sha256"] != compatibility_sha:
            raise SyncError("compatibility Skill SHA mismatch")
        compatibility_version = sources.compatibility.get("toolchain_contract_version")
        if compatibility_version not in {1, 2, 3, 4, 5}:
            raise SyncError("unsupported toolchain compatibility contract version")
        if (
            manifest.get("compatible_toolchain_contract_version")
            != compatibility_version
        ):
            raise SyncError("toolchain compatibility version mismatch")
        config = sources.tool_config[tool]
        if contract.get("contract_version") != config.get(
            "public_cli_contract_version"
        ):
            raise SyncError("public contract version is incompatible with workflow")
        compatibility_key = tool.replace("-", "_") + "_contract_version"
        if sources.compatibility.get(compatibility_key) != config.get(
            "public_cli_contract_version"
        ):
            raise SyncError("top-level toolchain contract binding is inconsistent")
        if manifest.get("skill_contract_version") != config.get(
            "skill_contract_version"
        ):
            raise SyncError("Skill contract version is incompatible with workflow")
        controller_contract = sources.compatibility.get(
            "worktree_controller_contract_version"
        )
        controller_status = sources.compatibility.get("worktree_controller_status")
        if compatibility_version == 1:
            if (
                controller_contract is not None
                or controller_status != "planned_unavailable"
            ):
                raise SyncError(
                    "v1 requires Worktree Controller contract to remain null/planned"
                )
        elif (
            controller_contract
            != (3 if compatibility_version >= 5 else 2 if compatibility_version >= 4 else 1)
            or controller_status != "available_external"
        ):
            raise SyncError(
                "external Worktree Controller contract is incompatible with the toolchain"
            )
    except SyncError as exc:
        errors.append(str(exc))
        manifest = {}
        contract_sha = None
        skill_sha = None

    return {
        "tool": tool,
        "status": "PASS" if not errors else "FAIL",
        "canonical_commit": git_head(repo),
        "canonical_source_dirty": git_dirty(repo),
        "canonical_tool_version": manifest.get("tool_version"),
        "canonical_skill_sha256": skill_sha,
        "public_cli_contract_sha256": contract_sha,
        "errors": errors,
    }


def extract_version(output: str) -> str | None:
    matches = re.findall(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])", output)
    return matches[-1] if matches else None


def bundled_sync_version(bundle: Path) -> str:
    implementation = bundle / "executable" / "tooling" / "tool_skill_sync.py"
    require_regular(implementation, "bundled tool-skill-sync implementation")
    versions = set(
        re.findall(
            r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])",
            implementation.read_text(encoding="utf-8"),
        )
    )
    if len(versions) != 1:
        raise SyncError("bundled tool-skill-sync version is ambiguous")
    return versions.pop()


def resolve_entry(name: str, bin_dir: Path | None) -> Path | None:
    if bin_dir is not None:
        candidate = bin_dir / name
        return candidate.resolve() if candidate.exists() else None
    resolved = shutil.which(name)
    return Path(resolved).resolve() if resolved else None


def installed_binary_state(
    entrypoints: Sequence[str], bin_dir: Path | None
) -> dict[str, Any]:
    entries: dict[str, dict[str, str | None]] = {}
    hashes: dict[str, str] = {}
    versions: set[str] = set()
    complete = True
    for entrypoint in entrypoints:
        path = resolve_entry(entrypoint, bin_dir)
        if path is None or path.is_symlink() or not path.is_file():
            entries[entrypoint] = {
                "path": str(path) if path else None,
                "sha256": None,
                "version": None,
            }
            complete = False
            continue
        digest = sha256_file(path)
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        result = run((str(path), "--version"), env=environment)
        version = (
            extract_version(result.stdout + "\n" + result.stderr)
            if result.returncode == 0
            else None
        )
        if version is None:
            complete = False
        else:
            versions.add(version)
        hashes[entrypoint] = digest
        entries[entrypoint] = {"path": str(path), "sha256": digest, "version": version}
    installed_version = (
        next(iter(versions)) if complete and len(versions) == 1 else None
    )
    return {
        "version": installed_version,
        "sha256": aggregate_hash(hashes) if complete else None,
        "entries": entries,
    }


def check_tool_deployment(
    sources: Sources,
    tool: str,
    agents_root: Path,
    bin_dir: Path | None,
) -> dict[str, Any]:
    """Validate the source contract and the installed production state together.

    ``source_contract_status`` keeps the original meaning: the canonical source repo is
    internally consistent. ``installed_drift`` reports whether the executables actually on
    PATH match the declared canonical release. ``status`` is PASS only when both hold, so
    a stale or mismatched installation can no longer be reported as clean.
    """
    checked = check_tool(sources, tool)
    source_status = str(checked["status"])
    installed_skill_sha, installed_compatibility_sha = installed_skill_hashes(
        agents_root
    )
    errors = list(checked["errors"])
    binary: dict[str, Any] = {"version": None, "sha256": None, "entries": {}}
    drift = "unknown"
    try:
        manifest = read_json(sources.manifest_path(tool))
        entrypoints = validate_install_targets(manifest)
        binary = installed_binary_state(entrypoints, bin_dir)
        drift = classify_drift(
            manifest,
            source_status=source_status,
            binary_version=binary["version"],
            installed_skill_sha=installed_skill_sha,
            installed_compatibility_sha=installed_compatibility_sha,
        )
        if drift != "none":
            errors.append(
                f"installed production state drifts from the canonical release: {drift}"
            )
    except SyncError as exc:
        errors.append(str(exc))

    result = dict(checked)
    result["source_contract_status"] = source_status
    result["installed_binary_version"] = binary["version"]
    result["installed_binary_sha256"] = binary["sha256"]
    result["installed_binaries"] = binary["entries"]
    result["installed_skill_sha256"] = installed_skill_sha
    result["installed_compatibility_skill_sha256"] = installed_compatibility_sha
    result["installed_drift"] = drift
    result["errors"] = errors
    result["status"] = "PASS" if source_status == "PASS" and drift == "none" else "FAIL"
    return result


UV_EXECUTABLE = "uv"


def read_uv_receipt(root: Path) -> dict[str, Any]:
    receipt = root / "uv-receipt.toml"
    require_regular(receipt, "uv tool receipt")
    with receipt.open("rb") as stream:
        return tomllib.load(stream)


def python_tool_installation(
    entrypoints: Sequence[str], bin_dir: Path | None
) -> dict[str, Any]:
    """Describe the uv tool installation that backs one tool's console scripts."""
    resolved = {name: resolve_entry(name, bin_dir) for name in entrypoints}
    missing = sorted(name for name, path in resolved.items() if path is None)
    if missing:
        raise SyncError(f"installed entrypoints are missing: {', '.join(missing)}")
    # Retained legacy launchers deliberately live outside the uv tool directory and shim
    # into it, so the uv root is the one directory that actually carries a uv receipt.
    roots = set()
    for path in resolved.values():
        if path is None:
            continue
        for candidate in list(path.parents)[:3]:
            if (candidate / "uv-receipt.toml").is_file():
                roots.add(candidate)
                break
    if not roots:
        raise SyncError(
            "installed entrypoints do not resolve into a uv tool installation"
        )
    if len(roots) != 1:
        raise SyncError("installed entrypoints resolve to more than one uv tool root")
    root = roots.pop()
    require_owned_directory(root)
    receipt = read_uv_receipt(root)
    tool_section = receipt.get("tool", {})
    options = tool_section.get("options", {})
    install_paths = {
        entry["name"]: Path(entry["install-path"])
        for entry in tool_section.get("entrypoints", [])
        if isinstance(entry, dict) and "name" in entry and "install-path" in entry
    }
    if not set(entrypoints) <= set(install_paths):
        raise SyncError("uv receipt does not cover every declared entrypoint")
    directories = {path.parent for path in install_paths.values()}
    if len(directories) != 1:
        raise SyncError("uv receipt entrypoints span more than one bin directory")
    indexes = [
        item["url"]
        for item in options.get("index", [])
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    ]
    return {
        "root": str(root),
        "bin_dir": str(directories.pop()),
        "indexes": indexes,
        "no_build": bool(options.get("no-build", False)),
        "entry_targets": {
            name: str(path) for name, path in sorted(install_paths.items())
        },
        "resolved": {
            name: str(path)
            for name, path in sorted(resolved.items())
            if path is not None
        },
    }


def verify_installed_record(root: Path) -> dict[str, Any]:
    """Verify installed runtime files against the published wheel RECORD."""
    candidates = sorted(root.glob("lib/python*/site-packages"))
    if len(candidates) != 1:
        raise SyncError("uv tool installation has no single site-packages directory")
    site_packages = candidates[0]
    dist_infos = sorted(site_packages.glob("*.dist-info"))
    if len(dist_infos) != 1:
        raise SyncError("uv tool installation has no single dist-info directory")
    dist_info = dist_infos[0]
    record = dist_info / "RECORD"
    require_regular(record, "installed RECORD")
    verified = 0
    for line in record.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        name, _, remainder = line.partition(",")
        digest, _, _size = remainder.partition(",")
        # Console scripts live outside site-packages and RECORD lists itself without a hash.
        if not digest.startswith("sha256=") or name.startswith("../"):
            continue
        target = site_packages / name
        if not target.is_file():
            raise SyncError(f"installed file is missing: {name}")
        expected = digest.split("=", 1)[1]
        actual = (
            base64.urlsafe_b64encode(hashlib.sha256(target.read_bytes()).digest())
            .rstrip(b"=")
            .decode()
        )
        if actual != expected:
            raise SyncError(
                f"installed file does not match the published artifact: {name}"
            )
        verified += 1
    if not verified:
        raise SyncError("installed RECORD verified no files")
    return {"dist_info": dist_info.name, "verified_files": verified}


def install_python_tool(
    tool: str,
    version: str,
    *,
    bin_dir: str,
    indexes: Sequence[str],
    no_build: bool,
) -> None:
    """Install one pinned uv tool release, preserving the recorded installation shape.

    ``--force`` is required: ``--reinstall`` removes the existing installation before it
    validates entrypoint conflicts, which can leave production with no working CLI.
    """
    argv = [UV_EXECUTABLE, "tool", "install", "--force"]
    if no_build:
        argv.append("--no-build")
    for index in indexes:
        argv.extend(("--index", index))
    argv.append(f"{tool}=={version}")
    environment = dict(os.environ, UV_TOOL_BIN_DIR=bin_dir)
    result = subprocess.run(
        argv, capture_output=True, text=True, check=False, env=environment
    )
    if result.returncode:
        raise SyncError(
            f"uv tool install failed for {tool}=={version} "
            f"(exit {result.returncode}): {result.stderr.strip()[:200]}"
        )


def canonical_install_policy(
    sources: Sources, tool: str, observed: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Return the intended uv install shape and whether the observed receipt drifted.

    ``uv_install_policy`` in the toolchain compatibility contract is authoritative when
    present, so a receipt that drifted away from the declared bin directory, package index
    or no-build policy is normalized instead of being preserved. Without a declared policy
    the observed receipt is kept, which is the previous behavior.
    """
    policy = sources.tool_config.get(tool, {}).get("uv_install_policy")
    if policy is None:
        return dict(observed), False
    if not isinstance(policy, dict):
        raise SyncError(f"{tool} uv_install_policy must be an object")
    bin_dir = policy.get("bin_dir")
    if not isinstance(bin_dir, str) or not bin_dir:
        raise SyncError(f"{tool} uv_install_policy bin_dir is invalid")
    resolved = Path(bin_dir).expanduser()
    if not resolved.is_absolute():
        raise SyncError(
            f"{tool} uv_install_policy bin_dir must resolve to an absolute path"
        )
    indexes = policy.get("index", [])
    if not isinstance(indexes, list) or not all(
        isinstance(item, str) and item for item in indexes
    ):
        raise SyncError(f"{tool} uv_install_policy index must be a list of URLs")
    intended = dict(observed)
    intended["bin_dir"] = os.fspath(resolved)
    intended["indexes"] = list(indexes)
    intended["no_build"] = bool(policy.get("no_build", False))
    drifted = (
        observed["bin_dir"] != intended["bin_dir"]
        or observed["no_build"] != intended["no_build"]
        or not set(intended["indexes"]) <= set(observed["indexes"])
    )
    return intended, drifted


def verify_python_tool(
    tool: str,
    version: str,
    entrypoints: Sequence[str],
    bin_dir: Path | None,
    expected: dict[str, Any],
    *,
    expect_stable_paths: bool = True,
) -> dict[str, Any]:
    """Fail closed unless every entrypoint, hash and receipt field matches the release."""
    binary = installed_binary_state(entrypoints, bin_dir)
    if binary["version"] != version:
        raise SyncError(
            f"installed {tool} reports {binary['version']}, expected {version}"
        )
    installation = python_tool_installation(entrypoints, bin_dir)
    if installation["bin_dir"] != expected["bin_dir"]:
        raise SyncError("upgrade changed the entrypoint bin directory")
    if installation["no_build"] != expected["no_build"]:
        raise SyncError("upgrade changed the receipt no-build setting")
    if not set(expected["indexes"]) <= set(installation["indexes"]):
        raise SyncError("upgrade dropped a recorded package index")
    # A release may intentionally remove console scripts, so compare only the entrypoints
    # the target release declares. Retained entrypoints must keep their exact install path
    # and resolved target; entrypoints the new release drops are reported as retired.
    for name in entrypoints if expect_stable_paths else ():
        previous_target = expected["entry_targets"].get(name)
        if (
            previous_target is not None
            and installation["entry_targets"][name] != previous_target
        ):
            raise SyncError(f"upgrade changed the install path of {name}")
        previous_resolved = expected["resolved"].get(name)
        if (
            previous_resolved is not None
            and installation["resolved"][name] != previous_resolved
        ):
            raise SyncError(f"upgrade changed the resolved target of {name}")
    retired = sorted(set(expected["entry_targets"]) - set(entrypoints))
    for name in retired:
        path = Path(expected["entry_targets"][name])
        if path.exists() or path.is_symlink():
            raise SyncError(f"upgrade left a retired entrypoint installed: {name}")
    record = verify_installed_record(Path(installation["root"]))
    return {
        "version": binary["version"],
        "binary_sha256": binary["sha256"],
        "entries": binary["entries"],
        "installation": installation,
        "retired_entrypoints": retired,
        "record": record,
    }


def upgrade(
    sources: Sources,
    tool: str,
    *,
    bin_dir: Path | None,
    dry_run: bool = False,
    previous_bundle: Path | None = None,
) -> dict[str, Any]:
    """Upgrade one console-script tool to its canonical release, or restore the old one."""
    manifest = read_json(sources.manifest_path(tool))
    entrypoints = validate_install_targets(manifest)
    if manifest["executable"]["kind"] != "python_console_scripts":
        raise SyncError(f"{tool} is not installed as Python console scripts")
    target_version = str(manifest["tool_version"])
    previous_entries = entrypoints
    previous_manifest = None
    if previous_bundle is not None:
        verify_bundle(previous_bundle, tool)
        previous_manifest = read_json(previous_bundle / "tool_skill_manifest.json")
        if previous_manifest["executable"]["kind"] != "python_console_scripts":
            raise SyncError("previous bundle is not a Python console-script tool")
        previous_entries = validate_install_targets(previous_manifest, allow_legacy=True)
    before = python_tool_installation(previous_entries, bin_dir)
    intended, drifted = canonical_install_policy(sources, tool, before)
    previous_version = installed_binary_state(previous_entries, bin_dir)["version"]
    if previous_manifest is not None:
        if previous_version != previous_manifest["tool_version"]:
            raise SyncError("installed release does not match the explicit previous bundle")
        verify_python_tool(tool, previous_version, previous_entries, bin_dir, before)
    stable_paths = intended["bin_dir"] == before["bin_dir"]
    plan = {
        "schema_version": SCHEMA_VERSION,
        "operation": "upgrade",
        "tool": tool,
        "previous_version": previous_version,
        "target_version": target_version,
        "bin_dir": intended["bin_dir"],
        "indexes": intended["indexes"],
        "no_build": intended["no_build"],
        "receipt_drifted": drifted,
        "observed_bin_dir": before["bin_dir"],
        "observed_no_build": before["no_build"],
    }
    if previous_version == target_version and not drifted and previous_entries == entrypoints:
        plan["status"] = "UNCHANGED"
        plan["verification"] = verify_python_tool(
            tool, target_version, entrypoints, bin_dir, intended
        )
        return plan
    if dry_run:
        plan["status"] = "DRY_RUN_NORMALIZE" if drifted else "DRY_RUN"
        return plan
    try:
        install_python_tool(
            tool,
            target_version,
            bin_dir=intended["bin_dir"],
            indexes=intended["indexes"],
            no_build=intended["no_build"],
        )
        plan["verification"] = verify_python_tool(
            tool,
            target_version,
            entrypoints,
            bin_dir,
            intended,
            expect_stable_paths=stable_paths,
        )
    except (SyncError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        plan["status"] = "RESTORED"
        plan["error"] = str(exc)
        if previous_version is None:
            plan["status"] = "FAILED"
            raise SyncError(f"{exc}; no previous version recorded to restore") from None
        try:
            install_python_tool(
                tool,
                previous_version,
                bin_dir=before["bin_dir"],
                indexes=before["indexes"],
                no_build=before["no_build"],
            )
            plan["restored_verification"] = verify_python_tool(
                tool, previous_version, previous_entries, bin_dir, before
            )
            for name in set(entrypoints) - set(previous_entries):
                path = Path(intended["bin_dir"]) / name
                if path.exists() or path.is_symlink():
                    raise SyncError(f"restoration left a new entrypoint installed: {name}")
            plan["restored_shape"] = "observed"
        except (SyncError, OSError, subprocess.SubprocessError) as restore_error:
            plan["status"] = "FAILED"
            raise SyncError(
                f"{exc}; restoring {previous_version} also failed: {restore_error}"
            ) from None
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise SyncError(
            f"upgrade failed and {previous_version} was restored: {exc}"
        ) from None
    plan["status"] = "NORMALIZED" if previous_version == target_version else "UPGRADED"
    return plan


def installed_skill_hashes(agents_root: Path) -> tuple[str | None, str | None]:
    """Return the installed canonical and compatibility Skill payload hashes."""
    try:
        skill_sha = tree_sha256(agents_root / "skills" / SKILL_NAME, SKILL_PAYLOAD)
    except SyncError:
        skill_sha = None
    compatibility = agents_root / "skills" / COMPATIBILITY_SKILL_NAME / "SKILL.md"
    try:
        require_regular(compatibility, "installed compatibility Skill")
        compatibility_sha = sha256_file(compatibility)
    except SyncError:
        compatibility_sha = None
    return skill_sha, compatibility_sha


def classify_drift(
    manifest: dict[str, Any],
    *,
    source_status: str,
    binary_version: str | None,
    installed_skill_sha: str | None,
    installed_compatibility_sha: str | None,
) -> str:
    """Classify installed production state against the declared canonical release."""
    binary_matches = binary_version == manifest.get("tool_version")
    skill_matches = installed_skill_sha == manifest.get(
        "canonical_skill_sha256"
    ) and installed_compatibility_sha == manifest.get("compatibility_skill", {}).get(
        "sha256"
    )
    if source_status != "PASS" or (not binary_matches and not skill_matches):
        return "incompatible"
    if binary_matches and skill_matches:
        return "none"
    if binary_matches:
        return "skill_only"
    return "binary_only"


def status(sources: Sources, agents_root: Path, bin_dir: Path | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "operation": "status",
        "tools": [],
    }
    installed_skill_sha, installed_compatibility_sha = installed_skill_hashes(
        agents_root
    )

    for tool in sources.names():
        checked = check_tool(sources, tool)
        repo = sources.repo(tool)
        try:
            manifest = read_json(sources.manifest_path(tool))
            entrypoints = validate_install_targets(manifest)
        except SyncError:
            result["tools"].append(
                {
                    "tool_name": tool,
                    "canonical_tool_version": checked["canonical_tool_version"],
                    "canonical_commit": checked["canonical_commit"],
                    "canonical_source_dirty": checked["canonical_source_dirty"],
                    "canonical_skill_sha256": checked["canonical_skill_sha256"],
                    "public_cli_contract_sha256": checked["public_cli_contract_sha256"],
                    "installed_binary_version": None,
                    "installed_binary_sha256": None,
                    "installed_binaries": {},
                    "installed_skill_sha256": installed_skill_sha,
                    "installed_compatibility_skill_sha256": (
                        installed_compatibility_sha
                    ),
                    "drift": "incompatible",
                }
            )
            continue
        binary = installed_binary_state(entrypoints, bin_dir)
        drift = classify_drift(
            manifest,
            source_status=str(checked["status"]),
            binary_version=binary["version"],
            installed_skill_sha=installed_skill_sha,
            installed_compatibility_sha=installed_compatibility_sha,
        )
        result["tools"].append(
            {
                "tool_name": tool,
                "canonical_tool_version": manifest.get("tool_version"),
                "canonical_commit": git_head(repo),
                "canonical_source_dirty": git_dirty(repo),
                "canonical_skill_sha256": manifest.get("canonical_skill_sha256"),
                "public_cli_contract_sha256": manifest.get(
                    "public_cli_contract_sha256"
                ),
                "installed_binary_version": binary["version"],
                "installed_binary_sha256": binary["sha256"],
                "installed_binaries": binary["entries"],
                "installed_skill_sha256": installed_skill_sha,
                "installed_compatibility_skill_sha256": installed_compatibility_sha,
                "drift": drift,
            }
        )
    return result


def copy_skill_payload(source: Path, target: Path) -> None:
    for relative in SKILL_PAYLOAD:
        source_path = source / relative
        target_path = target / relative
        target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path)
        target_path.chmod(0o600)


def copy_compatibility_skill(source: Path, target: Path) -> None:
    source_path = source / "SKILL.md"
    require_regular(source_path, "compatibility Skill source")
    target_path = target / COMPATIBILITY_BUNDLE_PATH
    target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source_path, target_path)
    target_path.chmod(0o600)


def source_executable_payload(
    repo: Path,
    manifest: dict[str, Any],
    target: Path,
    tool_commit: str,
) -> tuple[str, dict[str, str]]:
    executable = manifest["executable"]
    kind = executable["kind"]
    if kind == "python_console_scripts":
        identity = {
            "schema_version": SCHEMA_VERSION,
            "kind": kind,
            "tool_name": manifest["tool_name"],
            "tool_version": manifest["tool_version"],
            "tool_commit": tool_commit,
            "entrypoints": executable["entrypoints"],
        }
        identity_path = target / "executable_identity.json"
        write_json(identity_path, identity)
        digest = sha256_file(identity_path)
        return digest, {"executable_identity.json": digest}

    executable_root = target / "executable"
    executable_root.mkdir(mode=0o700)
    hashes: dict[str, str] = {}
    for relative in executable["paths"]:
        source_path = repo / relative
        require_regular(source_path, "executable source")
        target_path = executable_root / relative
        target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path)
        target_path.chmod(stat.S_IMODE(source_path.stat().st_mode) & 0o755)
        hashes[f"executable/{relative}"] = sha256_file(target_path)
    return aggregate_hash(hashes), hashes


def verify_bundle(bundle: Path, expected_tool: str | None = None) -> dict[str, Any]:
    require_owned_directory(bundle)
    release = read_json(bundle / "release_manifest.json")
    manifest = read_json(bundle / "tool_skill_manifest.json")
    contract = read_json(bundle / "tool_cli_contract.json")
    if release.get("schema_version") != SCHEMA_VERSION:
        raise SyncError("unsupported ToolReleaseBundle schema_version")
    if set(release) != {
        "schema_version",
        "tool_name",
        "tool_version",
        "tool_commit",
        "binary_or_entry_sha",
        "skill_sha",
        "cli_contract_sha",
        "manifest_sha",
        "source_state",
    }:
        raise SyncError(
            "ToolReleaseBundle release manifest fields do not match schema v1"
        )
    manifest_schema = manifest.get("schema_version")
    if manifest_schema not in {1, TOOL_SKILL_MANIFEST_SCHEMA_VERSION}:
        raise SyncError("unsupported bundled ToolSkillManifest schema_version")
    if contract.get("schema_version") != SCHEMA_VERSION:
        raise SyncError("unsupported bundled CLI contract schema_version")
    validate_manifest_static_fields(manifest, allow_legacy=True)
    validate_install_targets(manifest, allow_legacy=True)
    validate_contract_shape(contract)
    tool = release.get("tool_name")
    if expected_tool is not None and tool != expected_tool:
        raise SyncError("ToolReleaseBundle tool mismatch")
    if manifest.get("tool_name") != tool or contract.get("tool_name") != tool:
        raise SyncError("ToolReleaseBundle identity mismatch")
    if not (
        release.get("tool_version")
        == manifest.get("tool_version")
        == contract.get("tool_version")
    ):
        raise SyncError("ToolReleaseBundle version mismatch")
    if manifest.get("tool_commit") != release.get("tool_commit"):
        raise SyncError("ToolReleaseBundle commit mismatch")
    tool_commit = release.get("tool_commit")
    source_state = release.get("source_state")
    if source_state == "committed_release":
        if not isinstance(tool_commit, str) or not FULL_GIT_OID.fullmatch(tool_commit):
            raise SyncError("committed bundle does not bind a full Git OID")
    elif source_state == "dirty_test_only":
        if (
            not isinstance(tool_commit, str)
            or not tool_commit.startswith("worktree:")
            or not FULL_GIT_OID.fullmatch(tool_commit.removeprefix("worktree:"))
        ):
            raise SyncError("dirty test bundle has invalid worktree identity")
    else:
        raise SyncError("ToolReleaseBundle source_state is invalid")
    hashes = {
        "skill_sha": tree_sha256(bundle, SKILL_PAYLOAD),
        "cli_contract_sha": sha256_file(bundle / "tool_cli_contract.json"),
        "manifest_sha": sha256_file(bundle / "tool_skill_manifest.json"),
    }
    for key, value in hashes.items():
        if release.get(key) != value:
            raise SyncError(f"ToolReleaseBundle {key} mismatch")
    if manifest.get("canonical_skill_sha256") != hashes["skill_sha"]:
        raise SyncError("bundle Skill does not match ToolSkillManifest")
    if manifest_schema == TOOL_SKILL_MANIFEST_SCHEMA_VERSION:
        compatibility_path = bundle / COMPATIBILITY_BUNDLE_PATH
        require_regular(compatibility_path, "bundled compatibility Skill")
        if sha256_file(compatibility_path) != manifest["compatibility_skill"]["sha256"]:
            raise SyncError(
                "bundle compatibility Skill does not match ToolSkillManifest"
            )
    if manifest.get("public_cli_contract_sha256") != hashes["cli_contract_sha"]:
        raise SyncError("bundle CLI contract does not match ToolSkillManifest")
    executable = manifest.get("executable", {})
    if executable.get("kind") == "python_console_scripts":
        identity_path = bundle / "executable_identity.json"
        executable_hash = sha256_file(identity_path)
    elif executable.get("kind") == "source_files":
        file_hashes = {
            f"executable/{relative}": sha256_file(bundle / "executable" / relative)
            for relative in executable.get("paths", [])
        }
        executable_hash = aggregate_hash(file_hashes)
    else:
        raise SyncError("bundle executable kind is unsupported")
    if release.get("binary_or_entry_sha") != executable_hash:
        raise SyncError("ToolReleaseBundle executable identity mismatch")
    return release


def smoke_bundle(bundle: Path) -> None:
    release = verify_bundle(bundle)
    manifest = read_json(bundle / "tool_skill_manifest.json")
    if manifest["executable"]["kind"] == "python_console_scripts":
        identity = read_json(bundle / "executable_identity.json")
        if identity.get("tool_version") != release.get("tool_version"):
            raise SyncError("entrypoint identity smoke failed")
        return
    entry = bundle / "executable" / manifest["executable"]["entrypoints"][0]
    result = run((str(entry), "--version"))
    if result.returncode != 0 or extract_version(
        result.stdout + result.stderr
    ) != release.get("tool_version"):
        raise SyncError("bundled executable --version smoke failed")
    sync_entry = bundle / "executable" / "tool-skill-sync"
    if sync_entry.exists():
        result = run((str(sync_entry), "--version"))
        if result.returncode != 0 or extract_version(
            result.stdout + result.stderr
        ) != bundled_sync_version(bundle):
            raise SyncError("bundled tool-skill-sync --version smoke failed")


def atomic_symlink(directory: Path, name: str, target: Path) -> None:
    temporary = directory / f".{name}.{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    relative = os.path.relpath(target, directory)
    os.symlink(relative, temporary)
    os.replace(temporary, directory / name)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def resolve_pointer(pointer: Path, bundles_root: Path) -> Path | None:
    if not pointer.exists() and not pointer.is_symlink():
        return None
    if not pointer.is_symlink():
        raise SyncError(f"bundle pointer is not a symlink: {pointer}")
    resolved = pointer.resolve(strict=True)
    try:
        resolved.relative_to(bundles_root.resolve())
    except ValueError as exc:
        raise SyncError(f"bundle pointer escapes managed root: {pointer}") from exc
    return resolved


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_owned_regular(path: Path, label: str) -> None:
    require_regular(path, label)
    if path.stat().st_uid != os.getuid():
        raise SyncError(f"{label} is not owned by current user: {path}")


def ensure_owned_subdirectory(root: Path, relative: Path) -> Path:
    require_owned_directory(root, create=True)
    current = root
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise SyncError(f"unsafe production directory component: {relative}")
        current /= part
        require_owned_directory(current, create=True)
    return current


def production_target_sources(
    bundle: Path,
    *,
    agents_root: Path,
    bin_dir: Path | None,
    hooks_dir: Path,
    assert_installed_version: bool = True,
) -> tuple[dict[Path, Path], Path | None]:
    manifest = read_json(bundle / "tool_skill_manifest.json")
    entrypoints = validate_install_targets(manifest, allow_legacy=True)
    require_owned_directory(agents_root)
    skill_parent = ensure_owned_subdirectory(agents_root, Path("skills"))
    manifest_schema = manifest.get("schema_version")
    installed_skill_name = (
        COMPATIBILITY_SKILL_NAME if manifest_schema == 1 else SKILL_NAME
    )
    skill_root = ensure_owned_subdirectory(skill_parent, Path(installed_skill_name))
    targets: dict[Path, Path] = {}
    for skill_relative in SKILL_PAYLOAD:
        target = skill_root / skill_relative
        ensure_owned_subdirectory(skill_root, Path(skill_relative).parent)
        targets[target] = bundle / skill_relative
    if manifest_schema == TOOL_SKILL_MANIFEST_SCHEMA_VERSION:
        compatibility_root = ensure_owned_subdirectory(
            skill_parent, Path(COMPATIBILITY_SKILL_NAME)
        )
        compatibility_target = compatibility_root / "SKILL.md"
        targets[compatibility_target] = bundle / COMPATIBILITY_BUNDLE_PATH

    executable = manifest["executable"]
    if executable["kind"] == "python_console_scripts":
        if assert_installed_version:
            binary = installed_binary_state(entrypoints, bin_dir)
            if binary["version"] != manifest["tool_version"]:
                raise SyncError(
                    f"installed entrypoints do not match {manifest['tool_name']} "
                    f"{manifest['tool_version']}"
                )
        return targets, bin_dir

    if bin_dir is None:
        primary = resolve_entry(entrypoints[0], None)
        if primary is None:
            raise SyncError(f"stable entry is unavailable: {entrypoints[0]}")
        live_bin_dir = primary.parent
    else:
        live_bin_dir = bin_dir.resolve()
    require_owned_directory(live_bin_dir)
    roots = {"bin": live_bin_dir, "hooks": hooks_dir}
    for source_relative, target_relative in executable["production_targets"].items():
        target_path = Path(target_relative)
        root = roots[target_path.parts[0]]
        require_owned_directory(root)
        within_root = Path(*target_path.parts[1:])
        if not within_root.parts:
            raise SyncError("production target must name a file")
        ensure_owned_subdirectory(root, within_root.parent)
        target = root / within_root
        if target in targets:
            raise SyncError(f"duplicate production target: {target}")
        targets[target] = bundle / "executable" / source_relative
    return targets, live_bin_dir


def verify_production_bundle(
    bundle: Path,
    *,
    agents_root: Path,
    bin_dir: Path | None,
    hooks_dir: Path,
) -> dict[str, Any]:
    release = verify_bundle(bundle)
    manifest = read_json(bundle / "tool_skill_manifest.json")
    targets, live_bin_dir = production_target_sources(
        bundle,
        agents_root=agents_root,
        bin_dir=bin_dir,
        hooks_dir=hooks_dir,
    )
    for target, source in targets.items():
        require_owned_regular(target, "active production target")
        if sha256_file(target) != sha256_file(source):
            raise SyncError(f"active production target SHA mismatch: {target}")
    manifest_schema = manifest.get("schema_version")
    installed_skill_name = (
        COMPATIBILITY_SKILL_NAME if manifest_schema == 1 else SKILL_NAME
    )
    if (
        tree_sha256(
            agents_root / "skills" / installed_skill_name,
            SKILL_PAYLOAD,
        )
        != release["skill_sha"]
    ):
        raise SyncError("active Skill SHA does not match ToolReleaseBundle")
    if manifest_schema == TOOL_SKILL_MANIFEST_SCHEMA_VERSION:
        compatibility_path = (
            agents_root / "skills" / COMPATIBILITY_SKILL_NAME / "SKILL.md"
        )
        require_owned_regular(compatibility_path, "active compatibility Skill")
        if sha256_file(compatibility_path) != manifest["compatibility_skill"]["sha256"]:
            raise SyncError("active compatibility Skill SHA does not match bundle")

    entrypoints = validate_install_targets(manifest, allow_legacy=True)
    binary = installed_binary_state(entrypoints, live_bin_dir)
    if binary["version"] != release["tool_version"]:
        raise SyncError("active stable entry version does not match ToolReleaseBundle")
    if manifest["executable"]["kind"] == "source_files":
        sync_entry = next(
            (
                target
                for target, source in targets.items()
                if source.name == "tool-skill-sync"
            ),
            None,
        )
        if sync_entry is not None:
            result = run((str(sync_entry), "--version"))
            if result.returncode != 0 or extract_version(
                result.stdout + result.stderr
            ) != bundled_sync_version(bundle):
                raise SyncError("active tool-skill-sync smoke failed")
    return {
        "binary_version": binary["version"],
        "binary_sha256": binary["sha256"],
        "skill_sha256": release["skill_sha"],
        "cli_contract_sha256": release["cli_contract_sha"],
        "target_sha256": {
            str(target): sha256_file(target) for target in sorted(targets)
        },
    }


def production_pair_matches(
    bundle: Path,
    *,
    agents_root: Path,
    bin_dir: Path | None,
    hooks_dir: Path,
) -> bool:
    targets, _ = production_target_sources(
        bundle,
        agents_root=agents_root,
        bin_dir=bin_dir,
        hooks_dir=hooks_dir,
    )
    for target, source in targets.items():
        if not target.exists() and not target.is_symlink():
            return False
        require_owned_regular(target, "active production target")
        if sha256_file(target) != sha256_file(source):
            return False
    verify_production_bundle(
        bundle,
        agents_root=agents_root,
        bin_dir=bin_dir,
        hooks_dir=hooks_dir,
    )
    return True


def verify_production_backup(backup: Path) -> dict[str, Any]:
    require_owned_directory(backup)
    receipt = read_json(backup / "receipt.json")
    if set(receipt) != {"schema_version", "operation", "tool", "entries"} or (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("operation") != "production_pair_backup"
        or not isinstance(receipt.get("tool"), str)
        or not isinstance(receipt.get("entries"), list)
    ):
        raise SyncError("production backup receipt is invalid")
    seen: set[str] = set()
    for entry in receipt["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "target",
            "backup_file",
            "sha256",
            "mode",
        }:
            raise SyncError("production backup entry is invalid")
        target = entry["target"]
        if (
            not isinstance(target, str)
            or not Path(target).is_absolute()
            or target in seen
        ):
            raise SyncError("production backup target is invalid or duplicated")
        seen.add(target)
        backup_file = entry["backup_file"]
        digest = entry["sha256"]
        mode = entry["mode"]
        if backup_file is None:
            if digest is not None or mode is not None:
                raise SyncError("absent production backup entry has file metadata")
            continue
        if (
            not isinstance(backup_file, str)
            or Path(backup_file).is_absolute()
            or ".." in Path(backup_file).parts
            or not isinstance(digest, str)
            or not HEX_SHA256.fullmatch(digest)
            or not isinstance(mode, int)
        ):
            raise SyncError("production backup file metadata is invalid")
        stored = backup / backup_file
        require_owned_regular(stored, "production backup file")
        if sha256_file(stored) != digest:
            raise SyncError("production backup file SHA mismatch")
    return receipt


def capture_production_pair(
    tool: str,
    targets: Sequence[Path],
    production_root: Path,
) -> Path:
    backups_root = ensure_owned_subdirectory(production_root, Path("backups"))
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=backups_root))
    staging.chmod(0o700)
    try:
        entries: list[dict[str, Any]] = []
        files_root = staging / "files"
        files_root.mkdir(mode=0o700)
        for index, target in enumerate(sorted(targets)):
            if not target.exists() and not target.is_symlink():
                entries.append(
                    {
                        "target": str(target),
                        "backup_file": None,
                        "sha256": None,
                        "mode": None,
                    }
                )
                continue
            require_owned_regular(target, "active production target")
            relative = f"files/{index:03d}"
            stored = staging / relative
            shutil.copyfile(target, stored)
            mode = stat.S_IMODE(target.stat().st_mode)
            stored.chmod(mode)
            entries.append(
                {
                    "target": str(target),
                    "backup_file": relative,
                    "sha256": sha256_file(stored),
                    "mode": mode,
                }
            )
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "operation": "production_pair_backup",
            "tool": tool,
            "entries": entries,
        }
        write_json(staging / "receipt.json", receipt)
        identity = sha256_file(staging / "receipt.json")
        destination = backups_root / identity
        if destination.exists():
            verify_production_backup(destination)
            shutil.rmtree(staging)
            return destination
        os.replace(staging, destination)
        fsync_directory(backups_root)
        verify_production_backup(destination)
        return destination
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def restore_production_pair(backup: Path, expected_targets: Sequence[Path]) -> None:
    receipt = verify_production_backup(backup)
    entries = {Path(entry["target"]): entry for entry in receipt["entries"]}
    if set(entries) != set(expected_targets):
        raise SyncError("production backup target set does not match active pair")
    staged: dict[Path, Path] = {}
    try:
        for target, entry in entries.items():
            if entry["backup_file"] is None:
                continue
            ensure_owned_subdirectory(target.parent, Path("."))
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.restore-"
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            shutil.copyfile(backup / entry["backup_file"], temporary)
            temporary.chmod(entry["mode"])
            staged[target] = temporary
        for target, entry in entries.items():
            if entry["backup_file"] is None:
                if target.exists() or target.is_symlink():
                    require_owned_regular(target, "active production target")
                    target.unlink()
                    fsync_directory(target.parent)
                continue
            os.replace(staged.pop(target), target)
            fsync_directory(target.parent)
    finally:
        for temporary in staged.values():
            if temporary.exists():
                temporary.unlink()


def installed_skill_tree(agents_root: Path, bundle: Path) -> str | None:
    manifest = read_json(bundle / "tool_skill_manifest.json")
    installed_skill_name = (
        COMPATIBILITY_SKILL_NAME
        if manifest.get("schema_version") == 1
        else SKILL_NAME
    )
    skill_root = agents_root / "skills" / installed_skill_name
    present = [
        skill_root / relative
        for relative in SKILL_PAYLOAD
        if (skill_root / relative).exists() or (skill_root / relative).is_symlink()
    ]
    if not present:
        return None
    if len(present) != len(SKILL_PAYLOAD):
        missing = ", ".join(
            relative
            for relative in SKILL_PAYLOAD
            if not (skill_root / relative).exists()
            and not (skill_root / relative).is_symlink()
        )
        raise SyncError(
            "installed production Skill is incomplete; preserve and classify the "
            f"missing payload files before activation: {missing}"
        )
    return tree_sha256(skill_root, SKILL_PAYLOAD)


def validate_production_skill_trusted(
    bundle: Path,
    agents_root: Path,
    *,
    previous_bundle: Path | None,
) -> None:
    live_tree = installed_skill_tree(agents_root, bundle)
    if live_tree is None:
        return
    bundle_tree = verify_bundle(bundle)["skill_sha"]
    if live_tree == bundle_tree:
        return
    if live_tree in RELEASED_CANONICAL_SKILL_SHA256:
        return
    if previous_bundle is not None:
        if verify_bundle(previous_bundle)["skill_sha"] == live_tree:
            return
    manifest = read_json(bundle / "tool_skill_manifest.json")
    installed_skill_name = (
        COMPATIBILITY_SKILL_NAME
        if manifest.get("schema_version") == 1
        else SKILL_NAME
    )
    skill_root = agents_root / "skills" / installed_skill_name
    drifted = ", ".join(
        f"{relative} live={sha256_file(skill_root / relative)}"
        for relative in SKILL_PAYLOAD
        if sha256_file(skill_root / relative) != sha256_file(bundle / relative)
    )
    raise SyncError(
        f"unknown content drift in installed production Skill (live_tree={live_tree} "
        f"bundle_tree={bundle_tree} accepted_released_trees="
        f"{', '.join(sorted(RELEASED_CANONICAL_SKILL_SHA256))}); preserve and "
        "classify these files first, activation refuses to overwrite them: "
        + drifted
    )


def replace_production_pair(targets: dict[Path, Path]) -> None:
    staged: dict[Path, Path] = {}
    try:
        for target, source in targets.items():
            require_owned_regular(source, "bundled production source")
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.install-"
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            shutil.copyfile(source, temporary)
            temporary.chmod(stat.S_IMODE(source.stat().st_mode))
            staged[target] = temporary
        for target in sorted(staged):
            if target.exists() or target.is_symlink():
                require_owned_regular(target, "active production target")
            os.replace(staged.pop(target), target)
            fsync_directory(target.parent)
    finally:
        for temporary in staged.values():
            if temporary.exists():
                temporary.unlink()


def activate_production_bundle(
    bundle: Path,
    tool: str,
    production_root: Path,
    *,
    agents_root: Path,
    bin_dir: Path | None,
    hooks_dir: Path,
    previous_bundle: Path | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    targets, live_bin_dir = production_target_sources(
        bundle,
        agents_root=agents_root,
        bin_dir=bin_dir,
        hooks_dir=hooks_dir,
    )
    validate_production_skill_trusted(
        bundle,
        agents_root,
        previous_bundle=previous_bundle,
    )
    previous_targets = {}
    if previous_bundle is not None:
        # Transition bookkeeping only: the retired target set is a static
        # property of the previous manifest. Installed-version consistency is
        # asserted against the incoming bundle and by the post-activation
        # verification, never against the superseded release.
        previous_targets, _ = production_target_sources(
            previous_bundle,
            agents_root=agents_root,
            bin_dir=live_bin_dir,
            hooks_dir=hooks_dir,
            assert_installed_version=False,
        )
    retired = set(previous_targets) - set(targets)
    for target in retired:
        if not target.exists() and not target.is_symlink():
            continue
        require_owned_regular(target, "retiring production target")
        source = previous_targets[target]
        if (sha256_file(target) != sha256_file(source)
                or stat.S_IMODE(target.stat().st_mode) != stat.S_IMODE(source.stat().st_mode)):
            raise SyncError(f"retiring production target drifted: {target}")
    all_targets = tuple(set(targets) | set(previous_targets))
    if not retired and production_pair_matches(
        bundle,
        agents_root=agents_root,
        bin_dir=live_bin_dir,
        hooks_dir=hooks_dir,
    ):
        return None, verify_production_bundle(
            bundle,
            agents_root=agents_root,
            bin_dir=live_bin_dir,
            hooks_dir=hooks_dir,
        )
    backup = capture_production_pair(tool, all_targets, production_root)
    try:
        replace_production_pair(targets)
        verified = verify_production_bundle(
            bundle,
            agents_root=agents_root,
            bin_dir=live_bin_dir,
            hooks_dir=hooks_dir,
        )
        for target in retired:
            if target.exists():
                target.unlink()
                fsync_directory(target.parent)
        verified["retired_targets"] = sorted(map(str, retired))
    except BaseException:
        restore_production_pair(backup, all_targets)
        raise
    return backup, verified


def resolve_production_backup(pointer: Path, backups_root: Path) -> Path | None:
    return resolve_pointer(pointer, backups_root)


def restore_pointer(directory: Path, name: str, target: Path | None) -> None:
    pointer = directory / name
    if target is None:
        if pointer.exists() or pointer.is_symlink():
            if not pointer.is_symlink():
                raise SyncError(f"managed pointer is not a symlink: {pointer}")
            pointer.unlink()
            fsync_directory(directory)
        return
    atomic_symlink(directory, name, target)


def build_bundle(
    sources: Sources,
    tool: str,
    bundles_root: Path,
    *,
    allow_dirty_source: bool,
) -> Path:
    checked = check_tool(sources, tool)
    if checked["status"] != "PASS":
        raise SyncError("canonical check failed: " + "; ".join(checked["errors"]))
    repo = sources.repo(tool)
    head = git_head(repo)
    dirty = git_dirty(repo) or git_dirty(sources.owner_repo)
    if dirty and not allow_dirty_source:
        raise SyncError(
            "source repositories are dirty; release bundle requires committed sources"
        )
    tool_commit = f"worktree:{head}" if dirty else head
    manifest = read_json(sources.manifest_path(tool))
    manifest["tool_commit"] = tool_commit
    tool_root = bundles_root / tool
    require_owned_directory(tool_root, create=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=tool_root))
    staging.chmod(0o700)
    try:
        copy_skill_payload(sources.skill_root, staging)
        copy_compatibility_skill(sources.compatibility_skill_root, staging)
        shutil.copyfile(
            repo / "tool_cli_contract.json", staging / "tool_cli_contract.json"
        )
        (staging / "tool_cli_contract.json").chmod(0o600)
        write_json(staging / "tool_skill_manifest.json", manifest)
        executable_sha, _ = source_executable_payload(
            repo, manifest, staging, tool_commit
        )
        release = {
            "schema_version": SCHEMA_VERSION,
            "tool_name": tool,
            "tool_version": manifest["tool_version"],
            "tool_commit": tool_commit,
            "binary_or_entry_sha": executable_sha,
            "skill_sha": tree_sha256(staging, SKILL_PAYLOAD),
            "cli_contract_sha": sha256_file(staging / "tool_cli_contract.json"),
            "manifest_sha": sha256_file(staging / "tool_skill_manifest.json"),
            "source_state": "dirty_test_only" if dirty else "committed_release",
        }
        write_json(staging / "release_manifest.json", release)
        verify_bundle(staging, tool)
        smoke_bundle(staging)
        release_sha = sha256_file(staging / "release_manifest.json")
        safe_commit = tool_commit.replace(":", "-")[:21]
        bundle_name = f"{manifest['tool_version']}-{safe_commit}-{release_sha[:12]}"
        destination = tool_root / bundle_name
        if destination.exists():
            verify_bundle(destination, tool)
            shutil.rmtree(staging)
            return destination
        os.replace(staging, destination)
        return destination
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def install(
    sources: Sources,
    tool: str,
    install_root: Path,
    *,
    allow_dirty_source: bool,
    activate_production: bool = False,
    agents_root: Path | None = None,
    bin_dir: Path | None = None,
    hooks_dir: Path | None = None,
) -> dict[str, Any]:
    require_owned_directory(install_root, create=True)
    bundles_root = install_root / "bundles"
    state_root = install_root / "tools" / tool
    require_owned_directory(bundles_root, create=True)
    require_owned_directory(state_root, create=True)
    bundle = build_bundle(
        sources,
        tool,
        bundles_root,
        allow_dirty_source=allow_dirty_source,
    )
    current = resolve_pointer(state_root / "CURRENT", bundles_root)
    if current == bundle and not activate_production:
        return {"status": "UNCHANGED", "tool": tool, "current_bundle": bundle.name}
    previous_before = resolve_pointer(state_root / "PREVIOUS", bundles_root)
    if current is not None:
        verify_bundle(current, tool)
        smoke_bundle(current)

    production_root: Path | None = None
    production_previous: Path | None = None
    production_backup: Path | None = None
    production_verified: dict[str, Any] | None = None
    if activate_production:
        if agents_root is None or hooks_dir is None:
            raise SyncError("production activation requires agents_root and hooks_dir")
        production_root = state_root / "production"
        require_owned_directory(production_root, create=True)
        backups_root = ensure_owned_subdirectory(production_root, Path("backups"))
        production_previous = resolve_production_backup(
            production_root / "PREVIOUS", backups_root
        )
        production_backup, production_verified = activate_production_bundle(
            bundle,
            tool,
            production_root,
            agents_root=agents_root,
            bin_dir=bin_dir,
            hooks_dir=hooks_dir,
            previous_bundle=current,
        )

    try:
        if current != bundle:
            if current is not None:
                atomic_symlink(state_root, "PREVIOUS", current)
            atomic_symlink(state_root, "CURRENT", bundle)
        active = resolve_pointer(state_root / "CURRENT", bundles_root)
        verify_bundle(active or bundle, tool)
        if production_backup is not None and production_root is not None:
            atomic_symlink(production_root, "PREVIOUS", production_backup)
        if activate_production:
            assert agents_root is not None
            assert hooks_dir is not None
            production_verified = verify_production_bundle(
                bundle,
                agents_root=agents_root,
                bin_dir=bin_dir,
                hooks_dir=hooks_dir,
            )
    except BaseException:
        if production_backup is not None:
            # Failure cleanup must never be blocked by pairing assertions;
            # these calls only rebuild the target set for byte-verified
            # restoration from the recorded backup.
            targets, _ = production_target_sources(
                bundle,
                agents_root=agents_root or Path(),
                bin_dir=bin_dir,
                hooks_dir=hooks_dir or Path(),
                assert_installed_version=False,
            )
            if current is not None:
                previous_targets, _ = production_target_sources(
                    current, agents_root=agents_root or Path(), bin_dir=bin_dir,
                    hooks_dir=hooks_dir or Path(),
                    assert_installed_version=False,
                )
                targets.update(previous_targets)
            restore_production_pair(production_backup, tuple(targets))
        restore_pointer(state_root, "CURRENT", current)
        restore_pointer(state_root, "PREVIOUS", previous_before)
        if production_root is not None:
            restore_pointer(production_root, "PREVIOUS", production_previous)
        raise
    return {
        "status": "INSTALLED" if current != bundle else "ACTIVATED",
        "tool": tool,
        "current_bundle": bundle.name,
        "previous_bundle": current.name if current else None,
        "production_activation": activate_production,
        "production_verified": production_verified,
    }


def rollback(
    tool: str,
    install_root: Path,
    *,
    activate_production: bool = False,
    agents_root: Path | None = None,
    bin_dir: Path | None = None,
    hooks_dir: Path | None = None,
) -> dict[str, Any]:
    bundles_root = install_root / "bundles"
    state_root = install_root / "tools" / tool
    require_owned_directory(bundles_root)
    require_owned_directory(state_root)
    current = resolve_pointer(state_root / "CURRENT", bundles_root)
    previous = resolve_pointer(state_root / "PREVIOUS", bundles_root)
    if current is None or previous is None:
        raise SyncError(f"rollback requires CURRENT and PREVIOUS for {tool}")
    verify_bundle(current, tool)
    verify_bundle(previous, tool)
    smoke_bundle(previous)
    production_root: Path | None = None
    production_previous: Path | None = None
    current_production_backup: Path | None = None
    production_verified: dict[str, Any] | None = None
    current_targets: dict[Path, Path] = {}
    if activate_production:
        if agents_root is None or hooks_dir is None:
            raise SyncError("production rollback requires agents_root and hooks_dir")
        production_root = state_root / "production"
        require_owned_directory(production_root)
        backups_root = ensure_owned_subdirectory(production_root, Path("backups"))
        production_previous = resolve_production_backup(
            production_root / "PREVIOUS", backups_root
        )
        current_targets, _ = production_target_sources(
            current,
            agents_root=agents_root,
            bin_dir=bin_dir,
            hooks_dir=hooks_dir,
            assert_installed_version=False,
        )
        previous_targets, _ = production_target_sources(
            previous,
            agents_root=agents_root,
            bin_dir=bin_dir,
            hooks_dir=hooks_dir,
            assert_installed_version=False,
        )
        retired = set(current_targets) - set(previous_targets)
        all_targets = tuple(set(current_targets) | set(previous_targets))
        if not any(p.exists() or p.is_symlink() for p in retired) and production_pair_matches(
            previous,
            agents_root=agents_root,
            bin_dir=bin_dir,
            hooks_dir=hooks_dir,
        ):
            production_verified = verify_production_bundle(
                previous,
                agents_root=agents_root,
                bin_dir=bin_dir,
                hooks_dir=hooks_dir,
            )
        else:
            if production_previous is None:
                raise SyncError(f"production rollback has no previous pair for {tool}")
            verify_production_bundle(
                current, agents_root=agents_root, bin_dir=bin_dir, hooks_dir=hooks_dir
            )
            receipt = verify_production_backup(production_previous)
            if {Path(entry["target"]) for entry in receipt["entries"]} != set(all_targets):
                raise SyncError("production rollback backup does not cover both target sets")
            current_production_backup = capture_production_pair(
                tool, all_targets, production_root
            )
            try:
                restore_production_pair(production_previous, all_targets)
                production_verified = verify_production_bundle(
                    previous,
                    agents_root=agents_root,
                    bin_dir=bin_dir,
                    hooks_dir=hooks_dir,
                )
            except BaseException:
                restore_production_pair(
                    current_production_backup, all_targets
                )
                raise
    try:
        atomic_symlink(state_root, "PREVIOUS", current)
        atomic_symlink(state_root, "CURRENT", previous)
        active = resolve_pointer(state_root / "CURRENT", bundles_root)
        verify_bundle(active or previous, tool)
        if production_root is not None and current_production_backup is not None:
            atomic_symlink(production_root, "PREVIOUS", current_production_backup)
    except BaseException:
        restore_pointer(state_root, "CURRENT", current)
        restore_pointer(state_root, "PREVIOUS", previous)
        if current_production_backup is not None:
            restore_production_pair(current_production_backup, all_targets)
        if production_root is not None:
            restore_pointer(production_root, "PREVIOUS", production_previous)
        raise
    return {
        "status": "ROLLED_BACK",
        "tool": tool,
        "current_bundle": previous.name,
        "previous_bundle": current.name,
        "production_activation": activate_production,
        "production_verified": production_verified,
    }


def parser() -> argparse.ArgumentParser:
    owner_repo = Path(__file__).resolve().parent.parent
    result = argparse.ArgumentParser(prog="tool-skill-sync")
    result.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    result.add_argument("--source-root", type=Path, default=owner_repo.parent)
    result.add_argument(
        "--source-repo", action="append", default=[], metavar="TOOL=PATH",
        help="explicit source checkout; repeat for independently allocated worktrees",
    )
    result.add_argument(
        "--install-root",
        type=Path,
        default=Path.home() / ".local" / "state" / "tool-skill-sync",
    )
    result.add_argument("--agents-root", type=Path, default=Path.home() / ".agents")
    result.add_argument("--bin-dir", type=Path)
    result.add_argument(
        "--hooks-dir", type=Path, default=Path.home() / ".agents" / "hooks"
    )
    subcommands = result.add_subparsers(dest="command", required=True)
    subcommands.add_parser("status")
    check_parser = subcommands.add_parser("check")
    check_parser.add_argument("tool")
    check_parser.add_argument(
        "--source-only",
        action="store_true",
        help=(
            "validate only the canonical source contract and skip installed-production "
            "drift detection (for build environments with no installation)"
        ),
    )
    install_parser = subcommands.add_parser("install")
    install_parser.add_argument("tool")
    install_parser.add_argument("--allow-dirty-source", action="store_true")
    install_parser.add_argument("--activate-production", action="store_true")
    upgrade_parser = subcommands.add_parser("upgrade")
    upgrade_parser.add_argument("tool")
    upgrade_parser.add_argument("--dry-run", action="store_true")
    upgrade_parser.add_argument(
        "--previous-bundle", type=Path,
        help="verified prior release identity when all console-script names changed",
    )
    rollback_parser = subcommands.add_parser("rollback")
    rollback_parser.add_argument("tool")
    rollback_parser.add_argument("--activate-production", action="store_true")
    return result


def main(arguments: Sequence[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    try:
        source_repos = {}
        for value in args.source_repo:
            name, separator, path = value.partition("=")
            if not separator or not SAFE_NAME.fullmatch(name) or not Path(path).is_absolute():
                raise SyncError("source-repo requires TOOL=/absolute/path")
            if name in source_repos:
                raise SyncError("source-repo contains a duplicate tool")
            source_repos[name] = Path(path)
        sources = Sources(args.source_root, source_repos)
        if args.command == "status":
            output = status(sources, args.agents_root.resolve(), args.bin_dir)
        else:
            if args.tool not in sources.names():
                raise SyncError(f"unknown tool: {args.tool}")
            if args.command == "check":
                output = (
                    check_tool(sources, args.tool)
                    if args.source_only
                    else check_tool_deployment(
                        sources,
                        args.tool,
                        args.agents_root.resolve(),
                        args.bin_dir,
                    )
                )
                print(canonical_json(output).decode(), end="")
                return 0 if output["status"] == "PASS" else 1
            if args.command == "upgrade":
                output = upgrade(
                    sources,
                    args.tool,
                    bin_dir=args.bin_dir,
                    dry_run=args.dry_run,
                    previous_bundle=args.previous_bundle,
                )
                print(canonical_json(output).decode(), end="")
                return 0
            if args.command == "install":
                output = install(
                    sources,
                    args.tool,
                    args.install_root.resolve(),
                    allow_dirty_source=args.allow_dirty_source,
                    activate_production=args.activate_production,
                    agents_root=args.agents_root.resolve(),
                    bin_dir=args.bin_dir.resolve() if args.bin_dir else None,
                    hooks_dir=args.hooks_dir.resolve(),
                )
            else:
                output = rollback(
                    args.tool,
                    args.install_root.resolve(),
                    activate_production=args.activate_production,
                    agents_root=args.agents_root.resolve(),
                    bin_dir=args.bin_dir.resolve() if args.bin_dir else None,
                    hooks_dir=args.hooks_dir.resolve(),
                )
        print(canonical_json(output).decode(), end="")
        return 0
    except (OSError, SyncError, subprocess.SubprocessError) as exc:
        print(f"tool-skill-sync: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
