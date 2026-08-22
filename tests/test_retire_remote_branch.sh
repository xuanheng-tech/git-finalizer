#!/usr/bin/env bash

set -Eeuo pipefail

export LC_ALL=C
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/codex-git-finalize
tmp_root=$(mktemp -d /tmp/git-finalizer-retirement.XXXXXX)
current_case='startup'
passed=0
feature_branch='feat/integrated'

cleanup() {
    case "$tmp_root" in
        /tmp/git-finalizer-retirement.*) rm -rf -- "$tmp_root" ;;
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

    git -C "$repo" config user.name 'Synthetic Retirement Author'
    git -C "$repo" config user.email 'retirement@example.invalid'
    git -C "$repo" config commit.gpgsign false
}

make_integrated_repo() {
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
    git -C "$test_repo" push --quiet --set-upstream origin main
    git -C "$test_repo" switch --quiet -c "$feature_branch"
    printf 'feature\n' >>"$test_repo/wanted.txt"
    git -C "$test_repo" add -- wanted.txt
    git -C "$test_repo" commit --quiet -m feature
    feature_oid=$(git -C "$test_repo" rev-parse HEAD)
    git -C "$test_repo" push --quiet --set-upstream origin "$feature_branch"
    git -C "$test_repo" switch --quiet main
    git -C "$test_repo" merge --quiet --no-ff -m integrate "$feature_branch"
    integration_oid=$(git -C "$test_repo" rev-parse HEAD)
    git -C "$test_repo" push --quiet origin main
}

make_unintegrated_repo() {
    local case_dir=$1

    make_integrated_repo "$case_dir"
    feature_branch='feat/unintegrated'
    git -C "$test_repo" switch --quiet -c "$feature_branch"
    printf 'unintegrated\n' >"$test_repo/unintegrated.txt"
    git -C "$test_repo" add -- unintegrated.txt
    git -C "$test_repo" commit --quiet -m unintegrated
    feature_oid=$(git -C "$test_repo" rev-parse HEAD)
    git -C "$test_repo" push --quiet --set-upstream origin "$feature_branch"
    git -C "$test_repo" switch --quiet main
    integration_oid=$(git -C "$test_repo" rev-parse HEAD)
}

remote_ref_exists() {
    git --git-dir="$test_remote" show-ref --verify --quiet "refs/heads/$feature_branch"
}

remote_feature_oid() {
    git --git-dir="$test_remote" rev-parse "refs/heads/$feature_branch"
}

retire_command() {
    "$finalizer" --retire-remote-branch "$feature_branch" --remote origin \
        --integrated-into main --expected-remote-oid "$feature_oid" \
        --repo "$test_repo" "$@"
}

state_fingerprint() {
    git -C "$test_repo" rev-parse HEAD
    git -C "$test_repo" symbolic-ref HEAD
    git -C "$test_repo" ls-files --stage
    git -C "$test_repo" status --porcelain=v1 --untracked-files=all
    git -C "$test_repo" for-each-ref --format='%(objectname)%09%(refname)' refs/tags
}

summary_field() {
    local path=$1
    local expression=$2

    /usr/bin/python3 -B - "$path" "$expression" <<'PY'
import json
import sys

value = json.loads(open(sys.argv[1], encoding="utf-8").read())
for part in sys.argv[2].split("."):
    value = value[part]
if isinstance(value, bool):
    print(str(value).lower())
elif value is None:
    print("null")
else:
    print(value)
PY
}

test_success_and_state_preservation() {
    local case_dir=$tmp_root/success
    local output=$case_dir/output.json
    local before

    make_integrated_repo "$case_dir"
    before=$(state_fingerprint)
    expect_success "$output" retire_command --summary

    if remote_ref_exists; then
        fail_assertion 'successful retirement left the remote feature ref present'
    fi
    assert_equal "$before" "$(state_fingerprint)" 'retirement changed local HEAD/index/worktree/tags'
    assert_equal 'REMOTE_BRANCH_RETIRED_VERIFIED' \
        "$(summary_field "$output" mode_result.result)" 'success result is wrong'
    assert_equal 'true' "$(summary_field "$output" mode_result.expected_oid_lease_bound)" \
        'successful deletion was not lease-bound'
    assert_equal 'true' "$(summary_field "$output" mode_result.remote_absent_after)" \
        'post-delete absence was not verified'
}

test_protected_and_default_branches_are_rejected() {
    local case_dir output protected_oid

    case_dir=$tmp_root/protected
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    git -C "$test_repo" push --quiet origin main:refs/heads/production
    protected_oid=$(git --git-dir="$test_remote" rev-parse refs/heads/production)
    expect_failure "$output" "$finalizer" --retire-remote-branch production --remote origin \
        --integrated-into main --expected-remote-oid "$protected_oid" --repo "$test_repo"
    assert_file_contains "$output" '拒绝受保护分支: production' \
        'protected branch was not rejected'
    git --git-dir="$test_remote" show-ref --verify --quiet refs/heads/production ||
        fail_assertion 'protected branch was deleted'

    case_dir=$tmp_root/default
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    git -C "$test_repo" branch develop main
    git -C "$test_repo" push --quiet origin develop
    git --git-dir="$test_remote" symbolic-ref HEAD refs/heads/develop
    protected_oid=$(git --git-dir="$test_remote" rev-parse refs/heads/develop)
    expect_failure "$output" "$finalizer" --retire-remote-branch develop --remote origin \
        --integrated-into main --expected-remote-oid "$protected_oid" --repo "$test_repo"
    assert_file_contains "$output" '拒绝 remote default branch: develop' \
        'default branch was not rejected'
    git --git-dir="$test_remote" show-ref --verify --quiet refs/heads/develop ||
        fail_assertion 'default branch was deleted'
}

test_non_head_and_symbolic_ref_inputs_are_rejected() {
    local case_dir=$tmp_root/ref-validation
    local output=$case_dir/output.log

    make_integrated_repo "$case_dir"
    for invalid in refs/tags/v1 HEAD refs/remotes/origin/main; do
        expect_failure "$output" "$finalizer" --retire-remote-branch "$invalid" \
            --remote origin --integrated-into main --expected-remote-oid "$feature_oid" \
            --repo "$test_repo"
        if remote_ref_exists; then
            :
        else
            fail_assertion "invalid ref input deleted the feature branch: $invalid"
        fi
    done
    expect_failure "$output" "$finalizer" --retire-remote-branch "$feature_branch" \
        --remote origin --integrated-into refs/tags/v1 --expected-remote-oid "$feature_oid" \
        --repo "$test_repo"
    remote_ref_exists || fail_assertion 'tag integration target input deleted the feature branch'
}

test_detached_dirty_and_staged_are_rejected() {
    local case_dir output

    case_dir=$tmp_root/detached
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    git -C "$test_repo" checkout --quiet --detach
    expect_failure "$output" retire_command
    assert_file_contains "$output" 'symbolic attached branch' 'detached HEAD was accepted'

    case_dir=$tmp_root/dirty
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    printf 'dirty\n' >>"$test_repo/wanted.txt"
    expect_failure "$output" retire_command
    assert_file_contains "$output" 'worktree 完全干净' 'dirty worktree was accepted'

    case_dir=$tmp_root/staged
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    printf 'staged\n' >>"$test_repo/wanted.txt"
    git -C "$test_repo" add -- wanted.txt
    expect_failure "$output" retire_command
    assert_file_contains "$output" 'index/staged 状态干净' 'staged index was accepted'
}

test_absent_and_expected_oid_mismatch() {
    local case_dir output wrong_oid

    case_dir=$tmp_root/absent
    output=$case_dir/output.json
    make_integrated_repo "$case_dir"
    git --git-dir="$test_remote" update-ref -d "refs/heads/$feature_branch"
    expect_success "$output" retire_command --summary
    assert_equal 'ALREADY_ABSENT_VERIFIED' "$(summary_field "$output" mode_result.result)" \
        'absent ref was not reported idempotently'

    case_dir=$tmp_root/mismatch
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    wrong_oid=$(git -C "$test_repo" rev-parse main^)
    expect_failure "$output" "$finalizer" --retire-remote-branch "$feature_branch" \
        --remote origin --integrated-into main --expected-remote-oid "$wrong_oid" \
        --repo "$test_repo"
    assert_file_contains "$output" '与 expected_remote_oid 不一致' \
        'expected OID mismatch was accepted'
    assert_equal "$feature_oid" "$(remote_feature_oid)" 'OID mismatch changed remote target'
}

test_unintegrated_and_local_only_are_rejected() {
    local case_dir output

    case_dir=$tmp_root/unintegrated
    output=$case_dir/output.log
    make_unintegrated_repo "$case_dir"
    expect_failure "$output" retire_command
    assert_file_contains "$output" '不是 integration target OID 的 ancestor' \
        'unintegrated branch was accepted'

    feature_branch='feat/integrated'
    case_dir=$tmp_root/local-only
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    git -C "$test_repo" switch --quiet "$feature_branch"
    printf 'local-only\n' >"$test_repo/local-only.txt"
    git -C "$test_repo" add -- local-only.txt
    git -C "$test_repo" commit --quiet -m local-only
    git -C "$test_repo" switch --quiet main
    expect_failure "$output" retire_command
    assert_file_contains "$output" 'local-only commit' 'local-only commit was accepted'
    assert_equal "$feature_oid" "$(remote_feature_oid)" 'local-only blocker changed remote'
}

test_remote_only_divergence_is_rejected() {
    local case_dir=$tmp_root/remote-only
    local output=$case_dir/output.log
    local advance_branch='feature-advance'

    make_integrated_repo "$case_dir"
    git -C "$test_repo" switch --quiet -c "$advance_branch" "$feature_branch"
    printf 'remote-only\n' >"$test_repo/remote-only.txt"
    git -C "$test_repo" add -- remote-only.txt
    git -C "$test_repo" commit --quiet -m remote-only
    feature_oid=$(git -C "$test_repo" rev-parse HEAD)
    git -C "$test_repo" push --quiet origin "HEAD:refs/heads/$feature_branch"
    git -C "$test_repo" switch --quiet main
    git -C "$test_repo" merge --quiet --no-ff -m integrate-advance "$advance_branch"
    integration_oid=$(git -C "$test_repo" rev-parse HEAD)
    git -C "$test_repo" push --quiet origin main

    expect_failure "$output" retire_command
    assert_file_contains "$output" '与 remote feature OID 不一致' \
        'remote-only divergence was accepted'
    assert_equal "$feature_oid" "$(remote_feature_oid)" 'remote-only blocker changed target'
}

test_attached_worktree_and_other_upstream_are_rejected() {
    local case_dir output

    case_dir=$tmp_root/worktree
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    git -C "$test_repo" worktree add --quiet "$case_dir/attached" "$feature_branch"
    expect_failure "$output" retire_command
    assert_file_contains "$output" 'local worktree checkout' \
        'attached worktree dependency was accepted'

    case_dir=$tmp_root/upstream
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    git -C "$test_repo" branch consumer main
    git -C "$test_repo" config branch.consumer.remote origin
    git -C "$test_repo" config branch.consumer.merge "refs/heads/$feature_branch"
    expect_failure "$output" retire_command
    assert_file_contains "$output" '其他 local branch' 'other upstream dependency was accepted'
}

test_ci_evidence_gates() {
    local case_dir output wrong_oid

    case_dir=$tmp_root/ci-success
    output=$case_dir/output.json
    make_integrated_repo "$case_dir"
    expect_success "$output" retire_command --dry-run --summary --ci-required \
        --ci-status SUCCESS --ci-commit-oid "$integration_oid" \
        --ci-verification-source human_authenticated_ui \
        --ci-verified-at 2026-08-22T00:00:00Z
    assert_equal 'RETIREMENT_PREFLIGHT_PASSED' \
        "$(summary_field "$output" mode_result.result)" \
        'authenticated successful CI evidence was rejected'
    assert_equal 'human_authenticated_ui' \
        "$(summary_field "$output" mode_result.ci_verification_source)" \
        'CI verification source was not preserved in the receipt'

    case_dir=$tmp_root/ci-missing
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    expect_failure "$output" retire_command --ci-required
    assert_file_contains "$output" '--ci-required 必须指定 --ci-status' \
        'missing CI evidence was accepted'

    case_dir=$tmp_root/ci-failed
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    expect_failure "$output" retire_command --ci-required --ci-status FAILED \
        --ci-commit-oid "$integration_oid" --ci-verification-source human_authenticated_ui
    assert_file_contains "$output" '要求 CI SUCCESS' 'failed CI was accepted'

    case_dir=$tmp_root/ci-mismatch
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    wrong_oid=$feature_oid
    expect_failure "$output" retire_command --ci-required --ci-status SUCCESS \
        --ci-commit-oid "$wrong_oid" --ci-verification-source human_authenticated_ui
    assert_file_contains "$output" '与 live integration target OID 不一致' \
        'CI commit mismatch was accepted'
}

install_race_git() {
    local fake_bin=$1

    mkdir -p -- "$fake_bin"
    cat >"$fake_bin/git" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
for argument in "$@"; do
    if [[ "$argument" == push && ! -e "$RETIRE_RACE_MARKER" ]]; then
        : >"$RETIRE_RACE_MARKER"
        /usr/bin/git --git-dir="$RETIRE_TEST_REMOTE" update-ref \
            "$RETIRE_TEST_REF" "$RETIRE_RACE_OID"
        break
    fi
done
exec /usr/bin/git "$@"
SH
    chmod 700 "$fake_bin/git"
}

test_lease_race_and_server_rejection() {
    local case_dir output fake_bin race_branch race_oid hook

    case_dir=$tmp_root/lease-race
    output=$case_dir/output.log
    fake_bin=$case_dir/bin
    make_integrated_repo "$case_dir"
    race_branch='refs/heads/race-seed'
    git -C "$test_repo" switch --quiet -c race-seed main
    printf 'race\n' >"$test_repo/race.txt"
    git -C "$test_repo" add -- race.txt
    git -C "$test_repo" commit --quiet -m race
    race_oid=$(git -C "$test_repo" rev-parse HEAD)
    git -C "$test_repo" push --quiet origin "HEAD:$race_branch"
    git -C "$test_repo" switch --quiet main
    install_race_git "$fake_bin"
    expect_failure "$output" env PATH="$fake_bin:$PATH" \
        RETIRE_RACE_MARKER="$case_dir/raced" RETIRE_TEST_REMOTE="$test_remote" \
        RETIRE_TEST_REF="refs/heads/$feature_branch" RETIRE_RACE_OID="$race_oid" \
        "$finalizer" --retire-remote-branch "$feature_branch" --remote origin \
        --integrated-into main --expected-remote-oid "$feature_oid" --repo "$test_repo"
    assert_file_contains "$output" 'lease-bound remote delete 被拒绝' \
        'lease race did not fail closed'
    assert_equal "$race_oid" "$(remote_feature_oid)" 'lease race deleted the concurrent commit'

    case_dir=$tmp_root/server-rejection
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    hook=$test_remote/hooks/pre-receive
    printf '#!/usr/bin/env bash\nexit 1\n' >"$hook"
    chmod 700 "$hook"
    expect_failure "$output" retire_command
    assert_file_contains "$output" 'lease-bound remote delete 被拒绝' \
        'server rejection was not reported'
    assert_equal "$feature_oid" "$(remote_feature_oid)" 'server rejection changed remote target'
}

install_post_verify_failure_git() {
    local fake_bin=$1

    mkdir -p -- "$fake_bin"
    cat >"$fake_bin/git" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
is_push=0
is_fetch=0
for argument in "$@"; do
    [[ "$argument" == push ]] && is_push=1
    [[ "$argument" == fetch ]] && is_fetch=1
done
if ((is_push == 1)); then
    /usr/bin/git "$@"
    : >"$RETIRE_DELETE_COMPLETED"
    exit 0
fi
if ((is_fetch == 1)) && [[ -e "$RETIRE_DELETE_COMPLETED" && ! -e "$RETIRE_VERIFY_MARKER" ]]; then
    : >"$RETIRE_VERIFY_MARKER"
    exit 41
fi
exec /usr/bin/git "$@"
SH
    chmod 700 "$fake_bin/git"
}

test_post_verify_failure_is_truthful() {
    local case_dir=$tmp_root/post-verify-failure
    local output=$case_dir/output.json
    local fake_bin=$case_dir/bin

    make_integrated_repo "$case_dir"
    install_post_verify_failure_git "$fake_bin"
    expect_failure "$output" env PATH="$fake_bin:$PATH" \
        RETIRE_DELETE_COMPLETED="$case_dir/delete-completed" \
        RETIRE_VERIFY_MARKER="$case_dir/failed-once" \
        "$finalizer" --summary --retire-remote-branch "$feature_branch" --remote origin \
        --integrated-into main --expected-remote-oid "$feature_oid" --repo "$test_repo"
    assert_equal 'REMOTE_DELETE_UNVERIFIED' "$(summary_field "$output" mode_result.result)" \
        'post-verify failure claimed a verified retirement'
    if remote_ref_exists; then
        fail_assertion 'post-verify failure fixture did not delete the remote ref'
    fi
}

test_idempotent_second_run_and_deterministic_dry_run() {
    local case_dir first second dry_one dry_two

    case_dir=$tmp_root/idempotent
    first=$case_dir/first.json
    second=$case_dir/second.json
    make_integrated_repo "$case_dir"
    expect_success "$first" retire_command --summary
    expect_success "$second" retire_command --summary
    assert_equal 'ALREADY_ABSENT_VERIFIED' "$(summary_field "$second" mode_result.result)" \
        'second retirement was not idempotent'

    case_dir=$tmp_root/deterministic
    dry_one=$case_dir/one.json
    dry_two=$case_dir/two.json
    make_integrated_repo "$case_dir"
    expect_success "$dry_one" retire_command --dry-run --summary
    expect_success "$dry_two" retire_command --dry-run --summary
    cmp -s -- "$dry_one" "$dry_two" || fail_assertion 'dry-run receipt is not deterministic'
    assert_equal 'RETIREMENT_PREFLIGHT_PASSED' \
        "$(summary_field "$dry_one" mode_result.result)" 'dry-run result is wrong'
    assert_equal "$feature_oid" "$(remote_feature_oid)" 'dry-run deleted the remote ref'
}

run_case() {
    current_case=$1
    shift
    feature_branch='feat/integrated'
    "$@"
    passed=$((passed + 1))
    printf 'ok - %s\n' "$current_case"
}

run_case 'integrated feature retirement succeeds with complete local-state preservation' \
    test_success_and_state_preservation
run_case 'protected and remote default branches are rejected' \
    test_protected_and_default_branches_are_rejected
run_case 'tag symbolic and non-head ref inputs are rejected' \
    test_non_head_and_symbolic_ref_inputs_are_rejected
run_case 'detached dirty and staged repositories are rejected' \
    test_detached_dirty_and_staged_are_rejected
run_case 'absent refs are idempotent and expected OID mismatches block' \
    test_absent_and_expected_oid_mismatch
run_case 'unintegrated and local-only commits block retirement' \
    test_unintegrated_and_local_only_are_rejected
run_case 'remote-only divergence blocks retirement' test_remote_only_divergence_is_rejected
run_case 'attached worktrees and other upstream dependencies block retirement' \
    test_attached_worktree_and_other_upstream_are_rejected
run_case 'required CI evidence fails closed for missing failed and mismatched states' \
    test_ci_evidence_gates
run_case 'expected-OID lease races and server deletion rejection fail closed' \
    test_lease_race_and_server_rejection
run_case 'post-delete verification failures remain explicitly unverified' \
    test_post_verify_failure_is_truthful
run_case 'second execution is idempotent and dry-run receipts are deterministic' \
    test_idempotent_second_run_and_deterministic_dry_run

printf 'all %s remote retirement integration groups passed\n' "$passed"
