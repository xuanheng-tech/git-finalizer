#!/usr/bin/env bash

# Standalone contract: a plain Git repository, a minimal HOME/PATH, no
# Worktree Controller, no sibling tools, no installed Skill, and no private
# Gitea configuration must run the ordinary verify/commit/push lifecycle,
# while governed or explicitly requested integration paths fail closed with
# precise blockers and zero mutation.

set -Eeuo pipefail

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/git-finalize
tmp_root=$(mktemp -d /tmp/git-finalizer-standalone.XXXXXX)
current_case='startup'
passed=0

cleanup() {
    case "$tmp_root" in
        /tmp/git-finalizer-standalone.*) rm -rf -- "$tmp_root" ;;
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
    local expected=$1 actual=$2 message=$3
    [[ "$actual" == "$expected" ]] || {
        printf '%s (expected=%s actual=%s)\n' "$message" "$expected" "$actual" >&2
        return 1
    }
}

assert_file_contains() {
    local file=$1 needle=$2 message=$3
    grep -a -q -F -- "$needle" "$file" || {
        printf '%s (missing %q in %s)\n' "$message" "$needle" "$file" >&2
        sed -n '1,80p' "$file" >&2 || true
        return 1
    }
}

expect_success() {
    local output=$1
    shift
    if ! "$@" >"$output" 2>&1; then
        sed -n '1,120p' "$output" >&2
        fail_assertion 'command unexpectedly failed'
    fi
}

expect_failure() {
    local output=$1
    shift
    if "$@" >"$output" 2>&1; then
        sed -n '1,120p' "$output" >&2
        fail_assertion 'command unexpectedly succeeded'
    fi
}

summary_field() {
    local file=$1 field=$2
    python3 -B -c '
import json, sys
document = json.load(open(sys.argv[1]))
value = document
for part in sys.argv[2].split("."):
    value = value[part]
print(value)
' "$file" "$field"
}

make_repo() {
    local case_dir=$1
    repo=$case_dir/repo
    remote=$case_dir/remote.git
    fake_home=$case_dir/home
    mkdir -p -- "$fake_home"
    git init --quiet --bare --initial-branch=main "$remote"
    git init --quiet --initial-branch=main "$repo"
    git -C "$repo" config user.name 'Standalone Contract Author'
    git -C "$repo" config user.email 'standalone@example.invalid'
    git -C "$repo" config commit.gpgsign false
    git -C "$repo" remote add origin "$remote"
    printf 'base\n' >"$repo/wanted.txt"
    git -C "$repo" add -- wanted.txt
    git -C "$repo" commit --quiet -m base
    git -C "$repo" push --quiet --set-upstream origin main
}

isolated() {
    env -i HOME="$fake_home" LC_ALL=C PATH=/usr/bin:/bin GIT_CONFIG_GLOBAL=/dev/null \
        GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 "$@"
}

run_case() {
    current_case=$1
    shift
    "$@"
    passed=$((passed + 1))
    printf 'ok - %s\n' "$current_case"
}

test_version_and_help_standalone() {
    local case_dir=$tmp_root/version
    make_repo "$case_dir"
    expect_success "$case_dir/version.out" isolated "$finalizer" --version
    assert_file_contains "$case_dir/version.out" 'git-finalize' 'version output is wrong'
    expect_success "$case_dir/help.out" isolated "$finalizer" --help
}

test_verify_commit_publish_lifecycle_standalone() {
    local case_dir=$tmp_root/lifecycle
    make_repo "$case_dir"
    printf 'work\n' >>"$repo/wanted.txt"
    git -C "$repo" add -- wanted.txt
    expect_success "$case_dir/verify.json" isolated "$finalizer" --summary \
        --mode verify-only --repo "$repo" -- wanted.txt
    assert_equal 'success' "$(summary_field "$case_dir/verify.json" status)" \
        'standalone verify-only failed'
    expect_success "$case_dir/commit.json" isolated "$finalizer" --summary \
        --mode commit-only --message 'standalone commit' --repo "$repo" -- wanted.txt
    assert_equal 'True' "$(summary_field "$case_dir/commit.json" commit.created)" \
        'standalone commit-only did not create a commit'
    printf 'deliver\n' >>"$repo/wanted.txt"
    expect_success "$case_dir/publish.json" isolated "$finalizer" --summary \
        --message 'second standalone commit' --repo "$repo" -- wanted.txt
    local remote_oid local_oid
    remote_oid=$(git --git-dir="$remote" rev-parse refs/heads/main)
    local_oid=$(git -C "$repo" rev-parse HEAD)
    [[ $remote_oid == "$local_oid" ]] || fail_assertion 'standalone push did not converge'
}

test_resume_publish_standalone() {
    local case_dir=$tmp_root/resume
    make_repo "$case_dir"
    printf 'ahead\n' >>"$repo/wanted.txt"
    git -C "$repo" add -- wanted.txt
    git -C "$repo" commit -qm 'local ahead commit'
    local head_oid
    head_oid=$(git -C "$repo" rev-parse HEAD)
    expect_success "$case_dir/resume.json" isolated "$finalizer" --summary \
        --resume-publish "$head_oid" --repo "$repo"
    assert_equal "$head_oid" "$(git --git-dir="$remote" rev-parse refs/heads/main)" \
        'standalone resume-publish did not push'
}

test_governed_paths_fail_closed_without_dependencies() {
    local case_dir=$tmp_root/governed
    make_repo "$case_dir"
    git -C "$repo" switch --quiet -c feat/abandoned
    git -C "$repo" switch --quiet main
    local abandoned
    abandoned=$(git -C "$repo" rev-parse refs/heads/feat/abandoned)
    expect_failure "$case_dir/protected.log" isolated "$finalizer" --summary \
        --retire-local-branch main --remote origin --integrated-into main \
        --expected-local-oid "$(git -C "$repo" rev-parse HEAD)" \
        --expected-integrated-oid "$(git -C "$repo" rev-parse HEAD)" \
        --retirement-plan-id "$(printf 'e%.0s' $(seq 64))" --repo "$repo"
    assert_file_contains "$case_dir/protected.log" '拒绝受保护分支' \
        'protected branch retirement was not refused first'
    expect_failure "$case_dir/retire.log" isolated "$finalizer" --summary \
        --retire-local-branch feat/abandoned --remote origin --integrated-into main \
        --expected-local-oid "$abandoned" \
        --expected-integrated-oid "$(git -C "$repo" rev-parse HEAD)" \
        --retirement-plan-id "$(printf 'e%.0s' $(seq 64))" --repo "$repo"
    assert_file_contains "$case_dir/retire.log" 'Worktree Controller is required' \
        'unmanaged repository did not fail closed on retirement'
    printf '{"files":[]}\n' >"$case_dir/review.json"
    expect_failure "$case_dir/reviewed.log" isolated "$finalizer" --summary \
        --mode verify-only --repo "$repo" --reviewed-sensitive-source "$case_dir/review.json" \
        -- wanted.txt
    assert_file_contains "$case_dir/reviewed.log" '要求完整 Controller linkage' \
        'reviewed-source without linkage was accepted'
    expect_failure "$case_dir/no-gitea.log" isolated "$finalizer" \
        --repo-plan --repo "$repo"
    assert_file_contains "$case_dir/no-gitea.log" 'gitea-url' \
        'repo-plan must require an explicit public target'
}

test_snapshot_without_artifact_fails_precisely() {
    local case_dir=$tmp_root/snapshot
    make_repo "$case_dir"
    printf 'snap\n' >>"$repo/wanted.txt"
    git -C "$repo" add -- wanted.txt
    expect_failure "$case_dir/snap.log" isolated "$finalizer" \
        --initial-publish --remote origin --snapshot 0000000000000000000000000000000000000000000000000000000000000000 \
        --message 'snapshot without evidence' --repo "$repo" -- wanted.txt
    assert_file_contains "$case_dir/snap.log" 'snapshot' \
        'missing snapshot evidence was not reported precisely'
}

run_case 'standalone --version and --help work with minimal environment' \
    test_version_and_help_standalone
run_case 'standalone verify, commit-only, and publish lifecycle converge' \
    test_verify_commit_publish_lifecycle_standalone
run_case 'standalone resume-publish pushes verified local commits' \
    test_resume_publish_standalone
run_case 'retirement, reviewed-source, and repo-plan fail closed without their authorities' \
    test_governed_paths_fail_closed_without_dependencies
run_case 'snapshot evidence is required precisely, never silently skipped' \
    test_snapshot_without_artifact_fails_precisely

printf 'all %s standalone contract groups passed\n' "$passed"
