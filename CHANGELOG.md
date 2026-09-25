# Changelog

This file is the authoritative record of version-relevant behavior in this repository. Entries are
limited to facts verified from source and Git history. Dates are included only when verified from
the specific Git evidence stated in each section. For versions with backfilled tags, the heading
date remains the original source-version commit date; it is not the tag creation date or a formal
release date.

The initial commit `7dd9a6af1dbe07c365120a6460bc92b3384d6cc5` used the internal source identifier
`0.1` and introduced explicit-path staging, commit creation, fast-forward-safe branch push, and
post-push verification. It was never tagged, and no `0.1.0` version existed, so no SemVer version
section is invented for it.

## Unreleased

No pending unreleased changes are recorded. The bullets previously listed here described work
already contained in the trees released as 1.1.0 and 1.1.1 (including the tool formerly named
`codex-skill-sync`, renamed `tool-skill-sync` in 1.0.0 and shipped at its declared 2.0.0), and
the tool-version numbers quoted in them (1.2.0/1.3.0/1.5.0) were pre-rename source versions of
that same component. The authoritative per-version record remains the dated sections below; per
the header rule this note does not backfill or rewrite those records.

Historical addendum (verbatim; already contained in the 1.1.0 and 1.1.1 released trees):

- Update `codex-skill-sync` to 1.5.0 with a declared canonical uv installation policy.
  `tools.<tool>.uv_install_policy` in `toolchain_compatibility.json` is authoritative for the
  entrypoint bin directory, package index and `--no-build`, so `upgrade` normalizes a drifted
  receipt instead of faithfully preserving it. Without a declared policy the observed receipt
  is still preserved. A deployment-only normalization reports `NORMALIZED`, and the intended
  bin-directory move relaxes only the retained-path assertions while every other verification
  stays fail closed.
- Declare the Snapshot Runner policy: dedicated `~/.local/share/snapshot-runner/bin` entrypoint
  ownership, `https://pypi.org/simple` as the deterministic package source, and `--no-build`.
- Fix a misleading diagnostic: entrypoints that resolve into no uv tool installation now say so
  instead of reporting more than one root.

- Migrate the Snapshot Runner integration to the provider-neutral 2.0.0 contract. The
  manifest now declares `2.0.0` with the single `snapshot-runner` entrypoint, the toolchain
  binding moves Snapshot Runner's public CLI contract to version 2, the Skill reference
  documents `snapshot-runner <command>` and the neutral
  `~/.local/state/snapshot-runner/snapshots/` artifact namespace, and the PreToolUse bridge
  treats `/home/hsd/bin/snapshot-runner` as the inert read-only reference entry in place of
  the removed `codex-diff-audit` alias.

- Record the Snapshot Runner dual-remote topology: one canonical source tree and one commit
  history hosted on both GitHub (public source/release/PyPI authority, Finalizer upstream)
  and Gitea (governed daily delivery). "Single source of truth" means one source tree and
  one history, not one remote. Documents the explicit non-force dual-push, dual branch-OID
  post-verify, release tag-object/peeled-commit parity check, missing-remote-only retry, and
  the one-time bootstrap exception where no Finalizer mode applies.

- Update `codex-skill-sync` to 1.2.0. `check <tool>` now validates the installed
  production state as well as the canonical source contract, so a stale or mismatched
  installation can no longer report a clean PASS; `--source-only` keeps the previous
  source-contract-only behavior for build environments with no installation.
- Make the public `snapshot-runner` repository the canonical Snapshot Runner runtime and
  release source, and own its `ToolSkillManifest` at
  `manifests/snapshot-runner/tool_skill_manifest.json` covering the primary
  `snapshot-runner` entrypoint alongside the four retained `codex-*` aliases.
- Update `codex-skill-sync` to 1.3.0 with `upgrade <tool>`, the safe production upgrade
  path for `python_console_scripts` tools. It preserves the recorded `UV_TOOL_BIN_DIR`,
  index list and `no-build`, installs with `uv tool install --force` so entrypoint
  conflicts cannot strand production without a CLI, verifies every entrypoint, version,
  receipt field, symlink target and the installed files against the published wheel
  `RECORD`, and reinstalls the previous release if any step fails.
- Point the Snapshot Runner canonical source directory at `snapshot-runner` after the
  public tree was consolidated into that path. The former private Snapshot Runner
  repository is preserved as a read-only archive and is no longer an active source; the
  private `ToolSkillManifest` continues to live here.

## 1.4.0

- Close the Worktree Controller retirement integration. `--retirement-plan-id` is validated as
  a canonical 64-hex identifier on the bash side and re-checked in the companion verifier;
  `--integrated-into` now accepts any spelling that resolves to the plan's integration identity
  (bare branch, `<remote>/<branch>`, `refs/heads/...`, or the plan's own remote-tracking form)
  instead of requiring a byte-exact match against the operator's raw plan literal, and the
  verifier cross-checks both `integrated_into` and `integrated_ref` from the plan.
- Give `--operation` real meaning: a plan operation that already has a Controller receipt is
  refused at validation time ("already records a consumed receipt"), closing same-plan replay
  after registration, while Controller-side re-record remains idempotent.
- Propagate the verifier's concrete reason into `retirement_validation_error` instead of
  collapsing every refusal to the generic drift wording.
- Retire the fixture-only oracle: a new gated suite drives the REAL installed controller
  (public `branch-retirement-plan/status/record` contract) end-to-end in /tmp — genuine plan
  persistence, controller status revalidation, retirement receipt recording, replay guards,
  authority/policy/OID/plan-tamper drift, and lock serialization — and skips explicitly when
  the controller entry point is absent. The Finalizer side never reimplements controller
  digests in tests; the controller itself is the oracle.
- Attest the controller's published `branch_retirement_version` capability against the plan
  schema through the read-only `capabilities --json` contract before validation, failing closed
  on an unreadable probe and skipping attestation only when no controller binary exists (the
  independent-verifier legacy path). Emit precise namespace blockers: a repository still holding
  legacy `codex-worktree/v1` state, or holding no controller state at all, is refused by both
  the bash lock preflight and the companion verifier with distinct reasons; no code path reads
  non-authoritative legacy state. The state-digest recomputation is now explicitly isolated as a
  legacy compatibility mirror of the frozen v1 layout, with a module boundary note forbidding
  new governance logic from accumulating there.
- Establish the standalone operating contract with an isolated regression suite: the ordinary
  verify/commit/publish/resume lifecycle must succeed in a minimal HOME/PATH environment with no
  controller, no sibling tools, no installed Skill, and no private Gitea configuration, while
  retirement, reviewed-source linkage, explicit repository bootstrap, and snapshot evidence
  still fail closed with their precise blockers (protected-branch refusal ordered before state
  checks) and zero mutation. Documented the boundary in the root README and declared the shared
  Skill an optional agent-workflow asset rather than a runtime or authorization source; no core
  dependency on the toolchain siblings exists to unwind because governance integrations were
  already request-gated.
- Document the exact handoff (plan → validated CAS retirement → record → status), the quiet
  window between planning and consumption, and per-operation plan consumption in the Skill
  reference and README. Namespace migration of legacy `.git/codex-worktree` state and any
  `.agents/worktree-policy.toml` adoption stay under Worktree Controller governance.
- Turn the integration boundary into an enforced contract instead of a refactoring. Each
  external-layout fact is now confined to one marked `GF-INTEGRATION-ADAPTER` site: the
  Worktree Controller state root, the publication lease and the retirement plan/receipt file
  names, the Snapshot Runner evidence layout under `XDG_STATE_HOME`, and the Gitea REST surface
  used by explicit repository bootstrap. `docs/integration-boundaries.md` becomes the
  authoritative matrix of triggering command, capability detection, version compatibility and
  fail-closed blocker per integration, and a new boundary suite pins the adapter marker
  single-site rule, the per-literal confinement ledger, the Context Loader non-coupling, the
  agreement between documented blockers and the implementing source, the mode-independent
  top-level `--summary` key set together with the per-mode nested result shapes, and that the
  snapshot evidence adapter resolves only the state root it is given. The freeze is extracted
  from the source rather than restated: every `summary_status` and `summary_next_action`
  literal and every conditionally added top-level key must now appear in the matrix, and the
  exec-forwarded companions are pinned as emitting their own receipts (the bootstrap receipt
  and the seven-key candidate verdict) instead of the summary envelope. The ledger scope is the
  production code surface enumerated from the filesystem (root CLI, root-level companions and
  `tooling/`), so a new untracked companion cannot escape it.
- Record two honest debts surfaced by that audit instead of silently absorbing them: the Worktree
  Controller authority-state-digest mirror in the retirement verifier is now a named transitional
  deviation, because upstream published that recipe together with a read-only
  `branch-retirement-verify` verdict API and forbids consumers from recomputing it; and
  `tool_cli_contract.json` still describes companion exit code 3 as a deferred destructive
  confirmation, while the repository bootstrap implementation uses 3 for an unclassifiable
  decision. Rewording that field rebinding the published contract bytes and the three manifests,
  so it is left for a dedicated contract batch.
- Public CLI, security, CAS, lock and receipt semantics are unchanged.

## 1.3.0

- Fix the production upgrade/activation sequencing gap. `install <console-script-tool>
  --activate-production` used to fail after an `upgrade` because deriving the retired target
  set from the superseded CURRENT bundle asserted the installed entrypoint version against that
  old manifest ("installed entrypoints do not match"). Target-set derivation for transition
  bookkeeping (previous bundle during activation, current/previous during rollback, and both
  inside the failure-restore path) no longer applies that installed-version assertion; the
  assertion remains mandatory against the incoming bundle, in post-activation verification, and
  in byte-level retired-target and backup-restore checks. The supported production upgrade is
  now a single call: `upgrade <tool>` followed by `install <tool> --activate-production`.
- Make the canonical Skill payload portable: every runtime entrypoint reference in the
  hash-bearing payload now uses `$HOME`-rooted absolute paths
  (`$HOME/bin/git-finalize`, `$HOME/.local/bin/project-context`,
  `$HOME/bin/snapshot-runner`, `$HOME/.agents/skills/git-change-delivery/`) instead of
  host-specific `/home/hsd` literals, and the execution-boundary bullet names the generic
  native permission-and-review channel instead of one product's `auto-review` surface. Host
  documentation that intentionally records this machine's layout (root README canonical-source
  line, released CHANGELOG sections) and historical evidence keep their literals. The canonical
  payload tree is now `9c05e5b279731a37b3ce15e2fbda7ae9962f81355cbafb86fa410f28ec19f527`,
  rebound in all three ToolSkillManifests in the same change.
- Add the released 1.2.0 canonical payload tree to `RELEASED_CANONICAL_SKILL_SHA256` in both
  registries (deploy.py and tool_skill_sync), keeping released-canonical upgrades permitted;
  the drifted pre-migration tree remains unregistered by design.
- Replace the hardcoded home path in the temp-directory negative test with the runtime home
  directory, removing the last `/home/hsd` literal that encoded environment rather than fact.

## 1.2.0

- Fold the installed-live authorization-persistence policy into the source canonical Skill. The
  installed copy under `~/.agents/skills/git-change-delivery/` carried hand-enriched text that
  never existed in any canonical revision: 持续授权 semantics (persistence across pause, resume,
  context compression and Agent handoff; one standing authorization covering local commit plus
  push to a named private remote branch without per-step re-authorization; no merge, public
  release or production deployment implied), the credential-authority paragraph and the
  SKILL.md anchor-link in `references/git-finalizer.md`, the expanded trigger sentence, and
  the single-command native-permission/auto-review fail-closed bullet. The merge is a three-way
  reconciliation against the shared base revision; every canonical 1.1.x branch-retirement
  statement is retained, so neither side loses content. `quick_validate.py` adopts the installed
  superset (12 additional markers) and keeps validating the same section scopes.
- Update `references/snapshot-runner.md` to the released Snapshot Runner 2.3.1 / public CLI
  contract 3 surface: five subcommands (four evidence collectors plus the on-demand `read`
  expansion command) and the corrected `--summary` applicability.
- Rebind the canonical Skill payload: all three `ToolSkillManifest` files now pin the merged
  payload tree `7b85ec9da739bd60f76736ae6352e642dbf880bbd089058cce4cf7a9c0b5c665`.
- Re-pin both mirror manifests from the released sibling git tags (authoritative; the siblings'
  Gitea releases are not the release record): Context Loader 1.3.1 with public CLI contract 3
  bound byte-exactly at `82f1580f…`, Snapshot Runner 2.3.1 with public CLI contract 3 bound at
  `2852388c…`. `toolchain_compatibility.json` moves `context_loader_contract_version` and
  `snapshot_runner_contract_version` (and the per-tool `public_cli_contract_version` bindings)
  to 3 accordingly, so `just toolchain-check` validates green against current sibling sources for
  the first time. The superseded uncommitted 2.1.0 re-pin attempt remains preserved outside the
  tree and is not applied.
- Record the contract promotion rule in `docs/tool-skill-sync.md`: per-tool public CLI contract
  advances update the per-tool bindings and manifest byte pins without changing
  `toolchain_contract_version`; `toolchain_contract_version` (unchanged at 5 in this release)
  moves only when cross-tool binding semantics change, atomically with the supported-version
  set in `tool_skill_sync`, all manifests and tests.
- Make Skill deployment drift-safe. `deploy.py install` now refuses, before any write and with
  per-file digests, an installed payload whose tree digest is neither the source tree nor a
  released canonical tree from `RELEASED_CANONICAL_SKILL_SHA256`, and refuses an incomplete
  managed-file set; idempotent reinstall and upgrades from a released canonical line stay
  allowed. Previously the only protection against silently overwriting installed-only policy
  text was the incidental unknown-path abort triggered by a transient `__pycache__`.
- Close the production-activation bypass. `tool-skill-sync install --activate-production`
  applies the same trusted-state rule before capture/replace: the installed payload tree must
  equal the incoming bundle, a released canonical lineage entry, the CURRENT-pointer bundle
  (managed re-activation or rollback transition), or the Skill must be absent; unknown content
  drift and incomplete payloads fail closed without creating a backup. `rollback
  --activate-production` was already gated by per-target verification of the CURRENT pair.
  Tests lock the shared semantics: the two `RELEASED_CANONICAL_SKILL_SHA256` registries and
  the two payload-tree implementations (deploy.py and `tool_skill_sync`) must stay equal, and
  hand-edited production payloads are refused by every activation entrypoint with bytes
  preserved.
- Converge the CLI contract with enforced behavior without changing the declared surface
  version: `verify_only`, `commit_only`, `normal_publish` and `publish_existing_branch` now all
  declare `--repository-id`/`--allocation-id`/`--task-key`/`--authority-key` plus the
  `controller_linkage_required_with_reviewed_sensitive_source` precondition, matching the
  parser; `--help` no longer calls the linkage flags unconditionally optional; and the contract
  declares the truthful per-command exit-code surface (`0`/`1` for bash-owned commands,
  `0`/`2`/`3` for the exec-forwarded repo-bootstrap pair, `0`/`1`/`2` for the exec-forwarded
  integration publisher).
- Update `docs/process/phase-closure.md`: Plan-bound local branch retirement exists since
  1.1.0 (explicit, Controller-gated, never automatic worktree cleanup), the Worktree Controller
  is `available_external` under the v5 binding, releases exist only through the tag-triggered
  `release.yml`, and tool phases must record the installed-live versus source canonical Skill
  tree-hash comparison.
- Define terminology going forward: `live` means the installed copy; the 1.1.1 section's phrase
  "live canonical Skill" meant the then-current source canonical payload, not the installed
  copy; the glossary lives in `skills/git-change-delivery/README.md`.

## 1.1.1

- Re-pin the mirrored Context Loader and Snapshot Runner `ToolSkillManifest` files to the live
  canonical Skill payload hash. Commit `4084e22` had edited `skills/git-change-delivery/SKILL.md`
  and resynchronized only the root manifest, leaving both mirrors on the superseded
  `ff6d5bea…` binding and breaking the documented invariant that all three manifests bind the
  same canonical Skill payload.
- Add a fail-closed quality gate: `tests/test_tool_contract.py` now validates the canonical Skill
  binding, the compatibility Skill binding, the skill contract version and the toolchain contract
  pin for every `ToolSkillManifest` in the repository (root plus both mirrors). Previously only
  the root manifest was asserted, and `just check` — the single `quality.yml` step — could not
  observe mirror drift because `tool-skill-sync check` requires sibling source trees.
- Widen the release preflight. Before any release mutation, `release.yml` now additionally
  requires the tag to equal the CLI contract `tool_version`, the root manifest `tool_version`,
  every companion `VERSION`, the toolchain compatibility `git_finalizer_contract_version`, and
  the root manifest's byte-exact `public_cli_contract_sha256` binding of the shipped contract.
- Keep both mirror `tool_version` pins at the released sibling contracts (Context Loader 1.0.0,
  Snapshot Runner 2.0.0). A previously uncommitted attempt to pin Snapshot Runner 2.1.0 was
  based on the superseded contract-v4 revision and would have regressed
  `compatible_toolchain_contract_version`; the newer sibling 2.3.0 contract-3 re-pin is deferred
  until the toolchain contract advance rule is recorded.

## 1.1.0

- Add Controller-plan-bound local branch retirement using exact-OID `update-ref` CAS, live ancestry,
  checkout/upstream, remote, and post-mutation verification.
- Bind optional remote retirement to the same immutable Controller state and integration OID while
  keeping its existing standalone interface compatible.
- Emit independent deterministic local/remote retirement results so partial completion is auditable
  and safely resumable; semantic-equivalence and historical cleanup remain report-only upstream.

## 1.0.0

- Break the executable contract: use `git-finalize` and `tool-skill-sync` with neutral
  sidecar and module names. Remove the private execution bridge from the product;
  all callers use the same non-root CLI under their native execution permissions.
- Require explicit task/thread identity or `AGENT_TASK_ID`/`AGENT_THREAD_ID` for the
  unpublished queue; use the `toolchain/completed-unpublished/v1` state namespace.
- Consume Snapshot Runner's `snapshot-runner/snapshots` evidence namespace.
- Add explicit source-checkout selection for linked worktrees and preserve both
  target sets during a governed executable rename and production rollback.
- Include `tool-temp-dir` with a neutral state namespace and explicit registered
  basename contract, retaining inode, ownership, mount and FD-bound cleanup gates.

- Carry forward the 0.10.1 exact `HEAD -> index` deletion validation through the
  renamed CLI, including staged deletion, rename-old-side and invalid-path regressions.
- Preserve the exact fixture and reviewed-source first-branch publication contracts
  from 0.10.2 and 0.10.3 through the same neutral CLI and validators.

## 0.10.3

- Reuse the existing reviewed-sensitive-source validator for first publication of a clean
  existing branch. Require the same exact repository, allocation, explicit scope, source hash,
  evidence and validity; revalidate before push. Keep all secret detectors mandatory and reject
  historical versions whose blob differs from the reviewed HEAD source.
- Forward the review and exact scope through the host bridge without weakening any other
  publication mode or permitting content exceptions alongside a source review.

## 0.10.2

- Accept the existing exact fixture exception contract in `--publish-existing-branch`,
  preserving repository root/remote, literal test path, blob OID, SHA-256 and detector
  bindings. Unused exceptions, changed content, other paths/rules and existing targets
  remain blocked; path-only fixture exceptions and ordinary resume remain unsupported.
- Forward this exact option through the governed host boundary with unchanged writer,
  permission, content, non-force push and remote verification gates.

## 0.10.1

- Validate absent explicit paths against the exact `HEAD -> index` deletion, with rename
  detection disabled. Staged deletions and rename-old paths now survive preflight and
  verify-only, and are retained without trying to stage the absent path again.
- Keep invalid missing paths, historical deletions and out-of-scope staged changes blocked;
  preserve the existing behavior for ignored copies and deliberately recreated files.

## 0.10.0 - 2026-09-12

- Add explicit `--reviewed-sensitive-source` for normal, commit-only and verify-only operations.
  External, time-bounded review evidence binds the Git repository, Controller allocation/linkage,
  full explicit scope and exact Python source SHA256. Worktree, index and committed content are
  checked again; changed content or evidence invalidates the review. This only admits reviewed
  credential/secret/token-named source paths. All existing secret-content detectors remain mandatory,
  and content exceptions cannot be combined with source review.
- Include the source-review companion and receipt contract in the versioned ToolReleaseBundle and
  host bridge. Keep initial/history/integration publication modes unchanged and fail closed for
  unsupported review combinations.
- Govern the canonical Finalizer repository and preserve its existing linked checkout through
  Controller policy/adoption. Allow exact Controller scope arguments to name Finalizer source files
  without treating those inert filenames as wrapped Finalizer execution.

## 0.9.3 - 2026-09-07

- Validate each unpublished commit and first-parent merge changes when resuming publication,
  without treating unchanged, already-published content as newly introduced by a merge's other parent.
- Isolate bootstrap fake-service tests from caller proxy routing.

- Treat Codex `turn_aborted` as a terminal task event in unpublished-queue identity resolution;
  retain fail-closed handling for multiple genuinely unfinished turns without changing rollout data.
- Reconcile the installed 0.9.2 integration fix into canonical history and refresh private Skill bindings.

## 0.9.2 - 2026-09-03

- Accept Controller 0.6 IntegrationIntentV2 only in the lease-bound `PUBLISHING` state while
  retaining the pre-0.6 `VALIDATED` contract.
- Bind V2 publication to the exact lease UUID, prepared base/commit/tree/scope identity, tests and
  Snapshot evidence, and immutable prepared-candidate receipt; mismatches fail before remote mutation.

## 0.9.1 - 2026-09-01

- Add Git Finalizer `0.9.1` integration-candidate publication: consume one Controller schema v2
  lease, bind repository/target/holder/run/expected-main/candidate/validation identity, serialize
  through the existing Controller repository lock, and push only the exact non-force branch refspec.
- Recover an interrupted successful push from the live remote OID without a second mutation; reject
  expired or replaced writers before the mutation gate and leave canonical files, index, local branch,
  tags, and candidate branch unchanged.
- Extend the fail-closed host bridge, deterministic summary, public contract, release bundle, workflow
  Skill, and bare-remote tests for exact publication and duplicate-executor fencing.
- Accept Codex 0.149.1 current-user direct `permission_mode=bypassPermissions` in the fail-closed
  PreToolUse bridge while retaining the legacy escalated `default` contract, rejecting root,
  unsupported modes, unknown structures, and transcript permission mismatches.
- Rename the canonical workflow Skill to `git-change-delivery`, retain the former name only as a
  deprecated shim, and bind both through ToolSkillManifest v2/toolchain contract v3.
- Update `codex-skill-sync` to 1.1.1 so bundles and production activation install and verify the new
  canonical Skill plus its shim while accepting pre-rename bundles and their verified older sync
  runtime during fail-closed migration.

## 0.9.0 - 2026-08-23

- Add `--publish-existing-history` for normal non-force first publication of one or more existing
  local commits after explicit Gitea `repo-plan` / `repo-ensure` bootstrap.
- Verify the entire remote has no user Git refs before first push; existing main, non-main branches,
  tags, origin mismatch, dirty state, or ambiguous remote results fail closed without a new commit.
- Add `--resume-existing-history-publish` for the exact interrupted state where the remote remains
  empty or contains only the expected target OID, followed by live remote and `0/0` verification.
- Extend the stable JSON, host bridge, workflow Skill, and public CLI contract without exposing
  credentials or combining bootstrap and publication outcomes.

## 0.8.0 - 2026-08-23

- Add explicit `--repo-plan` and `--repo-ensure` Gitea repository bootstrap operations.
- Resolve API credentials through Git credential authority without accepting or recording tokens.
- Keep repository bootstrap receipts separate from commit/publication outcomes and reserve optional
  Worktree Controller linkage fields.
- Add explicit `--initial-commit-only` for one local root commit without remote access.

## 0.7.0 - 2026-08-22

- Added: `--retire-remote-branch` performs ancestry-only remote feature-branch retirement with
  explicit integration target, expected remote OID, optional CI evidence, and a read-only dry-run.
- Safety: Retirement reuses the protected-branch authority, rejects the live default branch and
  local lifecycle dependencies, and deletes only through an exact expected-OID lease-bound refspec.
- Verification: Success proves the remote and remote-tracking refs are absent, the integration OID
  is unchanged, and local HEAD, index, worktree, and tags are unchanged; bounded JSON provides a
  deterministic receipt and truthful absent, blocked, or unverified results.
- Added: Public CLI contracts and ToolSkillManifest v1 bind the three tool versions to the one
  canonical workflow Skill and an explicit toolchain compatibility contract.
- Added: `codex-skill-sync` reports binary/Skill drift, validates release inputs, stages complete
  versioned bundles, atomically switches a pair-level `CURRENT` pointer, and supports explicit
  transactional production activation and whole-pair rollback for stable entries, Skill, and bridge.
- Fixed: The PreToolUse bridge now accepts and exactly forwards repeatable `--allow-test-fixture`
  and `--allow-large-binary` values; its option arity and repeatability are checked against the
  public CLI contract, and bundle verification rejects a mismatched bridge.
- Fixed: Exact versioned/live Completed-but-Unpublished queue helper calls may carry
  `codex-git-finalize` as inert path data without being mistaken for a wrapped Finalizer execution;
  shell, interpreter, composite-command, and unknown-option execution paths remain rejected.
- Fixed: retirement accepts the matching `<remote>/<branch>` spelling for `--integrated-into` and
  normalizes it to the live `refs/heads/<branch>` target before ancestry and CI verification.
- Fixed: the Hook recognizes the queue helper's optional global `--state-dir` before its subcommand,
  so a self-release filename remains inert lifecycle data in default and isolated queue roots.

## 0.6.1 - 2026-08-22

- Added: `--publish-existing-branch <full-head-oid>` first-publishes an existing clean attached
  feature branch whose explicit same-name remote target is absent, without creating another commit.
- Safety: The mode rejects protected/detached/dirty/staged or already-tracking branches, validates
  commits not reachable from captured remote heads, uses a non-force branch-only push with
  `--no-follow-tags` and `--set-upstream`, and fail-closes if the target appears or verification is
  ambiguous.
- Verification: Success proves local HEAD/history unchanged, remote branch OID equals HEAD,
  configured upstream is exact, ahead/behind is `0/0`, and index/worktree remain clean; the
  PreToolUse bridge and summary schema expose the same explicit path.

## 0.6.0 - 2026-08-06

- Added: `--resume-publish <full-head-oid>` safely publishes one or more existing local commits
  ahead of the current attached branch's configured upstream without creating another commit.
- Safety: Existing-commit resume requires a clean index/worktree, ahead >= 1 and behind = 0,
  validates every commit and changed object in `upstream..HEAD`, and uses only a non-force,
  `--no-follow-tags`, explicit branch refspec push followed by remote 0/0 verification.
- Compatibility: The root-only `--resume-initial-publish` state machine, normal mode,
  commit-only, verify-only, and initial-branch behavior remain available under their existing
  interfaces; the bridge now fail-closed forwards both resume interfaces.

## 0.5.0 - 2026-08-06

- Added: `--mode commit-only` runs the existing local pre-commit checks and creates one scoped
  local commit without fetch, remote verification, push, tag changes, or configuration changes.
- Added: Default and JSON success reports identify commit-only mode, the new commit, the
  mode-driven push skip, and the final worktree state; normal publication behavior is unchanged.
- Added: `--mode verify-only` performs strict local pre-commit validation for explicit paths
  without requiring a commit message or upstream and without add, commit, push, or remote access.
- Added: Verify-only reports local validation, skipped commit/push/remote phases, final worktree
  state, and unchanged HEAD/index/worktree evidence; default and commit-only behavior is unchanged.

## 0.4.2 - 2026-08-03

- Added: `--initial-branch-publish --remote <name> --remote-branch <name>` first-publishes an
  existing-history local feature branch to an explicitly named, currently absent remote branch.
- Safety: The mode rejects protected or mismatched branch names, ignores a stale current upstream
  when selecting the push target, rechecks target absence after commit, and uses a non-force,
  `--no-follow-tags`, explicit branch refspec with `--set-upstream`.
- Added: Post-push verification proves HEAD, remote target, upstream, ahead/behind, index, explicit
  path cleanliness, local tags, remote tags, and every non-target remote ref; failures retain and
  report the real local commit and observed state.
- Added: Bare-remote integration and PreToolUse bridge coverage for success, stale upstream,
  existing targets, protected/detached/mismatched branches, scoped changes, tag protection,
  post-verify, races, and push failure.

## 0.4.1 - 2026-08-02

- Fixed: Normal branch pushes now explicitly pass `--no-follow-tags`, matching the existing
  initial and resume safety contract.
- Added: Real bare-remote adversarial coverage for normal, initial, and resume pushes when local,
  global, or command-environment `push.followTags=true` is active.
- Changed: Summary branch-only and follow-tags fields are now derived from the actual safe push
  plan and remain unset when no push was executed.
- Security: `0.4.0` and earlier affected versions could unintentionally push reachable annotated
  tags when `push.followTags=true` was enabled.

## 0.4.0 - 2026-08-02

- Added: Optional `--summary` output emits one deterministic, bounded JSON result for normal,
  initial, and resume flows without changing default output, Git operations, or exit status.
- Added: A tag-only Gitea Actions workflow for a deterministic script bundle, SHA-256 verification,
  and refusal to overwrite an existing Release.
- Changed: Changelog validation now requires one non-empty section for the source version, and
  Gitea Release notes are extracted from that exact section.
- Changed: Updated historical release-status metadata after verified annotated tags were backfilled
  on 2026-08-02.
- Compatibility: Git Finalizer remains branch-only and retains `--no-follow-tags`; it does not
  create or push tags or call the Gitea Release API.

## 0.3.1 - 2026-08-01

- Fixed: Repository Python entry points run with `-B` so Finalizer and Hook checks do not write
  bytecode.
- Release status: The source version was completed in commit
  `4d88db801fa58fbb5bfbf49d683c01cbe1279b2b` on the date shown above; that date is not the tag
  backfill date or a formal release date. Annotated tag `v0.3.1` was backfilled on 2026-08-02 and
  points to that original version commit. No Gitea Release was created.

## 0.3.0 - 2026-08-01

- Added: Optional initial-publish binding to complete Snapshot Runner evidence, including path,
  size, SHA-256, executable-bit, index, and root-tree checks.
- Release status: The source version was completed in commit
  `cd4b16330bf0ec4b41c78b844b3577440adea629` on the date shown above; that date is not the tag
  backfill date or a formal release date. Annotated tag `v0.3.0` was backfilled on 2026-08-02 and
  points to that original version commit. No Gitea Release was created.

## 0.2.4 - 2026-07-27

- Fixed: Preserve an explicitly staged deletion when an ignored local copy exists at the same path.
- Added: The versioned Codex PreToolUse bridge, its tests, and explicit deployment checks.
- Compatibility: No `0.2.3` source version or tag existed; the repository moved directly from
  `0.2.2` to `0.2.4`.
- Release status: Tagged as `v0.2.4`.

## 0.2.2 - 2026-07-21

- Added: Explicitly named large binary files are allowed within fixed size and safety limits.
- Release status: Source version committed as
  `f86c2169fcd9fac36be57bd10588c6dd64177058`; `v0.2.2` does not exist, so this version was not
  formally released.

## 0.2.1 - 2026-07-20

- Fixed: Accept a configured but not yet resolvable upstream when publishing or resuming an
  empty-remote clone.
- Release status: Tagged as `v0.2.1`.

## 0.2.0 - 2026-07-20

- Added: Initial publication to an empty remote and exact resume of a root commit after an uncertain
  push.
- Changed: Remote state is validated before creating the commit.
- Release status: Tagged as `v0.2.0`.
