#!/usr/bin/env python3
"""Release preparation and verification for Git Finalizer.

Single implementation shared by every release host, so a gate cannot exist on one host and be
missing on another. Subcommands:

  preflight <tag>       verify the version batch and the hash-bound files
  build <tag>           stage the package, build it deterministically, verify it, write checksums
  verify <tag>          verify a built artifact against the checksums `build` recorded
  selfcheck <tag>       build twice entirely inside a temporary directory and report the digest
  publication-status <tag>  observe one explicit GitHub remote; never create or replace a release

The build recipe is fixed: ustar, sorted members, owner/group 0, numeric owner ids, mtime pinned to
the commit timestamp, and gzip without a name or timestamp. Two builds of one commit must be
byte-identical, and the file set must equal the installation contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tooling import tool_skill_sync as sync  # noqa: E402  (path setup above)

COMPANIONS = (
    "git-finalize-integration-publish.py",
    "git-finalize-repo-bootstrap.py",
    "git-finalize-retirement-plan.py",
)
METADATA_FILES = (
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "tool_cli_contract.json",
    "tool_skill_manifest.json",
    "toolchain_compatibility.json",
)
VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+"


class ReleaseError(Exception):
    """A release precondition is not met; nothing was published."""


def package_name(version: str) -> str:
    if re.fullmatch(VERSION_PATTERN, version) is None:
        raise ReleaseError(f"invalid release version: {version}")
    return f"git-finalizer-{version}"


def tag_version(version_tag: str) -> str:
    if re.fullmatch(rf"v({VERSION_PATTERN})", version_tag) is None:
        raise ReleaseError(f"invalid release tag: {version_tag}")
    return version_tag[1:]


def package_paths() -> set[str]:
    manifest = sync.read_json(ROOT / "tool_skill_manifest.json")
    paths = set(manifest["executable"]["paths"])
    paths.update(METADATA_FILES)
    paths.update(f"skills/{sync.SKILL_NAME}/{p}" for p in sync.SKILL_PAYLOAD)
    paths.add(manifest["compatibility_skill"]["path"])
    paths.update(
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "manifests").glob("*/tool_skill_manifest.json"))
    )
    return paths


def read_version_literal(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise ReleaseError(f"cannot read a version literal from {path}")
    return match.group(1)


def preflight(version_tag: str) -> str:
    version = tag_version(version_tag)
    script_version = read_version_literal(
        ROOT / "git-finalize", r'^readonly VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$'
    )
    readme_version = read_version_literal(
        ROOT / "README.md", r"^当前版本：`([0-9]+\.[0-9]+\.[0-9]+)`$"
    )
    if script_version != readme_version:
        raise ReleaseError("Finalizer version declarations disagree")
    if version_tag != f"v{script_version}":
        raise ReleaseError(f"tag {version_tag} does not match Finalizer version {script_version}")
    for companion in COMPANIONS:
        companion_version = read_version_literal(
            ROOT / companion, r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$'
        )
        if companion_version != version:
            raise ReleaseError(f"companion declares a different version: {companion}")
    contract = sync.read_json(ROOT / "tool_cli_contract.json")
    manifest = sync.read_json(ROOT / "tool_skill_manifest.json")
    compatibility = sync.read_json(ROOT / "toolchain_compatibility.json")
    if contract["tool_version"] != version:
        raise ReleaseError("CLI contract declares a different version")
    if manifest["tool_version"] != version:
        raise ReleaseError("root manifest declares a different version")
    if contract["contract_version"] != compatibility["git_finalizer_contract_version"]:
        raise ReleaseError("CLI contract version disagrees with toolchain compatibility")
    contract_digest = sync.sha256_file(ROOT / "tool_cli_contract.json")
    if manifest["public_cli_contract_sha256"] != contract_digest:
        raise ReleaseError("root manifest does not bind the shipped CLI contract bytes")
    payload_digest = sync.tree_sha256(ROOT / "skills" / sync.SKILL_NAME, sync.SKILL_PAYLOAD)
    manifests = [ROOT / "tool_skill_manifest.json"]
    manifests.extend(sorted((ROOT / "manifests").glob("*/tool_skill_manifest.json")))
    for path in manifests:
        bound = sync.read_json(path)["canonical_skill_sha256"]
        if bound != payload_digest:
            raise ReleaseError(
                f"{path.relative_to(ROOT)} does not bind the shipped Skill payload {payload_digest}"
            )
    if not (ROOT / "LICENSE").is_file():
        raise ReleaseError("LICENSE is missing from the released package surface")
    return version


def stage(stage_root: Path) -> None:
    stage_root.mkdir(parents=True, exist_ok=True)
    for relative in sorted(package_paths()):
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
            raise ReleaseError(f"packaged file has an unsafe path: {relative}")
        source = ROOT / relative
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(ROOT):
            raise ReleaseError(f"packaged file is missing: {relative}")
        target = stage_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(stat.S_IMODE(source.stat().st_mode) & 0o755)
    # Directory modes must not depend on the builder's umask, or two hosts produce
    # different bytes from the same commit.
    for directory in sorted(stage_root.rglob("*"), key=lambda p: str(p)):
        if directory.is_dir():
            directory.chmod(0o755)
    stage_root.chmod(0o755)


def commit_timestamp() -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "show", "-s", "--format=%ct", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ReleaseError("cannot read the commit timestamp for reproducible mtime")
    return result.stdout.strip()


def tar_bytes(stage_root: Path, package: str, timestamp: str) -> bytes:
    command = [
        "tar",
        "--sort=name",
        "--format=ustar",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        f"--mtime=@{timestamp}",
        "-C",
        str(stage_root),
        "-cf",
        "-",
        package,
    ]
    archive = subprocess.run(command, capture_output=True, check=True).stdout
    gzip = subprocess.run(
        ["gzip", "-n"], input=archive, capture_output=True, check=True
    ).stdout
    return gzip


def build(version_tag: str, dist: Path | None = None, staging: Path | None = None) -> Path:
    version = preflight(version_tag)
    package = package_name(version)
    dist_root = dist or ROOT / "dist"
    expected_outputs = {f"{package}.tar.gz", "SHA256SUMS.txt"}
    if dist_root.is_symlink() or (dist_root.exists() and not dist_root.is_dir()):
        raise ReleaseError(f"release output directory is unsafe: {dist_root}")
    if dist_root.exists():
        outputs = list(dist_root.iterdir())
        if any(path.name not in expected_outputs or path.is_symlink() or not path.is_file()
               for path in outputs):
            raise ReleaseError("release output contains unknown files; preserve them before building")
        if outputs:
            # Establish that an overwritten artifact is a verified generated result.
            verify(version_tag, dist_root)
    if staging is not None and (staging.exists() or staging.is_symlink()):
        raise ReleaseError("release staging directory already exists; refusing to discard it")
    dist_root.mkdir(parents=True, exist_ok=True)
    artifact = dist_root / f"{package}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="git-finalizer-build-") as temporary:
        stage_root = staging or Path(temporary) / "stage"
        stage(stage_root / package)
        timestamp = commit_timestamp()
        built_bytes = tar_bytes(stage_root, package, timestamp)
        if built_bytes != tar_bytes(stage_root, package, timestamp):
            raise ReleaseError("release build is not deterministic for one commit")
        candidate = Path(temporary) / artifact.name
        candidate.write_bytes(built_bytes)
        verify_bytes(candidate, version_tag)
        digest = hashlib.sha256(built_bytes).hexdigest()
        artifact.write_bytes(built_bytes)
        (dist_root / "SHA256SUMS.txt").write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")
    return artifact


def verify_bytes(artifact: Path, version_tag: str) -> None:
    version = tag_version(version_tag)
    package = package_name(version)
    expected = {f"{package}/{p}" for p in package_paths()}
    try:
        archive = tarfile.open(artifact, "r:gz")
    except tarfile.TarError as error:
        raise ReleaseError(f"release artifact is not a readable gzip tarball: {error}") from error
    with archive:
        members = archive.getmembers()
        names = [member.name.rstrip("/") for member in members]
        if len(names) != len(set(names)):
            raise ReleaseError("release package contains duplicate members")
        files = {member.name for member in members if member.isfile()}
        if files != expected:
            missing = sorted(expected - files)
            extra = sorted(files - expected)
            raise ReleaseError(
                "release package file set does not match the installation contract "
                f"(missing={missing} extra={extra})"
            )
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or path.as_posix() != member.name.rstrip("/"):
                raise ReleaseError(f"release package contains an unsafe path: {member.name}")
            if not member.isfile() and not member.isdir():
                raise ReleaseError(f"release package contains a link or special member: {member.name}")
        expected_directories = {
            str(parent) for name in expected for parent in PurePosixPath(name).parents
            if str(parent) != "."
        }
        directories = {member.name.rstrip("/") for member in members if member.isdir()}
        if directories != expected_directories:
            raise ReleaseError("release package directory file set does not match the installation contract")
        entry = archive.getmember(f"{package}/git-finalize")
        if stat.S_IMODE(entry.mode) != 0o755:
            raise ReleaseError("Finalizer entry point is not executable in the release package")
        for member in members:
            if member.isdir() and stat.S_IMODE(member.mode) != 0o755:
                raise ReleaseError(
                    "release package directory mode depends on the builder umask: "
                    f"{member.name} mode={stat.S_IMODE(member.mode):#o}"
                )
        stream = archive.extractfile(entry)
        if stream is None:
            raise ReleaseError("Finalizer entry point is unreadable in the release package")
        match = re.search(
            rb'^readonly VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$', stream.read(), re.MULTILINE
        )
        if match is None or match.group(1).decode() != version:
            raise ReleaseError("release package contains the wrong Finalizer version")


def verify(version_tag: str, dist: Path | None = None) -> str:
    version = tag_version(version_tag)
    package = package_name(version)
    dist_root = dist or ROOT / "dist"
    artifact = dist_root / f"{package}.tar.gz"
    if artifact.is_symlink() or not artifact.is_file():
        raise ReleaseError(f"release artifact is missing: {artifact}")
    verify_bytes(artifact, version_tag)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    checksums = dist_root / "SHA256SUMS.txt"
    if checksums.is_symlink() or not checksums.is_file():
        raise ReleaseError(
            f"{checksums.name} is missing next to the artifact; only `build` may author a "
            "checksum file, otherwise verification would grade its own homework"
        )
    recorded = checksums.read_text(encoding="utf-8")
    if recorded != f"{digest}  {artifact.name}\n":
        raise ReleaseError("SHA256SUMS.txt does not match the built artifact bytes")
    return digest


def selfcheck(version_tag: str) -> str:
    """Build twice into scratch space and report the digest; writes nothing into the repository."""
    with tempfile.TemporaryDirectory(prefix="git-finalizer-selfcheck-") as temporary:
        scratch = Path(temporary)
        first = build(version_tag, dist=scratch / "one", staging=scratch / "stage-one")
        second = build(version_tag, dist=scratch / "two", staging=scratch / "stage-two")
        digest_one = hashlib.sha256(first.read_bytes()).hexdigest()
        digest_two = hashlib.sha256(second.read_bytes()).hexdigest()
        if digest_one != digest_two:
            raise ReleaseError("two selfcheck builds differ")
    return digest_one


def inspection_command(command: list[str], *, timeout: int = 30) -> str:
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    try:
        result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                                text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError("publication inspection command is unavailable or timed out") from None
    if result.returncode != 0:
        # Git/gh diagnostics can contain credential-bearing endpoints or account data.
        raise ReleaseError("cannot observe publication state; inspect the existing remote before retrying")
    return result.stdout.strip()


def github_repository(remote: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote) is None:
        raise ReleaseError("an explicit, valid Git remote name is required")
    fetch = inspection_command(["git", "remote", "get-url", "--all", remote]).splitlines()
    push = inspection_command(["git", "remote", "get-url", "--push", "--all", remote]).splitlines()
    if len(fetch) != 1 or fetch != push:
        raise ReleaseError("release inspection requires one identical fetch/push endpoint")
    endpoint = fetch[0]
    if endpoint.startswith("git@github.com:"):
        path = endpoint.removeprefix("git@github.com:")
    else:
        parsed = urlparse(endpoint)
        if parsed.hostname != "github.com" or parsed.query or parsed.fragment:
            raise ReleaseError("publication-status supports an explicit github.com remote")
        if (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443)):
            path = parsed.path.lstrip("/")
        elif (parsed.scheme == "ssh" and parsed.username == "git" and not parsed.password
              and parsed.port in (None, 22)):
            path = parsed.path.lstrip("/")
        else:
            raise ReleaseError("unsupported or credential-bearing release endpoint")
    repository = path.removesuffix(".git")
    if (re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository) is None
            or repository.endswith(("/.", "/.."))):
        raise ReleaseError("release endpoint does not name exactly one GitHub repository")
    return repository


def github_response(repository: str, suffix: str = "") -> tuple[int, dict]:
    try:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "--include", f"repos/{repository}{suffix}"],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError("GitHub observation is unavailable; use the existing authorized gh client") from None
    header, separator, body = result.stdout.replace("\r\n", "\n").partition("\n\n")
    match = re.match(r"HTTP/\S+ ([0-9]{3})\b", header)
    if not separator or match is None:
        raise ReleaseError("GitHub returned no verifiable HTTP status; publication state is unknown")
    status = int(match.group(1))
    if status not in (200, 404) or (status == 200 and result.returncode != 0):
        raise ReleaseError("GitHub observation failed; authentication or transport failure is not absence")
    try:
        value = json.loads(body)
    except (ValueError, TypeError):
        raise ReleaseError("GitHub returned invalid publication metadata") from None
    if not isinstance(value, dict):
        raise ReleaseError("GitHub returned invalid publication metadata")
    return status, value


def remote_tag(remote: str, version_tag: str) -> tuple[str | None, str | None]:
    reference = f"refs/tags/{version_tag}"
    text = inspection_command(["git", "ls-remote", remote, reference, reference + "^{}"])
    refs = {}
    for line in text.splitlines():
        fields = line.split()
        if (len(fields) != 2
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0]) is None
                or fields[1] not in (reference, reference + "^{}") or fields[1] in refs):
            raise ReleaseError("remote returned ambiguous release tag metadata")
        refs[fields[1]] = fields[0]
    if reference + "^{}" in refs and reference not in refs:
        raise ReleaseError("remote returned a peeled tag without its tag object")
    return refs.get(reference), refs.get(reference + "^{}")


def visible_release_after_not_found(repository: str, repository_data: dict,
                                   version_tag: str) -> dict | None:
    permissions = repository_data.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("push") is not True:
        raise ReleaseError("release absence cannot be established without draft visibility; use an authorized maintainer gh client")
    # The by-tag endpoint documents published releases only. The complete listing
    # includes drafts for users with push access, so its absence is meaningful.
    text = inspection_command(["gh", "api", "--hostname", "github.com", "--paginate",
                               f"repos/{repository}/releases?per_page=100"], timeout=60)
    pages = []
    try:
        # --paginate emits consecutive JSON pages on older gh versions too.
        # Do not require the newer --slurp flag or upgrade the caller's client.
        decoder = json.JSONDecoder()
        remaining = text.lstrip()
        while remaining:
            page, consumed = decoder.raw_decode(remaining)
            pages.append(page)
            remaining = remaining[consumed:].lstrip()
    except (ValueError, TypeError):
        raise ReleaseError("GitHub returned invalid paginated release metadata") from None
    if not pages or any(not isinstance(page, list) for page in pages):
        raise ReleaseError("GitHub returned incomplete paginated release metadata")
    matches = []
    for page in pages:
        for release in page:
            if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str):
                raise ReleaseError("GitHub returned incomplete release metadata")
            if release["tag_name"] == version_tag:
                matches.append(release)
    if len(matches) > 1:
        raise ReleaseError("multiple releases name the same tag; inspect existing records")
    return matches[0] if matches else None


def publication_status(version_tag: str, remote: str, candidate: str | None,
                       expected_sha256: str | None = None) -> dict:
    report = {"schema_version": 1, "operation": "publication-status", "tag": version_tag,
              "remote": remote, "status": "blocked", "state": "unavailable",
              "next_action": "inspect_existing_remote_before_retrying"}
    try:
        version = tag_version(version_tag)
        if expected_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            raise ReleaseError("expected SHA-256 must be the full digest from the clean candidate build")
        commit = candidate or inspection_command(["git", "rev-parse", "--verify", "HEAD"])
        if (re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None
                or inspection_command(["git", "cat-file", "-t", commit]) != "commit"):
            raise ReleaseError("candidate must name one complete existing commit OID")
        repository = github_repository(remote)
        report.update(candidate_commit=commit, repository=repository)
        status, repository_data = github_response(repository)
        if (status != 200
                or str(repository_data.get("full_name", "")).casefold() != repository.casefold()
                or repository_data.get("archived") is not False):
            raise ReleaseError("target repository is missing, inaccessible, archived or redirected")
        tag_object, tag_commit = remote_tag(remote, version_tag)
        report.update(tag_object=tag_object, tag_commit=tag_commit)
        if tag_object is not None and tag_commit != commit:
            report["state"] = "conflict"
            raise ReleaseError("remote tag is lightweight or points to another candidate; never recreate it")
        status, release = github_response(repository, f"/releases/tags/{version_tag}")
        if status == 404:
            release = visible_release_after_not_found(repository, repository_data, version_tag)
            if release is None:
                if tag_object is None:
                    report.update(status="ok", state="unpublished",
                                  next_action="run_clean_candidate_gates_then_create_annotated_tag")
                else:
                    report.update(status="ok", state="tagged_unpublished",
                                  next_action="inspect_workflow_then_resume_without_recreating_tag")
                return report
        if release.get("tag_name") != version_tag or release.get("target_commitish") != commit:
            report["state"] = "conflict"
            raise ReleaseError("release metadata does not bind the observed tag and exact candidate")
        if (type(release.get("id")) is not int or release["id"] <= 0
                or type(release.get("draft")) is not bool or release.get("prerelease") is not False):
            raise ReleaseError("release metadata is incomplete or is not a stable release")
        report["release_id"] = release["id"]
        if release["draft"]:
            report.update(status="ok", state="draft_incomplete",
                          next_action="inspect_existing_draft_and_tag_before_resuming")
            return report
        if tag_object is None:
            report["state"] = "conflict"
            raise ReleaseError("published release has no matching annotated remote tag")
        report["state"] = "published_incomplete"
        artifact_name = f"{package_name(version)}.tar.gz"
        expected_names = {artifact_name, "SHA256SUMS.txt"}
        assets = release.get("assets")
        if (not isinstance(assets, list) or len(assets) != 2
                or any(not isinstance(asset, dict) for asset in assets)):
            raise ReleaseError("published asset inventory differs; inspect the existing release")
        if ({asset.get("name") for asset in assets} != expected_names
                or any(type(asset.get("size")) is not int or asset["size"] <= 0
                       or asset.get("state") != "uploaded" for asset in assets)):
            raise ReleaseError("published asset inventory differs; inspect the existing release")
        with tempfile.TemporaryDirectory(prefix="gf-release-status-") as temporary:
            downloaded = Path(temporary)
            inspection_command(["gh", "release", "download", version_tag, "--repo", repository,
                                "--dir", temporary, "--pattern", artifact_name,
                                "--pattern", "SHA256SUMS.txt"], timeout=60)
            if any(not (downloaded / asset["name"]).is_file()
                   or (downloaded / asset["name"]).stat().st_size != asset["size"]
                   for asset in assets):
                raise ReleaseError("downloaded asset sizes differ from observed release metadata")
            try:
                digest = verify(version_tag, dist=downloaded)
            except (ReleaseError, OSError, tarfile.TarError):
                raise ReleaseError("published assets failed package/checksum verification") from None
            report.update(artifact_sha256=digest, artifact_size=(downloaded / artifact_name).stat().st_size)
        if remote_tag(remote, version_tag) != (tag_object, tag_commit):
            report["state"] = "conflict"
            raise ReleaseError("release tag drifted during observation; never recreate it")
        if expected_sha256 is None:
            report.update(status="ok", state="published_assets_verified",
                          next_action="compare_with_clean_candidate_build_then_record")
        elif digest != expected_sha256:
            report["state"] = "conflict"
            raise ReleaseError("published artifact differs from the expected clean candidate build")
        else:
            report.update(status="ok", state="published_verified", next_action="none")
    except ReleaseError as error:
        report["reason"] = str(error)
    except (OSError, ValueError, TypeError, KeyError):
        report["reason"] = "publication metadata or temporary storage is unavailable; inspect before retrying"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("preflight", "build", "verify", "selfcheck", "publication-status"))
    parser.add_argument("tag", help="release tag, for example v1.4.0")
    parser.add_argument("--remote", help="explicit existing GitHub Git remote for publication-status")
    parser.add_argument("--candidate", help="complete candidate commit OID (default: observed HEAD)")
    parser.add_argument("--expected-sha256", help="digest from the clean candidate selfcheck, not a downloaded checksum")
    arguments = parser.parse_args(argv)
    if arguments.action == "publication-status":
        if not arguments.remote:
            parser.error("publication-status requires --remote")
        report = publication_status(arguments.tag, arguments.remote, arguments.candidate,
                                    arguments.expected_sha256)
        print(json.dumps(report, sort_keys=True))
        return 0 if report["status"] == "ok" else 2
    if arguments.remote or arguments.candidate or arguments.expected_sha256:
        parser.error("publication observation options require publication-status")
    try:
        if arguments.action == "preflight":
            print(f"release_version={preflight(arguments.tag)}")
        elif arguments.action == "build":
            artifact = build(arguments.tag)
            print(f"built={artifact} sha256={hashlib.sha256(artifact.read_bytes()).hexdigest()}")
        elif arguments.action == "verify":
            print(f"verified sha256={verify(arguments.tag)}")
        else:
            print(f"selfcheck sha256={selfcheck(arguments.tag)}")
    except (ReleaseError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
