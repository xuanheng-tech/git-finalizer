#!/usr/bin/env bash

set -Eeuo pipefail

export LC_ALL=C
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/git-finalize
tmp_root=$(mktemp -d /tmp/git-finalizer-initial-branch.XXXXXX)
current_case='startup'
passed=0
feature_branch='feat/initial-branch-publish'

cleanup() {
    case "$tmp_root" in
        /tmp/git-finalizer-initial-branch.*) rm -rf -- "$tmp_root" ;;
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
        sed -n '1,220p' "$output" >&2
        fail_assertion 'command unexpectedly succeeded'
    fi
}

expect_success() {
    local output=$1
    shift

    if ! "$@" >"$output" 2>&1; then
        sed -n '1,260p' "$output" >&2
        fail_assertion 'command unexpectedly failed'
    fi
}

configure_author() {
    local repo=$1

    git -C "$repo" config user.name 'Synthetic Branch Publisher'
    git -C "$repo" config user.email 'branch-publisher@example.invalid'
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
    printf 'wanted base\n' >"$test_repo/wanted.txt"
    printf 'outside base\n' >"$test_repo/outside.txt"
    git -C "$test_repo" add -- wanted.txt outside.txt
    git -C "$test_repo" commit --quiet -m base
    git -C "$test_repo" remote add origin "$test_remote"
    git -C "$test_repo" push --quiet --set-upstream origin HEAD:refs/heads/main
    git -C "$test_repo" switch --quiet -c "$feature_branch"
}

publish_feature() {
    local output=$1
    shift

    "$@" "$finalizer" --initial-branch-publish --remote origin \
        --remote-branch "$feature_branch" --repo "$test_repo" \
        --message 'publish feature branch' -- wanted.txt >"$output" 2>&1
}

remote_oid() {
    git --git-dir="$1" rev-parse "$2"
}

remote_target_exists() {
    git --git-dir="$1" show-ref --verify --quiet "refs/heads/$feature_branch"
}

status_snapshot() {
    git -C "$1" status --porcelain=v1 --untracked-files=all
}

assert_precommit_unchanged() {
    local expected_head=$1
    local expected_index=$2
    local expected_status=$3

    assert_equal "$expected_head" "$(git -C "$test_repo" rev-parse HEAD)" 'HEAD changed'
    assert_equal "$expected_index" "$(git -C "$test_repo" ls-files --stage)" 'index changed'
    assert_equal "$expected_status" "$(status_snapshot "$test_repo")" 'worktree changed'
}

assert_published() {
    local main_before=$1
    local final_head
    local upstream
    local counts

    final_head=$(git -C "$test_repo" rev-parse HEAD)
    upstream=$(git -C "$test_repo" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')
    counts=$(git -C "$test_repo" rev-list --left-right --count 'HEAD...@{upstream}')
    assert_equal "$final_head" \
        "$(remote_oid "$test_remote" "refs/heads/$feature_branch")" \
        'remote feature branch does not match HEAD'
    assert_equal 'origin/'"$feature_branch" "$upstream" 'upstream did not target feature branch'
    assert_equal $'0\t0' "$counts" 'ahead/behind is not 0/0'
    assert_equal "$main_before" "$(remote_oid "$test_remote" refs/heads/main)" \
        'remote main changed'
    assert_equal '' "$(git -C "$test_repo" diff --cached --name-only)" \
        'publish left staged files'
    assert_equal '' "$(git -C "$test_repo" status --short -- wanted.txt)" \
        'explicit path is not clean'
}

test_existing_history_success() {
    local case_dir=$tmp_root/success
    local output=$case_dir/output.log
    local base main_before committed

    make_feature_repo "$case_dir"
    base=$(git -C "$test_repo" rev-parse HEAD)
    main_before=$(remote_oid "$test_remote" refs/heads/main)
    printf 'feature change\n' >>"$test_repo/wanted.txt"

    expect_success "$output" publish_feature "$output" env

    assert_equal "$base" "$(git -C "$test_repo" rev-parse 'HEAD^')" \
        'published commit has the wrong parent'
    committed=$(git -C "$test_repo" diff-tree --no-commit-id --name-only -r HEAD)
    assert_equal 'wanted.txt' "$committed" 'published commit has the wrong scope'
    assert_published "$main_before"
}

test_wrong_upstream_is_replaced() {
    local case_dir=$tmp_root/wrong-upstream
    local output=$case_dir/output.log
    local main_before

    make_feature_repo "$case_dir"
    git -C "$test_repo" branch --set-upstream-to=origin/main
    assert_equal 'origin/main' \
        "$(git -C "$test_repo" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')" \
        'wrong-upstream fixture was not established'
    main_before=$(remote_oid "$test_remote" refs/heads/main)
    printf 'feature change\n' >>"$test_repo/wanted.txt"

    expect_success "$output" publish_feature "$output" env

    assert_published "$main_before"
}

test_existing_remote_target_is_rejected() {
    local case_dir=$tmp_root/existing-target
    local output=$case_dir/output.log
    local head_before index_before status_before

    make_feature_repo "$case_dir"
    git --git-dir="$test_remote" update-ref "refs/heads/$feature_branch" \
        "$(remote_oid "$test_remote" refs/heads/main)"
    printf 'feature change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(git -C "$test_repo" ls-files --stage)
    status_before=$(status_snapshot "$test_repo")

    expect_failure "$output" "$finalizer" --initial-branch-publish --remote origin \
        --remote-branch "$feature_branch" --repo "$test_repo" --message rejected -- wanted.txt

    assert_file_contains "$output" '远端目标 branch 已存在' \
        'existing-target diagnostic is missing'
    assert_precommit_unchanged "$head_before" "$index_before" "$status_before"
}

test_protected_branch_is_rejected() {
    local case_dir=$tmp_root/protected
    local output=$case_dir/output.log
    local head_before index_before status_before

    make_feature_repo "$case_dir"
    git -C "$test_repo" switch --quiet main
    printf 'protected change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(git -C "$test_repo" ls-files --stage)
    status_before=$(status_snapshot "$test_repo")

    expect_failure "$output" "$finalizer" --initial-branch-publish --remote origin \
        --remote-branch main --repo "$test_repo" --message rejected -- wanted.txt

    assert_file_contains "$output" '拒绝受保护分支: main' \
        'protected-branch diagnostic is missing'
    assert_precommit_unchanged "$head_before" "$index_before" "$status_before"
}

test_detached_head_is_rejected() {
    local case_dir=$tmp_root/detached
    local output=$case_dir/output.log
    local head_before index_before status_before

    make_feature_repo "$case_dir"
    git -C "$test_repo" checkout --quiet --detach
    printf 'detached change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(git -C "$test_repo" ls-files --stage)
    status_before=$(status_snapshot "$test_repo")

    expect_failure "$output" "$finalizer" --initial-branch-publish --remote origin \
        --remote-branch "$feature_branch" --repo "$test_repo" --message rejected -- wanted.txt

    assert_file_contains "$output" '要求 symbolic attached branch' \
        'detached diagnostic is missing'
    assert_precommit_unchanged "$head_before" "$index_before" "$status_before"
}

test_mismatched_branch_name_is_rejected() {
    local case_dir=$tmp_root/mismatch
    local output=$case_dir/output.log
    local head_before index_before status_before

    make_feature_repo "$case_dir"
    printf 'feature change\n' >>"$test_repo/wanted.txt"
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    index_before=$(git -C "$test_repo" ls-files --stage)
    status_before=$(status_snapshot "$test_repo")

    expect_failure "$output" "$finalizer" --initial-branch-publish --remote origin \
        --remote-branch feat/other --repo "$test_repo" --message rejected -- wanted.txt

    assert_file_contains "$output" '必须与 --remote-branch 完全一致' \
        'branch-name mismatch diagnostic is missing'
    assert_precommit_unchanged "$head_before" "$index_before" "$status_before"
}

test_out_of_scope_change_is_preserved() {
    local case_dir=$tmp_root/out-of-scope
    local output=$case_dir/output.log
    local main_before committed

    make_feature_repo "$case_dir"
    main_before=$(remote_oid "$test_remote" refs/heads/main)
    printf 'feature change\n' >>"$test_repo/wanted.txt"
    printf 'unrelated change\n' >>"$test_repo/outside.txt"

    expect_success "$output" publish_feature "$output" env

    committed=$(git -C "$test_repo" diff-tree --no-commit-id --name-only -r HEAD)
    assert_equal 'wanted.txt' "$committed" 'out-of-scope file entered commit'
    assert_equal ' M outside.txt' "$(git -C "$test_repo" status --short -- outside.txt)" \
        'out-of-scope change was not preserved'
    assert_published "$main_before"
}

test_follow_tags_is_disabled() {
    local case_dir=$tmp_root/no-follow-tags
    local output=$case_dir/output.log
    local main_before tag=local-only-feature-tag

    make_feature_repo "$case_dir"
    main_before=$(remote_oid "$test_remote" refs/heads/main)
    git -C "$test_repo" config push.followTags true
    git -C "$test_repo" tag -a -m synthetic "$tag" HEAD
    printf 'feature change\n' >>"$test_repo/wanted.txt"

    expect_success "$output" publish_feature "$output" env

    assert_published "$main_before"
    git -C "$test_repo" show-ref --verify --quiet "refs/tags/$tag" ||
        fail_assertion 'local tag fixture disappeared'
    if git --git-dir="$test_remote" show-ref --verify --quiet "refs/tags/$tag"; then
        fail_assertion 'initial branch push followed a local tag'
    fi
}

test_summary_post_verify_contract() {
    local case_dir=$tmp_root/summary
    local output=$case_dir/output.json
    local main_before

    make_feature_repo "$case_dir"
    git -C "$test_repo" branch --set-upstream-to=origin/main
    main_before=$(remote_oid "$test_remote" refs/heads/main)
    printf 'feature change\n' >>"$test_repo/wanted.txt"

    expect_success "$output" "$finalizer" --summary --initial-branch-publish \
        --remote origin --remote-branch "$feature_branch" --repo "$test_repo" \
        --message summary -- wanted.txt

    /usr/bin/python3 -B - "$output" "$feature_branch" <<'PY'
import json
from pathlib import Path
import sys

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
branch = sys.argv[2]
assert data["finalizer_version"] == "1.0.0"
assert data["mode"] == "initial_branch"
assert data["status"] == "success"
assert data["branch"] == branch
assert data["upstream"] == f"origin/{branch}"
assert data["requested_path_count"] == 1
assert data["commit"]["created"] is True
assert data["push"] == {
    "executed": True,
    "result": "succeeded",
    "branch_refspec_only": True,
    "follow_tags_requested": False,
}
assert data["mode_result"] == {
    "initial_branch_publish": "verified",
    "remote_conclusion": "published_and_upstream_aligned",
    "post_verify": "passed",
}
PY
    assert_published "$main_before"
}

test_push_failure_reports_retained_state() {
    local case_dir=$tmp_root/push-failure
    local output=$case_dir/output.log
    local base count_before main_before

    make_feature_repo "$case_dir"
    git -C "$test_repo" branch --set-upstream-to=origin/main
    printf '#!/usr/bin/env bash\nexit 1\n' >"$test_remote/hooks/pre-receive"
    chmod 700 "$test_remote/hooks/pre-receive"
    base=$(git -C "$test_repo" rev-parse HEAD)
    count_before=$(git -C "$test_repo" rev-list --count HEAD)
    main_before=$(remote_oid "$test_remote" refs/heads/main)
    printf 'feature change\n' >>"$test_repo/wanted.txt"

    expect_failure "$output" "$finalizer" --initial-branch-publish --remote origin \
        --remote-branch "$feature_branch" --repo "$test_repo" \
        --message rejected -- wanted.txt

    assert_file_contains "$output" 'initial branch push 失败或结果无法确认' \
        'push-failure diagnostic is missing'
    assert_file_contains "$output" '本地提交' 'retained commit was not reported'
    assert_file_contains "$output" '当前 upstream: origin/main' \
        'actual upstream was not reported'
    assert_equal "$base" "$(git -C "$test_repo" rev-parse 'HEAD^')" \
        'retained commit has the wrong parent'
    assert_equal "$((count_before + 1))" "$(git -C "$test_repo" rev-list --count HEAD)" \
        'push failure did not retain exactly one commit'
    assert_equal "$main_before" "$(remote_oid "$test_remote" refs/heads/main)" \
        'push failure changed main'
    if remote_target_exists "$test_remote"; then
        fail_assertion 'rejected push created the feature branch'
    fi
}

test_post_commit_target_race_is_rejected() {
    local case_dir=$tmp_root/target-race
    local output=$case_dir/output.log
    local base main_before

    make_feature_repo "$case_dir"
    base=$(git -C "$test_repo" rev-parse HEAD)
    main_before=$(remote_oid "$test_remote" refs/heads/main)
    printf '#!/usr/bin/env bash\nset -euo pipefail\ngit --git-dir=%q update-ref %q %q\n' \
        "$test_remote" "refs/heads/$feature_branch" "$main_before" \
        >"$test_repo/.git/hooks/post-commit"
    chmod 700 "$test_repo/.git/hooks/post-commit"
    printf 'feature change\n' >>"$test_repo/wanted.txt"

    expect_failure "$output" "$finalizer" --initial-branch-publish --remote origin \
        --remote-branch "$feature_branch" --repo "$test_repo" --message race -- wanted.txt

    assert_file_contains "$output" '已在 push 前出现' \
        'post-commit target race was not reported'
    assert_equal "$base" "$(git -C "$test_repo" rev-parse 'HEAD^')" \
        'race did not retain the new local commit'
    assert_equal "$main_before" "$(remote_oid "$test_remote" refs/heads/main)" \
        'target race changed main'
    assert_equal "$main_before" \
        "$(remote_oid "$test_remote" "refs/heads/$feature_branch")" \
        'race fixture target has the wrong OID'
}

run_case() {
    current_case=$1
    shift
    "$@"
    passed=$((passed + 1))
    printf 'ok - %s\n' "$current_case"
}

run_case 'existing-history feature branch is first-published' test_existing_history_success
run_case 'wrong upstream is replaced by the explicit target' test_wrong_upstream_is_replaced
run_case 'existing remote target is rejected before staging' test_existing_remote_target_is_rejected
run_case 'protected main branch is rejected' test_protected_branch_is_rejected
run_case 'detached HEAD is rejected' test_detached_head_is_rejected
run_case 'mismatched target branch is rejected' test_mismatched_branch_name_is_rejected
run_case 'out-of-scope changes remain uncommitted' test_out_of_scope_change_is_preserved
run_case 'configured follow-tags does not publish a tag' test_follow_tags_is_disabled
run_case 'summary proves upstream and remote post-verify' test_summary_post_verify_contract
run_case 'push failure reports the retained real state' test_push_failure_reports_retained_state
run_case 'post-commit target creation is caught before push' test_post_commit_target_race_is_rejected

printf 'all %s initial-branch integration tests passed\n' "$passed"
