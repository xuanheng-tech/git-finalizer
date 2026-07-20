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

remote_main() {
    git --git-dir="$1" rev-parse refs/heads/main
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
run_case 'post-commit remote change is caught before push' test_post_commit_remote_change
run_case 'push failure keeps commit without remote output' \
    test_push_failure_keeps_commit_without_remote_output
run_case 'detached HEAD is rejected' test_detached
run_case 'unborn normal mode is rejected' test_unborn
run_case 'bare and non-Git repositories are rejected' test_invalid_repositories
run_case 'dry-run performs no remote or Git mutation' test_dry_run
run_case 'fixture override remains exact' test_fixture_scope
run_case 'push target mismatch is rejected' test_push_target_mismatch

printf 'all %s integration tests passed\n' "$passed"
