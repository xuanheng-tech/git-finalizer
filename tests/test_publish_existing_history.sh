#!/usr/bin/env bash

set -Eeuo pipefail

export LC_ALL=C
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/codex-git-finalize
tmp_root=$(mktemp -d /tmp/git-finalizer-publish-history.XXXXXX)
current_case='startup'
passed=0

cleanup() {
    case "$tmp_root" in
        /tmp/git-finalizer-publish-history.*) rm -rf -- "$tmp_root" ;;
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

    git -C "$repo" config user.name 'Synthetic History Publisher'
    git -C "$repo" config user.email 'history-publisher@example.invalid'
    git -C "$repo" config commit.gpgsign false
}

make_repo() {
    local case_dir=$1
    local commit_count=${2:-1}
    local index

    test_repo=$case_dir/repo
    test_remote=$case_dir/remote.git
    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$test_remote"
    git init --quiet --initial-branch=main "$test_repo"
    configure_author "$test_repo"
    for ((index = 1; index <= commit_count; index++)); do
        printf 'history %s\n' "$index" >>"$test_repo/history.txt"
        git -C "$test_repo" add -- history.txt
        git -C "$test_repo" commit --quiet -m "history $index"
    done
    git -C "$test_repo" remote add origin "$test_remote"
    expected_head=$(git -C "$test_repo" rev-parse HEAD)
    expected_count=$(git -C "$test_repo" rev-list --count HEAD)
}

publish_history() {
    "$finalizer" --publish-existing-history "$expected_head" \
        --remote origin --remote-branch main --repo "$test_repo"
}

resume_history() {
    "$finalizer" --resume-existing-history-publish "$expected_head" \
        --remote origin --remote-branch main --repo "$test_repo"
}

remote_refs() {
    git --git-dir="$test_remote" for-each-ref --format='%(objectname)%09%(refname)'
}

seed_remote_ref() {
    local ref=$1

    git --git-dir="$test_remote" fetch --quiet "$test_repo" "HEAD:$ref"
}

assert_local_unchanged() {
    assert_equal "$expected_head" "$(git -C "$test_repo" rev-parse HEAD)" 'HEAD changed'
    assert_equal "$expected_count" "$(git -C "$test_repo" rev-list --count HEAD)" \
        'commit count changed'
    assert_equal '' "$(git -C "$test_repo" status --porcelain=v1 --untracked-files=all)" \
        'worktree is not clean'
    assert_equal '' "$(git -C "$test_repo" diff --cached --name-only)" 'index is not clean'
}

assert_published() {
    assert_equal "$expected_head" \
        "$(git --git-dir="$test_remote" rev-parse refs/heads/main)" \
        'remote main does not match local HEAD'
    assert_equal 'origin/main' \
        "$(git -C "$test_repo" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')" \
        'upstream does not match origin/main'
    assert_equal $'0\t0' \
        "$(git -C "$test_repo" rev-list --left-right --count 'HEAD...@{upstream}')" \
        'ahead/behind is not 0/0'
    assert_equal "$expected_head"$'\trefs/heads/main' "$(remote_refs)" \
        'remote contains refs other than the published branch'
    assert_local_unchanged
}

test_single_commit_to_empty_remote() {
    local case_dir=$tmp_root/single
    local output=$case_dir/output.log

    make_repo "$case_dir" 1
    expect_success "$output" publish_history
    assert_published
}

test_multi_commit_to_empty_remote() {
    local case_dir=$tmp_root/multi
    local output=$case_dir/output.json

    make_repo "$case_dir" 3
    expect_success "$output" "$finalizer" --summary --publish-existing-history \
        "$expected_head" --remote origin --remote-branch main --repo "$test_repo"
    /usr/bin/python3 -B - "$output" "$expected_head" <<'PY'
import json
from pathlib import Path
import sys

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
head = sys.argv[2]
assert data["finalizer_version"] == "0.9.3"
assert data["mode"] == "publish_existing_history"
assert data["status"] == "success"
assert data["branch"] == "main"
assert data["upstream"] == "origin/main"
assert data["requested_path_count"] == 0
assert data["commit"] == {"created": False, "sha": head}
assert data["push"] == {
    "executed": True,
    "result": "succeeded",
    "branch_refspec_only": True,
    "follow_tags_requested": False,
}
result = data["mode_result"]
assert result["interface"] == "publish-existing-history"
assert result["bootstrap_prerequisite"] == "repo-plan/repo-ensure"
assert result["commit_reused"] is True
assert result["pre_publish"]["commit_count"] == 3
assert result["remote_conclusion"] == "published_and_upstream_aligned"
assert result["post_verify"] == "passed"
assert result["head"] == {"before": head, "after": head, "unchanged": True}
assert result["index_unchanged"] is True
assert result["worktree"] == {"before": "clean", "after": "clean", "unchanged": True}
assert result["final"] == {"head": head, "remote_ref_oid": head}
PY
    assert_published
}

test_remote_main_is_rejected() {
    local case_dir=$tmp_root/remote-main
    local output=$case_dir/output.log

    make_repo "$case_dir" 2
    seed_remote_ref refs/heads/main
    expect_failure "$output" publish_history
    assert_file_contains "$output" '远端不是完全空 repository' \
        'existing main did not fail closed'
    assert_local_unchanged
}

test_remote_non_main_ref_is_rejected() {
    local case_dir=$tmp_root/remote-other
    local output=$case_dir/output.log

    make_repo "$case_dir" 2
    seed_remote_ref refs/heads/other
    expect_failure "$output" publish_history
    assert_file_contains "$output" '非目标用户 Git refs' \
        'non-main ref did not fail closed'
    assert_local_unchanged
}

test_remote_tag_only_is_rejected() {
    local case_dir=$tmp_root/remote-tag
    local output=$case_dir/output.log

    make_repo "$case_dir" 2
    seed_remote_ref refs/tags/v0-test
    expect_failure "$output" publish_history
    assert_file_contains "$output" '非目标用户 Git refs' 'tag-only remote did not fail closed'
    assert_local_unchanged
}

test_missing_origin_is_rejected_until_repo_ensure_adds_it() {
    local case_dir=$tmp_root/missing-origin
    local output=$case_dir/output.log

    make_repo "$case_dir" 1
    git -C "$test_repo" remote remove origin
    expect_failure "$output" publish_history
    assert_file_contains "$output" '未配置指定 remote' \
        'missing origin did not require explicit bootstrap handling'
    assert_local_unchanged
}

test_push_rejection_is_fail_closed() {
    local case_dir=$tmp_root/push-rejected
    local output=$case_dir/output.log

    make_repo "$case_dir" 2
    printf '#!/usr/bin/env bash\nexit 1\n' >"$test_remote/hooks/pre-receive"
    chmod 700 "$test_remote/hooks/pre-receive"
    expect_failure "$output" publish_history
    assert_file_contains "$output" 'push 失败或结果无法确认' \
        'push rejection diagnostic is missing'
    assert_equal '' "$(remote_refs)" 'rejected push created a remote ref'
    assert_local_unchanged
}

test_interrupted_push_resumes_exact_history() {
    local case_dir=$tmp_root/interrupted
    local first_output=$case_dir/first.log
    local resume_output=$case_dir/resume.json
    local fake_bin=$case_dir/bin
    local marker=$case_dir/failed-once

    make_repo "$case_dir" 3
    mkdir -p -- "$fake_bin"
    # The wrapper lets the real non-force push complete once, then reports an interrupted caller.
    # shellcheck disable=SC2016
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -Eeuo pipefail' \
        'is_push=0' \
        'for argument in "$@"; do [[ "$argument" == push ]] && is_push=1; done' \
        'if ((is_push == 1)) && [[ ! -e "$INTERRUPT_MARKER" ]]; then' \
        '  /usr/bin/git "$@"' \
        '  : >"$INTERRUPT_MARKER"' \
        '  exit 1' \
        'fi' \
        'exec /usr/bin/git "$@"' \
        >"$fake_bin/git"
    chmod 700 "$fake_bin/git"

    expect_failure "$first_output" env INTERRUPT_MARKER="$marker" PATH="$fake_bin:$PATH" \
        "$finalizer" --publish-existing-history "$expected_head" \
        --remote origin --remote-branch main --repo "$test_repo"
    assert_file_contains "$first_output" '--resume-existing-history-publish' \
        'interrupted publication did not emit the exact resume interface'
    assert_equal "$expected_head" \
        "$(git --git-dir="$test_remote" rev-parse refs/heads/main)" \
        'interrupted push did not preserve the expected remote result'

    expect_success "$resume_output" "$finalizer" --summary \
        --resume-existing-history-publish "$expected_head" \
        --remote origin --remote-branch main --repo "$test_repo"
    /usr/bin/python3 -B - "$resume_output" <<'PY'
import json
from pathlib import Path
import sys

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert data["mode"] == "resume"
assert data["mode_result"]["interface"] == "resume-existing-history-publish"
assert data["mode_result"]["publish_kind"] == "existing_history_first"
assert data["mode_result"]["post_verify"] == "passed"
assert data["mode_result"]["resume_required"] is False
PY
    assert_published
}

test_nonempty_expected_target_requires_explicit_resume() {
    local case_dir=$tmp_root/explicit-resume
    local first_output=$case_dir/first.log
    local resume_output=$case_dir/resume.log

    make_repo "$case_dir" 2
    seed_remote_ref refs/heads/main
    expect_failure "$first_output" publish_history
    expect_success "$resume_output" resume_history
    assert_published
}

test_post_push_remote_mismatch_is_fail_closed() {
    local case_dir=$tmp_root/post-mismatch
    local output=$case_dir/output.log

    make_repo "$case_dir" 2
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -Eeuo pipefail' \
        'read -r _old new ref' \
        'parent=$(/usr/bin/git rev-parse "$new^")' \
        '/usr/bin/git update-ref "$ref" "$parent"' \
        >"$test_remote/hooks/post-receive"
    chmod 700 "$test_remote/hooks/post-receive"

    expect_failure "$output" publish_history
    assert_file_contains "$output" '远端目标 branch 与 HEAD 不一致' \
        'post-push remote mismatch was not detected'
    assert_local_unchanged
}

test_push_is_branch_only_non_force_and_no_extra_commit() {
    local case_dir=$tmp_root/non-force
    local output=$case_dir/output.log
    local probe=$case_dir/probe.log
    local fake_bin=$case_dir/bin
    local push_line

    make_repo "$case_dir" 2
    mkdir -p -- "$fake_bin"
    # shellcheck disable=SC2016
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -Eeuo pipefail' \
        'printf "%q " "$@" >>"$GIT_PROBE"' \
        'printf "\n" >>"$GIT_PROBE"' \
        'exec /usr/bin/git "$@"' \
        >"$fake_bin/git"
    chmod 700 "$fake_bin/git"

    expect_success "$output" env GIT_PROBE="$probe" PATH="$fake_bin:$PATH" \
        "$finalizer" --publish-existing-history "$expected_head" \
        --remote origin --remote-branch main --repo "$test_repo"
    push_line=$(grep -E '(^| )push( |$)' "$probe")
    [[ "$push_line" == *'--no-follow-tags'* ]] ||
        fail_assertion 'push did not disable follow-tags'
    [[ "$push_line" == *'--set-upstream'* ]] || fail_assertion 'push did not set upstream'
    [[ "$push_line" == *'HEAD:refs/heads/main'* ]] ||
        fail_assertion 'push did not use the exact branch refspec'
    [[ "$push_line" != *'--force'* && "$push_line" != *'+HEAD'* ]] ||
        fail_assertion 'push unexpectedly used force semantics'
    assert_published
}

run_case() {
    current_case=$1
    shift
    "$@"
    passed=$((passed + 1))
    printf 'ok - %s\n' "$current_case"
}

run_case 'single existing commit publishes to a verified-empty remote' \
    test_single_commit_to_empty_remote
run_case 'multi-commit history publishes without an extra commit' \
    test_multi_commit_to_empty_remote
run_case 'remote main blocks first publication' test_remote_main_is_rejected
run_case 'remote non-main branch blocks first publication' test_remote_non_main_ref_is_rejected
run_case 'tag-only remote blocks first publication' test_remote_tag_only_is_rejected
run_case 'missing origin requires explicit repo-ensure handling' \
    test_missing_origin_is_rejected_until_repo_ensure_adds_it
run_case 'push rejection remains fail closed' test_push_rejection_is_fail_closed
run_case 'interrupted publication resumes with the exact history' \
    test_interrupted_push_resumes_exact_history
run_case 'nonempty expected target requires the explicit resume interface' \
    test_nonempty_expected_target_requires_explicit_resume
run_case 'post-push remote mismatch is detected' test_post_push_remote_mismatch_is_fail_closed
run_case 'push is branch-only non-force and history remains unchanged' \
    test_push_is_branch_only_non_force_and_no_extra_commit

printf 'all %s publish-existing-history integration tests passed\n' "$passed"
