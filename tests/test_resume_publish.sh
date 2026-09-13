#!/usr/bin/env bash

set -Eeuo pipefail

export LC_ALL=C
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/git-finalize
tmp_root=$(mktemp -d /tmp/git-finalizer-resume-publish.XXXXXX)
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

assert_file_excludes() {
    local path=$1
    local text=$2
    local message=$3

    if grep -Fq -- "$text" "$path"; then
        fail_assertion "$message"
    fi
}

expect_success() {
    local output=$1
    shift

    if ! "$@" >"$output" 2>&1; then
        sed -n '1,240p' "$output" >&2
        fail_assertion 'command unexpectedly failed'
    fi
}

expect_failure() {
    local output=$1
    shift

    if "$@" >"$output" 2>&1; then
        sed -n '1,240p' "$output" >&2
        fail_assertion 'command unexpectedly succeeded'
    fi
}

configure_author() {
    local target_repo=$1

    git -C "$target_repo" config user.name 'Synthetic Resume Publisher'
    git -C "$target_repo" config user.email 'resume-publish@example.invalid'
    git -C "$target_repo" config commit.gpgsign false
}

make_published_repo() {
    local case_dir=$1

    test_repo=$case_dir/repo
    test_remote=$case_dir/remote.git
    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$test_remote"
    git init --quiet --initial-branch=main "$test_repo"
    configure_author "$test_repo"
    printf 'root\n' >"$test_repo/root.txt"
    git -C "$test_repo" add -- root.txt
    git -C "$test_repo" commit --quiet -m root
    printf 'published base\n' >"$test_repo/base.txt"
    git -C "$test_repo" add -- base.txt
    git -C "$test_repo" commit --quiet -m 'published base'
    git -C "$test_repo" remote add origin "$test_remote"
    git -C "$test_repo" push --quiet --set-upstream origin HEAD:refs/heads/main
    published_head=$(git -C "$test_repo" rev-parse HEAD)
}

make_ahead_commit() {
    local number=$1

    printf 'ahead %s\n' "$number" >"$test_repo/ahead-$number.txt"
    git -C "$test_repo" add -- "ahead-$number.txt"
    git -C "$test_repo" commit --quiet -m "ahead $number"
    expected_head=$(git -C "$test_repo" rev-parse HEAD)
}

resume_command() {
    "$finalizer" --resume-publish "$expected_head" --repo "$test_repo"
}

remote_head() {
    git --git-dir="$test_remote" rev-parse refs/heads/main
}

status_snapshot() {
    git -C "$1" status --porcelain=v1 --untracked-files=all
}

config_snapshot() {
    git -C "$1" config --local --list
}

tag_snapshot() {
    git -C "$1" for-each-ref --format='%(objectname)%09%(refname)' refs/tags
}

make_git_audit_wrapper() {
    local case_dir=$1

    audit_bin=$case_dir/audit-bin
    audit_log=$case_dir/git-audit.log
    mkdir -p -- "$audit_bin"
    # shellcheck disable=SC2016  # The generated wrapper expands these variables at runtime.
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'subcommand=$1' \
        'if [[ "$1" == "-C" ]]; then subcommand=$3; fi' \
        'printf '\''subcommand=%q '\'' "$subcommand" >>"$GIT_AUDIT_LOG"' \
        'printf '\''%q '\'' "$@" >>"$GIT_AUDIT_LOG"' \
        'printf '\''\n'\'' >>"$GIT_AUDIT_LOG"' \
        'exec /usr/bin/git "$@"' >"$audit_bin/git"
    chmod 700 "$audit_bin/git"
}

assert_audit_has_no_git_mutations() {
    if grep -Eq -- '^subcommand=(add|commit|tag|config) ' \
        "$audit_log"; then
        fail_assertion 'resume-publish invoked add, commit, tag, or config'
    fi
    if grep -Eq -- '^subcommand=push .*--force( |$)|^subcommand=push .*--force-with-lease( |$)' \
        "$audit_log"; then
        fail_assertion 'resume-publish invoked a force push'
    fi
}

make_remote_commit() {
    local case_dir=$1
    local peer=$case_dir/peer

    git clone --quiet "$test_remote" "$peer" 2>/dev/null
    configure_author "$peer"
    printf 'remote advance\n' >"$peer/remote.txt"
    git -C "$peer" add -- remote.txt
    git -C "$peer" commit --quiet -m 'remote advance'
    git -C "$peer" push --quiet origin HEAD:refs/heads/main
    remote_advanced_head=$(git -C "$peer" rev-parse HEAD)
}

test_ahead_one_publishes_without_local_mutation() {
    local case_dir=$tmp_root/ahead-one
    local output=$case_dir/output.log
    local head_before commit_count_before config_before tags_before

    make_published_repo "$case_dir"
    make_ahead_commit 1
    git -C "$test_repo" tag -a -m local-only local-only HEAD
    git -C "$test_repo" config push.followTags true
    head_before=$expected_head
    commit_count_before=$(git -C "$test_repo" rev-list --count HEAD)
    config_before=$(config_snapshot "$test_repo")
    tags_before=$(tag_snapshot "$test_repo")
    make_git_audit_wrapper "$case_dir"

    expect_success "$output" env GIT_AUDIT_LOG="$audit_log" \
        PATH="$audit_bin:$PATH" "$finalizer" --resume-publish "$expected_head" \
        --repo "$test_repo"

    assert_equal "$head_before" "$(git -C "$test_repo" rev-parse HEAD)" \
        'resume-publish changed HEAD'
    assert_equal "$commit_count_before" "$(git -C "$test_repo" rev-list --count HEAD)" \
        'resume-publish created a commit'
    assert_equal "$head_before" "$(remote_head)" 'remote target does not equal HEAD'
    assert_equal $'0\t0' \
        "$(git -C "$test_repo" rev-list --left-right --count 'HEAD...@{upstream}')" \
        'ahead/behind is not 0/0'
    assert_equal '' "$(status_snapshot "$test_repo")" 'worktree is not clean'
    assert_equal "$config_before" "$(config_snapshot "$test_repo")" \
        'local config changed'
    assert_equal "$tags_before" "$(tag_snapshot "$test_repo")" 'local tags changed'
    assert_equal '' \
        "$(git --git-dir="$test_remote" for-each-ref --format='%(refname)' refs/tags)" \
        'resume-publish unexpectedly published a tag'
    assert_file_contains "$output" 'pre-publish ahead/behind: 1/0' \
        'pre-publish counts are absent'
    assert_file_contains "$output" 'mode: resume-publish' \
        'actual resume publish mode is absent'
    assert_file_contains "$output" 'publish kind: existing_commits' \
        'actual publish kind is absent'
    assert_file_contains "$output" 'commit count: 1' 'commit count is absent'
    assert_file_contains "$output" 'range safety checks: passed' \
        'range checks did not run'
    assert_file_contains "$output" 'local branch, tags, and Git config: unchanged' \
        'post-verify invariants are absent'
    assert_file_contains "$output" 'post-verify: passed' \
        'post-verify result is absent'
    assert_equal '1' "$(grep -Ec -- '^subcommand=push ' "$audit_log")" \
        'resume-publish did not invoke exactly one push'
    assert_file_contains "$audit_log" \
        'push --no-follow-tags origin HEAD:refs/heads/main' \
        'resume-publish did not target the exact configured upstream ref'
    assert_audit_has_no_git_mutations
}

test_multiple_ahead_commits_publish_as_one_range() {
    local case_dir=$tmp_root/ahead-multiple
    local output=$case_dir/output.log
    local commit_count_before

    make_published_repo "$case_dir"
    make_ahead_commit 1
    make_ahead_commit 2
    make_ahead_commit 3
    commit_count_before=$(git -C "$test_repo" rev-list --count HEAD)

    expect_success "$output" resume_command

    assert_equal "$expected_head" "$(remote_head)" 'multi-commit range was not published'
    assert_equal "$commit_count_before" "$(git -C "$test_repo" rev-list --count HEAD)" \
        'multi-commit resume created another commit'
    assert_file_contains "$output" 'pre-publish ahead/behind: 3/0' \
        'multi-commit ahead count is absent'
    assert_file_contains "$output" 'commit count: 3' \
        'multi-commit publish count is absent'
}

test_merge_range_preserves_published_baseline_and_checks_new_content() {
    local kind case_dir output remote_before

    for kind in clean whitespace secret-history; do
        case_dir=$tmp_root/merge-$kind
        output=$case_dir/output.log
        make_published_repo "$case_dir"
        git -C "$test_repo" branch feature
        printf 'historical license\n\n' >"$test_repo/LICENSE"
        git -C "$test_repo" add -- LICENSE
        git -C "$test_repo" commit --quiet -m 'published historical baseline'
        git -C "$test_repo" push --quiet origin HEAD:refs/heads/main
        remote_before=$(remote_head)
        git -C "$test_repo" switch --quiet feature
        make_ahead_commit 1
        if [[ "$kind" == secret-history ]]; then
            printf 'synthetic fixture\n' >"$test_repo/.env"
            git -C "$test_repo" add -- .env
            git -C "$test_repo" commit --quiet -m 'unsafe intermediate path'
            git -C "$test_repo" rm --quiet -- .env
            git -C "$test_repo" commit --quiet -m 'remove unsafe path'
        fi
        git -C "$test_repo" switch --quiet main
        git -C "$test_repo" merge --quiet --no-ff --no-commit feature
        if [[ "$kind" == whitespace ]]; then
            printf 'new trailing whitespace \n' >"$test_repo/introduced.txt"
            git -C "$test_repo" add -- introduced.txt
        fi
        git -C "$test_repo" commit --quiet -m 'integrate feature'
        expected_head=$(git -C "$test_repo" rev-parse HEAD)

        if [[ "$kind" == clean ]]; then
            expect_success "$output" resume_command
            assert_equal "$expected_head" "$(remote_head)" 'merge was not published'
        else
            expect_failure "$output" resume_command
            assert_equal "$remote_before" "$(remote_head)" 'unsafe merge changed remote'
            if [[ "$kind" == whitespace ]]; then
                assert_file_contains "$output" 'whitespace' 'merge-introduced whitespace was accepted'
            else
                assert_file_contains "$output" '敏感文件' 'side-branch history was not scanned'
            fi
        fi
    done
}

test_ahead_zero_is_rejected() {
    local case_dir=$tmp_root/ahead-zero
    local output=$case_dir/output.log
    local remote_before

    make_published_repo "$case_dir"
    expected_head=$published_head
    remote_before=$(remote_head)
    expect_failure "$output" resume_command

    assert_file_contains "$output" '至少 ahead configured upstream 1 个 commit' \
        'ahead-zero rejection is absent'
    assert_equal "$remote_before" "$(remote_head)" 'ahead-zero changed remote'
}

test_dirty_and_staged_states_are_rejected() {
    local case_dir output remote_before

    case_dir=$tmp_root/dirty
    make_published_repo "$case_dir"
    make_ahead_commit 1
    remote_before=$(remote_head)
    printf 'dirty\n' >>"$test_repo/ahead-1.txt"
    output=$case_dir/output.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" 'worktree 完全干净' 'dirty worktree was accepted'
    assert_equal "$remote_before" "$(remote_head)" 'dirty rejection changed remote'

    case_dir=$tmp_root/staged
    make_published_repo "$case_dir"
    make_ahead_commit 1
    remote_before=$(remote_head)
    printf 'staged\n' >"$test_repo/staged.txt"
    git -C "$test_repo" add -- staged.txt
    output=$case_dir/output.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" 'index/staged 状态干净' 'staged state was accepted'
    assert_equal "$remote_before" "$(remote_head)" 'staged rejection changed remote'
}

test_missing_upstream_is_rejected() {
    local case_dir=$tmp_root/no-upstream
    local output=$case_dir/output.log

    make_published_repo "$case_dir"
    make_ahead_commit 1
    git -C "$test_repo" branch --unset-upstream
    expect_failure "$output" resume_command

    assert_file_contains "$output" 'upstream' 'missing upstream rejection is absent'
    assert_equal "$published_head" "$(remote_head)" 'missing-upstream case changed remote'
}

test_behind_and_diverged_are_rejected() {
    local case_dir output

    case_dir=$tmp_root/behind
    make_published_repo "$case_dir"
    expected_head=$published_head
    make_remote_commit "$case_dir"
    output=$case_dir/output.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" '本地落后 upstream' 'behind state was accepted'
    assert_equal "$remote_advanced_head" "$(remote_head)" 'behind rejection changed remote'

    case_dir=$tmp_root/diverged
    make_published_repo "$case_dir"
    make_ahead_commit 1
    make_remote_commit "$case_dir"
    output=$case_dir/output.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" '本地已与 upstream 分叉' 'diverged state was accepted'
    assert_equal "$remote_advanced_head" "$(remote_head)" 'diverged rejection changed remote'
}

test_push_failure_is_retryable_without_new_commit() {
    local case_dir=$tmp_root/push-failure
    local output=$case_dir/output.log
    local head_before commit_count_before

    make_published_repo "$case_dir"
    make_ahead_commit 1
    head_before=$expected_head
    commit_count_before=$(git -C "$test_repo" rev-list --count HEAD)
    printf '%s\n' '#!/usr/bin/env bash' 'exit 1' >"$test_remote/hooks/pre-receive"
    chmod 700 "$test_remote/hooks/pre-receive"

    expect_failure "$output" resume_command

    assert_file_contains "$output" '可安全再次运行同一恢复命令' \
        'push failure did not report a retryable resume'
    assert_file_contains "$output" '--resume-publish' \
        'push failure did not retain the generalized resume command'
    assert_equal "$head_before" "$(git -C "$test_repo" rev-parse HEAD)" \
        'push failure changed HEAD'
    assert_equal "$commit_count_before" "$(git -C "$test_repo" rev-list --count HEAD)" \
        'push failure created a commit'
    assert_equal "$published_head" "$(remote_head)" 'failed push changed remote target'
}

test_remote_post_verify_failure_is_reported() {
    local case_dir=$tmp_root/post-verify-failure
    local output=$case_dir/output.log
    local wrapper=$case_dir/bin/git
    local fetch_count=$case_dir/fetch-count
    local head_before

    make_published_repo "$case_dir"
    make_ahead_commit 1
    head_before=$expected_head
    mkdir -p -- "${wrapper%/*}"
    # shellcheck disable=SC2016  # The generated wrapper expands these variables at runtime.
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'subcommand=$1' \
        'if [[ "$1" == "-C" ]]; then subcommand=$3; fi' \
        'if [[ "$subcommand" == "fetch" ]]; then' \
        '    count=0' \
        '    if [[ -f "$FETCH_COUNT_FILE" ]]; then count=$(<"$FETCH_COUNT_FILE"); fi' \
        '    count=$((count + 1))' \
        '    printf '\''%s\n'\'' "$count" >"$FETCH_COUNT_FILE"' \
        '    if ((count >= 2)); then exit 88; fi' \
        'fi' \
        'exec /usr/bin/git "$@"' >"$wrapper"
    chmod 700 "$wrapper"

    expect_failure "$output" env FETCH_COUNT_FILE="$fetch_count" \
        PATH="${wrapper%/*}:$PATH" "$finalizer" --resume-publish "$expected_head" \
        --repo "$test_repo"

    assert_file_contains "$output" 'resume-publish 后 git fetch 失败' \
        'post-verify failure was not reported'
    assert_equal "$head_before" "$(git -C "$test_repo" rev-parse HEAD)" \
        'post-verify failure changed HEAD'
    assert_equal "$head_before" "$(remote_head)" \
        'fixture did not reach post-verify after a successful push'
}

test_entire_range_is_scanned_including_intermediate_commits() {
    local case_dir=$tmp_root/intermediate-sensitive
    local output=$case_dir/output.log

    make_published_repo "$case_dir"
    printf '%s%s\n' 'tok' 'en = "abcdefghijklmnop"' >"$test_repo/temporary.txt"
    git -C "$test_repo" add -- temporary.txt
    git -C "$test_repo" commit --quiet -m 'temporary unsafe content'
    git -C "$test_repo" rm --quiet temporary.txt
    git -C "$test_repo" commit --quiet -m 'remove temporary content'
    expected_head=$(git -C "$test_repo" rev-parse HEAD)

    expect_failure "$output" resume_command

    assert_file_contains "$output" '敏感文件' \
        'intermediate sensitive content was not scanned'
    assert_equal "$published_head" "$(remote_head)" \
        'unsafe intermediate range was published'
}

test_range_rejects_ignored_whitespace_and_abnormal_files() {
    local case_dir output

    case_dir=$tmp_root/ignored
    make_published_repo "$case_dir"
    printf '*.ignored\n' >"$test_repo/.gitignore"
    printf 'ignored\n' >"$test_repo/file.ignored"
    git -C "$test_repo" add -- .gitignore
    git -C "$test_repo" add -f -- file.ignored
    git -C "$test_repo" commit --quiet -m 'ignored file'
    expected_head=$(git -C "$test_repo" rev-parse HEAD)
    output=$case_dir/output.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" 'ignored 文件' 'ignored committed file was accepted'

    case_dir=$tmp_root/whitespace
    make_published_repo "$case_dir"
    printf 'trailing whitespace   \n' >"$test_repo/whitespace.txt"
    git -C "$test_repo" add -- whitespace.txt
    git -C "$test_repo" commit --quiet -m whitespace
    expected_head=$(git -C "$test_repo" rev-parse HEAD)
    output=$case_dir/output.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" 'whitespace 错误' 'whitespace error was accepted'

    case_dir=$tmp_root/abnormal
    make_published_repo "$case_dir"
    ln -s base.txt "$test_repo/link.txt"
    git -C "$test_repo" add -- link.txt
    git -C "$test_repo" commit --quiet -m symlink
    expected_head=$(git -C "$test_repo" rev-parse HEAD)
    output=$case_dir/output.log
    expect_failure "$output" resume_command
    assert_file_contains "$output" '非普通文件或异常对象' \
        'abnormal committed file was accepted'
}

test_range_rejects_large_binary() {
    local case_dir=$tmp_root/large-binary
    local output=$case_dir/output.log

    make_published_repo "$case_dir"
    dd if=/dev/zero of="$test_repo/large.bin" bs=1048576 count=6 status=none
    git -C "$test_repo" add -- large.bin
    git -C "$test_repo" commit --quiet -m 'large binary'
    expected_head=$(git -C "$test_repo" rev-parse HEAD)

    expect_failure "$output" resume_command

    assert_file_contains "$output" '大于 5 MiB' 'large committed binary was accepted'
    assert_equal "$published_head" "$(remote_head)" 'large binary range was published'
}

test_summary_reports_publish_contract() {
    local case_dir=$tmp_root/summary
    local output=$case_dir/summary.json
    local expected_range

    make_published_repo "$case_dir"
    make_ahead_commit 1
    expected_range="$published_head..$expected_head"
    expect_success "$output" "$finalizer" --summary --resume-publish \
        "$expected_head" --repo "$test_repo"

    python3 -B - "$output" "$expected_head" "$expected_range" <<'PY'
import json
import sys

path, expected_head, expected_range = sys.argv[1:]
with open(path, encoding="utf-8") as stream:
    result = json.load(stream)
assert result["mode"] == "resume"
assert result["status"] == "success"
assert result["commit"] == {"created": False, "sha": expected_head}
assert result["mode_result"]["interface"] == "resume-publish"
assert result["mode_result"]["publish_kind"] == "existing_commits"
assert result["mode_result"]["pre_publish"] == {
    "ahead": 1,
    "behind": 0,
    "commit_count": 1,
    "commit_range": expected_range,
}
assert result["mode_result"]["push_target"] == "origin:refs/heads/main"
assert result["mode_result"]["post_verify"] == "passed"
assert result["mode_result"]["final"] == {
    "head": expected_head,
    "remote_ref_oid": expected_head,
    "worktree": "clean",
}
assert result["push"]["executed"] is True
assert result["push"]["branch_refspec_only"] is True
assert result["push"]["follow_tags_requested"] is False
PY
}

test_cli_contract_rejects_conflicts() {
    local case_dir=$tmp_root/cli
    local output

    make_published_repo "$case_dir"
    make_ahead_commit 1

    output=$case_dir/remote.log
    expect_failure "$output" "$finalizer" --resume-publish "$expected_head" \
        --remote origin --repo "$test_repo"
    assert_file_contains "$output" '不接受 --remote' 'generic resume accepted --remote'

    output=$case_dir/files.log
    expect_failure "$output" "$finalizer" --resume-publish "$expected_head" \
        --repo "$test_repo" -- base.txt
    assert_file_contains "$output" '不接受文件列表' 'generic resume accepted file paths'

    output=$case_dir/root.log
    expect_failure "$output" "$finalizer" --resume-publish "$published_head" \
        --repo "$test_repo"
    assert_file_contains "$output" '当前 HEAD 与 expected HEAD 不一致' \
        'generic resume accepted a non-HEAD OID'
}

test_generic_resume_keeps_root_boundary_explicit() {
    local case_dir=$tmp_root/generic-root
    local output=$case_dir/output.log

    test_repo=$case_dir/repo
    test_remote=$case_dir/remote.git
    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$test_remote"
    git init --quiet --initial-branch=main "$test_repo"
    configure_author "$test_repo"
    printf 'root\n' >"$test_repo/root.txt"
    git -C "$test_repo" add -- root.txt
    git -C "$test_repo" commit --quiet -m root
    git -C "$test_repo" remote add origin "$test_remote"
    git -C "$test_repo" push --quiet --set-upstream origin HEAD:refs/heads/main
    expected_head=$(git -C "$test_repo" rev-parse HEAD)

    expect_failure "$output" resume_command

    assert_file_contains "$output" '要求非 root commit' \
        'generic resume did not retain the explicit root boundary'
    assert_file_contains "$output" '--resume-initial-publish' \
        'generic root rejection did not identify the compatible root entry'
    assert_equal "$expected_head" "$(remote_head)" 'generic root rejection changed remote'
}

run_case() {
    current_case=$1
    shift
    "$@"
    passed=$((passed + 1))
    printf 'ok - %s\n' "$current_case"
}

run_case 'resume-publish publishes one ahead commit without local Git mutation' \
    test_ahead_one_publishes_without_local_mutation
run_case 'resume-publish publishes multiple ahead commits as one validated range' \
    test_multiple_ahead_commits_publish_as_one_range
run_case 'resume-publish checks new merge content and side history without rescanning the published baseline' \
    test_merge_range_preserves_published_baseline_and_checks_new_content
run_case 'resume-publish rejects an ahead-zero clean branch' test_ahead_zero_is_rejected
run_case 'resume-publish rejects dirty and staged states' \
    test_dirty_and_staged_states_are_rejected
run_case 'resume-publish requires a configured upstream' test_missing_upstream_is_rejected
run_case 'resume-publish rejects behind and diverged branches' \
    test_behind_and_diverged_are_rejected
run_case 'resume-publish reports a rejected push as safely retryable' \
    test_push_failure_is_retryable_without_new_commit
run_case 'resume-publish reports remote post-verify failure' \
    test_remote_post_verify_failure_is_reported
run_case 'resume-publish scans unsafe intermediate commits' \
    test_entire_range_is_scanned_including_intermediate_commits
run_case 'resume-publish rejects ignored whitespace and abnormal files' \
    test_range_rejects_ignored_whitespace_and_abnormal_files
run_case 'resume-publish rejects committed large binaries' test_range_rejects_large_binary
run_case 'resume-publish emits a complete JSON publish summary' \
    test_summary_reports_publish_contract
run_case 'resume-publish CLI remains explicit and conflict-free' \
    test_cli_contract_rejects_conflicts
run_case 'resume-publish retains the legacy root-only boundary' \
    test_generic_resume_keeps_root_boundary_explicit

printf 'all %s existing-commit resume integration tests passed\n' "$passed"
