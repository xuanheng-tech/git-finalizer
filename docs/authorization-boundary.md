# Authorization boundary

Who is allowed to ask a human before a Git write happens, and what Git Finalizer provides so that
question can be asked **once per task** instead of once per command.

Git Finalizer does not implement authorization and must never start to. It is a non-interactive CLI:
it reads no stdin, keeps no grant, remembers nothing between invocations, and reports what it did.
The component that decides whether to run it — an agent, an executor, or a human at a terminal — owns
the approval decision. `tests/test_authorization_boundary.py` and `tests/test_executor_contract.py`
hold both halves of that claim: the same command succeeds repeatedly from different executors with an
empty `HOME`, and no run leaves a state file behind that a later run could consult.

## Risk tiers

The tiers below are read out of `tool_cli_contract.json`, not invented here: `commands[].operation_class`
names the class of each interface and `protected_operations` lists what the tool refuses outright.
The approval scope an executor grants should be expressed in these terms.

| Tier | Contract class | Interfaces | What a granted scope may cover |
| --- | --- | --- | --- |
| T0 | `read_only` | `verify_only`, `repo_plan` | nothing to approve; no repository or remote state changes |
| T1 | `local_mutation` | `commit_only`, `initial_commit_only` | repeated commits inside one task, same repository and branch |
| T1 (gated) | `local_mutation` | `retire_local_branch` | never covered by an ordinary T1 grant: destructive, and requires a Controller-issued plan id plus expected OIDs |
| T2 | `remote_mutation` | `normal_publish`, `initial_publish`, `initial_branch_publish`, `publish_existing_branch`, `publish_existing_history`, `resume_publish`, `sync_published_branch`, `resume_initial_publish`, `resume_existing_history_publish`, `integration_candidate_publish`, `repo_ensure` | push of the task's own commits to the granted remote ref, non-force, re-verified after the fact |
| T2 (gated) | `remote_mutation` | `retire_remote_branch` | never covered by an ordinary T2 grant: deletes a remote ref, requires the exact ref plus `--expected-remote-oid`, and re-fetches live state before the compare-and-delete |
| T3 | `protected_operations` | `force_push`, `protected_branch_mutation`, `default_branch_retirement`, `local_branch_retirement_without_controller_plan`, `tag_mutation`, `semantic_equivalence_retirement` | not grantable: refused by the tool, so no scope expression can authorise them |

`sync_published_branch` is a separately named T2 action: it copies an exact already-published
OID between granted existing remotes and the same existing branch. Its local `branch` and
`upstream` remain unchanged, so the grant must bind its source and target fields above. It cannot
create a protected branch or deliver unpublished local changes; new main integration continues
through the Controller-leased interface. It grants no authority by itself.

T1 and T2 are separated on purpose. "Commit this" authorises a local commit and does **not** authorise
a push; the Skill's mode table enforces that mapping, and no tier here collapses the two.

## What a task-scoped grant has to name

For an approval to be reusable without becoming a blanket permission, it must be narrow enough that a
later command can be checked against it mechanically. Git Finalizer reports the matching fields in its
`--summary` object, so an executor can bind a grant to the same tuple and audit the use afterwards:

| Grant dimension | Field(s) that identify it at execution time |
| --- | --- |
| repository | `repository`, `repository_id`, `worktree_path` |
| ref and remote | `branch`, `upstream`, and the `--remote` / `--remote-branch` arguments; sync also binds `mode_result.source_remote`, `published_oid`, `expected_target_oid` and `push_target` |
| operation set | `mode` (the interface actually run), plus `commit.created` and `push.executed` in the receipt |
| task | `task_key`, `authority_key`, `allocation_id` — opaque linkage the Controller mints; `null` for ordinary use and therefore never a source of inference |
| tool identity | `finalizer_version`, so a grant can be pinned to the build that was reviewed |
| outcome to audit | `status`, `final_phase`, `next_action`, `reason`, `exit_code`, `commit.sha`, `push.result`, `push.post_verify` |

Two consequences follow. A grant that names only "git commands" is too wide to be safe and too vague
to audit. And a scope that expires by **use** — one approval consumed by one commit, as some layers in
this stack do — forces a fresh human decision for every step of a normal task, which is the friction
this document exists to remove.

Release-side actions are not in this table because Git Finalizer does not perform them: creating a tag,
publishing a release, and activating production have their own procedure in
[release-governance.md](release-governance.md). A continuing task grant may explicitly include
these acts within named repositories, existing channels and visibility; that grant remains valid
through ordinary delivery retries. A commit/push-only grant does not include them. Tag mutation
remains refused by this CLI even when the executor is authorised to use the release workflow.

`verify-only` preserves repository state but needs a writable temporary directory for its
isolated candidate object store. A filesystem sandbox that denies those writes can block this
read-only mode. The `candidate object temporary directory is unavailable` blocker identifies this
precondition before isolated candidate-object scanning; filter failures retain their existing
blocker. Use an
executor-authorised writable temporary directory or native channel; never weaken its permissions or
skip candidate scanning to turn that refusal into a success. A writable `TMPDIR` can select the
normal Python temporary location, but Python may fall back to other locations when it is unusable.
This temporary-store check belongs to verify-only; commit-only retains its existing staging checks.

## Where the remaining blocker lives

The gap between per-command and per-task approval is not inside this repository:

- The CLI permission layer in front of the agent matches **exact command strings**, so any change to a
  path, flag or message produces a new prompt. Scoping approvals by tier and repository is the
  executor's job; the fields above are what it needs, and they are already published.
- Agent Workspace models finalization requests, decisions and executions with a hash chain, but its
  request hash includes `expected_head_oid`, so committing invalidates the artefact and the next
  commit needs a new request. It also has no caller in production code today. Fixing the granularity
  there means scoping a grant to repository + ref + operation set + task, not to one pre-state.
- The Worktree Controller deliberately issues single-use grants (`one plan, one operation, one use`).
  That is correct for retirement, where each destructive CAS must be re-checked, and must not be
  generalised to ordinary delivery.

Git Finalizer's obligation to those layers is only this: publish the class of every interface, refuse
the protected operations, never hold state between runs, and never ask. If a future version needs to
expose the tier inside the `--summary` object, that is a contract change and belongs to a new
`contract_version` — it is deliberately not done here, because consumers today can derive the tier from
the `mode` they requested.
