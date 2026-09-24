#!/usr/bin/env bash

set -Eeuo pipefail

export LC_ALL=C
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/git-finalize
tmp_root=$(mktemp -d /tmp/git-finalizer-publish-existing.XXXXXX)
current_case='startup'
passed=0
feature_branch='feat/publish-existing'

cleanup() {
    case "$tmp_root" in
        /tmp/git-finalizer-publish-existing.*) rm -rf -- "$tmp_root" ;;
        *) printf 'refusing unexpected cleanup target: %s\n' "$tmp_root" >&2 ;;
    esac
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

expect_failure() {
    local output=$1
    shift

    if "$@" >"$output" 2>&1; then
        sed -n '1,260p' "$output" >&2
        fail_assertion 'command unexpectedly succeeded'
    fi
}

expect_success() {
    local output=$1
    shift

    if ! "$@" >"$output" 2>&1; then
        sed -n '1,320p' "$output" >&2
        fail_assertion 'command unexpectedly failed'
    fi
}

configure_author() {
    local repo=$1

    git -C "$repo" config user.name 'Synthetic Existing Publisher'
    git -C "$repo" config user.email 'existing-publisher@example.invalid'
    git -C "$repo" config commit.gpgsign false
}

make_feature_repo() {
    local case_dir=$1

    test_repo=$case_dir/repo
    test_remote=$case_dir/remote.git
    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$test_remote"
    git init --quiet --initial-branch=main "$test_repo"
    configure_author "$test_repo"
    printf 'base\n' >"$test_repo/wanted.txt"
    git -C "$test_repo" add -- wanted.txt
    git -C "$test_repo" commit --quiet -m base
    git -C "$test_repo" remote add origin "$test_remote"
    git -C "$test_repo" push --quiet --set-upstream origin HEAD:refs/heads/main
    git -C "$test_repo" switch --quiet -c "$feature_branch"
    printf 'feature\n' >>"$test_repo/wanted.txt"
    git -C "$test_repo" add -- wanted.txt
    git -C "$test_repo" commit --quiet -m feature
    git -C "$test_repo" config --unset-all "branch.$feature_branch.remote" 2>/dev/null || true
    git -C "$test_repo" config --unset-all "branch.$feature_branch.merge" 2>/dev/null || true
}

expected_head() {
    git -C "$test_repo" rev-parse HEAD
}

remote_oid() {
    git --git-dir="$test_remote" rev-parse "$1"
}

remote_target_exists() {
    git --git-dir="$test_remote" show-ref --verify --quiet "refs/heads/$feature_branch"
}

publish_existing() {
    local output=$1
    shift
    local head

    head=$(expected_head)
    "$@" "$finalizer" --publish-existing-branch "$head" --remote origin \
        --remote-branch "$feature_branch" --repo "$test_repo" >"$output" 2>&1
}

assert_unchanged_local_history() {
    local head_before=$1
    local count_before=$2

    assert_equal "$head_before" "$(expected_head)" 'HEAD changed'
    assert_equal "$count_before" "$(git -C "$test_repo" rev-list --count HEAD)" \
        'commit count changed'
}

assert_published() {
    local head_before=$1
    local main_before=$2
    local upstream
    local counts

    upstream=$(git -C "$test_repo" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')
    counts=$(git -C "$test_repo" rev-list --left-right --count 'HEAD...@{upstream}')
    assert_equal "$head_before" "$(remote_oid "refs/heads/$feature_branch")" \
        'remote feature branch does not match HEAD'
    assert_equal "origin/$feature_branch" "$upstream" 'upstream is not the published branch'
    assert_equal $'0\t0' "$counts" 'ahead/behind is not 0/0'
    assert_equal "$main_before" "$(remote_oid refs/heads/main)" 'remote main changed'
    assert_equal '' "$(git -C "$test_repo" status --porcelain=v1 --untracked-files=all)" \
        'worktree is not clean'
    assert_equal '' "$(git -C "$test_repo" diff --cached --name-only)" \
        'index is not clean'
}

test_success_reuses_head_and_sets_upstream() {
    local case_dir=$tmp_root/success
    local output=$case_dir/output.log
    local head_before count_before main_before

    make_feature_repo "$case_dir"
    head_before=$(expected_head)
    count_before=$(git -C "$test_repo" rev-list --count HEAD)
    main_before=$(remote_oid refs/heads/main)

    expect_success "$output" publish_existing "$output" env

    assert_unchanged_local_history "$head_before" "$count_before"
    assert_published "$head_before" "$main_before"
}

test_push_is_non_force_branch_only() {
    local case_dir=$tmp_root/non-force
    local output=$case_dir/output.log
    local probe=$case_dir/probe.log
    local fake_bin=$case_dir/bin
    local push_line

    make_feature_repo "$case_dir"
    mkdir -p -- "$fake_bin"
    # Preserve variables for the generated probe script.
    # shellcheck disable=SC2016
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -Eeuo pipefail' \
        'printf "%q " "$@" >>"$GIT_PROBE"' \
        'printf "\n" >>"$GIT_PROBE"' \
        'exec /usr/bin/git "$@"' \
        >"$fake_bin/git"
    chmod 700 "$fake_bin/git"

    expect_success "$output" publish_existing "$output" \
        env GIT_PROBE="$probe" PATH="$fake_bin:$PATH"

    push_line=$(grep -E '(^| )push( |$)' "$probe")
    [[ "$push_line" == *'--no-follow-tags'* ]] ||
        fail_assertion 'push did not disable follow-tags'
    [[ "$push_line" == *'--set-upstream'* ]] ||
        fail_assertion 'push did not set upstream'
    [[ "$push_line" == *"HEAD:refs/heads/$feature_branch"* ]] ||
        fail_assertion 'push did not use the exact branch refspec'
    [[ "$push_line" != *'--force'* && "$push_line" != *'+HEAD'* ]] ||
        fail_assertion 'push unexpectedly used force semantics'
}

test_dirty_worktree_is_rejected() {
    local case_dir=$tmp_root/dirty
    local output=$case_dir/output.log
    local head_before count_before

    make_feature_repo "$case_dir"
    head_before=$(expected_head)
    count_before=$(git -C "$test_repo" rev-list --count HEAD)
    printf 'dirty\n' >>"$test_repo/wanted.txt"

    expect_failure "$output" publish_existing "$output" env

    assert_file_contains "$output" 'worktree 完全干净' 'dirty diagnostic is missing'
    assert_unchanged_local_history "$head_before" "$count_before"
    if remote_target_exists; then
        fail_assertion 'dirty preflight created the remote branch'
    fi
}

test_staged_index_is_rejected() {
    local case_dir=$tmp_root/staged
    local output=$case_dir/output.log
    local head_before count_before

    make_feature_repo "$case_dir"
    head_before=$(expected_head)
    count_before=$(git -C "$test_repo" rev-list --count HEAD)
    printf 'staged\n' >>"$test_repo/wanted.txt"
    git -C "$test_repo" add -- wanted.txt

    expect_failure "$output" publish_existing "$output" env

    assert_file_contains "$output" 'index/staged 状态干净' 'staged diagnostic is missing'
    assert_unchanged_local_history "$head_before" "$count_before"
    if remote_target_exists; then
        fail_assertion 'staged preflight created the remote branch'
    fi
}

test_detached_head_is_rejected() {
    local case_dir=$tmp_root/detached
    local output=$case_dir/output.log
    local head_before count_before

    make_feature_repo "$case_dir"
    git -C "$test_repo" checkout --quiet --detach
    head_before=$(expected_head)
    count_before=$(git -C "$test_repo" rev-list --count HEAD)

    expect_failure "$output" publish_existing "$output" env

    assert_file_contains "$output" 'symbolic attached branch' 'detached diagnostic is missing'
    assert_unchanged_local_history "$head_before" "$count_before"
}

test_protected_branch_is_rejected() {
    local case_dir=$tmp_root/protected
    local output=$case_dir/output.log

    make_feature_repo "$case_dir"
    git -C "$test_repo" switch --quiet main
    git -C "$test_repo" config --unset-all branch.main.remote 2>/dev/null || true
    git -C "$test_repo" config --unset-all branch.main.merge 2>/dev/null || true

    expect_failure "$output" "$finalizer" --publish-existing-branch \
        "$(expected_head)" --remote origin --remote-branch main --repo "$test_repo"

    assert_file_contains "$output" '拒绝受保护分支: main' \
        'protected-branch diagnostic is missing'
}

test_existing_remote_target_is_rejected() {
    local case_dir=$tmp_root/existing-target
    local output=$case_dir/output.log
    local head_before count_before

    make_feature_repo "$case_dir"
    head_before=$(expected_head)
    count_before=$(git -C "$test_repo" rev-list --count HEAD)
    git --git-dir="$test_remote" update-ref "refs/heads/$feature_branch" \
        "$(remote_oid refs/heads/main)"

    expect_failure "$output" publish_existing "$output" env

    assert_file_contains "$output" '远端目标 branch 已存在' \
        'existing-target diagnostic is missing'
    assert_unchanged_local_history "$head_before" "$count_before"
}

test_configured_upstream_is_rejected() {
    local case_dir=$tmp_root/upstream
    local output=$case_dir/output.log

    make_feature_repo "$case_dir"
    git -C "$test_repo" branch --set-upstream-to=origin/main

    expect_failure "$output" publish_existing "$output" env

    assert_file_contains "$output" '只接受尚未配置 upstream' \
        'configured-upstream diagnostic is missing'
    if remote_target_exists; then
        fail_assertion 'configured-upstream preflight created the remote branch'
    fi
}

test_push_failure_is_fail_closed() {
    local case_dir=$tmp_root/push-failure
    local output=$case_dir/output.log
    local head_before count_before

    make_feature_repo "$case_dir"
    head_before=$(expected_head)
    count_before=$(git -C "$test_repo" rev-list --count HEAD)
    printf '#!/usr/bin/env bash\nexit 1\n' >"$test_remote/hooks/pre-receive"
    chmod 700 "$test_remote/hooks/pre-receive"

    expect_failure "$output" publish_existing "$output" env

    assert_file_contains "$output" 'push 失败或结果无法确认' \
        'push-failure diagnostic is missing'
    assert_unchanged_local_history "$head_before" "$count_before"
    assert_equal '' "$(git -C "$test_repo" config --get "branch.$feature_branch.remote" || true)" \
        'failed push configured upstream'
    if remote_target_exists; then
        fail_assertion 'rejected push created the remote branch'
    fi
}

test_post_verify_failure_is_fail_closed() {
    local case_dir=$tmp_root/post-verify-failure
    local output=$case_dir/output.log
    local head_before count_before

    make_feature_repo "$case_dir"
    head_before=$(expected_head)
    count_before=$(git -C "$test_repo" rev-list --count HEAD)
    printf '#!/usr/bin/env bash\nprintf "post-push drift\\n" >>%q\n' \
        "$test_repo/wanted.txt" >"$test_remote/hooks/post-receive"
    chmod 700 "$test_remote/hooks/post-receive"

    expect_failure "$output" publish_existing "$output" env

    assert_file_contains "$output" 'worktree 在 existing-branch publish 期间发生变化' \
        'post-verify failure diagnostic is missing'
    assert_unchanged_local_history "$head_before" "$count_before"
    assert_equal "$head_before" "$(remote_oid "refs/heads/$feature_branch")" \
        'post-verify failure did not preserve the observed remote result'
}

test_summary_proves_no_commit_and_remote_verify() {
    local case_dir=$tmp_root/summary
    local output=$case_dir/output.json
    local head_before count_before

    make_feature_repo "$case_dir"
    head_before=$(expected_head)
    count_before=$(git -C "$test_repo" rev-list --count HEAD)

    expect_success "$output" "$finalizer" --summary --publish-existing-branch \
        "$head_before" --remote origin --remote-branch "$feature_branch" --repo "$test_repo"

    /usr/bin/python3 -B - "$output" "$head_before" "$feature_branch" <<'PY'
import json
from pathlib import Path
import sys

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
head = sys.argv[2]
branch = sys.argv[3]
assert data["finalizer_version"] == "1.4.0"
assert data["mode"] == "publish_existing_branch"
assert data["status"] == "success"
assert data["branch"] == branch
assert data["upstream"] == f"origin/{branch}"
assert data["requested_path_count"] == 0
assert data["commit"] == {"created": False, "sha": head}
assert data["push"] == {
    "executed": True,
    "result": "succeeded",
    "branch_refspec_only": True,
    "follow_tags_requested": False,
}
result = data["mode_result"]
assert result["interface"] == "publish-existing-branch"
assert result["commit_reused"] is True
assert result["remote_conclusion"] == "published_and_upstream_aligned"
assert result["post_verify"] == "passed"
assert result["head"] == {"before": head, "after": head, "unchanged": True}
assert result["index_unchanged"] is True
assert result["worktree"] == {
    "before": "clean",
    "after": "clean",
    "unchanged": True,
}
assert result["final"] == {"head": head, "remote_ref_oid": head}
PY
    assert_unchanged_local_history "$head_before" "$count_before"
}

run_case() {
    current_case=$1
    shift
    "$@"
    passed=$((passed + 1))
    printf 'ok - %s\n' "$current_case"
}

run_case 'clean existing branch publishes without a new commit' \
    test_success_reuses_head_and_sets_upstream
run_case 'first push is branch-only non-force and sets upstream' \
    test_push_is_non_force_branch_only
run_case 'dirty worktree is rejected' test_dirty_worktree_is_rejected
run_case 'staged index is rejected' test_staged_index_is_rejected
run_case 'detached HEAD is rejected' test_detached_head_is_rejected
run_case 'protected branch is rejected' test_protected_branch_is_rejected
run_case 'existing remote target is rejected' test_existing_remote_target_is_rejected
run_case 'configured upstream is rejected' test_configured_upstream_is_rejected
run_case 'push failure remains fail closed' test_push_failure_is_fail_closed
run_case 'post-verify failure remains fail closed' test_post_verify_failure_is_fail_closed
run_case 'summary proves reused HEAD and remote verification' \
    test_summary_proves_no_commit_and_remote_verify

printf 'all %s publish-existing-branch integration tests passed\n' "$passed"
