#!/usr/bin/env bash

set -Eeuo pipefail

export LC_ALL=C
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
finalizer=$project_root/codex-git-finalize
tmp_root=$(mktemp -d /tmp/git-finalizer-summary-eval-XXXXXX)
current_case='startup'
passed=0
declare -a metric_arguments=()

cleanup() {
    case "$tmp_root" in
        /tmp/git-finalizer-summary-eval-*) rm -rf -- "$tmp_root" ;;
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

assert_file_excludes() {
    local path=$1
    local text=$2
    local message=$3

    if grep -Fq -- "$text" "$path"; then
        fail_assertion "$message"
    fi
}

run_capture() {
    local expected_status=$1
    local output=$2
    shift 2
    local actual_status

    if "$@" >"$output" 2>&1; then
        actual_status=0
    else
        actual_status=$?
    fi
    assert_equal "$expected_status" "$actual_status" 'unexpected command exit status'
}

assert_json_value() {
    local path=$1
    local dotted_key=$2
    local expected_json=$3

    /usr/bin/python3 -B - "$path" "$dotted_key" "$expected_json" <<'PY'
import json
from pathlib import Path
import sys

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for key in sys.argv[2].split("."):
    value = value[key]
expected = json.loads(sys.argv[3])
if value != expected:
    raise SystemExit(f"{sys.argv[2]}: expected {expected!r}, got {value!r}")
PY
}

assert_summary_contract() {
    local path=$1

    /usr/bin/python3 -B - "$path" <<'PY'
import json
from pathlib import Path
import sys

raw = Path(sys.argv[1]).read_text(encoding="utf-8")
if raw.count("\n") != 1 or not raw.endswith("\n"):
    raise SystemExit("summary is not exactly one JSON line")
data = json.loads(raw)
expected_keys = [
    "summary_schema_version",
    "finalizer_version",
    "mode",
    "status",
    "final_phase",
    "exit_code",
    "repository",
    "branch",
    "upstream",
    "requested_path_count",
    "dry_run",
    "commit",
    "push",
    "warnings",
    "warnings_omitted",
    "warning_characters_omitted",
    "reason",
    "reason_omitted_characters",
    "next_action",
    "next_action_omitted_characters",
    "mode_result",
]
if list(data)[: len(expected_keys)] != expected_keys:
    raise SystemExit("summary field order changed")
if len(data["warnings"]) > 5:
    raise SystemExit("warnings are not bounded")
PY
}

configure_author() {
    local repo=$1

    git -C "$repo" config user.name 'Synthetic Summary Author'
    git -C "$repo" config user.email 'summary@example.invalid'
    git -C "$repo" config commit.gpgsign false
}

make_synced_repo() {
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
    git -C "$test_repo" push --quiet --set-upstream origin HEAD:refs/heads/main
    printf 'SUMMARY_FILE_BODY_SENTINEL\n' >>"$test_repo/wanted.txt"
}

make_unborn_repo() {
    local case_dir=$1

    test_repo=$case_dir/repo
    test_remote=$case_dir/remote.git
    mkdir -p -- "$case_dir"
    git init --quiet --bare --initial-branch=main "$test_remote"
    git init --quiet --initial-branch=main "$test_repo"
    configure_author "$test_repo"
    git -C "$test_repo" remote add origin "$test_remote"
    printf 'SUMMARY_FILE_BODY_SENTINEL\n' >"$test_repo/first.txt"
}

make_resume_repo() {
    local case_dir=$1

    make_unborn_repo "$case_dir"
    git -C "$test_repo" add -- first.txt
    git -C "$test_repo" commit --quiet -m root
    expected_head=$(git -C "$test_repo" rev-parse HEAD)
}

remote_refs() {
    git --git-dir="$1" for-each-ref --format='%(objectname)%09%(refname)'
}

record_metric() {
    metric_arguments+=("$1" "$2" "$3")
}

test_normal_success() {
    local case_dir=$tmp_root/normal-success
    local default_output=$case_dir/default/output.log
    local summary_output=$case_dir/summary/output.log
    local default_repo default_remote summary_repo summary_remote

    make_synced_repo "$case_dir/default"
    default_repo=$test_repo
    default_remote=$test_remote
    run_capture 0 "$default_output" "$finalizer" --repo "$default_repo" \
        --message SUMMARY_MESSAGE_SENTINEL -- wanted.txt

    make_synced_repo "$case_dir/summary"
    summary_repo=$test_repo
    summary_remote=$test_remote
    run_capture 0 "$summary_output" "$finalizer" --summary --repo "$summary_repo" \
        --message SUMMARY_MESSAGE_SENTINEL -- wanted.txt

    assert_file_contains "$default_output" 'Git Finalizer 完成' \
        'default success output contract changed'
    assert_file_excludes "$default_output" '"summary_schema_version"' \
        'default mode emitted summary JSON'
    assert_summary_contract "$summary_output"
    assert_json_value "$summary_output" status '"success"'
    assert_json_value "$summary_output" mode '"normal"'
    assert_json_value "$summary_output" commit.created 'true'
    assert_json_value "$summary_output" push.result '"succeeded"'
    assert_json_value "$summary_output" mode_result.post_verify '"passed"'
    assert_file_excludes "$summary_output" 'SUMMARY_FILE_BODY_SENTINEL' \
        'summary disclosed file content'
    assert_file_excludes "$summary_output" 'SUMMARY_MESSAGE_SENTINEL' \
        'summary disclosed the commit message'
    assert_file_excludes "$summary_output" "$summary_remote" \
        'summary disclosed the remote endpoint'
    assert_file_excludes "$summary_output" '$ git' 'summary included command logs'
    assert_file_excludes "$summary_output" '"resume":' \
        'successful normal summary invented a resume reference'
    assert_equal "$(git -C "$default_repo" rev-parse 'HEAD^{tree}')" \
        "$(git -C "$summary_repo" rev-parse 'HEAD^{tree}')" \
        'summary changed the committed tree'
    assert_equal "$(git -C "$default_repo" rev-parse HEAD)" \
        "$(git --git-dir="$default_remote" rev-parse refs/heads/main)" \
        'default push did not publish its commit'
    assert_equal "$(git -C "$summary_repo" rev-parse HEAD)" \
        "$(git --git-dir="$summary_remote" rev-parse refs/heads/main)" \
        'summary push did not publish its commit'
    assert_equal '1' "$(remote_refs "$summary_remote" | wc -l)" \
        'summary mode pushed an extra ref'
    record_metric normal_success "$default_output" "$summary_output"
}

test_initial_success() {
    local case_dir=$tmp_root/initial-success
    local default_output=$case_dir/default/output.log
    local summary_output=$case_dir/summary/output.log
    local summary_repo summary_remote

    make_unborn_repo "$case_dir/default"
    run_capture 0 "$default_output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message initial -- first.txt

    make_unborn_repo "$case_dir/summary"
    summary_repo=$test_repo
    summary_remote=$test_remote
    run_capture 0 "$summary_output" "$finalizer" --summary --initial-publish \
        --remote origin --repo "$summary_repo" --message initial -- first.txt

    assert_summary_contract "$summary_output"
    assert_json_value "$summary_output" mode '"initial"'
    assert_json_value "$summary_output" status '"success"'
    assert_json_value "$summary_output" mode_result.initial_publish '"verified"'
    assert_json_value "$summary_output" mode_result.snapshot_verification '"not_requested"'
    assert_json_value "$summary_output" mode_result.remote_conclusion \
        '"published_and_upstream_aligned"'
    assert_file_excludes "$summary_output" "$summary_remote" \
        'initial summary disclosed the remote endpoint'
    assert_file_excludes "$summary_output" '"resume":' \
        'successful initial summary invented a resume reference'
    assert_equal "$(git -C "$summary_repo" rev-parse HEAD)" \
        "$(git --git-dir="$summary_remote" rev-parse refs/heads/main)" \
        'summary initial publish remote mismatch'
    assert_equal '1' "$(remote_refs "$summary_remote" | wc -l)" \
        'summary initial publish created an extra ref'
    record_metric initial_success "$default_output" "$summary_output"
}

test_resume_success() {
    local case_dir=$tmp_root/resume-success
    local default_output=$case_dir/default/output.log
    local summary_output=$case_dir/summary/output.log
    local summary_repo summary_remote summary_head

    make_resume_repo "$case_dir/default"
    run_capture 0 "$default_output" "$finalizer" --resume-initial-publish \
        "$expected_head" --remote origin --repo "$test_repo"

    make_resume_repo "$case_dir/summary"
    summary_repo=$test_repo
    summary_remote=$test_remote
    summary_head=$expected_head
    run_capture 0 "$summary_output" "$finalizer" --summary --resume-initial-publish \
        "$summary_head" --remote origin --repo "$summary_repo"

    assert_summary_contract "$summary_output"
    assert_json_value "$summary_output" mode '"resume"'
    assert_json_value "$summary_output" status '"success"'
    assert_json_value "$summary_output" commit.created 'false'
    assert_json_value "$summary_output" commit.sha "\"$summary_head\""
    assert_json_value "$summary_output" mode_result.commit_reused 'true'
    assert_json_value "$summary_output" mode_result.recovery_start '"empty_remote"'
    assert_json_value "$summary_output" mode_result.resume_required 'false'
    assert_file_excludes "$summary_output" '"resume":' \
        'successful resume summary invented another resume reference'
    assert_equal "$summary_head" \
        "$(git --git-dir="$summary_remote" rev-parse refs/heads/main)" \
        'summary resume remote mismatch'
    record_metric resume_success "$default_output" "$summary_output"
}

test_preflight_blocker() {
    local case_dir=$tmp_root/preflight-blocker
    local default_output=$case_dir/default/output.log
    local summary_output=$case_dir/summary/output.log
    local head_before

    make_synced_repo "$case_dir/default"
    git -C "$test_repo" config --unset-all branch.main.remote
    git -C "$test_repo" config --unset-all branch.main.merge
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    run_capture 1 "$default_output" "$finalizer" --repo "$test_repo" \
        --message blocked -- wanted.txt
    assert_equal "$head_before" "$(git -C "$test_repo" rev-parse HEAD)" \
        'default blocker created a commit'

    make_synced_repo "$case_dir/summary"
    git -C "$test_repo" config --unset-all branch.main.remote
    git -C "$test_repo" config --unset-all branch.main.merge
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    run_capture 1 "$summary_output" "$finalizer" --summary --repo "$test_repo" \
        --message blocked -- wanted.txt
    assert_summary_contract "$summary_output"
    assert_json_value "$summary_output" status '"blocked"'
    assert_json_value "$summary_output" commit.created 'false'
    assert_json_value "$summary_output" push.executed 'false'
    assert_equal "$head_before" "$(git -C "$test_repo" rev-parse HEAD)" \
        'summary blocker created a commit'
    record_metric simple_blocker "$default_output" "$summary_output"
}

install_failing_hook() {
    local repo=$1
    local name=$2

    printf '#!/usr/bin/env bash\nexit 1\n' >"$repo/.git/hooks/$name"
    chmod 700 "$repo/.git/hooks/$name"
}

test_recoverable_initial_push_failure() {
    local case_dir=$tmp_root/recoverable-failure
    local default_output=$case_dir/default/output.log
    local summary_output=$case_dir/summary/output.log
    local retained_head

    make_unborn_repo "$case_dir/default"
    install_failing_hook "$test_repo" pre-push
    run_capture 1 "$default_output" "$finalizer" --initial-publish --remote origin \
        --repo "$test_repo" --message retained -- first.txt
    git -C "$test_repo" rev-parse --verify HEAD >/dev/null

    make_unborn_repo "$case_dir/summary"
    install_failing_hook "$test_repo" pre-push
    run_capture 1 "$summary_output" "$finalizer" --summary --initial-publish \
        --remote origin --repo "$test_repo" --message retained -- first.txt
    retained_head=$(git -C "$test_repo" rev-parse --verify HEAD)
    assert_summary_contract "$summary_output"
    assert_json_value "$summary_output" status '"failed"'
    assert_json_value "$summary_output" final_phase '"push"'
    assert_json_value "$summary_output" commit.created 'true'
    assert_json_value "$summary_output" push.executed 'true'
    assert_json_value "$summary_output" push.result '"uncertain"'
    assert_json_value "$summary_output" next_action '"resume_initial_publish"'
    assert_json_value "$summary_output" resume.head_oid "\"$retained_head\""
    assert_equal '' "$(remote_refs "$test_remote")" \
        'failed summary initial push changed the remote'
    record_metric recoverable_failure "$default_output" "$summary_output"
}

test_commit_failure_without_resume() {
    local case_dir=$tmp_root/terminal-failure
    local default_output=$case_dir/default/output.log
    local summary_output=$case_dir/summary/output.log
    local head_before

    make_synced_repo "$case_dir/default"
    install_failing_hook "$test_repo" pre-commit
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    run_capture 1 "$default_output" "$finalizer" --repo "$test_repo" \
        --message rejected -- wanted.txt
    assert_equal "$head_before" "$(git -C "$test_repo" rev-parse HEAD)" \
        'default commit failure created a commit'

    make_synced_repo "$case_dir/summary"
    install_failing_hook "$test_repo" pre-commit
    head_before=$(git -C "$test_repo" rev-parse HEAD)
    run_capture 1 "$summary_output" "$finalizer" --summary --repo "$test_repo" \
        --message rejected -- wanted.txt
    assert_summary_contract "$summary_output"
    assert_json_value "$summary_output" status '"failed"'
    assert_json_value "$summary_output" final_phase '"commit"'
    assert_json_value "$summary_output" commit.created 'false'
    assert_json_value "$summary_output" push.executed 'false'
    assert_json_value "$summary_output" next_action '"inspect_failure"'
    assert_equal "$head_before" "$(git -C "$test_repo" rev-parse HEAD)" \
        'summary commit failure created a commit'
    record_metric terminal_failure "$default_output" "$summary_output"
}

test_deterministic_and_bounded_output() {
    local case_dir=$tmp_root/deterministic-bounded
    local first=$case_dir/first.json
    local second=$case_dir/second.json
    local warnings=$case_dir/warnings.json
    local path
    local -a unchanged_paths=()

    make_synced_repo "$case_dir"
    run_capture 0 "$first" "$finalizer" --summary --dry-run --repo "$test_repo" \
        --message deterministic -- wanted.txt
    run_capture 0 "$second" "$finalizer" --summary --dry-run --repo "$test_repo" \
        --message deterministic -- wanted.txt
    cmp -s -- "$first" "$second" || fail_assertion 'same input produced different summaries'

    for path in one two three four five six; do
        printf '%s\n' "$path" >"$test_repo/$path.txt"
        unchanged_paths+=("$path.txt")
    done
    git -C "$test_repo" add -- "${unchanged_paths[@]}"
    git -C "$test_repo" commit --quiet -m warning-fixtures
    git -C "$test_repo" push --quiet origin HEAD:refs/heads/main
    run_capture 0 "$warnings" "$finalizer" --summary --dry-run --repo "$test_repo" \
        --message warnings -- "${unchanged_paths[@]}"
    assert_summary_contract "$warnings"
    assert_json_value "$warnings" warnings_omitted '2'
    assert_json_value "$warnings" dry_run 'true'
}

report_metrics() {
    /usr/bin/python3 -B - "${metric_arguments[@]}" <<'PY'
from pathlib import Path
from statistics import median
import sys

arguments = sys.argv[1:]
if len(arguments) % 3:
    raise SystemExit("invalid metric arguments")

character_reductions = []
byte_reductions = []
total_default_characters = 0
total_summary_characters = 0
total_default_bytes = 0
total_summary_bytes = 0
for index in range(0, len(arguments), 3):
    name, default_path, summary_path = arguments[index : index + 3]
    default_bytes_value = Path(default_path).read_bytes()
    summary_bytes_value = Path(summary_path).read_bytes()
    default_characters = len(default_bytes_value.decode("utf-8"))
    summary_characters = len(summary_bytes_value.decode("utf-8"))
    default_bytes = len(default_bytes_value)
    summary_bytes = len(summary_bytes_value)
    character_reduction = 100 * (default_characters - summary_characters) / default_characters
    byte_reduction = 100 * (default_bytes - summary_bytes) / default_bytes
    character_reductions.append(character_reduction)
    byte_reductions.append(byte_reduction)
    total_default_characters += default_characters
    total_summary_characters += summary_characters
    total_default_bytes += default_bytes
    total_summary_bytes += summary_bytes
    print(
        "summary_efficiency"
        f" scenario={name}"
        f" default_characters={default_characters}"
        f" summary_characters={summary_characters}"
        f" character_reduction_percentage={character_reduction:.2f}"
        f" default_bytes={default_bytes}"
        f" summary_bytes={summary_bytes}"
        f" byte_reduction_percentage={byte_reduction:.2f}"
    )

total_character_reduction = 100 * (
    total_default_characters - total_summary_characters
) / total_default_characters
total_byte_reduction = 100 * (total_default_bytes - total_summary_bytes) / total_default_bytes
if total_character_reduction <= 0 or total_byte_reduction <= 0:
    raise SystemExit("summary did not reduce aggregate output")
print(
    "summary_efficiency_total"
    f" sample_count={len(character_reductions)}"
    f" default_characters={total_default_characters}"
    f" summary_characters={total_summary_characters}"
    f" character_reduction_percentage={total_character_reduction:.2f}"
    f" default_bytes={total_default_bytes}"
    f" summary_bytes={total_summary_bytes}"
    f" byte_reduction_percentage={total_byte_reduction:.2f}"
    f" median_character_reduction_percentage={median(character_reductions):.2f}"
    f" median_byte_reduction_percentage={median(byte_reductions):.2f}"
)
PY
}

run_case() {
    current_case=$1
    shift
    "$@"
    passed=$((passed + 1))
    printf 'ok %s - %s\n' "$passed" "$current_case"
}

run_case 'normal summary preserves commit and push results' test_normal_success
run_case 'initial summary preserves root publication results' test_initial_success
run_case 'resume summary reports reuse and remaining publication' test_resume_success
run_case 'preflight blocker remains non-mutating and nonzero' test_preflight_blocker
run_case 'post-commit failure retains a bounded resume reference' \
    test_recoverable_initial_push_failure
run_case 'commit failure remains nonzero without a false resume path' \
    test_commit_failure_without_resume
run_case 'summary serialization is deterministic and bounded' \
    test_deterministic_and_bounded_output
report_metrics
printf '1..%s\n' "$passed"
