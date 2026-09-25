#!/usr/bin/env bash

# Integration boundary contract: external-layout knowledge stays confined to
# the single marked adapter per integration, the documented boundary matrix
# matches the code, and the machine-readable summary contract is frozen.

set -Eeuo pipefail

export LC_ALL=C
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/git-finalize
doc=$project_root/docs/integration-boundaries.md
tmp_root=$(mktemp -d /tmp/git-finalizer-boundaries.XXXXXX)
tmp_root=$(cd -- "$tmp_root" && pwd -P)
current_case='startup'
passed=0

cleanup() {
    case "$tmp_root" in
        /tmp/git-finalizer-boundaries.*) rm -rf -- "$tmp_root" ;;
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
        printf '%s\n(expected=%s\nactual  =%s)\n' "$message" "$expected" "$actual" >&2
        return 1
    }
}

run_case() {
    current_case=$1
    shift
    "$@"
    passed=$((passed + 1))
    printf 'ok - %s\n' "$current_case"
}

# Production code surface only: the root CLI plus its companion adapters.
PRODUCTION_CODE=('git-finalize' 'git-finalize-*.py')

adapter_files() {
    case "$1" in
        legacy-namespace) printf 'git-finalize git-finalize-retirement-plan.py ' ;;
        controller-state-root)
            printf 'git-finalize git-finalize-integration-publish.py git-finalize-retirement-plan.py ' ;;
        controller-lease)
            printf 'git-finalize-integration-publish.py git-finalize-retirement-plan.py ' ;;
        snapshot-state-root) printf 'git-finalize-snapshot-verify.py ' ;;
        gitea-api) printf 'git-finalize-repo-bootstrap.py ' ;;
        retirement-plan-files) printf 'git-finalize-retirement-plan.py ' ;;
        *) printf 'unknown ledger: %s\n' "$1" >&2; return 1 ;;
    esac
}

assert_ledger() {
    local ledger=$1 expected hits
    shift
    local -a patterns=()
    local pattern
    for pattern in "$@"; do
        patterns+=(-e "$pattern")
    done
    expected=$(adapter_files "$ledger")
    hits=$(git -C "$project_root" grep -l -F "${patterns[@]}" -- \
        "${PRODUCTION_CODE[@]}" | sort | tr '\n' ' ') || true
    assert_equal "$expected" "$hits" \
        "external layout knowledge left its adapter (${ledger})"
}

test_adapter_markers_are_single_sites() {
    local entry file marker count
    for entry in \
        'git-finalize:worktree-controller-retirement' \
        'git-finalize-retirement-plan.py:worktree-controller-retirement' \
        'git-finalize-integration-publish.py:worktree-controller-integration-publication' \
        'git-finalize-snapshot-verify.py:snapshot-runner-evidence' \
        'git-finalize-repo-bootstrap.py:gitea-repository-bootstrap'; do
        file=${entry%%:*}
        marker=${entry#*:}
        count=$(grep -c "GF-INTEGRATION-ADAPTER: $marker" "$project_root/$file") || true
        assert_equal 1 "$count" "adapter marker for $marker must be a single site in $file"
    done
    grep -q -F 'test_integration_boundaries.sh' "$project_root/tests/run.sh" ||
        fail_assertion 'boundary contract suite is not wired into tests/run.sh'
}

test_layout_literals_do_not_spread() {
    assert_ledger legacy-namespace 'codex-worktree'
    assert_ledger controller-state-root \
        'worktree-controller/v1' '"worktree-controller" / "v1"'
    assert_ledger controller-lease 'publication-lease'
    assert_ledger retirement-plan-files \
        'branch-retirement-plans' 'branch-retirement-receipts'
    assert_ledger snapshot-state-root \
        'snapshot-runner/snapshots' '"snapshot-runner" / "snapshots"'
    assert_ledger gitea-api '/api/v1'
    if git -C "$project_root" grep -q -F 'Context Loader' -- "${PRODUCTION_CODE[@]}"; then
        fail_assertion 'core must not couple to Context Loader'
    fi
    if git -C "$project_root" grep -q -F 'project-context' -- "${PRODUCTION_CODE[@]}"; then
        fail_assertion 'core must not couple to Context Loader state'
    fi
}

test_documented_matrix_matches_code() {
    local needle needle_file
    while IFS=$'\t' read -r needle needle_file; do
        [[ -n $needle ]] || continue
        grep -q -F -- "$needle" "$doc" ||
            fail_assertion "boundary matrix lost its blocker wording: $needle"
        git -C "$project_root" grep -q -F -- "$needle" -- "$needle_file" ||
            fail_assertion "documented blocker missing from ${needle_file}: $needle"
    done <<'NEEDLES'
capabilities probe failed	git-finalize-retirement-plan.py
does not match plan schema	git-finalize-retirement-plan.py
already records a consumed receipt	git-finalize-retirement-plan.py
repo.lock 缺失或不安全	git-finalize
legacy codex-worktree namespace	git-finalize-retirement-plan.py
source review 要求完整 Controller linkage	git-finalize
snapshot evidence rejected	git-finalize-snapshot-verify.py
BLOCK_INVALID_CONFIG	git-finalize-repo-bootstrap.py
CREATE_ALLOWED	git-finalize-repo-bootstrap.py
NEEDLES
    for needle in 'XDG_STATE_HOME' 'summary_schema_version' \
        'resolve_blocker_and_retry' 'commit_retained' 'GF-INTEGRATION-ADAPTER'; do
        grep -q -F -- "$needle" "$doc" ||
            fail_assertion "boundary matrix lost its contract wording: $needle"
    done
    local referenced cited
    cited=$(grep -o 'tests/test_[a-z_0-9]*\.\(sh\|py\)\|tests/run\.sh' "$doc" | sort -u)
    for referenced in $cited; do
        [[ -f $project_root/$referenced ]] ||
            fail_assertion "boundary matrix cites a missing suite: $referenced"
    done
    [[ $(printf '%s\n' "$cited" | wc -l) -ge 8 ]] ||
        fail_assertion 'boundary matrix stopped citing its evidence suites'
}

make_scratch_repo() {
    local case_dir=$1 repo=$2 remote=$3
    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$remote"
    git init --quiet --initial-branch=main "$repo"
    git -C "$repo" config user.name 'Boundary Contract Author'
    git -C "$repo" config user.email 'boundary@example.invalid'
    printf 'a\n' >"$repo/f.txt"
    git -C "$repo" add -- f.txt
    git -C "$repo" commit --quiet -m seed
    git -C "$repo" remote add origin "$remote"
    git -C "$repo" push --quiet --set-upstream origin main
    printf 'b\n' >"$repo/f.txt"
    git -C "$repo" add -- f.txt
}

test_summary_machine_contract_is_frozen() {
    local case_dir=$tmp_root/summary repo actual documented
    repo=$case_dir/repo
    make_scratch_repo "$case_dir" "$repo" "$case_dir/remote.git"
    repo=$(cd -- "$repo" && pwd -P)
    "$finalizer" --summary --mode verify-only --repo "$repo" -- f.txt \
        >"$case_dir/ok.json" 2>"$case_dir/ok.err" || {
        sed -n '1,40p' "$case_dir/ok.json" "$case_dir/ok.err" >&2
        fail_assertion 'healthy verify-only run failed'
    }
    /usr/bin/python3 -B -c '
import json, sys
document = json.load(open(sys.argv[1]))
assert document["status"] == "success", document["status"]
assert document["final_phase"] == "complete", document["final_phase"]
assert document["next_action"] == "none", document["next_action"]
assert document["exit_code"] == 0, document["exit_code"]
assert document["summary_schema_version"] == 1
assert document["reason"] is None, document["reason"]
print(",".join(sorted(document)))
' "$case_dir/ok.json" >"$case_dir/keys"
    documented=$(grep -o 'allocation_id,authority_key,[a-z_,]*' "$doc" | head -1)
    assert_equal "$documented" "$(cat "$case_dir/keys")" \
        'summary top-level key set drifted from the documented machine contract'

    "$finalizer" --summary --mode verify-only --reviewed-sensitive-source \
        "$case_dir/missing-review.json" --repo "$repo" -- f.txt \
        >"$case_dir/blocked.json" 2>&1 || true
    /usr/bin/python3 -B -c '
import json, sys
document = json.load(open(sys.argv[1]))
assert document["status"] == "blocked", document["status"]
assert document["final_phase"] == "cli", document["final_phase"]
assert document["next_action"] == "resolve_blocker_and_retry", document["next_action"]
assert document["exit_code"] == 1, document["exit_code"]
assert document["reason"], "blocked summary lost its reason"
assert set(document) == set(json.load(open(sys.argv[2]))), "error path changed the key set"
' "$case_dir/blocked.json" "$case_dir/ok.json" ||
        fail_assertion 'blocked summary broke the machine contract'

    "$finalizer" --summary --mode commit-only --repo "$repo" \
        --message 'boundary: contract probe' -- f.txt \
        >"$case_dir/committed.json" 2>&1 || {
        sed -n '1,40p' "$case_dir/committed.json" >&2
        fail_assertion 'commit-only contract probe failed'
    }
    /usr/bin/python3 -B -c '
import json, sys
document = json.load(open(sys.argv[1]))
assert document["status"] == "success", document["status"]
assert document["commit"]["created"] is True
assert document["push"]["executed"] is False
assert document["push"]["result"] == "skipped_by_commit_only"
assert document["mode_result"]["remote_verification"] == "skipped_by_commit_only"
assert set(document) == set(json.load(open(sys.argv[2]))), "success path changed the key set"
' "$case_dir/committed.json" "$case_dir/ok.json" ||
        fail_assertion 'commit-only summary broke the machine contract'
}

private_state_tree() {
    local root=$1 snapshot_id=$2 directory
    mkdir -p -- "$root/snapshot-runner/snapshots"
    chmod 700 "$root" "$root/snapshot-runner" "$root/snapshot-runner/snapshots"
    if [[ -n $snapshot_id ]]; then
        directory=$root/snapshot-runner/snapshots/$snapshot_id
        mkdir -p -- "$directory"
        chmod 700 "$directory"
    fi
}

test_snapshot_adapter_is_state_relocatable() {
    local case_dir=$tmp_root/snapshot-state snapshot_id repo
    mkdir -p -- "$case_dir"
    case_dir=$(cd -- "$case_dir" && pwd -P)
    snapshot_id=$(printf 'a%.0s' $(seq 1 64))
    mkdir -p -- "$case_dir/repo"
    repo=$(cd -- "$case_dir/repo" && pwd -P)
    printf 'x\n' >"$repo/f"
    private_state_tree "$case_dir/state-present" "$snapshot_id"
    private_state_tree "$case_dir/state-absent" ''

    # A materialized artifact directory is reached only under the redirected
    # state root, proving the adapter never falls back to production state.
    if XDG_STATE_HOME=$case_dir/state-present /usr/bin/python3 -B \
        "$project_root/git-finalize-snapshot-verify.py" \
        "$snapshot_id" "$repo" head f >"$case_dir/present.log" 2>&1; then
        fail_assertion 'snapshot adapter accepted an empty artifact directory'
    fi
    grep -a -q -F 'contents are not canonical' "$case_dir/present.log" || {
        sed -n '1,20p' "$case_dir/present.log" >&2
        fail_assertion 'snapshot adapter did not resolve the redirected state root'
    }
    if XDG_STATE_HOME=$case_dir/state-absent /usr/bin/python3 -B \
        "$project_root/git-finalize-snapshot-verify.py" \
        "$snapshot_id" "$repo" head f >"$case_dir/absent.log" 2>&1; then
        fail_assertion 'snapshot adapter accepted a missing artifact directory'
    fi
    grep -a -q -F 'snapshot directory is unavailable' "$case_dir/absent.log" || {
        sed -n '1,20p' "$case_dir/absent.log" >&2
        fail_assertion 'snapshot adapter lost its fail-closed missing-artifact diagnosis'
    }
    if grep -a -F -q -- "${HOME:?}/.local/state" \
        "$case_dir/present.log" "$case_dir/absent.log"; then
        fail_assertion 'snapshot adapter leaked the production state root'
    fi
}

run_case 'adapter markers stay single-site and the suite is wired' \
    test_adapter_markers_are_single_sites
run_case 'external layout literals do not spread beyond adapters' \
    test_layout_literals_do_not_spread
run_case 'documented boundary matrix matches implemented blockers' \
    test_documented_matrix_matches_code
run_case 'summary machine contract and error taxonomy are frozen' \
    test_summary_machine_contract_is_frozen
run_case 'snapshot adapter is state-root relocatable' \
    test_snapshot_adapter_is_state_relocatable

printf 'all %s boundary contract groups passed\n' "$passed"
