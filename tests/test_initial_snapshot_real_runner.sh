#!/usr/bin/env bash

# End-to-end initial publication bound to evidence from the REAL installed
# Snapshot Runner. The producer writes the artifact through its public CLI;
# this suite never fabricates snapshot bytes, so a producer security epoch or
# artifact change that Finalizer cannot verify fails here instead of in use.
# tests/run.sh reports SKIP when the entry point is unavailable.

set -Eeuo pipefail

export LC_ALL=C
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/git-finalize
tmp_root=$(mktemp -d /tmp/git-finalizer-real-runner.XXXXXX)
current_case='startup'
passed=0

cleanup() {
    case "$tmp_root" in
        /tmp/git-finalizer-real-runner.*) rm -rf -- "$tmp_root" ;;
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

json_field() {
    python3 -B -c '
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for part in sys.argv[2].split("."):
    value = value[part]
print(value)
' "$1" "$2"
}

make_case() {
    local case_dir=$1
    test_repo=$case_dir/repo
    test_remote=$case_dir/remote.git
    test_state=$case_dir/state
    mkdir -p -- "$case_dir"
    install -d -m 0700 -- "$test_state"
    git init --quiet --bare --initial-branch=main "$test_remote"
    git init --quiet --initial-branch=main "$test_repo"
    git -C "$test_repo" config user.name 'Synthetic Real Runner Author'
    git -C "$test_repo" config user.email 'real-runner@example.invalid'
    git -C "$test_repo" config commit.gpgsign false
    git -C "$test_repo" remote add origin "$test_remote"
    mkdir -p -- "$test_repo/src"
    printf '# Real runner fixture\n' >"$test_repo/README.md"
    printf 'print("real runner")\n' >"$test_repo/src/app.py"
    chmod 0755 "$test_repo/src/app.py"
}

collect_snapshot() {
    local output=$1
    expect_success "$output" env XDG_STATE_HOME="$test_state" snapshot-runner diff-audit \
        --repo "$test_repo" --initial-publish-evidence --summary
    snapshot_id=$(json_field "$output" snapshot_id)
    [[ $snapshot_id =~ ^[0-9a-f]{64}$ ]] ||
        fail_assertion "real Snapshot Runner returned no snapshot id: $snapshot_id"
}

remote_refs() {
    git --git-dir="$1" for-each-ref --format='%(refname) %(objectname)'
}

test_real_snapshot_binds_initial_publication() {
    local case_dir=$tmp_root/bound
    local output=$case_dir/finalizer.json
    make_case "$case_dir"
    collect_snapshot "$case_dir/runner.json"
    printf 'producer: %s, security epoch %s\n' \
        "$(snapshot-runner --version)" \
        "$(json_field "$test_state/snapshot-runner/snapshots/$snapshot_id/meta.json" \
            producer_security_epoch)"

    expect_success "$output" env XDG_STATE_HOME="$test_state" "$finalizer" --summary \
        --initial-publish --remote origin --repo "$test_repo" \
        --message 'real runner initial' --snapshot "$snapshot_id" -- README.md src/app.py

    assert_equal success "$(json_field "$output" status)" 'bound publication did not succeed'
    assert_equal passed "$(json_field "$output" mode_result.snapshot_verification)" \
        'real snapshot verification was not reported as passed'
    assert_equal "$(git -C "$test_repo" rev-parse HEAD)" \
        "$(git --git-dir="$test_remote" rev-parse refs/heads/main)" \
        'remote main does not match the bound root commit'
}

test_real_snapshot_rejects_drift_without_mutation() {
    local case_dir=$tmp_root/drift
    local output=$case_dir/finalizer.log
    make_case "$case_dir"
    collect_snapshot "$case_dir/runner.json"
    printf 'drift\n' >>"$test_repo/README.md"

    expect_failure "$output" env XDG_STATE_HOME="$test_state" "$finalizer" \
        --initial-publish --remote origin --repo "$test_repo" \
        --message 'real runner drift' --snapshot "$snapshot_id" -- README.md src/app.py

    assert_file_contains "$output" 'snapshot evidence rejected' \
        'real snapshot drift diagnostic absent'
    if git -C "$test_repo" rev-parse --verify --quiet HEAD >/dev/null; then
        fail_assertion 'drift rejection created a commit'
    fi
    assert_equal '' "$(git -C "$test_repo" ls-files --stage)" \
        'drift rejection changed the index'
    assert_equal '' "$(remote_refs "$test_remote")" 'drift rejection changed the remote'
}

run_case() {
    current_case=$1
    shift
    "$@"
    passed=$((passed + 1))
    printf 'ok - %s\n' "$current_case"
}

run_case 'real Snapshot Runner evidence binds an initial publication' \
    test_real_snapshot_binds_initial_publication
run_case 'real Snapshot Runner evidence rejects workspace drift without mutation' \
    test_real_snapshot_rejects_drift_without_mutation

printf 'all %s real Snapshot Runner tests passed\n' "$passed"
