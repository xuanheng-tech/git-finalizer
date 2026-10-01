#!/usr/bin/env bash

# End-to-end retirement against the REAL installed Worktree Controller public
# contract. The controller itself plans, revalidates, records receipts, and
# reports status; this suite never reimplements controller digests. It skips
# with explicit ok lines when the controller entry point is unavailable.

set -Eeuo pipefail

export LC_ALL=C
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/git-finalize
tmp_root=$(mktemp -d /tmp/git-finalizer-real-controller.XXXXXX)
current_case='startup'
passed=0
skipped=0
feature_branch='feat/real-controller'

cleanup() {
    case "$tmp_root" in
        /tmp/git-finalizer-real-controller.*) rm -rf -- "$tmp_root" ;;
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
        sed -n '1,160p' "$output" >&2
        fail_assertion 'command unexpectedly failed'
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

controller_available() { command -v worktree-controller >/dev/null 2>&1; }

make_env() {
    local case_dir=$1
    test_repo=$case_dir/repo
    test_remote=$case_dir/remote.git
    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$test_remote"
    git init --quiet --initial-branch=main "$test_repo"
    git -C "$test_repo" config user.name 'Synthetic Real Chain Author'
    git -C "$test_repo" config user.email 'real-chain@example.invalid'
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
    git -C "$test_repo" push --quiet origin "$feature_branch"
    git -C "$test_repo" switch --quiet main
    git -C "$test_repo" merge --quiet --no-ff -m "merge $feature_branch" "$feature_branch"
    git -C "$test_repo" push --quiet origin main
    integration_oid=$(git -C "$test_repo" rev-parse HEAD)
    python3 -B "$case_dir/build_store.py" "$test_repo" "$feature_branch" "$feature_oid"
    worktree-controller branch-retirement-plan \
        --repo "$test_repo" --branch "$feature_branch" --remote origin \
        --integrated-into main --json >"$case_dir/plan.json" 2>"$case_dir/plan.err" || {
        sed -n '1,60p' "$case_dir/plan.err" >&2
        sed -n '1,60p' "$case_dir/plan.json" >&2
        fail_assertion 'real controller refused to persist a valid plan'
    }
    plan_id=$(python3 -B -c '
import json, re, sys
def walk(value):
    if isinstance(value, dict):
        found = value.get("plan_id")
        if isinstance(found, str) and re.fullmatch(r"[0-9a-f]{64}", found):
            return found
        for nested in value.values():
            result = walk(nested)
            if result:
                return result
    elif isinstance(value, list):
        for nested in value:
            result = walk(nested)
            if result:
                return result
    return None
print(walk(json.load(open(sys.argv[1]))))
' "$case_dir/plan.json")
    [[ -n $plan_id && $plan_id != None ]] || fail_assertion 'controller plan_id missing'
    local persisted
    persisted=$(git -C "$test_repo" rev-parse --path-format=absolute --git-common-dir)/worktree-controller/v1/branch-retirement-plans/$plan_id.json
    [[ -f $persisted ]] || fail_assertion 'controller did not persist the plan it planned'
}

write_build_store() {
    mkdir -p -- "$1"
    cat >"$1/build_store.py" <<'BUILD'
"""Seed schema-valid public controller metadata so the REAL controller plans."""
import json, subprocess, sys
from pathlib import Path

repo = Path(sys.argv[1]); branch = sys.argv[2]; released_head = sys.argv[3]
common = Path(subprocess.check_output(
    ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
    text=True).strip())
root = common / "worktree-controller" / "v1"
for name in ("records", "release-receipts", "branch-retirement-plans"):
    (root / name).mkdir(parents=True, exist_ok=True)
(root / "repo.lock").touch(mode=0o600)
def atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
atomic(root / "repo.json", {"schema_version": 4, "repository_id": "22222222-2222-4222-8222-222222222222",
                            "created_at": "2026-09-25T00:00:00+08:00",
                            "updated_at": "2026-09-25T00:00:00+08:00"})
base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", f"{released_head}^"],
                               text=True).strip()
atomic(root / "records" / "11111111-1111-4111-8111-111111111111.json", {
    "schema_version": 2, "record_version": 2,
    "allocation_id": "11111111-1111-4111-8111-111111111111", "task_key": "real-chain-task",
    "authority_key": "real-chain-authority", "module": "synthetic", "role": "feature",
    "owner": "synthetic", "lifecycle": "RELEASED", "created_at": "2026-09-25T00:00:00+08:00",
    "last_reviewed_at": "2026-09-25T00:00:00+08:00", "handoff_commit": released_head,
    "finalizer_record_id": None, "base_commit": base, "task_commit": released_head,
    "finalizer_commit": released_head, "scope_paths": ["wanted.txt"], "source_allocations": [],
    "quarantine_expected_commit": None, "quarantine_observed_commit": None,
    "quarantine_reason": None, "recovery_allocation_id": None, "recovery_ref": None,
    "disposable_resources": [], "external_bindings": [],
    "history": [{"at": "2026-09-25T00:00:00+08:00", "event": "RELEASED",
                 "from": "HANDOFF_READY", "owner": "synthetic", "to": "RELEASED"}]})
atomic(root / "release-receipts" / "33333333-3333-4333-8333-333333333333.json", {
    "schema_version": 1, "receipt_id": "33333333-3333-4333-8333-333333333333",
    "plan_id": "0" * 64, "repository_id": "22222222-2222-4222-8222-222222222222",
    "allocation_id": "11111111-1111-4111-8111-111111111111", "release_intent_id": None,
    "target_path": str(repo), "branch": branch, "released_head": released_head,
    "expected_record_version": 1, "released_record_version": 2, "trigger": "manual",
    "resource_receipt_ids": [], "result": "RELEASED",
    "released_at": "2026-09-25T00:00:00+08:00"})
BUILD
}

retire_local() {
    "$finalizer" --summary --retire-local-branch "$feature_branch" --remote origin \
        --integrated-into main --expected-local-oid "$feature_oid" \
        --expected-integrated-oid "$integration_oid" --retirement-plan-id "$plan_id" \
        --repo "$test_repo" "$@"
}

run_case() {
    current_case=$1
    shift
    if ! controller_available; then
        printf 'ok - %s (skipped: worktree-controller unavailable)\n' "$current_case"
        skipped=$((skipped + 1))
        return
    fi
    "$@"
    passed=$((passed + 1))
    printf 'ok - %s\n' "$current_case"
}

test_plan_status_and_finalizer_accept() {
    local case_dir=$tmp_root/accept
    write_build_store "$case_dir"
    make_env "$case_dir"
    local out=$case_dir/status.json
    expect_success "$out" worktree-controller branch-retirement-status \
        --repo "$test_repo" --plan-id "$plan_id" --json
    expect_success "$case_dir/dryrun.json" retire_local --dry-run
}

test_retirement_completion_and_idempotent_recovery() {
    local case_dir=$tmp_root/record
    local out=$case_dir/retire.json
    write_build_store "$case_dir"
    make_env "$case_dir"
    expect_success "$out" retire_local
    if git -C "$test_repo" show-ref --verify --quiet "refs/heads/$feature_branch"; then
        fail_assertion 'real-controller plan retirement left the local ref'
    fi
    assert_equal 'LOCAL_BRANCH_RETIRED_VERIFIED' \
        "$(summary_field "$out" mode_result.result)" 'unexpected retirement result'
    assert_equal 'True' "$(summary_field "$out" mode_result.expected_oid_lease_bound)" \
        'actual deletion lost its expected-OID authorization'
    expect_success "$case_dir/record.json" worktree-controller branch-retirement-record \
        --repo "$test_repo" --plan-id "$plan_id" --operation local \
        --summary-file "$out" --json
    local receipt_file
    receipt_file=$(git -C "$test_repo" rev-parse --path-format=absolute --git-common-dir)/worktree-controller/v1/branch-retirement-receipts/$plan_id-local.json
    [[ -f $receipt_file ]] || fail_assertion 'controller record did not persist the receipt'
    assert_equal 'AUTHORIZED_EXECUTION' \
        "$(summary_field "$receipt_file" provenance)" 'completion lost authorized provenance'
    assert_equal "$(summary_field "$receipt_file" receipt_id)" \
        "$(summary_field "$out" mode_result.retirement_receipt_id)" \
        'summary receipt does not identify the real Controller receipt'
    expect_success "$case_dir/executions.json" worktree-controller branch-retirement-executions \
        --repo "$test_repo" --action list --json
    assert_equal '[]' "$(summary_field "$case_dir/executions.json" decision.authorizations)" \
        'completed operation left an outstanding authorization'
    expect_success "$case_dir/status-after.json" worktree-controller branch-retirement-record \
        --repo "$test_repo" --plan-id "$plan_id" --operation local \
        --summary-file "$out" --json
    assert_equal 1 "$(find "$(dirname "$receipt_file")" -maxdepth 1 -name "$plan_id-local.json" | wc -l)" \
        'duplicate record created a second receipt'
    expect_success "$case_dir/replay.json" retire_local
    assert_equal 'ALREADY_ABSENT_VERIFIED' \
        "$(summary_field "$case_dir/replay.json" mode_result.result)" 'completed recovery was not idempotent'
    assert_equal 'False' "$(summary_field "$case_dir/replay.json" mode_result.expected_oid_lease_bound)" \
        'completed recovery claimed another CAS'
    assert_equal "$(summary_field "$receipt_file" receipt_id)" \
        "$(summary_field "$case_dir/replay.json" mode_result.retirement_receipt_id)" \
        'idempotent summary changed the real Controller receipt identity'
}

test_remote_authorization_and_local_continuation() {
    local case_dir=$tmp_root/remote-authorization
    local out=$case_dir/remote.json
    write_build_store "$case_dir"
    make_env "$case_dir"
    expect_success "$out" "$finalizer" --summary --retire-remote-branch "$feature_branch" \
        --remote origin --integrated-into main --expected-remote-oid "$feature_oid" \
        --expected-integrated-oid "$integration_oid" --retirement-plan-id "$plan_id" \
        --repo "$test_repo"
    assert_equal 'REMOTE_BRANCH_RETIRED_VERIFIED' \
        "$(summary_field "$out" mode_result.result)" 'real remote completion failed'
    assert_equal 'True' "$(summary_field "$out" push.executed)" 'actual remote CAS was not reported'
    if git --git-dir="$test_remote" show-ref --verify --quiet "refs/heads/$feature_branch"; then
        fail_assertion 'remote authorization left the live ref'
    fi
    if git -C "$test_repo" show-ref --verify --quiet "refs/remotes/origin/$feature_branch"; then
        fail_assertion 'remote completion left the tracking ref'
    fi
    git -C "$test_repo" show-ref --verify --quiet "refs/heads/$feature_branch" ||
        fail_assertion 'remote CAS changed the local branch'
    expect_success "$case_dir/local.json" retire_local
    expect_success "$case_dir/status.json" worktree-controller branch-retirement-status \
        --repo "$test_repo" --plan-id "$plan_id" --json
    assert_equal "['local', 'remote']" \
        "$(summary_field "$case_dir/status.json" decision.completed_operations)" \
        'independent operations did not both complete'
    expect_success "$case_dir/executions.json" worktree-controller branch-retirement-executions \
        --repo "$test_repo" --action list --json
    assert_equal '[]' "$(summary_field "$case_dir/executions.json" decision.authorizations)" \
        'remote/local continuation leaked an authorization'
}

test_legacy_absence_has_observed_provenance() {
    local case_dir=$tmp_root/legacy-observation
    write_build_store "$case_dir"
    make_env "$case_dir"
    git -C "$test_repo" update-ref -d "refs/heads/$feature_branch" "$feature_oid"
    expect_success "$case_dir/out.json" retire_local
    assert_equal 'ALREADY_ABSENT_VERIFIED' \
        "$(summary_field "$case_dir/out.json" mode_result.result)" 'legacy absence was not observed'
    assert_equal 'False' "$(summary_field "$case_dir/out.json" mode_result.expected_oid_lease_bound)" \
        'legacy absence claimed an authorized CAS'
    local receipt_file
    receipt_file=$(git -C "$test_repo" rev-parse --path-format=absolute --git-common-dir)/worktree-controller/v1/branch-retirement-receipts/$plan_id-local.json
    assert_equal 'CONTROLLER_OBSERVED' \
        "$(summary_field "$receipt_file" provenance)" 'legacy observation lost its distinct provenance'
}

test_authority_and_oid_drift_fail_closed() {
    local case_dir=$tmp_root/authority-drift
    write_build_store "$case_dir"
    make_env "$case_dir"
    printf ' \n' >>"$case_dir/repo/.git/worktree-controller/v1/release-receipts/33333333-3333-4333-8333-333333333333.json"
    expect_failure "$case_dir/out.log" retire_local
    assert_file_contains "$case_dir/out.log" 'Controller retirement identity differs' 'authority drift was accepted'
    git -C "$test_repo" show-ref --verify --quiet "refs/heads/$feature_branch" ||
        fail_assertion 'authority drift deleted the local ref'

    case_dir=$tmp_root/policy-drift
    write_build_store "$case_dir"
    make_env "$case_dir"
    mkdir -p "$case_dir/repo/.agents"
    cat >"$case_dir/repo/.agents/worktree-policy.toml" <<'POLICY'
schema_version = 1
[limits]
normal_limit = 5
hard_limit = 6
[canonical]
protected = true
allowed_roles = ["canonical", "control-plane"]
[slots]
integration = 1
feature_or_audit = 3
emergency = 1
[review]
stale_warning_days = 8
[authority]
require_adoption = true
require_task_key = true
require_authority_key = true
unique_active_feature_authority = true
POLICY
    expect_failure "$case_dir/out.log" retire_local
    assert_file_contains "$case_dir/out.log" 'Controller retirement identity differs' 'policy drift was accepted'
    rm -f -- "$case_dir/repo/.agents/worktree-policy.toml"

    case_dir=$tmp_root/oid-change
    write_build_store "$case_dir"
    make_env "$case_dir"
    git -C "$test_repo" switch --quiet "$feature_branch"
    printf 'post-plan commit\n' >>"$test_repo/wanted.txt"
    git -C "$test_repo" commit -qam post-plan
    git -C "$test_repo" switch --quiet main
    expect_failure "$case_dir/out.log" retire_local
    git -C "$test_repo" show-ref --verify --quiet "refs/heads/$feature_branch" ||
        fail_assertion 'OID-changed branch vanished despite CAS expectation'
}

test_tampered_plan_and_lock_contention() {
    local case_dir=$tmp_root/tamper
    write_build_store "$case_dir"
    make_env "$case_dir"
    local plan_file=$case_dir/repo/.git/worktree-controller/v1/branch-retirement-plans/$plan_id.json
    python3 -B -c '
import json, sys
path = sys.argv[1]
plan = json.load(open(path))
plan["plan_id"] = "f" * 64
open(path, "w").write(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
' "$plan_file"
    expect_failure "$case_dir/out.log" retire_local
    assert_file_contains "$case_dir/out.log" 'Controller branch-retirement-status refused or failed' \
        'tampered plan was accepted'

    case_dir=$tmp_root/lock
    write_build_store "$case_dir"
    make_env "$case_dir"
    local lock=$case_dir/repo/.git/worktree-controller/v1/repo.lock
    python3 -B -c '
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
time.sleep(3)
' "$lock" &
    local holder=$!
    sleep 1
    local blocked_rc=0
    timeout 2 "$finalizer" --summary --retire-local-branch "$feature_branch" \
        --remote origin --integrated-into main --expected-local-oid "$feature_oid" \
        --expected-integrated-oid "$integration_oid" --retirement-plan-id "$plan_id" \
        --repo "$test_repo" >"$case_dir/blocked.log" 2>&1 || blocked_rc=$?
    wait "$holder" 2>/dev/null || true
    assert_equal 124 "$blocked_rc" 'finalizer did not block behind the held retirement lock'
    git -C "$test_repo" show-ref --verify --quiet "refs/heads/$feature_branch" ||
        fail_assertion 'locked-out finalizer deleted the ref'
    expect_success "$case_dir/done.log" retire_local
    if git -C "$test_repo" show-ref --verify --quiet "refs/heads/$feature_branch"; then
        fail_assertion 'finalizer failed to retire after the lock was released'
    fi
}

test_namespace_and_capability_blockers() {
    local case_dir=$tmp_root/namespace
    mkdir -p -- "$case_dir/repo"
    git init --quiet --initial-branch=main "$case_dir/repo"
    printf 'x\n' >"$case_dir/repo/f"
    git -C "$case_dir/repo" add -- f
    git -C "$case_dir/repo" -c user.name=t -c user.email=t@x commit --quiet -m seed
    mkdir -p -- "$case_dir/repo/.git/codex-worktree/v1"
    expect_failure "$case_dir/companion-legacy.log" "$finalizer" --summary \
        --repo "$case_dir/repo" --retirement-plan-id "$(printf 'e%.0s' $(seq 64))" \
        --retire-local-branch "$feature_branch" --remote origin --integrated-into main \
        --expected-local-oid "$(printf '0%.0s' $(seq 40))" \
        --expected-integrated-oid "$(printf '0%.0s' $(seq 40))"
    assert_file_contains "$case_dir/companion-legacy.log" 'legacy codex-worktree' \
        'companion accepted a legacy-namespace repository'
    mkdir -p -- "$case_dir/repo/.git/worktree-controller/v1"
    touch "$case_dir/repo/.git/worktree-controller/v1/repo.lock"
    chmod 600 "$case_dir/repo/.git/worktree-controller/v1/repo.lock"
    expect_failure "$case_dir/companion-noplan.log" python3 -B \
        "$project_root/git-finalize-retirement-plan.py" \
        --repo "$case_dir/repo" --plan-id "$(printf 'e%.0s' $(seq 64))" \
        --operation local --branch main --remote origin --integrated-into main \
        --expected-oid "$(printf '0%.0s' $(seq 40))" \
        --expected-integrated-oid "$(printf '0%.0s' $(seq 40))"
    assert_file_contains "$case_dir/companion-noplan.log" 'Controller branch-retirement-status refused or failed' \
        'missing plan was accepted'

    case_dir=$tmp_root/capability
    write_build_store "$case_dir"
    make_env "$case_dir"
    mkdir -p -- "$case_dir/bin"
    printf '%s\n' '#!/bin/sh' \
        "printf '{\"decision\":{\"branch_retirement_version\":99},\"schema_version\":1}\n'" \
        'exit 0' >"$case_dir/bin/worktree-controller"
    chmod 755 "$case_dir/bin/worktree-controller"
    expect_failure "$case_dir/mismatch.log" env PATH="$case_dir/bin:$PATH" \
        "$finalizer" --summary --retire-local-branch "$feature_branch" --remote origin \
        --integrated-into main --expected-local-oid "$feature_oid" \
        --expected-integrated-oid "$integration_oid" --retirement-plan-id "$plan_id" \
        --repo "$test_repo"
    assert_file_contains "$case_dir/mismatch.log" 'required public retirement protocols' \
        'capability mismatch was accepted'
    git -C "$test_repo" show-ref --verify --quiet "refs/heads/$feature_branch" ||
        fail_assertion 'capability mismatch deleted the branch anyway'
    printf '%s\n' '#!/bin/sh' 'exit 3' >"$case_dir/bin/worktree-controller"
    expect_failure "$case_dir/probe.log" env PATH="$case_dir/bin:$PATH" \
        "$finalizer" --summary --retire-local-branch "$feature_branch" --remote origin \
        --integrated-into main --expected-local-oid "$feature_oid" \
        --expected-integrated-oid "$integration_oid" --retirement-plan-id "$plan_id" \
        --repo "$test_repo"
    assert_file_contains "$case_dir/probe.log" 'Controller capabilities refused or failed' \
        'failing capability probe was accepted'
    git -C "$test_repo" show-ref --verify --quiet "refs/heads/$feature_branch" ||
        fail_assertion 'failed probe deleted the branch anyway'
}

run_case 'real controller plan validates and finalizer dry-run accepts' \
    test_plan_status_and_finalizer_accept
run_case 'real retirement completes authorization and recovers idempotently' \
    test_retirement_completion_and_idempotent_recovery
run_case 'real remote CAS completes before independent local continuation' \
    test_remote_authorization_and_local_continuation
run_case 'legacy absent ref is recorded as an observation without a CAS' \
    test_legacy_absence_has_observed_provenance
run_case 'authority, policy, and OID drift fail closed' \
    test_authority_and_oid_drift_fail_closed
run_case 'tampered plan and lock contention never mutate' \
    test_tampered_plan_and_lock_contention
run_case 'namespace and capability blockers fail closed' \
    test_namespace_and_capability_blockers

if ((passed == 0 && skipped > 0)); then
    printf 'real-controller integration: 0 groups executed, %s skipped (worktree-controller absent from PATH)\n' "$skipped"
else
    printf 'all %s real-controller integration groups passed' "$passed"
    ((skipped == 0)) || printf ', %s skipped' "$skipped"
    printf '\n'
fi
