# Agent contract

Machine-readable results for automation. This page is the public, versioned description of what
`git-finalize --summary` emits and how to act on it. Human-readable output is a convenience; an
agent should request `--summary` exactly once per invocation and parse the single JSON object it
prints.

`tests/test_integration_boundaries.sh` extracts the vocabularies below from the implementation and
fails if a value exists in code but not here, so this page cannot silently drift.

## Envelope

One JSON object per run, always present when a bash-owned lifecycle command runs with
`--summary`, including on refusals. Field set is mode-independent:

```
allocation_id, authority_key, branch, commit, dry_run, exit_code, final_phase,
finalizer_version, mode, mode_result, next_action, next_action_omitted_characters, push,
reason, reason_omitted_characters, repository, repository_id, requested_path_count, role,
status, summary_schema_version, task_key, upstream, warning_characters_omitted, warnings,
warnings_omitted, worktree_path
```

`summary_schema_version` is `1`. Three keys are conditional and must be treated as optional:

| Key | Appears when |
| --- | --- |
| `resume` | a resume interface reported recovery context |
| `fixture_exceptions` | the run consumed documented fixture exceptions |
| `reviewed_sensitive_sources` | `--reviewed-sensitive-source` reviews were consumed |

Governance identity fields (`allocation_id`, `authority_key`, `task_key`, `role`,
`worktree_path`, `repository_id`) are `null` for ordinary use; they exist so an orchestration
layer can correlate a run with its own records. Do not infer anything from their absence.

If the serializer itself cannot produce a compliant object, the fallback object uses a different
key shape (`summary_status`, `operation_status`, `operation_exit_code`). Treat that as a failure,
never as a partial success.

## Classification triple

Classify on `(final_phase, status, next_action)`. Never parse `reason`: it is human-oriented
diagnostic prose and may be localized; long values are truncated with the omitted count reported
in `reason_omitted_characters`.

### `status`

| Value | Meaning |
| --- | --- |
| `success` | the requested operation completed with its own verification passed |
| `blocked` | a precondition, authorization or safety gate refused; nothing was mutated that was not already authorized |
| `failed` | work started and could not be completed or confirmed; inspect `commit` and `push` before retrying |

`commit_retained` is **not** a status. It is mode-state vocabulary reported inside
`mode_result`/`summary_mode_state` to say "a commit exists but publication did not land".

### `final_phase`

| Value | Meaning |
| --- | --- |
| `cli` | argument or authorization validation |
| `preflight` | early repository/state preflight |
| `remote_preflight` | remote reachability and divergence preflight |
| `local_validation` | local submission checks |
| `staging` | index staging |
| `commit` | commit creation |
| `local_mutation` | a governed local write (for example a branch ref compare-and-set) |
| `remote_mutation` | the push or remote ref operation |
| `post_commit_verify` | verification immediately after commit |
| `post_verify` | live remote verification after publication |
| `push` | push transport stage |
| `complete` | finished, including its verification |

### `next_action`

Closed enumeration:

| Value | Agent behaviour |
| --- | --- |
| `none` | nothing to do; success |
| `resolve_blocker_and_retry` | governance, authorization, scope or safety blocker; stop and fix the precondition |
| `inspect_failure` | inspect the reported failure before deciding |
| `inspect_failure_with_local_commit_retained` | a commit exists locally and the failure is after it; read `commit.sha` |
| `inspect_local_root_commit` | a root commit exists locally; publication did not |
| `inspect_existing_branch_publish_failure` | first branch publication failed; inspect remote state before retrying |
| `inspect_initial_branch_publish_failure` | initial-branch publication failed; inspect before retrying |
| `resume_initial_publish` | re-enter the initial-publication resume interface with the reported OID |
| `resume_existing_history_publish` | re-enter the existing-history resume interface |
| `retry_same_resume` | the same resume call is safe to repeat |
| `inspect_remote_state_then_rerun_same_retirement` | verify remote OID state, then rerun the same retirement request |
| `rerun_without_summary_for_diagnostics` | rerun without `--summary` to see full diagnostics |
| `run_without_dry_run_after_review` | reviewed; rerun without `--dry-run` |
| `publish_install_verify_then_run_without_dry_run` | complete the install/verify step, then rerun without `--dry-run` |

### `push.result`

| Value | Meaning |
| --- | --- |
| `not_run` | no push was attempted |
| `succeeded` | push returned success and live verification confirmed it |
| `confirmed_after_uncertain` | the push outcome was initially uncertain and was then confirmed from the remote |
| `uncertain` | the push could not be confirmed; **do not** treat the commit as published, and do not force-push a "fix" |
| `skipped_by_verify_only` | mode never touches the remote |
| `skipped_by_commit_only` | mode never touches the remote |
| `skipped_by_initial_commit_only` | mode never touches the remote |

`push.executed`, `push.branch_refspec_only` and `push.follow_tags_requested` describe how the push
was performed: publication is always a non-force, branch-only refspec with tag following disabled.

### `mode`

| Value | Selected by |
| --- | --- |
| `verify-only` | `--mode verify-only` |
| `commit-only` | `--mode commit-only` |
| `normal` | default mode: commit then publish |
| `initial` | initial publication of an unborn repository |
| `initial-commit-only` | local root commit only |
| `initial_branch` | first publication of a new branch with existing history |
| `publish_existing_branch` | `--publish-existing-branch` |
| `publish_existing_history` | `--publish-existing-history` |
| `resume` | `--resume-publish` and the other resume interfaces |
| `retire_local_branch` | `--retire-local-branch` |
| `retire_remote_branch` | `--retire-remote-branch` |

`mode` is an internal envelope label and does not always equal a command name in
`tool_cli_contract.json`; match on the triple plus `mode`.

## Exit codes

Bash-owned lifecycle commands use **only 0 and 1**, and `exit_code` in the JSON equals the process
exit status: `0` for `success`, `1` otherwise (blocked or failed).

Exec-forwarded companion commands emit their **own** receipt rather than this envelope, so do not
assume the same keys:

- `--repo-plan` / `--repo-ensure`: keys include `operation`, `decision`, `target`, `remote`,
  `bootstrap`, `publication`, `errors`; no `exit_code`, `final_phase` or `mode_result`. Exit `0`
  success, `2` argparse usage error or a `BLOCK_*` refusal (`status=blocked`), `3` a failure whose
  decision could not be classified (`status=failed`). Whether any remote change already happened is
  reported by `bootstrap.executed`, not by the exit code: a `BLOCK_REMOTE_MISMATCH` can be raised
  after a repository was created.
- `--publish-integration-candidate`: a seven-key verdict object
  (`final_phase, finalizer_version, mode, next_action, reason, status, summary_schema_version`)
  with its own `next_action` vocabulary, for example
  `read_remote_fact_and_reenter_controller_publish_gate`. Exit codes `0`, `1`, and argparse `2`.

`1` is also possible on either companion when the entrypoint's pre-execution guard finds the
companion file missing or unsafe, or when an uncaught traceback occurs.

## Examples

Real output captured from the released `1.4.0` binary against a local scratch repository with a
local bare remote. `finalizer_version` always mirrors the running binary, so expect the version you
installed there; every other field is stable across releases and is asserted against the source by
`tests/test_agent_contract.py`.

Success (`--summary --repo <repo> --message 'docs: capture success example' -- f.txt`):

```json
{
 "summary_schema_version": 1,
 "finalizer_version": "1.4.0",
 "mode": "normal",
 "status": "success",
 "final_phase": "complete",
 "exit_code": 0,
 "repository": "/tmp/example/repo",
 "repository_id": null,
 "allocation_id": null,
 "task_key": null,
 "authority_key": null,
 "worktree_path": null,
 "role": null,
 "branch": "main",
 "upstream": "origin/main",
 "requested_path_count": 1,
 "dry_run": false,
 "commit": { "created": true, "sha": "6e14cfe95b57abf60ce0f56e7ffafbdb147e3c5b" },
 "push": { "executed": true, "result": "succeeded", "branch_refspec_only": true, "follow_tags_requested": false },
 "warnings": [],
 "warnings_omitted": 0,
 "warning_characters_omitted": 0,
 "reason": null,
 "reason_omitted_characters": 0,
 "next_action": "none",
 "next_action_omitted_characters": 0,
 "mode_result": { "post_verify": "passed" }
}
```

Blocked, before anything was written (`--summary --mode verify-only --reviewed-sensitive-source
<missing file> --repo <repo> -- f.txt`, exit status `1`):

```json
{
 "summary_schema_version": 1,
 "finalizer_version": "1.4.0",
 "mode": "verify-only",
 "status": "blocked",
 "final_phase": "cli",
 "exit_code": 1,
 "requested_path_count": 1,
 "reason": "source review 要求完整 Controller linkage",
 "next_action": "resolve_blocker_and_retry"
}
```

Only the fields that matter for the classification are shown; the real object always carries the
full key set. Note that `reason` is prose and may be Chinese — classify on
`status`/`final_phase`/`next_action`.

`mode_result` is mode-specific: `verify-only` reports local validation sub-results, `commit-only`
and `initial-commit-only` mark remote stages as skipped, and the publication modes report
`post_verify`. Read the keys you need for the mode you invoked; do not assume identical
sub-shapes across modes.

### `mode_result` sub-field vocabularies

`mode_result` is a small fixed set of per-stage verdicts. These are machine tokens, not prose.

`commit` (how the commit stage ended): `not_run`, `reused` (an existing commit was published without
creating a new one), `skipped_by_verify_only`.

`post_verify` (live verification after the write): `passed`, `failed`, `not_run`,
`skipped_by_dry_run`, `skipped_by_commit_only`, `skipped_by_initial_commit_only`,
`skipped_by_verify_only`.

`remote_conclusion` (what the live remote was found to hold):

| Family | Values |
| --- | --- |
| Not applicable | `not_checked`, `skipped_by_commit_only`, `skipped_by_initial_commit_only`, `skipped_by_verify_only` |
| Preflight observation | `empty_remote`, `empty_repository_reverified`, `expected_branch_present`, `fast_forward_publish_ready`, `retirement_preflight_passed`, `target_absent_reverified`, `target_absent_verified`, `empty_remote_reverified`, `empty_remote_verified` |
| Published | `published_and_upstream_aligned` |
| Already in place | `already_absent_verified`, `local_branch_already_absent_verified` |
| Retired | `local_branch_retired_verified`, `remote_branch_retired_verified` |
| After an uncertain push | `empty_after_failed_push`, `expected_target_present_after_failed_push`, `target_absent_after_failed_push`, `remote_state_unknown_after_failed_push`, `conflicting_or_ambiguous_after_failed_push`, `conflicting_or_unknown_after_failed_push`, `empty_or_expected_target_reverified` |
| Unsafe | `conflicting_or_ambiguous` |

`mode_state` (the run's overall stage label): `not_started`, `preflight`, `verified`, `dry_run_complete`,
`commit_created`, `committed`, `pushed`, `commit_retained` (a commit exists but publication did not
land), `recovering`, `passed`.

Retirement runs additionally report `mode_result.result` with the uppercase verdicts
`LOCAL_BRANCH_RETIRED_VERIFIED`, `LOCAL_DELETE_UNVERIFIED`, `REMOTE_BRANCH_RETIRED_VERIFIED`,
`ALREADY_ABSENT_VERIFIED`, `RETIREMENT_PREFLIGHT_PASSED`, `RETIREMENT_BLOCKED` and
`REMOTE_DELETE_UNVERIFIED`.

### Documented asymmetry: nothing to stage

The same underlying condition, "the requested explicit paths contain no staged change", is
classified differently by mode. This is released behaviour (`1.3.0` and `1.4.0`, unchanged since)
`tests/test_agent_contract.py`-pinned, so an agent can rely on it:

| Mode | `status` | `final_phase` | `next_action` |
| --- | --- | --- | --- |
| `--mode verify-only` | `blocked` | `local_validation` | `resolve_blocker_and_retry` |
| `--mode commit-only` | `failed` | `staging` | `inspect_failure` |
| default (commit + publish) | `failed` | `staging` | `inspect_failure` |

In every case `commit.created` is `false`, `push.executed` is `false`, and no repository state was
written: the reason text is identical. `failed` here means "the write stage could not start", not
"the repository is broken"; do not retry with force options, and do not treat it as publication
failure. A future classification batch may normalise this to `blocked`; until then, read the table.

## Interpretation rules for agents

1. `exit_code` and `status` are authoritative for the run; the process exit status equals
   `exit_code` for bash-owned commands.
2. Never upgrade a result. `push.result` of `uncertain` is not a success and is not something to
   "fix" with a force push; only `succeeded` or `confirmed_after_uncertain`, together with
   `post_verify` evidence in `mode_result`, means the remote holds what you published.
3. `blocked` means the requested write did not happen **as a result of this call**. If `commit`
   shows `created: true`, a commit exists and you must report that fact.
4. `warnings` is bounded; `warnings_omitted` and `*_omitted_characters` tell you that detail was
   dropped. Rerun without `--summary` (or with `rerun_without_summary_for_diagnostics`) when the
   omitted part matters.
5. A blocker naming another project's authority (for example a missing governance lock or an
   unattested capability) is not a bug in this tool and must not be worked around by calling raw
   Git commands.
