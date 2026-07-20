#!/usr/bin/env bash

set -Eeuo pipefail

export LC_ALL=C
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/codex-git-finalize
tmp_root=$(mktemp -d /tmp/git-finalizer-phase03.XXXXXX)
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

expect_failure() {
    local output=$1
    shift

    if "$@" >"$output" 2>&1; then
        sed -n '1,180p' "$output" >&2
        fail_assertion 'command unexpectedly succeeded'
    fi
}

expect_success() {
    local output=$1
    shift

    if ! "$@" >"$output" 2>&1; then
        sed -n '1,220p' "$output" >&2
        fail_assertion 'command unexpectedly failed'
    fi
}

configure_author() {
    local repo=$1

    git -C "$repo" config user.name 'Synthetic Resume Author'
    git -C "$repo" config user.email 'resume@example.invalid'
    git -C "$repo" config commit.gpgsign false
}

make_root_repo() {
    local case_dir=$1

    test_repo=$case_dir/repo
    test_remote=$case_dir/remote.git
    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$test_remote"
    git init --quiet --initial-branch=main "$test_repo"
    configure_author "$test_repo"
    printf 'root content\n' >"$test_repo/root.txt"
    git -C "$test_repo" add -- root.txt
    git -C "$test_repo" commit --quiet -m root
    git -C "$test_repo" remote add origin "$test_remote"
    expected_head=$(git -C "$test_repo" rev-parse HEAD)
}

make_seed_repo() {
    local destination=$1

    git init --quiet --initial-branch=main "$destination"
    configure_author "$destination"
    printf 'competitor\n' >"$destination/competitor.txt"
    git -C "$destination" add -- competitor.txt
    git -C "$destination" commit --quiet -m competitor
}

push_seed_ref() {
    local seed=$1
    local remote=$2
    local destination_ref=$3

    make_seed_repo "$seed"
    git -C "$seed" push --quiet "$remote" "HEAD:$destination_ref"
}

remote_refs() {
    git --git-dir="$1" for-each-ref --format='%(objectname)%09%(refname)'
}

status_snapshot() {
    git -C "$1" status --porcelain=v1 --untracked-files=all
}

assert_one_root_commit() {
    local repo=$1
    local head

    head=$(git -C "$repo" rev-parse HEAD)
    assert_equal "$head" "$(git -C "$repo" rev-list --parents -n 1 HEAD)" \
        'HEAD is not a root commit'
    assert_equal '1' "$(git -C "$repo" rev-list --count HEAD)" \
        'repository contains more than one commit'
}

resume_command() {
    "$finalizer" --resume-initial-publish "$expected_head" \
        --remote origin --repo "$test_repo"
}

test_resume_empty_remote() {
    local case_dir=$tmp_root/empty-remote
    local output=$case_dir/output.log

    make_root_repo "$case_dir"
    expect_success "$output" resume_command

    assert_one_root_commit "$test_repo"
    assert_equal "$expected_head" "$(git -C "$test_repo" rev-parse '@{upstream}')" \
        'resume did not establish upstream'
    assert_equal "$expected_head"$'\trefs/heads/main' "$(remote_refs "$test_remote")" \
        'resume remote refs are not exact'
    assert_equal $'0\t0' \
        "$(git -C "$test_repo" rev-list --left-right --count 'HEAD...@{upstream}')" \
        'resume ahead/behind is not 0/0'
    assert_equal '' "$(status_snapshot "$test_repo")" 'resume left worktree dirty'
    assert_equal '' "$(git -C "$test_repo" diff --cached --name-only)" \
        'resume left staged changes'
}

test_resume_expected_remote_without_upstream() {
    local case_dir=$tmp_root/expected-no-upstream
    local output=$case_dir/output.log

    make_root_repo "$case_dir"
    git -C "$test_repo" push --quiet origin HEAD:refs/heads/main
    if git -C "$test_repo" config --get branch.main.remote >/dev/null 2>&1; then
        fail_assertion 'test setup unexpectedly configured upstream'
    fi

    expect_success "$output" resume_command
    assert_equal "$expected_head" "$(git -C "$test_repo" rev-parse '@{upstream}')" \
        'state B recovery did not establish upstream'
    assert_equal "$expected_head"$'\trefs/heads/main' "$(remote_refs "$test_remote")" \
        'state B recovery changed remote refs'
    assert_one_root_commit "$test_repo"
}

test_resume_is_idempotent() {
    local case_dir=$tmp_root/idempotent
    local first_output=$case_dir/first.log
    local second_output=$case_dir/second.log
    local marker=$case_dir/pre-push.marker
    local refs_before

    make_root_repo "$case_dir"
    expect_success "$first_output" resume_command
    refs_before=$(remote_refs "$test_remote")
    printf '#!/usr/bin/env bash\nprintf reached >%q\nexit 1\n' \
        "$marker" >"$test_repo/.git/hooks/pre-push"
    chmod 700 "$test_repo/.git/hooks/pre-push"

    expect_success "$second_output" resume_command
    [[ ! -e "$marker" ]] || fail_assertion 'idempotent resume performed another push'
    assert_equal "$refs_before" "$(remote_refs "$test_remote")" \
        'idempotent resume changed remote refs'
    assert_one_root_commit "$test_repo"
}

test_resume_oid_validation() {
    local case_dir=$tmp_root/oid-validation
    local output
    local wrong_head

    make_root_repo "$case_dir"
    wrong_head=0${expected_head:1}
    if [[ "$wrong_head" == "$expected_head" ]]; then
        wrong_head=1${expected_head:1}
    fi

    output=$case_dir/abbreviated.log
    expect_failure "$output" "$finalizer" --resume-initial-publish "${expected_head:0:12}" \
        --remote origin --repo "$test_repo"
    assert_file_contains "$output" '完整小写十六进制 OID' 'abbreviated OID was accepted'

    output=$case_dir/wrong.log
    expect_failure "$output" "$finalizer" --resume-initial-publish "$wrong_head" \
        --remote origin --repo "$test_repo"
    assert_file_contains "$output" '当前 HEAD 与 expected HEAD 不一致' \
        'wrong full OID was accepted'

    output=$case_dir/nonhex.log
    expect_failure "$output" "$finalizer" --resume-initial-publish \
        zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz --remote origin --repo "$test_repo"
    assert_file_contains "$output" '完整小写十六进制 OID' 'non-hex OID was accepted'

    output=$case_dir/length.log
    expect_failure "$output" "$finalizer" --resume-initial-publish "${expected_head:0:39}" \
        --remote origin --repo "$test_repo"
    assert_file_contains "$output" '完整小写十六进制 OID' \
        'object-format length mismatch was accepted'
    assert_one_root_commit "$test_repo"
    assert_equal '' "$(remote_refs "$test_remote")" 'OID rejection changed remote'
}

test_resume_rejects_non_root_history() {
    local case_dir=$tmp_root/non-root
    local output=$case_dir/output.log

    make_root_repo "$case_dir"
    printf 'second\n' >"$test_repo/second.txt"
    git -C "$test_repo" add -- second.txt
    git -C "$test_repo" commit --quiet -m second
    expected_head=$(git -C "$test_repo" rev-parse HEAD)

    expect_failure "$output" resume_command
    assert_file_contains "$output" 'root commit' 'non-root HEAD was accepted'
    assert_equal '2' "$(git -C "$test_repo" rev-list --count HEAD)" \
        'non-root rejection changed history'
    assert_equal '' "$(remote_refs "$test_remote")" 'non-root rejection changed remote'
}

test_resume_rejects_dirty_states() {
    local case_dir=$tmp_root/dirty-states
    local output

    make_root_repo "$case_dir/tracked"
    printf 'dirty\n' >>"$test_repo/root.txt"
    output=$case_dir/tracked.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" 'worktree 完全干净' 'dirty tracked file was accepted'

    make_root_repo "$case_dir/staged"
    printf 'staged\n' >"$test_repo/staged.txt"
    git -C "$test_repo" add -- staged.txt
    output=$case_dir/staged.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" 'index/staged 状态干净' 'staged file was accepted'

    make_root_repo "$case_dir/untracked"
    printf 'untracked\n' >"$test_repo/untracked.txt"
    output=$case_dir/untracked.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" 'worktree 完全干净' 'untracked file was accepted'
}

test_resume_rejects_invalid_repositories() {
    local case_dir=$tmp_root/invalid-repositories
    local output
    local unborn=$case_dir/unborn
    local unborn_remote=$case_dir/unborn.git
    local bare=$case_dir/bare.git
    local plain=$case_dir/plain

    make_root_repo "$case_dir/detached"
    git -C "$test_repo" checkout --quiet --detach
    output=$case_dir/detached.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" 'symbolic attached branch' 'detached HEAD was accepted'

    git init --quiet --bare --initial-branch=main "$unborn_remote"
    git init --quiet --initial-branch=main "$unborn"
    git -C "$unborn" remote add origin "$unborn_remote"
    output=$case_dir/unborn.log
    expect_failure "$output" "$finalizer" --resume-initial-publish \
        0000000000000000000000000000000000000000 --remote origin --repo "$unborn"
    assert_file_contains "$output" 'HEAD 无法解析为 commit' 'unborn HEAD was accepted'

    mkdir -p -- "$plain"
    git init --quiet --bare "$bare"
    output=$case_dir/bare.log
    expect_failure "$output" "$finalizer" --resume-initial-publish \
        0000000000000000000000000000000000000000 --remote origin --repo "$bare"
    assert_file_contains "$output" '不是 Git worktree' 'bare repository was accepted'
    output=$case_dir/plain.log
    expect_failure "$output" "$finalizer" --resume-initial-publish \
        0000000000000000000000000000000000000000 --remote origin --repo "$plain"
    assert_file_contains "$output" '不是 Git worktree' 'non-Git directory was accepted'
}

test_resume_remote_configuration_validation() {
    local case_dir=$tmp_root/remote-config
    local output
    local other
    local endpoint_marker='PRIVATE_RESUME_ENDPOINT_DO_NOT_PRINT'

    make_root_repo "$case_dir/missing"
    output=$case_dir/missing.log
    expect_failure "$output" "$finalizer" --resume-initial-publish "$expected_head" \
        --remote missing --repo "$test_repo"
    assert_file_contains "$output" '未配置指定 remote' 'missing remote was accepted'

    make_root_repo "$case_dir/multi-fetch"
    other=$case_dir/$endpoint_marker-fetch.git
    git init --quiet --bare --initial-branch=main "$other"
    git -C "$test_repo" config --add remote.origin.url "$other"
    output=$case_dir/multi-fetch.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" '多个 fetch endpoint' 'multiple fetch URLs were accepted'
    if grep -Fq -- "$endpoint_marker" "$output"; then
        fail_assertion 'fetch endpoint leaked in diagnostic'
    fi

    make_root_repo "$case_dir/multi-push"
    other=$case_dir/$endpoint_marker-push.git
    git init --quiet --bare --initial-branch=main "$other"
    git -C "$test_repo" config --add remote.origin.pushurl "$test_remote"
    git -C "$test_repo" config --add remote.origin.pushurl "$other"
    output=$case_dir/multi-push.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" '多个 push endpoint' 'multiple push URLs were accepted'
    if grep -Fq -- "$endpoint_marker" "$output"; then
        fail_assertion 'push endpoint leaked in diagnostic'
    fi

    make_root_repo "$case_dir/mismatch"
    other=$case_dir/$endpoint_marker-mismatch.git
    git init --quiet --bare --initial-branch=main "$other"
    git -C "$test_repo" config remote.origin.pushurl "$other"
    output=$case_dir/mismatch.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" 'fetch 与 push endpoint 不一致' \
        'mismatched endpoint was accepted'
    if grep -Fq -- "$endpoint_marker" "$output"; then
        fail_assertion 'mismatched endpoint leaked in diagnostic'
    fi
}

test_resume_rejects_mismatched_upstream() {
    local case_dir=$tmp_root/upstream-mismatch
    local output
    local backup

    make_root_repo "$case_dir/remote"
    backup=$case_dir/remote/backup.git
    git init --quiet --bare --initial-branch=main "$backup"
    git -C "$test_repo" remote add backup "$backup"
    git -C "$test_repo" push --quiet backup HEAD:refs/heads/main
    git -C "$test_repo" config branch.main.remote backup
    git -C "$test_repo" config branch.main.merge refs/heads/main
    output=$case_dir/other-remote.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" 'selected remote/branch 不一致' \
        'upstream on another remote was accepted'

    make_root_repo "$case_dir/branch"
    git -C "$test_repo" push --quiet origin HEAD:refs/heads/other
    git -C "$test_repo" config branch.main.remote origin
    git -C "$test_repo" config branch.main.merge refs/heads/other
    output=$case_dir/other-branch.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" 'selected remote/branch 不一致' \
        'upstream on another branch was accepted'
}

test_resume_rejects_remote_state_c() {
    local case_dir=$tmp_root/remote-state-c
    local output refs_before

    make_root_repo "$case_dir/other-oid"
    push_seed_ref "$case_dir/other-oid-seed" "$test_remote" refs/heads/main
    refs_before=$(remote_refs "$test_remote")
    output=$case_dir/other-oid.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" '其他 OID' 'different target OID was accepted'
    assert_equal "$refs_before" "$(remote_refs "$test_remote")" \
        'different target OID was modified'

    make_root_repo "$case_dir/other-branch"
    push_seed_ref "$case_dir/other-branch-seed" "$test_remote" refs/heads/other
    refs_before=$(remote_refs "$test_remote")
    output=$case_dir/other-branch.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" '非目标 ref' 'other branch was accepted'
    assert_equal "$refs_before" "$(remote_refs "$test_remote")" 'other branch was modified'

    make_root_repo "$case_dir/tag"
    push_seed_ref "$case_dir/tag-seed" "$test_remote" refs/tags/v0
    refs_before=$(remote_refs "$test_remote")
    output=$case_dir/tag.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" '非目标 ref' 'tag-only remote was accepted'
    assert_equal "$refs_before" "$(remote_refs "$test_remote")" 'tag ref was modified'

    make_root_repo "$case_dir/extra"
    git -C "$test_repo" push --quiet origin HEAD:refs/heads/main
    push_seed_ref "$case_dir/extra-seed" "$test_remote" refs/heads/other
    refs_before=$(remote_refs "$test_remote")
    output=$case_dir/extra.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" '额外或含糊' 'expected branch plus extra ref was accepted'
    assert_equal "$refs_before" "$(remote_refs "$test_remote")" 'extra refs were modified'
}

test_resume_push_failure_stays_retryable() {
    local case_dir=$tmp_root/push-failure
    local output=$case_dir/output.log

    make_root_repo "$case_dir"
    printf '#!/usr/bin/env bash\nexit 1\n' >"$test_remote/hooks/pre-receive"
    chmod 700 "$test_remote/hooks/pre-receive"

    expect_failure "$output" resume_command
    assert_file_contains "$output" '远端仍为空' 'empty remote push failure was misclassified'
    assert_file_contains "$output" '--resume-initial-publish' \
        'retryable push failure omitted resume command'
    assert_one_root_commit "$test_repo"
    assert_equal "$expected_head" "$(git -C "$test_repo" rev-parse HEAD)" \
        'push failure changed HEAD'
    assert_equal '' "$(remote_refs "$test_remote")" 'failed push changed remote'
}

test_resume_recovers_uncertain_push() {
    local case_dir=$tmp_root/uncertain-push
    local output=$case_dir/output.log
    local hook

    make_root_repo "$case_dir"
    hook=$test_repo/.git/hooks/pre-push
    printf '#!/usr/bin/env bash\nset -euo pipefail\nunset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX\nrm -- %q\ngit -C %q push --quiet --no-verify origin HEAD:refs/heads/main\nexit 1\n' \
        "$hook" "$test_repo" >"$hook"
    chmod 700 "$hook"

    expect_success "$output" resume_command
    assert_file_contains "$output" 'Resume up-to-date push' \
        'uncertain push did not enter bounded state-B closure'
    assert_one_root_commit "$test_repo"
    assert_equal "$expected_head" "$(git -C "$test_repo" rev-parse '@{upstream}')" \
        'uncertain push recovery did not establish upstream'
    assert_equal "$expected_head"$'\trefs/heads/main' "$(remote_refs "$test_remote")" \
        'uncertain push recovery remote mismatch'
}

test_resume_rejects_cli_conflicts() {
    local case_dir=$tmp_root/cli-conflicts
    local output
    local status_before

    make_root_repo "$case_dir"
    git -C "$test_repo" remote set-url origin "$case_dir/does-not-exist.git"
    status_before=$(status_snapshot "$test_repo")

    output=$case_dir/message.log
    expect_failure "$output" "$finalizer" --resume-initial-publish "$expected_head" \
        --remote origin --repo "$test_repo" --message no
    assert_file_contains "$output" '不接受 --message' 'resume accepted --message'
    output=$case_dir/files.log
    expect_failure "$output" "$finalizer" --resume-initial-publish "$expected_head" \
        --remote origin --repo "$test_repo" -- root.txt
    assert_file_contains "$output" '不接受文件列表' 'resume accepted file list'
    output=$case_dir/fixture.log
    expect_failure "$output" "$finalizer" --resume-initial-publish "$expected_head" \
        --remote origin --repo "$test_repo" --allow-test-fixture tests/fake
    assert_file_contains "$output" '不接受 --allow-test-fixture' \
        'resume accepted fixture override'
    output=$case_dir/initial.log
    expect_failure "$output" "$finalizer" --resume-initial-publish "$expected_head" \
        --initial-publish --remote origin --repo "$test_repo"
    assert_file_contains "$output" '不接受 --initial-publish' 'resume accepted initial mode'
    output=$case_dir/dry.log
    expect_failure "$output" "$finalizer" --resume-initial-publish "$expected_head" \
        --remote origin --repo "$test_repo" --dry-run
    assert_file_contains "$output" '不支持 --dry-run' 'resume accepted dry-run'
    output=$case_dir/remote.log
    expect_failure "$output" "$finalizer" --resume-initial-publish "$expected_head" \
        --repo "$test_repo"
    assert_file_contains "$output" '必须指定 --remote' 'resume accepted missing remote'

    assert_equal "$expected_head" "$(git -C "$test_repo" rev-parse HEAD)" \
        'CLI conflict changed HEAD'
    assert_equal "$status_before" "$(status_snapshot "$test_repo")" \
        'CLI conflict changed worktree'
}

test_resume_rejects_incomplete_operation() {
    local case_dir=$tmp_root/incomplete-operation
    local output=$case_dir/output.log

    make_root_repo "$case_dir"
    printf '%s\n' "$expected_head" >"$test_repo/.git/MERGE_HEAD"
    expect_failure "$output" resume_command
    assert_file_contains "$output" '未完成的 Git 操作' 'incomplete operation was accepted'
    assert_equal "$expected_head" "$(git -C "$test_repo" rev-parse HEAD)" \
        'incomplete-operation rejection changed HEAD'
    assert_equal '' "$(remote_refs "$test_remote")" \
        'incomplete-operation rejection changed remote'
}

run_case() {
    current_case=$1
    shift
    "$@"
    passed=$((passed + 1))
    printf 'ok - %s\n' "$current_case"
}

run_case 'resume publishes one root commit to an empty remote' test_resume_empty_remote
run_case 'resume closes state B without an upstream' test_resume_expected_remote_without_upstream
run_case 'resume is idempotent when state B and upstream are aligned' test_resume_is_idempotent
run_case 'resume validates a full object-format OID' test_resume_oid_validation
run_case 'resume rejects non-root history' test_resume_rejects_non_root_history
run_case 'resume rejects dirty staged and untracked states' test_resume_rejects_dirty_states
run_case 'resume rejects detached unborn bare and non-Git states' \
    test_resume_rejects_invalid_repositories
run_case 'resume validates remote endpoints without disclosure' \
    test_resume_remote_configuration_validation
run_case 'resume rejects a mismatched upstream' test_resume_rejects_mismatched_upstream
run_case 'resume rejects all state-C remote shapes' test_resume_rejects_remote_state_c
run_case 'resume keeps an empty-remote push failure retryable' \
    test_resume_push_failure_stays_retryable
run_case 'resume closes an uncertain accepted push without another commit' \
    test_resume_recovers_uncertain_push
run_case 'resume CLI conflicts fail before remote access' test_resume_rejects_cli_conflicts
run_case 'resume rejects an incomplete Git operation' test_resume_rejects_incomplete_operation

printf 'all %s resume integration tests passed\n' "$passed"
