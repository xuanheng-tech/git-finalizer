"""Read-only deployed workflow and instruction diagnostics; never activates a tool."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
from typing import Any

# GF-INTEGRATION-ADAPTER: workflow-deployment-health (single site; read-only installed runtime probes)
try:
    from . import tool_skill_sync as sync
except ImportError:
    import tool_skill_sync as sync


def readonly_git(repo: Path, *arguments: str) -> str:
    result = sync.run(["git", "-c", "core.fsmonitor=false", "-C", str(repo), *arguments],
                      env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    if result.returncode:
        raise sync.SyncError(f"read-only Git observation failed: {repo}")
    return result.stdout.strip()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(sync.canonical_json(value)).hexdigest()


def file_identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise sync.SyncError(f"instruction is not a regular file: {path}")
    content = resolved.read_bytes()
    content.decode("utf-8")
    return {"path": str(path), "real_path": str(resolved),
            "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def installed_tool(
    sources: sync.Sources, tool: str, agents_root: Path, bin_dir: Path | None,
    hooks_dir: Path,
) -> dict[str, Any]:
    return sync.check_tool_deployment(sources, tool, agents_root, bin_dir, hooks_dir)


def controller_state(
    requirements: dict[str, Any], entrypoint: Path | None,
    runtime_root: Path, source_repo: Path, agents_root: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "tool": "worktree-controller", "status": "FAIL", "errors": [],
        "source_repo": str(source_repo), "source_is_candidate": True,
    }
    try:
        sync.validate_controller_requirements(requirements)
        entry = entrypoint or (Path(shutil.which("worktree-controller"))
                               if shutil.which("worktree-controller") else None)
        if entry is None:
            raise sync.SyncError("worktree-controller is not installed")
        active = runtime_root / "active"
        runtime = active.resolve(strict=True)
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", GIT_OPTIONAL_LOCKS="0")
        command = sync.run([str(entry), "capabilities", "--json"], env=environment)
        if command.returncode != 0:
            raise sync.SyncError("installed Controller capabilities probe failed")
        envelope = json.loads(command.stdout)
        if (not isinstance(envelope, dict) or envelope.get("errors")
                or not isinstance(envelope.get("decision"), dict)):
            raise sync.SyncError("Controller returned invalid capability evidence")
        caps = envelope["decision"]
        result.update(entrypoint=str(entry), software_version=envelope.get("tool_version"),
                      cli_contract_version=caps.get("contract_version"),
                      capabilities_version=caps.get("capabilities_version"),
                      storage_layout_version=caps.get("storage_layout_version"))
        for key, allowed_key in [("contract_version", "cli_contract_versions"),
                                 ("storage_layout_version", "storage_layout_versions")]:
            if type(caps.get(key)) is not int or caps.get(key) not in requirements[allowed_key]:
                result["errors"].append(f"unreviewed {key}: {caps.get(key)}")
        for name, expected in requirements.get("capability_versions", {}).items():
            if type(caps.get(name)) is not int or caps.get(name) != expected:
                result["errors"].append(f"{name}: expected {expected}, observed {caps.get(name)}")
        result["capability_versions"] = {
            name: caps.get(name) for name in requirements.get("capability_versions", {})
        }
        result["active_runtime"] = str(runtime)
        contracts = list(runtime.glob("lib/python*/site-packages/worktree_controller/tool_cli_contract.json"))
        if len(contracts) != 1:
            raise sync.SyncError("active Controller frozen contract is missing/ambiguous")
        contract = json.loads(contracts[0].read_text(encoding="utf-8"))
        for name in ("contract_version", "capabilities_version", "storage_layout_version",
                     *requirements.get("capability_versions", {})):
            if caps.get(name) != contract.get(name):
                result["errors"].append(f"runtime/frozen contract disagree: {name}")
        if envelope.get("tool_version") != contract.get("tool_version"):
            result["errors"].append("software version differs from frozen runtime contract")
        result["frozen_contract_sha256"] = sync.sha256_file(contracts[0])
        result["artifact_verification"] = sync.verify_installed_record(runtime)
        # The active pinned commit is independent of the currently edited source checkout.
        commit = runtime.parent.name
        if not sync.FULL_GIT_OID.fullmatch(commit):
            raise sync.SyncError("active runtime does not identify a full pinned commit")
        result["active_commit"] = commit
        observed = sync.run(["git", "-C", str(source_repo), "show", f"{commit}:tool_cli_contract.json"])
        if observed.returncode != 0 or hashlib.sha256(observed.stdout.encode()).hexdigest() != result["frozen_contract_sha256"]:
            result["errors"].append("frozen contract does not match active commit")
        result["source_head"] = sync.git_head(source_repo)
        result["source_dirty"] = sync.git_dirty(source_repo)
        skill = agents_root / "skills/worktree-lifecycle/SKILL.md"
        result["installed_skill"] = file_identity(skill)
        published_skill = sync.run([
            "git", "-C", str(source_repo), "show", f"{commit}:skills/worktree-lifecycle/SKILL.md"
        ])
        if (published_skill.returncode != 0 or hashlib.sha256(published_skill.stdout.encode()).hexdigest()
                != result["installed_skill"]["sha256"]):
            result["errors"].append("installed worktree-lifecycle Skill differs from active commit; coordinate with WC owner")
        if active.resolve(strict=True) != runtime:
            result["errors"].append("Controller active runtime changed during observation; retry read-only check")
        result["status"] = "FAIL" if result["errors"] else "PASS"
    except (OSError, ValueError, KeyError, TypeError, sync.SyncError, subprocess.SubprocessError) as exc:
        result["errors"].append(str(exc))
    return result


def chosen_instruction(directory: Path, fallbacks: list[str]) -> Path | None:
    for name in ("AGENTS.override.md", "AGENTS.md", *fallbacks):
        if Path(name).name != name:
            raise sync.SyncError("instruction fallback must be a filename")
        candidate = directory / name
        if candidate.is_file() and candidate.stat().st_size:
            return candidate
    return None


def instruction_state(
    repo: Path | None, agents_root: Path, codex_home: Path, claude_home: Path | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "files": [], "warnings": [], "errors": [], "pending_checkouts": [],
        "session_reload": "reread changed files; the checker cannot verify model context",
    }
    try:
        config_path = codex_home / "config.toml"
        config = tomllib.loads(config_path.read_text()) if config_path.is_file() else {}
        limit = config.get("project_doc_max_bytes", 32768)
        fallbacks = config.get("project_doc_fallback_filenames", [])
        if (type(limit) is not int or limit <= 0 or not isinstance(fallbacks, list)
                or any(not isinstance(name, str) or not name for name in fallbacks)):
            raise sync.SyncError("invalid instruction discovery configuration")
        result["configured_limit_bytes"] = limit
        paths: list[Path] = []
        global_path = chosen_instruction(codex_home, [])
        if global_path is not None:
            paths.append(global_path)
        if repo is not None:
            cwd = repo.resolve(strict=True)
            top = Path(readonly_git(cwd, "rev-parse", "--show-toplevel"))
            if not cwd.is_relative_to(top):
                raise sync.SyncError("requested directory is outside resolved checkout")
            for directory in [top, *[top.joinpath(*cwd.relative_to(top).parts[:n])
                                      for n in range(1, len(cwd.relative_to(top).parts) + 1)]]:
                candidate = chosen_instruction(directory, fallbacks)
                if candidate is not None:
                    paths.append(candidate)
            result["checkout"] = str(top)
            result["checkout_head"] = sync.git_head(top)
            raw = readonly_git(top, "worktree", "list", "--porcelain")
            checkouts = [Path(line[9:]) for line in raw.splitlines() if line.startswith("worktree ")]
            if checkouts:
                result["canonical"] = str(checkouts[0])
            reference_names = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md",
                               "docs/engineering/development-workflow.md")
            instruction_names = ["AGENTS.override.md", "AGENTS.md", *fallbacks, *reference_names]
            for checkout in checkouts[:64]:
                if not checkout.is_dir():
                    continue
                modified = readonly_git(checkout, "status", "--porcelain=v1", "--untracked-files=all",
                                    "--", *instruction_names)
                if modified:
                    result["pending_checkouts"].append({"checkout": str(checkout), "changes": modified.splitlines()})
            if len(checkouts) > 64:
                result["warnings"].append("instruction checkout inventory truncated at 64")
            if result["pending_checkouts"]:
                result["warnings"].append("uncommitted instruction changes are not integrated main")
            if checkouts and top != checkouts[0]:
                # Live governance must invalidate the fingerprint even when the branch copy stays old.
                canonical_paths = [checkouts[0] / name for name in reference_names]
                canonical_agent = chosen_instruction(checkouts[0], fallbacks)
                if canonical_agent is not None:
                    canonical_paths.insert(0, canonical_agent)
                result["canonical_references"] = [
                    file_identity(path) for path in canonical_paths if path.is_file()
                ]
                local_agent = chosen_instruction(top, fallbacks)
                if canonical_agent and local_agent and sync.sha256_file(canonical_agent) != sync.sha256_file(local_agent):
                    result["warnings"].append("checkout AGENTS differs from canonical; review live governance without overwriting branch rules")
            for name in reference_names:
                candidate = top / name
                if candidate.is_file():
                    result.setdefault("references", []).append(file_identity(candidate))
        result["files"] = [file_identity(path) for path in paths]
        result["total_bytes"] = sum(item["bytes"] for item in result["files"])
        if result["total_bytes"] > limit:
            result["errors"].append("instruction chain exceeds configured discovery byte limit")
        result["skills"] = [
            file_identity(path) for path in sorted((agents_root / "skills").glob("*/SKILL.md"))
            if path.is_file()
        ]
        claude_home = claude_home if claude_home is not None else codex_home.parent / ".claude"
        claude_skills = claude_home / "skills"
        if claude_skills.is_symlink() and not claude_skills.exists():
            result["warnings"].append("Claude personal skills link is broken")
        elif claude_skills.exists():
            # Claude writes its account-synced skills under skills/synced; a whole-root link
            # would publish them to every client reading the shared root.
            shared_skills = agents_root / "skills"
            if claude_skills.resolve() == shared_skills.resolve():
                if (shared_skills / "synced").exists():
                    result["warnings"].append("Claude-synced skills leak into the shared personal skills root")
            else:
                for path in sorted(shared_skills.glob("*/SKILL.md")):
                    exposed = claude_skills / path.parent.name / "SKILL.md"
                    if not exposed.is_file() or exposed.resolve() != path.resolve():
                        result["warnings"].append(f"Claude skills do not expose shared skill {path.parent.name}")
        bridge = claude_home / "CLAUDE.md"
        if bridge.is_file():
            result.setdefault("references", []).append(file_identity(bridge))
    except (OSError, ValueError, TypeError, sync.SyncError, subprocess.SubprocessError) as exc:
        result["errors"].append(str(exc))
    result["status"] = "FAIL" if result["errors"] else "PASS"
    return result


def doctor(
    sources: sync.Sources, *, agents_root: Path, bin_dir: Path | None,
    hooks_dir: Path, repo: Path | None, codex_home: Path,
    controller: Path | None, controller_runtime_root: Path, controller_source: Path,
    previous_fingerprint: str | None = None, claude_home: Path | None = None,
) -> dict[str, Any]:
    results = []
    for name in sources.names():
        try:
            results.append(installed_tool(sources, name, agents_root, bin_dir, hooks_dir))
        except (OSError, ValueError, sync.SyncError, subprocess.SubprocessError) as exc:
            results.append({"tool": name, "status": "FAIL", "errors": [str(exc)]})
    runtime_requirements = sources.compatibility.get("runtime_requirements", {})
    requirements = (runtime_requirements.get("worktree-controller")
                    if isinstance(runtime_requirements, dict) else None)
    results.append(controller_state(requirements, controller, controller_runtime_root, controller_source, agents_root))
    instructions = instruction_state(repo, agents_root, codex_home, claude_home)
    compatibility_sha = fingerprint(sources.compatibility)
    identity = {
        "compatibility_sha256": compatibility_sha,
        "tools": [{k: v for k, v in item.items() if k not in {"artifact_verification"}}
                  for item in results],
        "instructions": instructions,
    }
    digest = fingerprint(identity)
    return {
        "schema_version": 1, "operation": "doctor",
        "status": "PASS" if all(item["status"] == "PASS" for item in [*results, instructions]) else "FAIL",
        "fingerprint": digest,
        "compatibility_sha256": compatibility_sha,
        "toolchain_contract_version": sources.compatibility.get("toolchain_contract_version"),
        "refresh_required": previous_fingerprint != digest,
        "tools": results, "instructions": instructions,
        "mutation_performed": False,
    }


def summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        **{k: result[k] for k in ("schema_version", "operation", "status", "fingerprint",
                                  "toolchain_contract_version", "compatibility_sha256",
                                  "refresh_required", "mutation_performed")},
        "tools": [{k: item.get(k) for k in ("tool", "status", "canonical_tool_version",
                    "installed_binary_version", "software_version", "active_commit",
                    "cli_contract_version", "storage_layout_version", "errors")}
                  for item in result["tools"]],
        "instructions": {k: result["instructions"].get(k) for k in (
            "status", "total_bytes", "configured_limit_bytes", "warnings", "errors",
            "pending_checkouts", "session_reload")},
    }
