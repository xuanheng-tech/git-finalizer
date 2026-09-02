#!/usr/bin/env bash

set -Eeuo pipefail

export LC_ALL=C
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/codex-git-finalize
tmp_root=$(mktemp -d /tmp/git-finalizer-phase01.XXXXXX)
current_case='startup'
passed=0

cleanup() {
    rm -rf -- "$tmp_root"
}

report_error() {
    local status=$?
    printf 'not ok - %s\n' "$current_case" >&2
    exit "$status"
}

trap cleanup EXIT
trap report_error ERR

fail_assertion() {
    printf 'assertion failed: %s\n' "$1" >&2
    return 1
}

assert_equal() {
    local expected=$1
    local actual=$2
    local message=$3

    [[ "$actual" == "$expected" ]] ||
        fail_assertion "$message (expected=$(printf '%q' "$expected"), actual=$(printf '%q' "$actual"))"
}

assert_file_contains() {
    local path=$1
    local text=$2
    local message=$3

    grep -Fq -- "$text" "$path" || fail_assertion "$message"
}

assert_file_not_contains() {
    local path=$1
    local text=$2
    local message=$3

    if grep -Fq -- "$text" "$path"; then
        fail_assertion "$message"
    fi
}

expect_failure() {
    local output=$1
    shift

    if "$@" >"$output" 2>&1; then
        sed -n '1,160p' "$output" >&2
        fail_assertion 'command unexpectedly succeeded'
    fi
}

expect_success() {
    local output=$1
    shift

    if ! "$@" >"$output" 2>&1; then
        sed -n '1,200p' "$output" >&2
        fail_assertion 'command unexpectedly failed'
    fi
}

configure_author() {
    local repo=$1

    git -C "$repo" config user.name 'Synthetic Test Author'
    git -C "$repo" config user.email 'synthetic@example.invalid'
}

make_synced_repo() {
    local case_dir=$1

    test_repo=$case_dir/repo
    test_remote=$case_dir/remote.git
    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$test_remote"
    git init --quiet --initial-branch=main "$test_repo"
    configure_author "$test_repo"
    printf 'wanted base\n' >"$test_repo/wanted.txt"
    printf 'outside base\n' >"$test_repo/outside.txt"
    git -C "$test_repo" add -- wanted.txt outside.txt
    git -C "$test_repo" commit --quiet -m 'base'
    git -C "$test_repo" remote add origin "$test_remote"
    git -C "$test_repo" push --quiet --set-upstream origin main
}

install_git_probe() {
    local fake_bin=$1

    mkdir -p -- "$fake_bin"
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -Eeuo pipefail' \
        'for argument in "$@"; do' \
        "    printf \"arg=%q\\\\n\" \"\$argument\" >>\"\$GIT_PROBE_LOG\"" \
        'done' \
        "printf \"command-end\\\\n\" >>\"\$GIT_PROBE_LOG\"" \
        'exec /usr/bin/git "$@"' \
        >"$fake_bin/git"
    chmod 700 "$fake_bin/git"
}

make_unborn_repo() {
    local case_dir=$1

    test_repo=$case_dir/repo
    test_remote=$case_dir/remote.git
    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$test_remote"
    git init --quiet --initial-branch=main "$test_repo"
    configure_author "$test_repo"
    git -C "$test_repo" remote add origin "$test_remote"
}

make_generated_snapshot() {
    local repo=$1
    local state=$2

    /usr/bin/python3 -B - "$repo" "$state" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


repo = Path(sys.argv[1])
state = Path(sys.argv[2])
paths = ("README.md", "generated/manifest.json", "generated/schema.json")


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


records: list[dict[str, object]] = []
contexts: list[dict[str, str]] = []
for relative in paths:
    target = repo / relative
    content = target.read_bytes()
    record: dict[str, object] = {
        "path": relative,
        "bytes": len(content),
        "sha256": digest(content),
        "executable": bool(target.stat().st_mode & 0o111),
        "coverage": "content",
    }
    if relative == "generated/schema.json":
        record["coverage"] = "generated_manifest"
        record["manifest_path"] = "generated/manifest.json"
    else:
        contexts.append({"path": relative, "content": content.decode("utf-8")})
    records.append(record)

workspace = hashlib.sha256()
for record in records:
    workspace.update(str(record["path"]).encode())
    workspace.update(b"\0")
    workspace.update(str(record["bytes"]).encode("ascii"))
    workspace.update(b"\0")
    workspace.update(str(record["sha256"]).encode("ascii"))
    workspace.update(b"\0")
    workspace.update(b"1" if record["executable"] is True else b"0")
    workspace.update(b"\n")

empty_tree = subprocess.run(
    ["/usr/bin/git", "-C", os.fspath(repo), "hash-object", "-t", "tree", "--stdin"],
    input=b"",
    capture_output=True,
    check=True,
).stdout.decode().strip()
manifest_record = records[1]
schema_record = records[2]
publication = {
    "schema_version": 1,
    "mode": "unborn",
    "complete": True,
    "sensitive_scan": "complete",
    "changed_file_count": 3,
    "covered_file_count": 3,
    "content_file_count": 2,
    "generated_file_count": 1,
    "workspace_sha256": workspace.hexdigest(),
    "files": records,
    "generated_trees": [
        {
            "path": "generated",
            "manifest_path": "generated/manifest.json",
            "manifest_bytes": manifest_record["bytes"],
            "manifest_sha256": manifest_record["sha256"],
            "file_count": 1,
            "total_bytes": schema_record["bytes"],
        }
    ],
}
status = "".join(f"?? {path}\n" for path in paths)
envelope = {
    "schema_version": 2,
    "producer_security_epoch": 4,
    "task": "diff-audit",
    "repository": repo.name,
    "data": {
        "status_short": status,
        "staged_diff": "",
        "unstaged_diff": "",
        "file_context": contexts,
        "baseline_kind": "empty_tree",
        "baseline_oid": empty_tree,
        "initial_publication": publication,
    },
    "truncated": False,
    "evidence_gaps": [],
    "redactions": {},
    "trust_boundary": "synthetic Finalizer integration fixture",
    "security_notice": "synthetic Finalizer integration fixture",
}
snapshot_bytes = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
snapshot_id = digest(snapshot_bytes)
preview_bytes = b"synthetic preview\n"
meta = {
    "schema_version": 2,
    "producer_security_epoch": 4,
    "snapshot_id": snapshot_id,
    "task": "diff-audit",
    "repository": repo.name,
    "snapshot_sha256": snapshot_id,
    "snapshot_bytes": len(snapshot_bytes),
    "preview_sha256": digest(preview_bytes),
    "preview_bytes": len(preview_bytes),
}
directory = state / "codex-exec" / "snapshots" / snapshot_id
directory.mkdir(parents=True, mode=0o700)
for parent in (state, state / "codex-exec", state / "codex-exec" / "snapshots", directory):
    parent.chmod(0o700)
for name, content in (
    ("snapshot.json", snapshot_bytes),
    ("preview.txt", preview_bytes),
    ("meta.json", json.dumps(meta, sort_keys=True).encode() + b"\n"),
):
    target = directory / name
    target.write_bytes(content)
    target.chmod(0o600)
print(snapshot_id)
PY
}

make_cloned_unborn_repo() {
    local case_dir=$1

    test_repo=$case_dir/repo
    test_remote=$case_dir/remote.git
    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$test_remote"
    git clone --quiet "$test_remote" "$test_repo" 2>/dev/null
    configure_author "$test_repo"
}

make_seed_repo() {
    local destination=$1

    git init --quiet --initial-branch=main "$destination"
    configure_author "$destination"
    printf 'seed\n' >"$destination/seed.txt"
    git -C "$destination" add -- seed.txt
    git -C "$destination" commit --quiet -m 'seed'
}

push_seed_ref() {
    local seed=$1
    local remote=$2
    local destination_ref=$3

    make_seed_repo "$seed"
    git -C "$seed" push --quiet "$remote" "HEAD:$destination_ref"
}

make_competitor() {
    local destination=$1

    git clone --quiet "$test_remote" "$destination"
    configure_author "$destination"
}

index_snapshot() {
    git -C "$1" ls-files --stage
}

status_snapshot() {
    git -C "$1" status --porcelain=v1 --untracked-files=all
}

file_snapshot() {
    git -C "$1" hash-object --no-filters -- "$2"
}

make_binary_file() {
    local path=$1
    local size_mib=$2

    dd if=/dev/zero of="$path" bs=1048576 count="$size_mib" status=none
}

remote_main() {
    git --git-dir="$1" rev-parse refs/heads/main
}

remote_refs() {
    git --git-dir="$1" for-each-ref --format='%(objectname)%09%(refname)'
}

git_metadata_snapshot() {
    local repo=$1
    local path

    while IFS= read -r -d '' path; do
        printf '%s\t' "${path#"$repo/.git/"}"
        sha256sum "$path" | awk '{print $1}'
    done < <(find "$repo/.git" -type f -print0 | sort -z)
}

assert_unborn() {
    local repo=$1

    if git -C "$repo" rev-parse --verify HEAD >/dev/null 2>&1; then
        fail_assertion 'repository is no longer unborn'
    fi
}

assert_root_commit() {
    local repo=$1
    local head

    head=$(git -C "$repo" rev-parse HEAD)
    assert_equal "$head" "$(git -C "$repo" rev-list --parents -n 1 HEAD)" \
        'commit is not a root commit'
}

assert_precommit_state_unchanged() {
    local repo=$1
    local expected_head=$2
    local expected_index=$3
    local expected_status=$4
    local expected_file=$5

    assert_equal "$expected_head" "$(git -C "$repo" rev-parse HEAD)" 'HEAD changed'
    assert_equal "$expected_index" "$(index_snapshot "$repo")" 'index changed'
    assert_equal "$expected_status" "$(status_snapshot "$repo")" 'worktree status changed'
    assert_equal "$expected_file" "$(file_snapshot "$repo" wanted.txt)" 'worktree content changed'
}

test_missing_upstream() {
    local case_dir=$tmp_root/missing-upstream
    local output=$case_dir/output.log
    local head_before index_before status_before file_before

    make_synced_repo "$case_dir"
    git -C "$test_repo" branch --unset-upstream
    printf 'local change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")
    status_before=$(status_snapshot "$test_repo")
    file_before=$(file_snapshot "$test_repo" wanted.txt)

    expect_failure "$output" "$finalizer" --repo "$test_repo" --message test -- wanted.txt
    assert_file_contains "$output" 'upstream' 'missing-upstream diagnostic absent'
    assert_precommit_state_unchanged \
        "$test_repo" "$head_before" "$index_before" "$status_before" "$file_before"
}

test_missing_remote_branch() {
    local case_dir=$tmp_root/missing-remote-branch
    local output=$case_dir/output.log
    local head_before index_before status_before file_before

    make_synced_repo "$case_dir"
    git --git-dir="$test_remote" update-ref -d refs/heads/main
    printf 'local change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")
    status_before=$(status_snapshot "$test_repo")
    file_before=$(file_snapshot "$test_repo" wanted.txt)

    expect_failure "$output" "$finalizer" --repo "$test_repo" --message test -- wanted.txt
    assert_file_contains "$output" '远端 branch 不存在' 'missing-remote diagnostic absent'
    assert_precommit_state_unchanged \
        "$test_repo" "$head_before" "$index_before" "$status_before" "$file_before"
    if git --git-dir="$test_remote" show-ref --verify --quiet refs/heads/main; then
        fail_assertion 'missing remote branch was recreated'
    fi
}

test_fetch_failure() {
    local case_dir=$tmp_root/fetch-failure
    local endpoint_marker='PRIVATE_ENDPOINT_MARKER_DO_NOT_PRINT'
    local output=$case_dir/output.log
    local head_before index_before status_before file_before

    make_synced_repo "$case_dir"
    git -C "$test_repo" remote set-url origin "$case_dir/$endpoint_marker.git"
    printf 'local change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")
    status_before=$(status_snapshot "$test_repo")
    file_before=$(file_snapshot "$test_repo" wanted.txt)

    expect_failure "$output" "$finalizer" --repo "$test_repo" --message test -- wanted.txt
    assert_file_contains "$output" 'commit 前 git fetch 失败' 'fetch-failure diagnostic absent'
    assert_file_not_contains "$output" "$endpoint_marker" 'fetch failure disclosed remote endpoint'
    assert_precommit_state_unchanged \
        "$test_repo" "$head_before" "$index_before" "$status_before" "$file_before"
}

test_behind() {
    local case_dir=$tmp_root/behind
    local competitor=$case_dir/competitor
    local output=$case_dir/output.log
    local head_before index_before status_before file_before remote_before

    make_synced_repo "$case_dir"
    make_competitor "$competitor"
    printf 'remote commit\n' >"$competitor/remote.txt"
    git -C "$competitor" add -- remote.txt
    git -C "$competitor" commit --quiet -m 'remote advance'
    git -C "$competitor" push --quiet origin main
    remote_before=$(remote_main "$test_remote")
    printf 'local change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")
    status_before=$(status_snapshot "$test_repo")
    file_before=$(file_snapshot "$test_repo" wanted.txt)

    expect_failure "$output" "$finalizer" --repo "$test_repo" --message test -- wanted.txt
    assert_file_contains "$output" '本地落后 upstream' 'behind diagnostic absent'
    assert_precommit_state_unchanged \
        "$test_repo" "$head_before" "$index_before" "$status_before" "$file_before"
    assert_equal "$remote_before" "$(remote_main "$test_remote")" 'behind case changed remote'
}

test_diverged() {
    local case_dir=$tmp_root/diverged
    local competitor=$case_dir/competitor
    local output=$case_dir/output.log
    local head_before index_before status_before file_before remote_before

    make_synced_repo "$case_dir"
    make_competitor "$competitor"
    printf 'local ahead\n' >"$test_repo/local.txt"
    git -C "$test_repo" add -- local.txt
    git -C "$test_repo" commit --quiet -m 'local ahead'
    printf 'remote ahead\n' >"$competitor/remote.txt"
    git -C "$competitor" add -- remote.txt
    git -C "$competitor" commit --quiet -m 'remote ahead'
    git -C "$competitor" push --quiet origin main
    remote_before=$(remote_main "$test_remote")
    printf 'local change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")
    status_before=$(status_snapshot "$test_repo")
    file_before=$(file_snapshot "$test_repo" wanted.txt)

    expect_failure "$output" "$finalizer" --repo "$test_repo" --message test -- wanted.txt
    assert_file_contains "$output" '本地已与 upstream 分叉' 'diverged diagnostic absent'
    assert_precommit_state_unchanged \
        "$test_repo" "$head_before" "$index_before" "$status_before" "$file_before"
    assert_equal "$remote_before" "$(remote_main "$test_remote")" 'diverged case changed remote'
}

test_local_ahead() {
    local case_dir=$tmp_root/local-ahead
    local output=$case_dir/output.log
    local ahead_commit final_head counts

    make_synced_repo "$case_dir"
    printf 'existing ahead\n' >"$test_repo/local.txt"
    git -C "$test_repo" add -- local.txt
    git -C "$test_repo" commit --quiet -m 'existing local ahead'
    ahead_commit=$(git -C "$test_repo" rev-parse HEAD)
    printf 'finalizer change\n' >>"$test_repo/wanted.txt"

    expect_success "$output" "$finalizer" \
        --repo "$test_repo" --message 'finalizer commit' -- wanted.txt
    final_head=$(git -C "$test_repo" rev-parse HEAD)
    [[ "$final_head" != "$ahead_commit" ]] || fail_assertion 'Finalizer did not create a commit'
    assert_equal "$final_head" "$(remote_main "$test_remote")" 'local-ahead push incomplete'
    assert_equal "$final_head" "$(git -C "$test_repo" rev-parse '@{upstream}')" \
        'local-ahead upstream mismatch'
    counts=$(git -C "$test_repo" rev-list --left-right --count 'HEAD...@{upstream}')
    assert_equal $'0\t0' "$counts" 'local-ahead final counts are not 0/0'
}

test_synced_success_scope() {
    local case_dir=$tmp_root/synced-success
    local output=$case_dir/output.log
    local final_head committed_paths outside_status

    make_synced_repo "$case_dir"
    printf 'wanted change\n' >>"$test_repo/wanted.txt"
    printf 'outside change\n' >>"$test_repo/outside.txt"

    expect_success "$output" "$finalizer" \
        --repo "$test_repo" --message 'scoped commit' -- wanted.txt
    final_head=$(git -C "$test_repo" rev-parse HEAD)
    assert_equal "$final_head" "$(remote_main "$test_remote")" 'synced push mismatch'
    committed_paths=$(git -C "$test_repo" diff-tree --no-commit-id --name-only -r HEAD)
    assert_equal 'wanted.txt' "$committed_paths" 'commit included a non-explicit path'
    outside_status=$(git -C "$test_repo" status --short -- outside.txt)
    assert_equal ' M outside.txt' "$outside_status" 'outside change was not preserved unstaged'
    assert_equal '' "$(git -C "$test_repo" diff --cached --name-only)" \
        'synced success left staged content'
}

test_default_mode_still_pushes_and_verifies() {
    local case_dir=$tmp_root/default-mode-contract
    local fake_bin=$case_dir/bin
    local probe=$case_dir/git-probe.log
    local output=$case_dir/output.log
    local final_head

    make_synced_repo "$case_dir"
    install_git_probe "$fake_bin"
    printf 'default mode change\n' >>"$test_repo/wanted.txt"

    GIT_PROBE_LOG="$probe" PATH="$fake_bin:$PATH" \
        expect_success "$output" "$finalizer" --repo "$test_repo" \
        --message 'default mode contract' -- wanted.txt

    final_head=$(git -C "$test_repo" rev-parse HEAD)
    assert_equal "$final_head" "$(remote_main "$test_remote")" \
        'default mode no longer pushed its commit'
    assert_file_contains "$probe" 'arg=fetch' 'default mode did not fetch'
    assert_file_contains "$probe" 'arg=ls-remote' 'default mode did not verify the remote branch'
    assert_file_contains "$probe" 'arg=push' 'default mode did not push'
    assert_file_contains "$output" 'commit 与 push 已验证' \
        'default success report changed'
}

test_commit_only_success_without_remote_commands() {
    local case_dir=$tmp_root/commit-only-success
    local fake_bin=$case_dir/bin
    local probe=$case_dir/git-probe.log
    local output=$case_dir/output.log
    local config_before tags_before remote_before head_before count_before final_head

    make_synced_repo "$case_dir"
    git -C "$test_repo" config --unset-all branch.main.remote
    git -C "$test_repo" config --unset-all branch.main.merge
    git -C "$test_repo" tag commit-only-local-tag HEAD
    install_git_probe "$fake_bin"
    config_before=$(git -C "$test_repo" config --local --list)
    tags_before=$(git -C "$test_repo" for-each-ref --format='%(objectname)%09%(refname)' refs/tags)
    remote_before=$(remote_refs "$test_remote")
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    count_before=$(git -C "$test_repo" rev-list --count HEAD)
    printf 'commit-only change\n' >>"$test_repo/wanted.txt"

    GIT_PROBE_LOG="$probe" PATH="$fake_bin:$PATH" \
        expect_success "$output" "$finalizer" --mode commit-only \
        --repo "$test_repo" --message 'local commit only' -- wanted.txt

    final_head=$(git -C "$test_repo" rev-parse HEAD)
    assert_equal "$head_before" "$(git -C "$test_repo" rev-parse 'HEAD^')" \
        'commit-only commit has the wrong parent'
    assert_equal "$((count_before + 1))" "$(git -C "$test_repo" rev-list --count HEAD)" \
        'commit-only did not create exactly one commit'
    assert_equal "$remote_before" "$(remote_refs "$test_remote")" \
        'commit-only changed remote refs'
    assert_equal "$tags_before" \
        "$(git -C "$test_repo" for-each-ref --format='%(objectname)%09%(refname)' refs/tags)" \
        'commit-only changed local tags'
    assert_equal "$config_before" "$(git -C "$test_repo" config --local --list)" \
        'commit-only changed local Git config'
    assert_file_not_contains "$probe" 'arg=fetch' 'commit-only invoked fetch'
    assert_file_not_contains "$probe" 'arg=ls-remote' 'commit-only invoked remote verification'
    assert_file_not_contains "$probe" 'arg=push' 'commit-only invoked push'
    assert_file_contains "$output" 'mode: commit-only' 'commit-only mode was not reported'
    assert_file_contains "$output" "commit hash: $final_head" \
        'commit-only result omitted the new commit hash'
    assert_file_contains "$output" 'push: skipped (--mode commit-only)' \
        'commit-only push skip was not reported'
    assert_file_contains "$output" 'remote verification: skipped (--mode commit-only)' \
        'commit-only remote-verification skip was not reported'
    assert_file_contains "$output" '$ git status --short' \
        'commit-only final worktree status was not reported'
    assert_file_contains "$output" '(clean)' 'commit-only final worktree state is not clean'
}

test_initial_commit_only_success_without_remote_commands() {
    local case_dir=$tmp_root/initial-commit-only-success
    local fake_bin=$case_dir/bin
    local probe=$case_dir/git-probe.log
    local output=$case_dir/output.log
    local config_before remote_before head committed_paths

    make_unborn_repo "$case_dir"
    git -C "$test_repo" remote remove origin
    install_git_probe "$fake_bin"
    config_before=$(git -C "$test_repo" config --local --list)
    remote_before=$(remote_refs "$test_remote")
    printf 'local root\n' >"$test_repo/first.txt"

    GIT_PROBE_LOG="$probe" PATH="$fake_bin:$PATH" \
        expect_success "$output" "$finalizer" --initial-commit-only \
        --repo "$test_repo" --message 'local root commit' -- first.txt

    head=$(git -C "$test_repo" rev-parse HEAD)
    assert_root_commit "$test_repo"
    assert_equal '1' "$(git -C "$test_repo" rev-list --count HEAD)" \
        'initial-commit-only did not create exactly one root commit'
    committed_paths=$(
        git -C "$test_repo" diff-tree --root --no-commit-id --name-only -r HEAD
    )
    assert_equal 'first.txt' "$committed_paths" \
        'initial-commit-only root contains an unexpected path'
    assert_equal "$remote_before" "$(remote_refs "$test_remote")" \
        'initial-commit-only changed remote refs'
    assert_equal "$config_before" "$(git -C "$test_repo" config --local --list)" \
        'initial-commit-only changed local Git config'
    assert_file_not_contains "$probe" 'arg=fetch' \
        'initial-commit-only invoked fetch'
    assert_file_not_contains "$probe" 'arg=ls-remote' \
        'initial-commit-only invoked remote verification'
    assert_file_not_contains "$probe" 'arg=push' \
        'initial-commit-only invoked push'
    assert_file_contains "$output" 'mode: initial-commit-only' \
        'initial-commit-only mode was not reported'
    assert_file_contains "$output" "commit hash: $head" \
        'initial-commit-only result omitted the root commit hash'
    assert_equal '' "$(status_snapshot "$test_repo")" \
        'initial-commit-only left the worktree dirty'
}

test_verify_only_success_without_git_side_effects() {
    local case_dir=$tmp_root/verify-only-success
    local fake_bin=$case_dir/bin
    local probe=$case_dir/git-probe.log
    local output=$case_dir/output.log
    local head_before index_before status_before file_before new_file_before
    local metadata_before metadata_after remote_before

    make_synced_repo "$case_dir"
    git -C "$test_repo" branch --unset-upstream
    git -C "$test_repo" config --unset user.name
    git -C "$test_repo" config --unset user.email
    git -C "$test_repo" tag verify-only-local-tag HEAD
    printf 'verify-only staged change\n' >>"$test_repo/wanted.txt"
    git -C "$test_repo" add -- wanted.txt
    printf 'verify-only unstaged change\n' >>"$test_repo/wanted.txt"
    printf 'verify-only untracked candidate\n' >"$test_repo/new.txt"
    install_git_probe "$fake_bin"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")
    status_before=$(status_snapshot "$test_repo")
    file_before=$(file_snapshot "$test_repo" wanted.txt)
    new_file_before=$(file_snapshot "$test_repo" new.txt)
    metadata_before=$(git_metadata_snapshot "$test_repo")
    remote_before=$(remote_refs "$test_remote")

    GIT_PROBE_LOG="$probe" PATH="$fake_bin:$PATH" \
        expect_success "$output" "$finalizer" --mode verify-only \
        --repo "$test_repo" -- wanted.txt new.txt

    metadata_after=$(git_metadata_snapshot "$test_repo")
    assert_equal "$metadata_before" "$metadata_after" \
        'verify-only changed Git metadata'
    assert_equal "$head_before" "$(git -C "$test_repo" rev-parse HEAD)" \
        'verify-only changed HEAD'
    assert_equal "$index_before" "$(index_snapshot "$test_repo")" \
        'verify-only changed index entries'
    assert_equal "$status_before" "$(status_snapshot "$test_repo")" \
        'verify-only changed worktree status'
    assert_equal "$file_before" "$(file_snapshot "$test_repo" wanted.txt)" \
        'verify-only changed worktree content'
    assert_equal "$new_file_before" "$(file_snapshot "$test_repo" new.txt)" \
        'verify-only changed untracked candidate content'
    assert_equal "$remote_before" "$(remote_refs "$test_remote")" \
        'verify-only changed remote refs'
    for forbidden in add commit push fetch ls-remote remote tag update-ref config; do
        assert_file_not_contains "$probe" "arg=$forbidden" \
            "verify-only invoked forbidden Git command: $forbidden"
    done
    assert_file_contains "$output" 'mode: verify-only' \
        'verify-only mode was not reported'
    assert_file_contains "$output" 'local validation: passed' \
        'verify-only success was not reported'
    assert_file_contains "$output" 'commit: skipped (--mode verify-only)' \
        'verify-only commit skip was not reported'
    assert_file_contains "$output" 'push: skipped (--mode verify-only)' \
        'verify-only push skip was not reported'
    assert_file_contains "$output" 'remote verification: skipped (--mode verify-only)' \
        'verify-only remote-verification skip was not reported'
    assert_file_contains "$output" 'HEAD unchanged: yes' \
        'verify-only HEAD preservation was not reported'
    assert_file_contains "$output" 'index unchanged: yes' \
        'verify-only index preservation was not reported'
    assert_file_contains "$output" 'worktree unchanged: yes' \
        'verify-only worktree preservation was not reported'
    assert_file_contains "$output" 'worktree after: dirty' \
        'verify-only final worktree state was not reported'
}

test_verify_only_failure_is_non_mutating() {
    local case_dir=$tmp_root/verify-only-failure
    local output=$case_dir/output.log
    local head_before index_before status_before file_before metadata_before metadata_after

    make_synced_repo "$case_dir"
    git -C "$test_repo" branch --unset-upstream
    printf 'trailing whitespace   \n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")
    status_before=$(status_snapshot "$test_repo")
    file_before=$(file_snapshot "$test_repo" wanted.txt)
    metadata_before=$(git_metadata_snapshot "$test_repo")

    expect_failure "$output" "$finalizer" --mode verify-only \
        --repo "$test_repo" -- wanted.txt

    metadata_after=$(git_metadata_snapshot "$test_repo")
    assert_file_contains "$output" 'git diff --check 失败' \
        'verify-only validation failure diagnostic absent'
    assert_file_not_contains "$output" 'local validation: passed' \
        'verify-only failure reported success'
    assert_equal "$metadata_before" "$metadata_after" \
        'failed verify-only changed Git metadata'
    assert_precommit_state_unchanged \
        "$test_repo" "$head_before" "$index_before" "$status_before" "$file_before"
}

test_invalid_mode_is_rejected() {
    local case_dir=$tmp_root/invalid-mode
    local output=$case_dir/output.log
    local head_before index_before status_before file_before

    make_synced_repo "$case_dir"
    printf 'invalid mode change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")
    status_before=$(status_snapshot "$test_repo")
    file_before=$(file_snapshot "$test_repo" wanted.txt)

    expect_failure "$output" "$finalizer" --mode invalid \
        --repo "$test_repo" --message test -- wanted.txt

    assert_file_contains "$output" '--mode 仅支持 commit-only 或 verify-only' \
        'invalid mode diagnostic absent'
    assert_precommit_state_unchanged \
        "$test_repo" "$head_before" "$index_before" "$status_before" "$file_before"
}

test_normal_disables_follow_tags_from_local_config() {
    local case_dir=$tmp_root/normal-no-follow-tags
    local output=$case_dir/output.log
    local final_head
    local tag=normal-local-only

    make_synced_repo "$case_dir"
    git -C "$test_repo" config push.followTags true
    git -C "$test_repo" tag -a -m follow-tags-fixture "$tag" HEAD
    printf 'safe branch-only push\n' >>"$test_repo/wanted.txt"

    expect_success "$output" "$finalizer" --repo "$test_repo" \
        --message 'disable configured follow-tags' -- wanted.txt

    final_head=$(git -C "$test_repo" rev-parse HEAD)
    assert_equal "$final_head"$'\trefs/heads/main' "$(remote_refs "$test_remote")" \
        'normal push followed an annotated tag from local config'
    git -C "$test_repo" show-ref --verify --quiet "refs/tags/$tag" ||
        fail_assertion 'normal follow-tags fixture is missing locally'
    if git --git-dir="$test_remote" show-ref --verify --quiet "refs/tags/$tag"; then
        fail_assertion 'normal push created the local annotated tag remotely'
    fi
}

test_staged_deletion_preserves_ignored_worktree_copy() {
    local case_dir=$tmp_root/staged-ignored-deletion
    local output=$case_dir/output.log
    local copy_hash final_head committed_status

    make_synced_repo "$case_dir"
    mkdir -p -- "$test_repo/scratch"
    printf '# tracked ignore rules\n' >"$test_repo/.gitignore"
    printf 'tracked scratch\n' >"$test_repo/scratch/草稿箱.md"
    git -C "$test_repo" add -- .gitignore scratch/草稿箱.md
    git -C "$test_repo" commit --quiet -m 'track scratch file'
    git -C "$test_repo" push --quiet

    printf 'scratch/\n' >"$test_repo/.gitignore"
    git -C "$test_repo" rm --cached --quiet -- scratch/草稿箱.md
    printf 'github_%s%s\n' 'pat_' 'SYNTHETIC_ONLY_0123456789ABCDEF' \
        >"$test_repo/scratch/草稿箱.md"
    copy_hash=$(file_snapshot "$test_repo" scratch/草稿箱.md)

    expect_success "$output" "$finalizer" --repo "$test_repo" \
        --message 'stop tracking local scratch' -- .gitignore scratch/草稿箱.md
    final_head=$(git -C "$test_repo" rev-parse HEAD)
    assert_equal "$final_head" "$(remote_main "$test_remote")" \
        'ignored-copy deletion push mismatch'
    committed_status=$(
        git -C "$test_repo" -c core.quotePath=false \
            diff-tree --no-commit-id --name-status -r --no-renames HEAD
    )
    assert_equal $'M\t.gitignore\nD\tscratch/草稿箱.md' "$committed_status" \
        'ignored-copy deletion commit has the wrong scope'
    assert_file_contains "$output" '保留已暂存删除及本地忽略副本' \
        'ignored-copy preservation was not reported'
    assert_equal "$copy_hash" "$(file_snapshot "$test_repo" scratch/草稿箱.md)" \
        'ignored worktree copy changed'
    if git -C "$test_repo" ls-files --error-unmatch -- scratch/草稿箱.md >/dev/null 2>&1; then
        fail_assertion 'ignored worktree copy remains tracked'
    fi
    git -C "$test_repo" check-ignore --quiet --no-index -- scratch/草稿箱.md ||
        fail_assertion 'retained worktree copy is not ignored'
    assert_equal '' "$(status_snapshot "$test_repo")" \
        'ignored worktree copy left visible repository changes'
}

test_post_commit_remote_change() {
    local case_dir=$tmp_root/post-commit-race
    local competitor=$case_dir/competitor
    local output=$case_dir/output.log
    local marker=$case_dir/pre-push.marker
    local head_before count_before final_head remote_after parent

    make_synced_repo "$case_dir"
    make_competitor "$competitor"
    printf 'competitor change\n' >"$competitor/remote.txt"
    git -C "$competitor" add -- remote.txt
    git -C "$competitor" commit --quiet -m 'competing remote commit'

    printf '#!/usr/bin/env bash\nset -euo pipefail\nunset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX\ngit -C %q push --quiet origin HEAD:refs/heads/main\n' \
        "$competitor" >"$test_repo/.git/hooks/post-commit"
    printf '#!/usr/bin/env bash\nset -euo pipefail\nprintf reached >%q\n' \
        "$marker" >"$test_repo/.git/hooks/pre-push"
    chmod 700 "$test_repo/.git/hooks/post-commit" "$test_repo/.git/hooks/pre-push"

    printf 'local finalizer change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    count_before=$(git -C "$test_repo" rev-list --count HEAD)
    expect_failure "$output" "$finalizer" \
        --repo "$test_repo" --message 'local competing commit' -- wanted.txt
    assert_file_contains "$output" 'commit 后 本地已与 upstream 分叉' \
        'post-commit race diagnostic absent'
    final_head=$(git -C "$test_repo" rev-parse HEAD)
    remote_after=$(remote_main "$test_remote")
    parent=$(git -C "$test_repo" rev-parse 'HEAD^')
    assert_equal "$head_before" "$parent" 'retained commit has the wrong parent'
    assert_equal "$((count_before + 1))" "$(git -C "$test_repo" rev-list --count HEAD)" \
        'post-commit race did not retain exactly one new commit'
    [[ "$final_head" != "$remote_after" ]] || fail_assertion 'remote competitor commit was overwritten'
    [[ ! -e "$marker" ]] || fail_assertion 'Finalizer reached push after post-commit race'
}

test_push_failure_keeps_commit_without_remote_output() {
    local case_dir=$tmp_root/push-failure
    local output=$case_dir/output.log
    local remote_marker='PRIVATE_REMOTE_REJECTION_DO_NOT_PRINT'
    local head_before count_before remote_before

    make_synced_repo "$case_dir"
    printf '#!/usr/bin/env bash\nprintf "%%s\\n" %q >&2\nexit 1\n' \
        "$remote_marker" >"$test_remote/hooks/pre-receive"
    chmod 700 "$test_remote/hooks/pre-receive"
    printf 'local finalizer change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    count_before=$(git -C "$test_repo" rev-list --count HEAD)
    remote_before=$(remote_main "$test_remote")

    expect_failure "$output" "$finalizer" \
        --repo "$test_repo" --message 'rejected push commit' -- wanted.txt
    assert_file_contains "$output" 'git push 失败' 'push-failure diagnostic absent'
    assert_file_not_contains "$output" "$remote_marker" 'push failure disclosed remote output'
    assert_equal "$head_before" "$(git -C "$test_repo" rev-parse 'HEAD^')" \
        'push-failure commit has the wrong parent'
    assert_equal "$((count_before + 1))" "$(git -C "$test_repo" rev-list --count HEAD)" \
        'push failure did not retain exactly one new commit'
    assert_equal "$remote_before" "$(remote_main "$test_remote")" \
        'rejected push changed remote branch'
}

test_detached() {
    local case_dir=$tmp_root/detached
    local output=$case_dir/output.log
    local head_before index_before status_before file_before

    make_synced_repo "$case_dir"
    git -C "$test_repo" checkout --quiet --detach
    printf 'detached change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")
    status_before=$(status_snapshot "$test_repo")
    file_before=$(file_snapshot "$test_repo" wanted.txt)
    expect_failure "$output" "$finalizer" --repo "$test_repo" --message test -- wanted.txt
    assert_file_contains "$output" 'detached HEAD' 'detached diagnostic absent'
    assert_precommit_state_unchanged \
        "$test_repo" "$head_before" "$index_before" "$status_before" "$file_before"
}

test_unborn() {
    local case_dir=$tmp_root/unborn
    local repo=$case_dir/repo
    local remote=$case_dir/remote.git
    local output=$case_dir/output.log

    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$remote"
    git init --quiet --initial-branch=main "$repo"
    configure_author "$repo"
    git -C "$repo" remote add origin "$remote"
    printf 'first file\n' >"$repo/wanted.txt"
    expect_failure "$output" "$finalizer" --repo "$repo" --message test -- wanted.txt
    assert_file_contains "$output" 'unborn HEAD' 'unborn diagnostic absent'
    if git -C "$repo" rev-parse --verify HEAD >/dev/null 2>&1; then
        fail_assertion 'normal mode created an unborn commit'
    fi
    assert_equal '' "$(index_snapshot "$repo")" 'unborn rejection changed index'
}

test_invalid_repositories() {
    local case_dir=$tmp_root/invalid-repositories
    local bare=$case_dir/bare.git
    local plain=$case_dir/plain
    local bare_output=$case_dir/bare.log
    local plain_output=$case_dir/plain.log

    mkdir -p -- "$plain"
    git init --quiet --bare "$bare"
    printf 'plain\n' >"$plain/wanted.txt"
    expect_failure "$bare_output" "$finalizer" --repo "$bare" --message test -- wanted.txt
    expect_failure "$plain_output" "$finalizer" --repo "$plain" --message test -- wanted.txt
    assert_file_contains "$bare_output" '不是 Git worktree' 'bare diagnostic absent'
    assert_file_contains "$plain_output" '不是 Git worktree' 'non-Git diagnostic absent'
}

test_dry_run() {
    local case_dir=$tmp_root/dry-run
    local output=$case_dir/output.log
    local head_before index_before status_before file_before tracking_before fetch_before fetch_after

    make_synced_repo "$case_dir"
    printf 'dry-run change\n' >>"$test_repo/wanted.txt"
    git -C "$test_repo" remote set-url origin "$case_dir/does-not-exist.git"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")
    status_before=$(status_snapshot "$test_repo")
    file_before=$(file_snapshot "$test_repo" wanted.txt)
    tracking_before=$(git -C "$test_repo" rev-parse refs/remotes/origin/main)
    if [[ -e "$test_repo/.git/FETCH_HEAD" ]]; then
        fetch_before=$(sha256sum "$test_repo/.git/FETCH_HEAD")
    else
        fetch_before='absent'
    fi

    expect_success "$output" "$finalizer" \
        --repo "$test_repo" --message test --dry-run -- wanted.txt
    assert_file_contains "$output" '未实时核验远端' 'dry-run remote notice absent'
    assert_file_not_contains "$output" '本地 commit 后停止 push' 'dry-run retained obsolete warning'
    assert_precommit_state_unchanged \
        "$test_repo" "$head_before" "$index_before" "$status_before" "$file_before"
    assert_equal "$tracking_before" "$(git -C "$test_repo" rev-parse refs/remotes/origin/main)" \
        'dry-run changed remote-tracking ref'
    if [[ -e "$test_repo/.git/FETCH_HEAD" ]]; then
        fetch_after=$(sha256sum "$test_repo/.git/FETCH_HEAD")
    else
        fetch_after='absent'
    fi
    assert_equal "$fetch_before" "$fetch_after" 'dry-run changed FETCH_HEAD'
}

test_fixture_scope() {
    local case_dir=$tmp_root/fixture
    local default_output=$case_dir/default.log
    local mismatch_output=$case_dir/mismatch.log
    local success_output=$case_dir/success.log
    local head_before index_before

    make_synced_repo "$case_dir"
    mkdir -p -- "$test_repo/tests"
    printf 'github_%s%s\n' 'pat_' 'SYNTHETIC_ONLY_0123456789ABCDEF' >"$test_repo/tests/allowed.txt"
    printf 'github_%s%s\n' 'pat_' 'SYNTHETIC_ONLY_ABCDEF0123456789' >"$test_repo/tests/blocked.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")

    expect_failure "$default_output" "$finalizer" \
        --repo "$test_repo" --message test -- tests/blocked.txt
    assert_file_contains "$default_output" '高置信度 token/credential' \
        'synthetic fixture was not rejected by default'
    expect_failure "$mismatch_output" "$finalizer" \
        --repo "$test_repo" --message test \
        --allow-test-fixture tests/allowed.txt -- tests/allowed.txt tests/blocked.txt
    assert_file_contains "$mismatch_output" 'tests/blocked.txt' 'fixture override was not exact'
    assert_equal "$head_before" "$(git -C "$test_repo" rev-parse HEAD)" \
        'fixture rejection created a commit'
    assert_equal "$index_before" "$(index_snapshot "$test_repo")" \
        'fixture rejection changed index'

    expect_success "$success_output" "$finalizer" \
        --repo "$test_repo" --message 'allowed fixture' \
        --allow-test-fixture tests/allowed.txt -- tests/allowed.txt
    assert_equal 'tests/allowed.txt' \
        "$(git -C "$test_repo" diff-tree --no-commit-id --name-only -r HEAD)" \
        'exact fixture commit contained another file'
    assert_equal '?? tests/blocked.txt' \
        "$(git -C "$test_repo" status --short -- tests/blocked.txt)" \
        'blocked fixture did not remain untracked'
}

test_large_binary_scope() {
    local case_dir=$tmp_root/large-binary
    local default_output=$case_dir/default.log
    local mismatch_output=$case_dir/mismatch.log
    local oversized_output=$case_dir/oversized.log
    local success_output=$case_dir/success.log
    local head_before index_before

    make_synced_repo "$case_dir"
    make_binary_file "$test_repo/allowed.bin" 6
    make_binary_file "$test_repo/blocked.bin" 6
    make_binary_file "$test_repo/oversized.bin" 26
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")

    expect_failure "$default_output" "$finalizer" \
        --repo "$test_repo" --message test -- allowed.bin
    assert_file_contains "$default_output" '大于 5 MiB' \
        'large binary was not rejected by default'
    expect_failure "$mismatch_output" "$finalizer" \
        --repo "$test_repo" --message test \
        --allow-large-binary allowed.bin -- blocked.bin
    assert_file_contains "$mismatch_output" '不在本次显式文件范围内' \
        'large binary override was not exact'
    expect_failure "$oversized_output" "$finalizer" \
        --repo "$test_repo" --message test \
        --allow-large-binary oversized.bin -- oversized.bin
    assert_file_contains "$oversized_output" '超过 25 MiB 硬上限' \
        'large binary hard cap was not enforced'
    assert_equal "$head_before" "$(git -C "$test_repo" rev-parse HEAD)" \
        'large binary rejection created a commit'
    assert_equal "$index_before" "$(index_snapshot "$test_repo")" \
        'large binary rejection changed index'

    expect_success "$success_output" "$finalizer" \
        --repo "$test_repo" --message 'allowed large binary' \
        --allow-large-binary allowed.bin -- allowed.bin
    assert_equal 'allowed.bin' \
        "$(git -C "$test_repo" diff-tree --no-commit-id --name-only -r HEAD)" \
        'exact large binary commit contained another file'
    assert_equal '?? blocked.bin' \
        "$(git -C "$test_repo" status --short -- blocked.bin)" \
        'blocked large binary did not remain untracked'
    assert_equal '?? oversized.bin' \
        "$(git -C "$test_repo" status --short -- oversized.bin)" \
        'oversized binary did not remain untracked'
}

test_push_target_mismatch() {
    local case_dir=$tmp_root/push-target-mismatch
    local backup=$case_dir/backup.git
    local output=$case_dir/output.log
    local head_before index_before status_before file_before

    make_synced_repo "$case_dir"
    git init --quiet --bare --initial-branch=main "$backup"
    git -C "$test_repo" remote add backup "$backup"
    git -C "$test_repo" config branch.main.pushRemote backup
    printf 'local change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(index_snapshot "$test_repo")
    status_before=$(status_snapshot "$test_repo")
    file_before=$(file_snapshot "$test_repo" wanted.txt)

    expect_failure "$output" "$finalizer" --repo "$test_repo" --message test -- wanted.txt
    assert_file_contains "$output" 'push remote 与 upstream remote 不一致' \
        'push-target mismatch diagnostic absent'
    assert_precommit_state_unchanged \
        "$test_repo" "$head_before" "$index_before" "$status_before" "$file_before"
}

test_initial_publish_success() {
    local case_dir=$tmp_root/initial-success
    local output=$case_dir/output.log
    local head committed_paths counts

    make_unborn_repo "$case_dir"
    printf 'first file\n' >"$test_repo/first.txt"
    printf 'second file\n' >"$test_repo/second.txt"

    expect_success "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message 'initial commit' -- first.txt second.txt

    head=$(git -C "$test_repo" rev-parse HEAD)
    assert_root_commit "$test_repo"
    assert_equal '1' "$(git -C "$test_repo" rev-list --count HEAD)" \
        'initial publish created more than one commit'
    committed_paths=$(git -C "$test_repo" diff-tree --root --no-commit-id --name-only -r HEAD)
    assert_equal $'first.txt\nsecond.txt' "$committed_paths" \
        'root commit did not contain the explicit files'
    assert_equal "$head" "$(git -C "$test_repo" rev-parse '@{upstream}')" \
        'initial upstream mismatch'
    assert_equal "$head" "$(remote_main "$test_remote")" 'initial remote mismatch'
    assert_equal "$head"$'\trefs/heads/main' "$(remote_refs "$test_remote")" \
        'initial remote contains unexpected refs'
    counts=$(git -C "$test_repo" rev-list --left-right --count 'HEAD...@{upstream}')
    assert_equal $'0\t0' "$counts" 'initial ahead/behind is not 0/0'
    assert_equal '' "$(status_snapshot "$test_repo")" 'initial success left worktree dirty'
    assert_equal '' "$(git -C "$test_repo" diff --cached --name-only)" \
        'initial success left staged changes'
}

test_initial_disables_follow_tags_from_global_config() {
    local case_dir=$tmp_root/initial-no-follow-tags
    local global_config=$case_dir/global.gitconfig
    local output=$case_dir/output.log
    local head
    local tag=initial-global-only

    make_unborn_repo "$case_dir"
    git config --file "$global_config" push.followTags true
    printf '#!/usr/bin/env bash\nset -euo pipefail\ngit -C %q tag -a -m follow-tags-fixture %q HEAD\n' \
        "$test_repo" "$tag" >"$test_repo/.git/hooks/post-commit"
    chmod 700 "$test_repo/.git/hooks/post-commit"
    printf 'first file\n' >"$test_repo/first.txt"

    expect_success "$output" env GIT_CONFIG_GLOBAL="$global_config" "$finalizer" \
        --initial-publish --remote origin --repo "$test_repo" \
        --message 'initial without followed tags' -- first.txt

    head=$(git -C "$test_repo" rev-parse HEAD)
    assert_equal "$head"$'\trefs/heads/main' "$(remote_refs "$test_remote")" \
        'initial push followed an annotated tag from global config'
    git -C "$test_repo" show-ref --verify --quiet "refs/tags/$tag" ||
        fail_assertion 'initial follow-tags fixture is missing locally'
    if git --git-dir="$test_remote" show-ref --verify --quiet "refs/tags/$tag"; then
        fail_assertion 'initial push created the local annotated tag remotely'
    fi
}

write_generated_snapshot_scope() {
    local repo=$1
    local schema_hash
    local schema_size

    mkdir -p -- "$repo/generated"
    printf 'initial readme\n' >"$repo/README.md"
    printf '{"type":"object"}\n' >"$repo/generated/schema.json"
    schema_hash=$(sha256sum "$repo/generated/schema.json" | awk '{print $1}')
    schema_size=$(stat -c '%s' "$repo/generated/schema.json")
    printf '{"file_count":1,"total_bytes":%s,"files":[{"path":"schema.json","bytes":%s,"sha256":"%s"}]}\n' \
        "$schema_size" "$schema_size" "$schema_hash" >"$repo/generated/manifest.json"
}

test_initial_snapshot_generated_coverage_success() {
    local case_dir=$tmp_root/initial-snapshot-success
    local state=$case_dir/state
    local output=$case_dir/output.log
    local snapshot head

    make_unborn_repo "$case_dir"
    write_generated_snapshot_scope "$test_repo"
    snapshot=$(make_generated_snapshot "$test_repo" "$state")

    expect_success "$output" env XDG_STATE_HOME="$state" "$finalizer" \
        --initial-publish --remote origin --repo "$test_repo" \
        --message 'snapshot initial' --snapshot "$snapshot" -- \
        README.md generated/manifest.json generated/schema.json

    assert_file_contains "$output" 'source=worktree covered=3/3' \
        'snapshot worktree verification was absent'
    assert_file_contains "$output" 'source=index covered=3/3' \
        'snapshot index verification was absent'
    assert_file_contains "$output" 'source=head covered=3/3' \
        'snapshot HEAD verification was absent'
    head=$(git -C "$test_repo" rev-parse HEAD)
    assert_root_commit "$test_repo"
    assert_equal "$head" "$(remote_main "$test_remote")" \
        'snapshot-bound initial remote mismatch'
    assert_equal '' "$(status_snapshot "$test_repo")" \
        'snapshot-bound initial publish left the worktree dirty'

    case_dir=$tmp_root/initial-snapshot-summary
    state=$case_dir/state
    output=$case_dir/output.json
    make_unborn_repo "$case_dir"
    write_generated_snapshot_scope "$test_repo"
    snapshot=$(make_generated_snapshot "$test_repo" "$state")
    expect_success "$output" env XDG_STATE_HOME="$state" "$finalizer" --summary \
        --initial-publish --remote origin --repo "$test_repo" \
        --message 'snapshot summary' --snapshot "$snapshot" -- \
        README.md generated/manifest.json generated/schema.json
    /usr/bin/python3 -B - "$output" <<'PY'
import json
from pathlib import Path
import sys

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if summary["mode_result"]["snapshot_verification"] != "passed":
    raise SystemExit("summary did not report completed snapshot verification")
PY
}

test_snapshot_verifier_disables_bytecode() {
    local case_dir=$tmp_root/snapshot-no-bytecode
    local state=$case_dir/state
    local bytecode_prefix=$case_dir/python-bytecode
    local output=$case_dir/output.log
    local snapshot

    make_unborn_repo "$case_dir"
    write_generated_snapshot_scope "$test_repo"
    snapshot=$(make_generated_snapshot "$test_repo" "$state")

    expect_success "$output" env -u PYTHONDONTWRITEBYTECODE \
        XDG_STATE_HOME="$state" PYTHONPYCACHEPREFIX="$bytecode_prefix" "$finalizer" \
        --initial-publish --remote origin --repo "$test_repo" \
        --message 'snapshot bytecode check' --snapshot "$snapshot" -- \
        README.md generated/manifest.json generated/schema.json

    [[ ! -e "$bytecode_prefix" ]] ||
        fail_assertion 'snapshot verifier wrote bytecode despite its process-local -B flag'
}

test_initial_snapshot_rejects_workspace_drift() {
    local case_dir=$tmp_root/initial-snapshot-drift
    local state=$case_dir/state
    local output=$case_dir/output.log
    local snapshot

    make_unborn_repo "$case_dir"
    write_generated_snapshot_scope "$test_repo"
    snapshot=$(make_generated_snapshot "$test_repo" "$state")
    printf 'drift\n' >>"$test_repo/README.md"

    expect_failure "$output" env XDG_STATE_HOME="$state" "$finalizer" \
        --initial-publish --remote origin --repo "$test_repo" \
        --message 'snapshot drift' --snapshot "$snapshot" -- \
        README.md generated/manifest.json generated/schema.json

    assert_file_contains "$output" 'snapshot evidence rejected' \
        'snapshot drift diagnostic absent'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" \
        'snapshot drift rejection changed the index'
    assert_equal '' "$(remote_refs "$test_remote")" \
        'snapshot drift rejection changed the remote'
}

test_initial_snapshot_rejects_uncovered_explicit_path() {
    local case_dir=$tmp_root/initial-snapshot-uncovered
    local state=$case_dir/state
    local output=$case_dir/output.log
    local snapshot

    make_unborn_repo "$case_dir"
    write_generated_snapshot_scope "$test_repo"
    snapshot=$(make_generated_snapshot "$test_repo" "$state")
    printf 'uncovered\n' >"$test_repo/extra.txt"

    expect_failure "$output" env XDG_STATE_HOME="$state" "$finalizer" \
        --initial-publish --remote origin --repo "$test_repo" \
        --message 'snapshot uncovered' --snapshot "$snapshot" -- \
        README.md extra.txt generated/manifest.json generated/schema.json

    assert_file_contains "$output" 'snapshot evidence rejected' \
        'uncovered publication path diagnostic absent'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" \
        'uncovered path rejection changed the index'
    assert_equal '' "$(remote_refs "$test_remote")" \
        'uncovered path rejection changed the remote'
}

test_initial_clone_created_upstream() {
    local case_dir=$tmp_root/initial-clone-upstream
    local dry_output=$case_dir/dry-run.log
    local output=$case_dir/output.log
    local status_before head counts

    make_cloned_unborn_repo "$case_dir"
    assert_equal 'origin' "$(git -C "$test_repo" config --get-all branch.main.remote)" \
        'clone did not configure the expected branch remote'
    assert_equal 'refs/heads/main' \
        "$(git -C "$test_repo" config --get-all branch.main.merge)" \
        'clone did not configure the expected merge ref'
    if git -C "$test_repo" rev-parse --verify '@{upstream}^{commit}' >/dev/null 2>&1; then
        fail_assertion 'empty-remote clone unexpectedly resolved its upstream commit'
    fi
    printf 'first file\n' >"$test_repo/first.txt"
    status_before=$(status_snapshot "$test_repo")

    expect_success "$dry_output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message 'initial clone' --dry-run -- first.txt
    assert_file_contains "$dry_output" '未执行 ls-remote' \
        'clone-created dry-run did not preserve the local-only contract'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" \
        'clone-created dry-run changed the index'
    assert_equal "$status_before" "$(status_snapshot "$test_repo")" \
        'clone-created dry-run changed the worktree'

    expect_success "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message 'initial clone' -- first.txt
    head=$(git -C "$test_repo" rev-parse HEAD)
    assert_root_commit "$test_repo"
    assert_equal '1' "$(git -C "$test_repo" rev-list --count HEAD)" \
        'clone-created initial publish created extra commits'
    assert_equal 'first.txt' \
        "$(git -C "$test_repo" diff-tree --root --no-commit-id --name-only -r HEAD)" \
        'clone-created root commit contained an unexpected path'
    assert_equal "$head" "$(git -C "$test_repo" rev-parse '@{upstream}')" \
        'clone-created upstream did not become resolved'
    assert_equal "$head" "$(remote_main "$test_remote")" \
        'clone-created remote OID mismatch'
    counts=$(git -C "$test_repo" rev-list --left-right --count 'HEAD...@{upstream}')
    assert_equal $'0\t0' "$counts" 'clone-created ahead/behind is not 0/0'
    assert_equal '' "$(status_snapshot "$test_repo")" \
        'clone-created initial publish left the worktree dirty'
}

assert_initial_upstream_conflict() {
    local case_dir=$1
    local output=$case_dir/output.log
    local status_before

    printf 'first file\n' >"$test_repo/first.txt"
    status_before=$(status_snapshot "$test_repo")
    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$output" 'upstream 配置' \
        'conflicting initial upstream diagnostic absent'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" \
        'conflicting initial upstream changed the index'
    assert_equal "$status_before" "$(status_snapshot "$test_repo")" \
        'conflicting initial upstream changed the worktree'
}

test_initial_rejects_upstream_conflicts() {
    local case_dir=$tmp_root/initial-upstream-conflicts
    local backup output status_before

    make_unborn_repo "$case_dir/missing-merge"
    git -C "$test_repo" config branch.main.remote origin
    assert_initial_upstream_conflict "$case_dir/missing-merge"

    make_unborn_repo "$case_dir/missing-remote"
    git -C "$test_repo" config branch.main.merge refs/heads/main
    assert_initial_upstream_conflict "$case_dir/missing-remote"

    make_unborn_repo "$case_dir/other-remote"
    backup=$case_dir/other-remote/backup.git
    git init --quiet --bare --initial-branch=main "$backup"
    git -C "$test_repo" remote add backup "$backup"
    git -C "$test_repo" config branch.main.remote backup
    git -C "$test_repo" config branch.main.merge refs/heads/main
    assert_initial_upstream_conflict "$case_dir/other-remote"

    make_unborn_repo "$case_dir/other-branch"
    git -C "$test_repo" config branch.main.remote origin
    git -C "$test_repo" config branch.main.merge refs/heads/other
    assert_initial_upstream_conflict "$case_dir/other-branch"

    make_unborn_repo "$case_dir/multiple-remote"
    git -C "$test_repo" config --add branch.main.remote origin
    git -C "$test_repo" config --add branch.main.remote origin
    git -C "$test_repo" config branch.main.merge refs/heads/main
    assert_initial_upstream_conflict "$case_dir/multiple-remote"

    make_unborn_repo "$case_dir/multiple-merge"
    git -C "$test_repo" config branch.main.remote origin
    git -C "$test_repo" config --add branch.main.merge refs/heads/main
    git -C "$test_repo" config --add branch.main.merge refs/heads/main
    assert_initial_upstream_conflict "$case_dir/multiple-merge"

    make_unborn_repo "$case_dir/empty-values"
    git -C "$test_repo" config branch.main.remote ''
    git -C "$test_repo" config branch.main.merge ''
    assert_initial_upstream_conflict "$case_dir/empty-values"

    make_unborn_repo "$case_dir/resolved"
    push_seed_ref "$case_dir/resolved-seed" "$test_remote" refs/heads/main
    git -C "$test_repo" config branch.main.remote origin
    git -C "$test_repo" config branch.main.merge refs/heads/main
    git -C "$test_repo" fetch --quiet origin \
        refs/heads/main:refs/remotes/origin/main
    git -C "$test_repo" rev-parse --verify '@{upstream}^{commit}' >/dev/null
    printf 'first file\n' >"$test_repo/first.txt"
    status_before=$(status_snapshot "$test_repo")
    output=$case_dir/resolved/output.log
    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$output" '已有可解析 commit' \
        'resolved initial upstream was not rejected'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" \
        'resolved initial upstream changed the index'
    assert_equal "$status_before" "$(status_snapshot "$test_repo")" \
        'resolved initial upstream changed the worktree'
}

test_initial_rejects_same_branch_remote() {
    local case_dir=$tmp_root/initial-remote-main
    local output=$case_dir/output.log
    local refs_before

    make_unborn_repo "$case_dir"
    push_seed_ref "$case_dir/seed" "$test_remote" refs/heads/main
    refs_before=$(remote_refs "$test_remote")
    printf 'first file\n' >"$test_repo/first.txt"

    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$output" '远端不是完全空 remote' \
        'same-branch remote was not rejected'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" 'same-branch rejection changed index'
    assert_equal "$refs_before" "$(remote_refs "$test_remote")" \
        'same-branch rejection changed remote'
}

test_initial_rejects_other_branch_remote() {
    local case_dir=$tmp_root/initial-remote-other
    local output=$case_dir/output.log
    local refs_before

    make_unborn_repo "$case_dir"
    push_seed_ref "$case_dir/seed" "$test_remote" refs/heads/other
    refs_before=$(remote_refs "$test_remote")
    printf 'first file\n' >"$test_repo/first.txt"

    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$output" '远端不是完全空 remote' \
        'other-branch remote was not rejected'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" 'other-branch rejection changed index'
    assert_equal "$refs_before" "$(remote_refs "$test_remote")" \
        'other-branch rejection changed remote'
}

test_initial_rejects_tag_only_remote() {
    local case_dir=$tmp_root/initial-remote-tag
    local output=$case_dir/output.log
    local refs_before

    make_unborn_repo "$case_dir"
    push_seed_ref "$case_dir/seed" "$test_remote" refs/tags/v0
    refs_before=$(remote_refs "$test_remote")
    printf 'first file\n' >"$test_repo/first.txt"

    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$output" '远端不是完全空 remote' 'tag-only remote was not rejected'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" 'tag-only rejection changed index'
    assert_equal "$refs_before" "$(remote_refs "$test_remote")" \
        'tag-only rejection changed remote'
}

test_initial_remote_configuration_validation() {
    local case_dir=$tmp_root/initial-remote-config
    local output
    local other_remote
    local endpoint_marker='PRIVATE_INITIAL_ENDPOINT_DO_NOT_PRINT'

    make_unborn_repo "$case_dir/missing"
    printf 'first file\n' >"$test_repo/first.txt"
    output=$case_dir/missing.log
    expect_failure "$output" "$finalizer" --initial-publish --remote missing \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$output" '未配置指定 remote' 'missing initial remote was not rejected'
    assert_unborn "$test_repo"

    make_unborn_repo "$case_dir/multi-fetch"
    other_remote=$case_dir/$endpoint_marker-fetch.git
    git init --quiet --bare --initial-branch=main "$other_remote"
    git -C "$test_repo" config --add remote.origin.url "$other_remote"
    printf 'first file\n' >"$test_repo/first.txt"
    output=$case_dir/multi-fetch.log
    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$output" '多个 fetch endpoint' \
        'multiple fetch endpoints were not rejected'
    assert_file_not_contains "$output" "$endpoint_marker" \
        'multiple-fetch diagnostic disclosed endpoint'
    assert_unborn "$test_repo"

    make_unborn_repo "$case_dir/multi-push"
    other_remote=$case_dir/$endpoint_marker-push.git
    git init --quiet --bare --initial-branch=main "$other_remote"
    git -C "$test_repo" config --add remote.origin.pushurl "$test_remote"
    git -C "$test_repo" config --add remote.origin.pushurl "$other_remote"
    printf 'first file\n' >"$test_repo/first.txt"
    output=$case_dir/multi-push.log
    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$output" '多个 push endpoint' \
        'multiple push endpoints were not rejected'
    assert_file_not_contains "$output" "$endpoint_marker" \
        'multiple-push diagnostic disclosed endpoint'
    assert_unborn "$test_repo"

    make_unborn_repo "$case_dir/mismatch"
    other_remote=$case_dir/$endpoint_marker-mismatch.git
    git init --quiet --bare --initial-branch=main "$other_remote"
    git -C "$test_repo" config remote.origin.pushurl "$other_remote"
    printf 'first file\n' >"$test_repo/first.txt"
    output=$case_dir/mismatch.log
    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$output" 'fetch 与 push endpoint 不一致' \
        'mismatched endpoints were not rejected'
    assert_file_not_contains "$output" "$endpoint_marker" \
        'endpoint-mismatch diagnostic disclosed endpoint'
    assert_unborn "$test_repo"

}

test_initial_rejects_non_unborn_repositories() {
    local case_dir=$tmp_root/initial-invalid-state
    local repo=$case_dir/repo
    local remote=$case_dir/remote.git
    local bare=$case_dir/bare.git
    local plain=$case_dir/plain
    local output
    local head_before

    mkdir -p -- "$case_dir" "$plain"
    git init --quiet --bare --initial-branch=main "$remote"
    git init --quiet --initial-branch=main "$repo"
    configure_author "$repo"
    printf 'base\n' >"$repo/first.txt"
    git -C "$repo" add -- first.txt
    git -C "$repo" commit --quiet -m base
    git -C "$repo" remote add origin "$remote"
    head_before=$(git -C "$repo" rev-parse HEAD)
    printf 'change\n' >>"$repo/first.txt"
    output=$case_dir/attached.log
    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$repo" --message test -- first.txt
    assert_file_contains "$output" 'unborn' 'attached initial mode diagnostic absent'
    assert_equal "$head_before" "$(git -C "$repo" rev-parse HEAD)" \
        'attached initial mode created a commit'

    git -C "$repo" checkout --quiet --detach
    output=$case_dir/detached.log
    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$repo" --message test -- first.txt
    assert_file_contains "$output" 'symbolic attached branch' \
        'detached initial mode diagnostic absent'
    assert_equal "$head_before" "$(git -C "$repo" rev-parse HEAD)" \
        'detached initial mode changed HEAD'

    git init --quiet --bare "$bare"
    printf 'plain\n' >"$plain/first.txt"
    output=$case_dir/bare.log
    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$bare" --message test -- first.txt
    assert_file_contains "$output" '不是 Git worktree' 'bare initial mode diagnostic absent'
    output=$case_dir/plain.log
    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$plain" --message test -- first.txt
    assert_file_contains "$output" '不是 Git worktree' 'non-Git initial mode diagnostic absent'
}

test_initial_rejects_existing_index() {
    local case_dir=$tmp_root/initial-index
    local output=$case_dir/output.log
    local index_before status_before

    make_unborn_repo "$case_dir"
    printf 'first file\n' >"$test_repo/first.txt"
    git -C "$test_repo" add -- first.txt
    index_before=$(index_snapshot "$test_repo")
    status_before=$(status_snapshot "$test_repo")

    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$output" '预先存在 index 或 staged 内容' \
        'pre-staged initial content was not rejected'
    assert_unborn "$test_repo"
    assert_equal "$index_before" "$(index_snapshot "$test_repo")" \
        'pre-staged rejection changed index'
    assert_equal "$status_before" "$(status_snapshot "$test_repo")" \
        'pre-staged rejection changed worktree state'
}

test_initial_rejects_out_of_scope_changes() {
    local case_dir=$tmp_root/initial-outside
    local output=$case_dir/output.log
    local status_before

    make_unborn_repo "$case_dir"
    printf 'first file\n' >"$test_repo/first.txt"
    printf 'outside file\n' >"$test_repo/outside.txt"
    status_before=$(status_snapshot "$test_repo")

    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$output" '显式范围外改动' \
        'out-of-scope initial file was not rejected'
    assert_file_contains "$output" 'outside.txt' 'out-of-scope path missing from diagnostic'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" 'out-of-scope rejection changed index'
    assert_equal "$status_before" "$(status_snapshot "$test_repo")" \
        'out-of-scope rejection changed worktree state'
}

test_initial_fixture_override() {
    local case_dir=$tmp_root/initial-fixture
    local default_output=$case_dir/default.log
    local mismatch_output=$case_dir/mismatch.log
    local success_output=$case_dir/success.log

    make_unborn_repo "$case_dir"
    mkdir -p -- "$test_repo/tests"
    printf 'github_%s%s\n' 'pat_' 'SYNTHETIC_ONLY_0123456789ABCDEF' \
        >"$test_repo/tests/allowed.txt"
    printf 'github_%s%s\n' 'pat_' 'SYNTHETIC_ONLY_ABCDEF0123456789' \
        >"$test_repo/tests/blocked.txt"

    expect_failure "$default_output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test -- tests/allowed.txt tests/blocked.txt
    assert_file_contains "$default_output" '高置信度 token/credential' \
        'initial fixture was not rejected by default'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" 'default fixture rejection changed index'

    expect_failure "$mismatch_output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test --allow-test-fixture tests/allowed.txt \
        -- tests/allowed.txt tests/blocked.txt
    assert_file_contains "$mismatch_output" 'tests/blocked.txt' \
        'initial fixture override was not exact'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" 'fixture mismatch changed index'

    rm -- "$test_repo/tests/blocked.txt"
    expect_success "$success_output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message 'allowed fixture' \
        --allow-test-fixture tests/allowed.txt -- tests/allowed.txt
    assert_root_commit "$test_repo"
    assert_equal 'tests/allowed.txt' \
        "$(git -C "$test_repo" diff-tree --root --no-commit-id --name-only -r HEAD)" \
        'initial exact fixture commit contained another file'
    assert_equal '' "$(status_snapshot "$test_repo")" \
        'initial fixture success left worktree dirty'
}

test_initial_large_binary_override() {
    local case_dir=$tmp_root/initial-large-binary
    local output=$case_dir/output.log

    make_unborn_repo "$case_dir"
    make_binary_file "$test_repo/original.bin" 6

    expect_success "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message 'initial large binary' \
        --allow-large-binary original.bin -- original.bin
    assert_root_commit "$test_repo"
    assert_equal 'original.bin' \
        "$(git -C "$test_repo" diff-tree --root --no-commit-id --name-only -r HEAD)" \
        'initial large binary commit contained another file'
    assert_equal '' "$(status_snapshot "$test_repo")" \
        'initial large binary success left worktree dirty'
}

test_initial_second_empty_check() {
    local case_dir=$tmp_root/initial-second-check
    local seed=$case_dir/seed
    local output=$case_dir/output.log
    local head

    make_unborn_repo "$case_dir"
    make_seed_repo "$seed"
    printf '#!/usr/bin/env bash\nset -euo pipefail\nunset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX\ngit -C %q push --quiet %q HEAD:refs/heads/other\n' \
        "$seed" "$test_remote" >"$test_repo/.git/hooks/post-commit"
    chmod 700 "$test_repo/.git/hooks/post-commit"
    printf 'first file\n' >"$test_repo/first.txt"

    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message 'initial race' -- first.txt
    head=$(git -C "$test_repo" rev-parse HEAD)
    assert_file_contains "$output" '远端不是完全空 remote' \
        'second empty check did not detect a new ref'
    assert_file_contains "$output" '不得重跑 --initial-publish' \
        'second-check failure omitted rerun warning'
    assert_file_contains "$output" '--resume-initial-publish' \
        'second-check failure omitted future resume command'
    assert_file_contains "$output" "$head" 'second-check failure omitted full root OID'
    assert_root_commit "$test_repo"
    assert_equal '1' "$(git -C "$test_repo" rev-list --count HEAD)" \
        'second-check failure retained more than one local commit'
    if git --git-dir="$test_remote" show-ref --verify --quiet refs/heads/main; then
        fail_assertion 'second-check failure pushed the target branch'
    fi
    git --git-dir="$test_remote" show-ref --verify --quiet refs/heads/other ||
        fail_assertion 'second-check competitor ref is absent'
}

test_initial_same_branch_push_race() {
    local case_dir=$tmp_root/initial-same-branch-race
    local seed=$case_dir/seed
    local output=$case_dir/output.log
    local head remote_head

    make_unborn_repo "$case_dir"
    make_seed_repo "$seed"
    printf '#!/usr/bin/env bash\nset -euo pipefail\nunset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX\ngit -C %q push --quiet %q HEAD:refs/heads/main\n' \
        "$seed" "$test_remote" >"$test_repo/.git/hooks/pre-push"
    chmod 700 "$test_repo/.git/hooks/pre-push"
    printf 'first file\n' >"$test_repo/first.txt"

    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message 'initial race' -- first.txt
    head=$(git -C "$test_repo" rev-parse HEAD)
    remote_head=$(remote_main "$test_remote")
    assert_file_contains "$output" 'initial push 失败或结果无法确认' \
        'same-branch race did not fail at non-force push'
    assert_file_contains "$output" '--resume-initial-publish' \
        'same-branch race omitted future resume command'
    assert_file_contains "$output" "$head" 'same-branch race omitted full root OID'
    assert_root_commit "$test_repo"
    assert_equal '1' "$(git -C "$test_repo" rev-list --count HEAD)" \
        'same-branch race retained more than one local commit'
    [[ "$head" != "$remote_head" ]] || fail_assertion 'same-branch race overwrote remote commit'
}

test_initial_other_ref_post_push_race() {
    local case_dir=$tmp_root/initial-other-ref-race
    local seed=$case_dir/seed
    local output=$case_dir/output.log
    local head

    make_unborn_repo "$case_dir"
    make_seed_repo "$seed"
    printf '#!/usr/bin/env bash\nset -euo pipefail\nunset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX\ngit -C %q push --quiet %q HEAD:refs/heads/other\n' \
        "$seed" "$test_remote" >"$test_repo/.git/hooks/pre-push"
    chmod 700 "$test_repo/.git/hooks/pre-push"
    printf 'first file\n' >"$test_repo/first.txt"

    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message 'initial race' -- first.txt
    head=$(git -C "$test_repo" rev-parse HEAD)
    assert_file_contains "$output" 'ref 集合不是唯一目标 branch' \
        'post-push verification missed an extra remote ref'
    assert_file_contains "$output" '--resume-initial-publish' \
        'post-push ref failure omitted future resume command'
    assert_root_commit "$test_repo"
    assert_equal "$head" "$(remote_main "$test_remote")" \
        'target branch was not published before post-push ref failure'
    git --git-dir="$test_remote" show-ref --verify --quiet refs/heads/other ||
        fail_assertion 'post-push competitor ref is absent'
    assert_equal '2' "$(remote_refs "$test_remote" | wc -l)" \
        'post-push race did not leave exactly two remote refs'
}

test_initial_push_failure_and_rerun() {
    local case_dir=$tmp_root/initial-push-failure
    local output=$case_dir/output.log
    local rerun_output=$case_dir/rerun.log
    local head

    make_unborn_repo "$case_dir"
    printf '#!/usr/bin/env bash\nexit 1\n' >"$test_remote/hooks/pre-receive"
    chmod 700 "$test_remote/hooks/pre-receive"
    printf 'first file\n' >"$test_repo/first.txt"

    expect_failure "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message 'rejected initial push' -- first.txt
    head=$(git -C "$test_repo" rev-parse HEAD)
    assert_file_contains "$output" 'initial push 失败或结果无法确认' \
        'initial push rejection diagnostic absent'
    assert_file_contains "$output" "$head" 'initial push failure omitted full root OID'
    assert_file_contains "$output" '--resume-initial-publish' \
        'initial push failure omitted future resume command'
    assert_root_commit "$test_repo"
    assert_equal '1' "$(git -C "$test_repo" rev-list --count HEAD)" \
        'initial push failure created more than one commit'
    assert_equal '' "$(remote_refs "$test_remote")" 'rejected initial push changed remote'

    expect_failure "$rerun_output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message 'must not repeat' -- first.txt
    assert_file_contains "$rerun_output" 'unborn' 'initial rerun was not rejected'
    assert_equal "$head" "$(git -C "$test_repo" rev-parse HEAD)" \
        'initial rerun changed HEAD'
    assert_equal '1' "$(git -C "$test_repo" rev-list --count HEAD)" \
        'initial rerun created another commit'
}

test_initial_dry_run() {
    local case_dir=$tmp_root/initial-dry-run
    local output=$case_dir/output.log
    local status_before metadata_before

    make_unborn_repo "$case_dir"
    git -C "$test_repo" remote set-url origin "$case_dir/does-not-exist.git"
    printf 'first file\n' >"$test_repo/first.txt"
    status_before=$(status_snapshot "$test_repo")
    metadata_before=$(git_metadata_snapshot "$test_repo")

    expect_success "$output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message test --dry-run -- first.txt
    assert_file_contains "$output" 'mode: initial-publish' 'initial dry-run mode absent'
    assert_file_contains "$output" 'remote: origin' 'initial dry-run remote absent'
    assert_file_contains "$output" '未执行 ls-remote' 'initial dry-run remote notice absent'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" 'initial dry-run changed index'
    assert_equal "$status_before" "$(status_snapshot "$test_repo")" \
        'initial dry-run changed worktree state'
    assert_equal "$metadata_before" "$(git_metadata_snapshot "$test_repo")" \
        'initial dry-run changed Git metadata'
}

test_initial_cli_validation() {
    local case_dir=$tmp_root/initial-cli
    local missing_output=$case_dir/missing.log
    local conflict_output=$case_dir/conflict.log
    local snapshot_conflict_output=$case_dir/snapshot-conflict.log
    local snapshot_invalid_output=$case_dir/snapshot-invalid.log
    local status_before

    make_unborn_repo "$case_dir"
    printf 'first file\n' >"$test_repo/first.txt"
    status_before=$(status_snapshot "$test_repo")

    expect_failure "$missing_output" "$finalizer" --initial-publish \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$missing_output" '必须指定 --remote' \
        'initial mode accepted missing --remote'
    expect_failure "$conflict_output" "$finalizer" --remote origin \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$conflict_output" '只允许与 --initial-publish' \
        'normal mode accepted --remote'
    expect_failure "$snapshot_conflict_output" "$finalizer" \
        --snapshot "$(printf 'a%.0s' {1..64})" \
        --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$snapshot_conflict_output" '只允许与 --initial-publish' \
        'normal mode accepted snapshot evidence'
    expect_failure "$snapshot_invalid_output" "$finalizer" --initial-publish --remote origin \
        --snapshot ABC123 --repo "$test_repo" --message test -- first.txt
    assert_file_contains "$snapshot_invalid_output" '64 位小写十六进制' \
        'initial mode accepted an invalid snapshot ID'
    assert_unborn "$test_repo"
    assert_equal '' "$(index_snapshot "$test_repo")" 'CLI validation changed index'
    assert_equal "$status_before" "$(status_snapshot "$test_repo")" \
        'CLI validation changed worktree state'
}

test_release_contract() {
    local case_dir=$tmp_root/release-contract
    local fake_bin=$case_dir/bin
    local git_probe=$case_dir/git-invoked
    local version_output=$case_dir/version.out
    local version_error=$case_dir/version.err
    local version_expected=$case_dir/version.expected
    local help_output=$case_dir/help.out

    mkdir -p -- "$fake_bin"
    printf '%s\n' '#!/usr/bin/env bash' ": >\"\$GIT_PROBE\"" 'exit 97' \
        >"$fake_bin/git"
    chmod 700 "$fake_bin/git"
    printf 'codex-git-finalize 0.9.2\n' >"$version_expected"

    GIT_PROBE="$git_probe" PATH="$fake_bin:$PATH" \
        "$finalizer" --version >"$version_output" 2>"$version_error"
    cmp -s -- "$version_expected" "$version_output" ||
        fail_assertion '--version output is not byte-exact'
    [[ ! -s "$version_error" ]] || fail_assertion '--version wrote to stderr'
    [[ ! -e "$git_probe" ]] || fail_assertion '--version invoked Git'

    expect_success "$help_output" "$finalizer" --help
    assert_file_contains "$help_output" \
        'codex-git-finalize --repo <absolute-repo>' 'normal mode missing from help'
    assert_file_contains "$help_output" '--mode commit-only' \
        'commit-only mode missing from help'
    assert_file_contains "$help_output" '--initial-commit-only' \
        'initial-commit-only mode missing from help'
    assert_file_contains "$help_output" '--mode verify-only' \
        'verify-only mode missing from help'
    assert_file_contains "$help_output" '--initial-publish' \
        'initial-publish mode missing from help'
    assert_file_contains "$help_output" '--initial-branch-publish' \
        'initial-branch-publish mode missing from help'
    assert_file_contains "$help_output" '--remote-branch' \
        'initial branch target option missing from help'
    assert_file_contains "$help_output" '--resume-initial-publish' \
        'resume-initial-publish mode missing from help'
    assert_file_contains "$help_output" '--resume-publish' \
        'existing-commit resume entry missing from help'
    assert_file_contains "$help_output" '--publish-existing-branch' \
        'existing clean branch first-publish entry missing from help'
    assert_file_contains "$help_output" '--retire-remote-branch' \
        'remote branch retirement entry missing from help'
    assert_file_contains "$help_output" '--allow-large-binary' \
        'large binary override missing from help'
    assert_file_contains "$help_output" '--snapshot' \
        'snapshot evidence option missing from help'
    assert_file_contains "$project_root/codex-git-finalize" \
        'readonly VERSION="0.9.2"' 'script version constant drifted'
    assert_file_contains "$project_root/README.md" "当前版本：\`0.9.2\`" \
        'README version drifted'
    [[ -f "$project_root/codex-git-finalize-snapshot-verify.py" ]] ||
        fail_assertion 'snapshot verifier companion is missing'
    assert_file_contains "$project_root/codex-git-finalize" \
        '/usr/bin/python3 -B "$snapshot_verifier"' \
        'snapshot verifier invocation does not disable bytecode writes'
}

run_case() {
    current_case=$1
    shift
    "$@"
    passed=$((passed + 1))
    printf 'ok - %s\n' "$current_case"
}

run_case 'missing upstream blocks before add' test_missing_upstream
run_case 'missing remote branch blocks before add' test_missing_remote_branch
run_case 'fetch failure blocks before add' test_fetch_failure
run_case 'behind blocks before add' test_behind
run_case 'diverged blocks before add' test_diverged
run_case 'local ahead remains supported' test_local_ahead
run_case 'synced success preserves out-of-scope changes' test_synced_success_scope
run_case 'default mode still pushes and verifies remote state' \
    test_default_mode_still_pushes_and_verifies
run_case 'commit-only creates one local commit without remote commands' \
    test_commit_only_success_without_remote_commands
run_case 'initial-commit-only creates one local root without remote commands' \
    test_initial_commit_only_success_without_remote_commands
run_case 'verify-only validates without local or remote Git side effects' \
    test_verify_only_success_without_git_side_effects
run_case 'verify-only validation failure remains non-mutating and nonzero' \
    test_verify_only_failure_is_non_mutating
run_case 'invalid explicit mode is rejected before mutation' test_invalid_mode_is_rejected
run_case 'normal push disables configured follow-tags' \
    test_normal_disables_follow_tags_from_local_config
run_case 'staged deletion preserves an ignored worktree copy' \
    test_staged_deletion_preserves_ignored_worktree_copy
run_case 'post-commit remote change is caught before push' test_post_commit_remote_change
run_case 'push failure keeps commit without remote output' \
    test_push_failure_keeps_commit_without_remote_output
run_case 'detached HEAD is rejected' test_detached
run_case 'unborn normal mode is rejected' test_unborn
run_case 'bare and non-Git repositories are rejected' test_invalid_repositories
run_case 'dry-run performs no remote or Git mutation' test_dry_run
run_case 'fixture override remains exact' test_fixture_scope
run_case 'large binary override remains exact and bounded' test_large_binary_scope
run_case 'push target mismatch is rejected' test_push_target_mismatch
run_case 'initial publish creates and verifies one root commit' test_initial_publish_success
run_case 'initial push disables configured follow-tags' \
    test_initial_disables_follow_tags_from_global_config
run_case 'initial snapshot accepts generated-manifest coverage' \
    test_initial_snapshot_generated_coverage_success
run_case 'snapshot verifier leaves no Python bytecode cache' \
    test_snapshot_verifier_disables_bytecode
run_case 'initial snapshot rejects workspace drift before staging' \
    test_initial_snapshot_rejects_workspace_drift
run_case 'initial snapshot rejects every uncovered explicit path' \
    test_initial_snapshot_rejects_uncovered_explicit_path
run_case 'initial publish accepts a clone-created unresolved upstream' \
    test_initial_clone_created_upstream
run_case 'initial publish rejects conflicting upstream configurations' \
    test_initial_rejects_upstream_conflicts
run_case 'initial publish rejects an existing target branch' \
    test_initial_rejects_same_branch_remote
run_case 'initial publish rejects an existing other branch' \
    test_initial_rejects_other_branch_remote
run_case 'initial publish rejects a tag-only remote' test_initial_rejects_tag_only_remote
run_case 'initial remote configuration is validated locally' \
    test_initial_remote_configuration_validation
run_case 'initial publish rejects attached detached bare and non-Git states' \
    test_initial_rejects_non_unborn_repositories
run_case 'initial publish rejects a pre-existing index' test_initial_rejects_existing_index
run_case 'initial publish rejects out-of-scope changes' \
    test_initial_rejects_out_of_scope_changes
run_case 'initial fixture override remains exact' test_initial_fixture_override
run_case 'initial large binary override remains exact and bounded' \
    test_initial_large_binary_override
run_case 'initial second empty check retains the root commit' \
    test_initial_second_empty_check
run_case 'initial non-force push rejects a same-branch race' \
    test_initial_same_branch_push_race
run_case 'initial post-push verification detects another ref' \
    test_initial_other_ref_post_push_race
run_case 'initial push failure and rerun retain one root commit' \
    test_initial_push_failure_and_rerun
run_case 'initial dry-run performs no remote or Git mutation' test_initial_dry_run
run_case 'initial CLI combinations are explicit' test_initial_cli_validation
run_case 'release version and public modes remain aligned' test_release_contract

printf 'all %s integration tests passed\n' "$passed"

bash "$project_root/tests/test_initial_branch_publish.sh"
bash "$project_root/tests/test_publish_existing_branch.sh"
bash "$project_root/tests/test_publish_existing_history.sh"
bash "$project_root/tests/test_resume_initial_publish.sh"
bash "$project_root/tests/test_resume_publish.sh"
bash "$project_root/tests/test_summary.sh"
bash "$project_root/tests/test_retire_remote_branch.sh"
