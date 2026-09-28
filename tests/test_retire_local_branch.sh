#!/usr/bin/env bash

set -Eeuo pipefail

export LC_ALL=C
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/git-finalize
tmp_root=$(mktemp -d /tmp/git-finalizer-local-retirement.XXXXXX)
current_case='startup'
passed=0
feature_branch='feat/integrated'

cleanup() {
    case "$tmp_root" in
        /tmp/git-finalizer-local-retirement.*) rm -rf -- "$tmp_root" ;;
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

summary_field() {
    local path=$1
    local expression=$2
    python3 -B - "$path" "$expression" <<'PY'
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

make_integrated_repo() {
    local case_dir=$1

    test_repo=$case_dir/repo
    test_remote=$case_dir/remote.git
    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$test_remote"
    git init --quiet --initial-branch=main "$test_repo"
    git -C "$test_repo" config user.name 'Synthetic Local Retirement Author'
    git -C "$test_repo" config user.email 'local-retirement@example.invalid'
    git -C "$test_repo" config commit.gpgsign false
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
    plan_id=$(make_controller_plan "$test_repo" "$feature_branch" "$feature_oid" "$integration_oid")
}

make_controller_plan() {
    local repo=$1
    local branch=$2
    local expected_oid=$3
    local integrated_oid=$4
    python3 -B - "$repo" "$branch" "$expected_oid" "$integrated_oid" <<'PY'
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
branch = sys.argv[2]
expected_oid = sys.argv[3]
integrated_oid = sys.argv[4]
common = Path(
    subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True,
    ).strip()
)
root = common / "worktree-controller" / "v1"
allocation_id = "11111111-1111-4111-8111-111111111111"
repository_id = "22222222-2222-4222-8222-222222222222"
release_receipt_id = "33333333-3333-4333-8333-333333333333"
(root / "records").mkdir(parents=True, exist_ok=True)
(root / "release-receipts").mkdir(parents=True, exist_ok=True)
(root / "branch-retirement-plans").mkdir(parents=True, exist_ok=True)
(root / "repo.lock").touch(mode=0o600)
(root / "records" / f"{allocation_id}.json").write_text(
    json.dumps(
        {
            "schema_version": 4,
            "allocation_id": allocation_id,
            "role": "feature",
            "lifecycle": "RELEASED",
            "external_bindings": [],
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
(root / "release-receipts" / f"{release_receipt_id}.json").write_text(
    json.dumps(
        {
            "schema_version": 1,
            "receipt_id": release_receipt_id,
            "allocation_id": allocation_id,
            "branch": branch,
            "released_head": expected_oid,
            "result": "RELEASED",
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)

authority_directories = (
    "records",
    "bindings",
    "integration-intents",
    "release-intents",
    "release-plans",
    "release-receipts",
    "publication-receipts",
    "writer-leases",
    "transactions",
)
digest = hashlib.sha256()
for directory_name in authority_directories:
    directory = root / directory_name
    if directory.is_dir():
        for path in sorted(directory.rglob("*.json")):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
digest.update(b"policy\0<default>")
state_digest = digest.hexdigest()
plan = {
    "schema_version": 1,
    "repository_id": repository_id,
    "allocation_id": allocation_id,
    "release_receipt_id": release_receipt_id,
    "branch": branch,
    "expected_oid": expected_oid,
    "remote": "origin",
    "remote_ref": f"refs/heads/{branch}",
    "remote_tracking_ref": f"refs/remotes/origin/{branch}",
    "integrated_into": "origin/main",
    "integrated_ref": "refs/remotes/origin/main",
    "expected_integrated_oid": integrated_oid,
    "classification": "ANCESTRY",
    "registration": "RELEASE_BACKED",
    "local_ref_state": "PRESENT_EXPECTED",
    "remote_tracking_state": "PRESENT_EXPECTED",
    "upstream_state": "EXPECTED",
    "upstream_ref": f"refs/remotes/origin/{branch}",
    "checked_out_paths": [],
    "controller_state_digest": state_digest,
    "eligible": True,
    "blockers": [],
}
identity_fields = (
    "schema_version",
    "repository_id",
    "allocation_id",
    "release_receipt_id",
    "branch",
    "expected_oid",
    "remote",
    "remote_ref",
    "remote_tracking_ref",
    "integrated_into",
    "integrated_ref",
    "expected_integrated_oid",
    "classification",
    "registration",
    "controller_state_digest",
)
identity = {field: plan.get(field) for field in identity_fields}
encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
plan_id = hashlib.sha256(encoded.encode()).hexdigest()
plan["plan_id"] = plan_id
plan["created_at"] = "2026-09-19T00:00:00+08:00"
(root / "branch-retirement-plans" / f"{plan_id}.json").write_text(
    json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(plan_id)
PY
}

local_retire_command() {
    "$finalizer" --retire-local-branch "$feature_branch" --remote origin \
        --integrated-into origin/main --expected-local-oid "$feature_oid" \
        --expected-integrated-oid "$integration_oid" --retirement-plan-id "$plan_id" \
        --repo "$test_repo" "$@"
}

remote_retire_command() {
    "$finalizer" --retire-remote-branch "$feature_branch" --remote origin \
        --integrated-into origin/main --expected-remote-oid "$feature_oid" \
        --expected-integrated-oid "$integration_oid" --retirement-plan-id "$plan_id" \
        --repo "$test_repo" "$@"
}

test_partial_completion_and_idempotent_resume() {
    local case_dir=$tmp_root/partial
    local local_output=$case_dir/local.json
    local remote_output=$case_dir/remote.json
    local repeated=$case_dir/repeated.json

    make_integrated_repo "$case_dir"
    expect_success "$local_output" local_retire_command --summary
    git -C "$test_repo" show-ref --verify --quiet "refs/heads/$feature_branch" &&
        fail_assertion 'local retirement left local feature ref present'
    git --git-dir="$test_remote" show-ref --verify --quiet "refs/heads/$feature_branch" ||
        fail_assertion 'local retirement changed remote feature ref'
    assert_equal 'LOCAL_BRANCH_RETIRED_VERIFIED' \
        "$(summary_field "$local_output" mode_result.result)" 'local result is wrong'
    assert_equal "$plan_id" \
        "$(summary_field "$local_output" mode_result.retirement_plan_id)" \
        'local result lost plan binding'
    assert_equal 'true' "$(summary_field "$local_output" mode_result.local_absent_after)" \
        'local absence was not verified'

    expect_success "$remote_output" remote_retire_command --summary
    git --git-dir="$test_remote" show-ref --verify --quiet "refs/heads/$feature_branch" &&
        fail_assertion 'remote continuation left remote feature ref present'
    assert_equal 'REMOTE_BRANCH_RETIRED_VERIFIED' \
        "$(summary_field "$remote_output" mode_result.result)" 'remote result is wrong'

    expect_success "$repeated" local_retire_command --summary
    assert_equal 'ALREADY_ABSENT_VERIFIED' \
        "$(summary_field "$repeated" mode_result.result)" \
        'repeated local retirement was not idempotent'
}

test_remote_then_local_completion_is_independent() {
    local case_dir=$tmp_root/reverse
    local remote_output=$case_dir/remote.json
    local local_output=$case_dir/local.json

    make_integrated_repo "$case_dir"
    expect_success "$remote_output" remote_retire_command --summary
    git -C "$test_repo" show-ref --verify --quiet "refs/heads/$feature_branch" ||
        fail_assertion 'remote-first retirement changed local feature ref'
    expect_success "$local_output" local_retire_command --summary
    assert_equal 'LOCAL_BRANCH_RETIRED_VERIFIED' \
        "$(summary_field "$local_output" mode_result.result)" \
        'local continuation after remote retirement failed'
}

test_oid_drift_recheckout_and_controller_drift_block() {
    local case_dir output record

    case_dir=$tmp_root/oid-drift
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    git -C "$test_repo" branch -f "$feature_branch" main
    expect_failure "$output" local_retire_command
    assert_file_contains "$output" 'local-only commit' 'local OID drift was accepted'

    case_dir=$tmp_root/recheckout
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    git -C "$test_repo" worktree add --quiet "$case_dir/attached" "$feature_branch"
    expect_failure "$output" local_retire_command
    assert_file_contains "$output" 'local worktree checkout' 'rechecked-out branch was deleted'

    case_dir=$tmp_root/controller-drift
    output=$case_dir/output.log
    make_integrated_repo "$case_dir"
    record=$test_repo/.git/worktree-controller/v1/records/11111111-1111-4111-8111-111111111111.json
    printf ' \n' >>"$record"
    expect_failure "$output" local_retire_command
    assert_file_contains "$output" 'authority state drifted' \
        'concurrent Controller authority drift was accepted'
    git -C "$test_repo" show-ref --verify --quiet "refs/heads/$feature_branch" ||
        fail_assertion 'Controller drift deleted local ref'
}

test_integration_drift_and_protected_ref_block() {
    local case_dir=$tmp_root/integration-drift
    local output=$case_dir/output.log

    make_integrated_repo "$case_dir"
    printf 'advance\n' >"$test_repo/advance.txt"
    git -C "$test_repo" add -- advance.txt
    git -C "$test_repo" commit --quiet -m advance-main
    git -C "$test_repo" push --quiet origin main
    expect_failure "$output" local_retire_command
    assert_file_contains "$output" 'expected_integrated_oid 不一致' \
        'integration OID drift was accepted'

    expect_failure "$output" "$finalizer" --retire-local-branch main --remote origin \
        --integrated-into origin/main --expected-local-oid "$(git -C "$test_repo" rev-parse main)" \
        --expected-integrated-oid "$(git -C "$test_repo" rev-parse main)" \
        --retirement-plan-id "$plan_id" --repo "$test_repo"
    assert_file_contains "$output" '拒绝受保护分支: main' 'protected local ref was accepted'

    expect_failure "$output" "$finalizer" --retire-local-branch recovery/preserved \
        --remote origin --integrated-into origin/main --expected-local-oid "$feature_oid" \
        --expected-integrated-oid "$integration_oid" --retirement-plan-id "$plan_id" \
        --repo "$test_repo"
    assert_file_contains "$output" '拒绝受保护分支: recovery/preserved' \
        'recovery ref was accepted'

    expect_failure "$output" "$finalizer" --retire-local-branch archive \
        --remote origin --integrated-into origin/main --expected-local-oid "$feature_oid" \
        --expected-integrated-oid "$integration_oid" --retirement-plan-id "$plan_id" \
        --repo "$test_repo"
    assert_file_contains "$output" '拒绝受保护分支: archive' \
        'exact special lifecycle ref was accepted'
}

test_verifier_hardening_guards() {
    local case_dir=$tmp_root/verifier-guards
    local bad_id_log=$tmp_root/verifier-guards-bad-id.log
    local alias_out=$tmp_root/verifier-guards-alias.json
    local replay_dir=$tmp_root/verifier-guards-replay
    local replay_out=$tmp_root/verifier-guards-replay.log
    local common receipt_root

    make_integrated_repo "$case_dir"
    expect_failure "$bad_id_log" "$finalizer" --retire-local-branch "$feature_branch" \
        --remote origin --integrated-into origin/main \
        --expected-local-oid "$feature_oid" --expected-integrated-oid "$integration_oid" \
        --retirement-plan-id "$(printf 'a%.0s' 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40)" \
        --repo "$test_repo"
    assert_file_contains "$bad_id_log" '64 位小写十六进制' 'malformed plan id was accepted'

    expect_success "$alias_out" "$finalizer" --summary \
        --retire-local-branch "$feature_branch" --remote origin --integrated-into main \
        --expected-local-oid "$feature_oid" --expected-integrated-oid "$integration_oid" \
        --retirement-plan-id "$plan_id" --repo "$test_repo"
    assert_equal 'LOCAL_BRANCH_RETIRED_VERIFIED' \
        "$(summary_field "$alias_out" mode_result.result)" \
        'bare integration alias was rejected'

    make_integrated_repo "$replay_dir"
    common=$(git -C "$test_repo" rev-parse --path-format=absolute --git-common-dir)
    receipt_root=$common/worktree-controller/v1/branch-retirement-receipts
    mkdir -p -- "$receipt_root"
    printf '{"synthetic":"consumed local receipt"}\n' \
        >"$receipt_root/$plan_id-local.json"
    expect_failure "$replay_out" "$finalizer" --summary \
        --retire-local-branch "$feature_branch" --remote origin --integrated-into main \
        --expected-local-oid "$feature_oid" --expected-integrated-oid "$integration_oid" \
        --retirement-plan-id "$plan_id" --repo "$test_repo"
    assert_file_contains "$replay_out" 'already records a consumed receipt' \
        'plan operation replay after consumption was accepted'
}

run_case() {
    current_case=$1
    shift
    feature_branch='feat/integrated'
    "$@"
    passed=$((passed + 1))
    printf 'ok - %s\n' "$current_case"
}

run_case 'local then remote retirement completes independently and resumes idempotently' \
    test_partial_completion_and_idempotent_resume
run_case 'remote then local retirement also completes independently' \
    test_remote_then_local_completion_is_independent
run_case 'OID drift recheckout and Controller authority drift fail closed' \
    test_oid_drift_recheckout_and_controller_drift_block
run_case 'integration drift and protected refs fail closed' \
    test_integration_drift_and_protected_ref_block
run_case 'plan-id canonical form, integration aliases, and consumed-receipt replay guard' \
    test_verifier_hardening_guards

printf 'all %s local retirement integration groups passed\n' "$passed"
