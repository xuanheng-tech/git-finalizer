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

- Restore owner write permission on validated, owned read-only subdirectories
  during registered temporary-directory cleanup.
- Allow explicit cleanup of owned FIFOs inside pytest subtrees of registered task
  directories, even when the task prefix is not a fixture name. Keep other FIFO
  locations and special files blocked before deletion.
- `tool-skill-sync doctor` accepts a Claude skills directory that links each shared personal Skill
  and warns when Claude-synced skills live inside the shared root, instead of requiring one
  whole-root link.
- Replace the mirrored Controller retirement digest with public verification, single-use CAS
  authorization and completion. Recover interrupted requests without duplicate deletion, preserve
  completion failures, and attest legacy already-absent refs through the producer.
- Bind a created commit to the reviewed index tree, original branch and parent. Preserve hooks and
  report commits created by a failing hook instead of retrying or publishing unreviewed content.
- Refuse pending Git operations and scan earlier unpublished commits in normal and initial-branch
  delivery. Share content signatures between worktree and blob checks and recognize JSON keys.
- Resolve Python from `PATH`, validate its minimum version, and disable interactive Git credential
  prompts. Redact conflicting bootstrap remotes and keep integration hook diagnostics out of JSON.
- Handle Gitea organization pagination and malformed URLs/timeouts without misclassifying membership
  or leaking credentials.
- Validate release tags before writes, preserve unknown output/staging files, reject archive links,
  duplicate members and duplicate checksum entries, and verify uploaded Gitea bytes before publication.
- Remove unused CI interpreter installs and record the actual build environment; require annotated
  release tags and a clean checkout. Make Renovate configuration independent of private presets.
- Correct the deployment-health adapter ledger, track custom Claude homes and project instruction
  files, register the independently attested released Skill payload, and ignore only host worktree policy.

## 1.7.0

- Consume Controller's provider-neutral `integration-publication-session/v1` instead of opening
  private leases, locks, bindings, allocation records, intents or prepared receipts. Require the
  advertised capability and verify each public response identity before executing an exact push.
- Preserve one producer-owned lock, branch-only non-force pushes, crash recovery without duplicate
  mutation, completion refusal and unchanged canonical checkout state. Bound waits and fail closed
  when the producer or its protocol is unavailable.
- Advance Git Finalizer CLI contract to 6 and accept reviewed Controller CLI contract 41. Core
  lifecycle commands remain independent of Controller and AI provider credentials.

## 1.6.3

- Classify the unique fixed public Controller capability description only in its matching schema.
  Keep other raw and decoded assignments, including plural credentials and fully escaped keys, checked.
- Check decoded known-token, private-key and SSH-key signatures independently of credential-only
  fixture reviews in worktrees and blobs; refuse classifier errors and unknown verdicts.
- Allow exact credential-assignment fixture reviews for single-level `backend/tests/test_*.py`
  paths. Keep known-token, private-key and SSH-key scans mandatory for those paths.
- Accept those exact reviews in local verify-only and commit-only modes without remote operations.
  Bind the existing local origin identity, raw/clean-filter blob and any changed index blob; refuse
  unchanged, out-of-scope, converted or drifted candidates before staging.
- Preserve existing publication review behavior and the original `tests/`-only blanket fixture flag.

## 1.6.2

- Accept reviewed Controller CLI contract version 40 alongside 37, 38 and 39.
  Keep storage layout 4 and the existing independently checked capability requirements.
- Preserve Git Finalizer CLI contract version 5 and the strict credential classifier.

## 1.6.1

- Recognize the exact public Controller retirement capability declaration during
  credential-assignment checks in worktrees and blobs. Reject opaque values,
  Bearer values, altered descriptions and credentials elsewhere in the same file.
  Preserve raw matches while also checking decoded assignments and plural credentials.
- Keep private-key, SSH-key and known-token signatures mandatory, including decoded
  schema strings. Malformed JSON and duplicate keys retain the strict result.
- Preserve CLI contract version 5, summary schema and publication interfaces.

## 1.6.0

Authorization-boundary release. It carries the rule that lets an approved delivery finish inside one
task without a fresh request per command, the contract that the layer asking for approval binds a
grant to, and the correctness fixes found while auditing the first public release. CLI behaviour,
`contract_version` (still 4) and the machine-readable summary are unchanged, so an existing caller
needs no edit to upgrade.

- Investigated why ordinary commit/push work kept asking for approval. Git Finalizer is not the
  asker: it reads no stdin, keeps no grant, and writes nothing that a later run could consult, so it
  cannot gate its second invocation. What asks is the layer in front of it, which matches
  **exact command strings** and therefore treats every new message, path list or flag as a fresh
  decision. `docs/authorization-boundary.md` now states the boundary that layer needs: a tier per
  interface read straight out of `tool_cli_contract.json` (`read_only`, `local_mutation`,
  `remote_mutation` plus the gated retirement interfaces), the protected operations that no scope
  expression may grant, and the `--summary` fields a task-scoped grant must bind to (repository,
  ref, mode, task linkage, tool version) so an approval can be re-checked and audited instead of
  re-typed. The gap left in the Agent Workspace layer is named there rather than implemented here.
- The `git-change-delivery` Skill now defines "scope unchanged" mechanically — same repository and
  worktree, same remote and target branch, same finalization scope, same file-ownership boundary —
  and states that while all four hold, consecutive ordinary commits and pushes in one task proceed
  without a fresh request, because a commit message, a retry or a failed acceptance round does not
  change the scope. Re-asking less is not reporting less: every invocation is still called and
  reported on its own. Force push, tag or release creation, branch retirement or deletion, production
  activation, and writes to public or protected branches stay separate authorisations.
- `tests/test_authorization_boundary.py` holds the boundary from both sides: the tier table is a
  projection of the published contract rather than a second opinion, every protected operation is
  documented as ungrantable, a run never reads stdin, three in-scope commits behave like one that
  succeeded three times, nothing is persisted under `HOME`, the summary carries the binding fields,
  and the entrypoint has no unconditional force-push token and no tag mutation.

- Recorded the `v1.5.0` release decision (GitHub as publishing host, no Gitea mirror) as a
  per-version table in `docs/release-governance.md` whose identifiers are measured, and made
  `tests/test_public_docs.py` re-derive its tag and commit columns from the repository's own refs.
  This also corrected an inherited claim that `v1.0.0` had been published on Gitea: no `v1.0.0` tag
  has ever existed, and the released line is `v0.4.0` onwards.
- Registered the canonical Skill payload that the two most recent releases shipped, in both the
  sync module and the Skill deployment mirror. Production holds that tree, so the released-lineage
  registry was stale by two releases and the next activation would have reported the very state it
  exists to upgrade as unknown content drift. `tests/test_tool_skill_sync.py` now activates from a
  freshly released production tree and fails without the registration, and
  `tests/test_tool_contract.py` replaced its "the source payload must stay unregistered" rule with
  the accurate one: registration requires a released ref that carries the payload, so an
  unreleased local edit is still treated as drift.
- Stated what branch protection on `main` actually requires today, and reworded the tag-immutability
  claim from a host guarantee into the policy it is.
- `README.md` now names where a release is fetched from, verifies the checksum before installing,
  and says that the `docs/...` links inside the shipped bundle resolve in the repository.
- `scripts/release.py selfcheck` builds entirely inside a temporary directory: it no longer clears
  `dist/` or stages inside the checkout it has just validated.
- The Gitea quality job checks out with `fetch-depth: 0`, because the documentation gate reads
  annotated tags; its shellcheck pre-install step is best effort, since `just lint` resolves the
  linter itself and fails with a remedy when none is usable.
- The documentation gate now covers every published markdown file instead of a curated list, so a
  newly added document cannot ship unscanned. Released `CHANGELOG.md` sections stay outside the
  private-path scan: a published section is immutable, and older ones quote the paths they
  documented as removed.
- `scripts/release.py verify` refuses to create `dist/SHA256SUMS.txt` when it is missing: a check that
  can write its own expected value proves nothing, so only `build` authors checksums. The GitHub
  release job now records the digest before uploading and compares the re-downloaded tarball against
  that value, and asserts that the published release carries exactly the checksum file and the
  tarball — an extra or missing asset fails the job instead of passing unnoticed.
- The Gitea release job got the two guards the quality job already had: a per-tag `concurrency` group
  (its publisher is a check-then-create sequence, so two runs for one tag could both see "no release
  yet") and a best-effort shellcheck warm-up, since `just lint` resolves the linter itself.
- New gates: release jobs cannot publish the same tag twice, a linter warm-up may not fail a run that
  can lint, and all four workflow files must pin the same action revisions and tool versions — one
  host's dependency bot can no longer drift the build environment silently.
- Corrected three documentation claims that a re-audit against the implementation disproved: the
  Security-model bullet credited every publication path with the protected-branch and
  remote-default-branch refusal that branch retirement and *first* branch publication perform;
  the adapter rule was stated as one marker per integration, while the enforced rule is one marker
  per adapter file and retirement legitimately marks two; and `docs/agent-contract.md` named
  `tests/test_integration_boundaries.sh` as its own drift guard when that page is guarded by
  `tests/test_agent_contract.py`.
- Corrected documentation that credited gates with work they do not do: `preflight` never reads
  `CHANGELOG.md` and binds only the Chinese README declaration (the English one and the CHANGELOG
  section are bound by `just check`, and the release notes by `changelog.py extract`); the release
  trigger glob accepts tags `preflight` rejects; a re-run of a job that already published fails at
  create rather than republishing; only GitHub verifies bytes after publication. `CONTRIBUTING.md`
  gains a verified re-pin command for the manifest digests, notes that each sibling mirror keeps its
  own contract digest, and marks `just skill-check` as needing an installed Skill.

- Lineage is handled by rule rather than by hope: this batch ships the canonical Skill payload
  `943ebf4e56ae344cf88ab3dc579d5e497663b052c8ccbade5c42f1f52b5efa93`, and that digest is deliberately
  **not** added to `RELEASED_CANONICAL_SKILL_SHA256` here. Registration requires a released ref that
  carries the payload, so it belongs to the batch that follows a publication, exactly as `0c82bceb…`
  was registered only after `v1.4.0` shipped. `tests/test_tool_contract.py` enforces both directions.
- No tag, release or production activation is part of this batch.

## 1.5.0

- Open-source readiness batch. Added the Apache-2.0 `LICENSE` (shipped in the release package),
  `SECURITY.md` recording the reporting channel and the refusal behaviours treated as security
  properties, `CONTRIBUTING.md` with the contract-boundary rules and the hash-bound file table, and
  `AGENTS.md` naming the literals the release preflight verifies.
- Rewrote `README.md` for a reader with no prior context: positioning, requirements, installation from
  the published tarball, a quick start, a mode table, the agent/JSON contract, the optional-integration
  table (Worktree Controller, Snapshot Runner, Gitea repository bootstrap and Context Loader are each
  optional), the security model and the standalone contract. The per-mode operating guide moved to
  `docs/cli-guide.zh.md` with its content preserved, and the repository-private delivery-topology
  runbook — another project's remote layout, legacy ref names and commit identifiers — was removed from
  the published surface rather than relocated.
- Published the Agent-facing machine-readable contract as `docs/agent-contract.md`: the envelope key
  set, the three conditional keys, the serialization-failure shape, closed vocabularies for `status`,
  `final_phase`, `next_action`, `mode` and `push.result`, the per-surface exit-code mapping, and real
  captured success/blocked examples. `tests/test_agent_contract.py` extracts each vocabulary from the
  implementation and fails when a value exists in code but not in the documentation, so an agent never
  has to parse human logs to decide what happened.
- Moved the release contract out of workflow heredocs into `scripts/release.py`
  (`preflight`, `build`, `verify`, `selfcheck`) and pointed both the existing Gitea workflow and the new
  GitHub workflow at it. The recipe now pins staged directory modes, so the build no longer depends on
  the builder's umask; rebuilding the released tag with this implementation reproduces the published
  `v1.4.0` artifact byte-for-byte under `umask` 022, 002 and 077. Preflight additionally requires a
  `LICENSE`, binds every manifest to the shipped Skill payload, and refuses an unreadable artifact
  instead of crashing. The package file set now also carries `LICENSE`, `SECURITY.md` and
  `CONTRIBUTING.md`, and a test asserts that set.
- Added `.github/workflows/ci.yml` and `.github/workflows/release.yml`, pinned to the same action
  revisions and toolchain versions the Gitea workflows already use, so both hosts run one `just check`
  gate and one build recipe. The GitHub release job re-downloads its own published assets and verifies
  them against the recorded `SHA256SUMS.txt`, closing the gap where checksums were only self-declared.
  No build-provenance attestation is claimed: the guarantee is a deterministic build with published
  checksums verified after download.
- Cleaned the published surface. `/home/hsd` now appears only inside immutable released CHANGELOG
  sections; the shared Skill source locator is written repository-relative; the boundary matrix no
  longer states another project's unpublished version generations; and the README no longer narrates
  private hosting history. `.gitignore` grew to the sibling standard (build output, virtualenv, caches,
  secret-shaped files, session and workspace-policy directories), a host-only workspace policy file that
  no code path reads was untracked while remaining on disk, and a stray empty tracked file named `0600`
  was removed.
- Repaired the Gitea release workflow, which the step extraction had left with three mis-indented
  step headers and therefore unparseable YAML: it would have failed the next tag on that host. All
  four workflow files are now parse-checked by test, and a structural guard rejects any step header
  that is not at the indentation these files use.
- Added `docs/release-governance.md` as the single authority for how a version is published: role
  split (GitHub is the public distribution entry, Gitea is the governed delivery remote and holds the
  v1.0.0 – v1.4.0 release record, production installation comes from `tool-skill-sync` bundles and
  consumes neither host's artifact), the one-host-per-version rule, tag-trigger semantics on both hosts
  including why pre-existing tags published nothing on GitHub, what `preflight` gates, the fixed build
  recipe, the fact that cross-host byte equality is expected but not yet proven, the minimal
  permissions each workflow declares, and a step-by-step first-public-release runbook with its
  acceptance checks and explicit "never" list. Both release workflows and `CONTRIBUTING.md` point at
  it, and the phase-closure SOP was corrected where it still claimed Gitea was the only release host.
- Completed the public maintenance surface: README gained a `## Support` section stating the supported
  standalone scope, the opt-in and non-promise status of optional integrations, and the unsupported
  set; issue and pull-request templates now carry the review gate (affected surface, negative test,
  hash-bound files, no private endpoints in the diff). A new documentation gate test enforces that
  relative links resolve, that public documents contain no private endpoints or host paths, and that
  the governance, support and template invariants stay in place — it immediately caught one
  repository-root link left behind when the operating guide was relocated into `docs/`.

- Documented one pre-existing classification asymmetry instead of silently changing it: an empty
  staged scope is reported as `blocked`/`local_validation`/`resolve_blocker_and_retry` by
  `--mode verify-only` but as `failed`/`staging`/`inspect_failure` by `--mode commit-only` and the
  default mode, with the same reason and the same zero-mutation outcome. The behaviour is identical
  in the released 1.3.0 and 1.4.0 binaries, is now pinned by test, and normalising it is left to a
  dedicated classification batch rather than bundled into open-sourcing.
- Added `just lint` (`scripts/lint.sh`) as a separate CI step on all four workflows, and kept
  `just check` limited to Bash, Git, Python and `just` so a clean clone can always run the contract
  gate. The resolver proves a usable `shellcheck` by executing it and falls back to
  `uvx --from shellcheck-py`, because a host may ship the binary without execute permission for the
  CI user; a workflow step that only tested `command -v shellcheck` reported success while the lint
  itself died with exit 127. The Gitea workflows now install the dependency explicitly for the build
  user before linting, `scripts/lint.sh` accepts a `GF_SHELLCHECK` override, probes each candidate by
  executing it twice, and exits with a stated remedy when none is usable.
- Release preparation batch for `1.5.0`: version literals in the entrypoint and the three governed
  companions, `tool_version` in the CLI contract and root manifest, the README release declarations,
  and the manifest's `public_cli_contract_sha256` re-pin to the new contract bytes move together.
  Nothing user-visible in behaviour changes: `contract_version` stays 4, the canonical Skill payload
  stays `0c82bceb…` and is therefore not added to the released-lineage registry ahead of release,
  and both sibling manifests keep pinning `1.3.1` and `2.3.1`.

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
  `tool_cli_contract.json` described companion exit code 3 as a deferred destructive
  confirmation, which the repository bootstrap implementation never performed.
- Close the second debt and the released-lineage gap it exposed. `tool_cli_contract.json` now
  states what the companions actually do — `2` is an argparse usage error or a `BLOCK_*` refusal
  reported as `status=blocked`, `3` is a bootstrap failure whose decision could not be
  classified, and `1` remains reachable through the pre-execution companion guard or an uncaught
  companion traceback, so no wording claims that a refusal happened before a remote mutation —
  and `tool_skill_manifest.json` is re-pinned to the new contract bytes in the same
  commit, so no CLI behaviour, flag, status or exit code changed. A new test extracts the
  bootstrap classification from source and refuses any drift between implementation, contract text
  and the boundary matrix.
- Register the canonical Skill payload tree carried by the released `v1.3.0` line
  (`9c05e5b2…`) in both released-lineage registries. Without it, `deploy.py install` and
  `tool-skill-sync --activate-production` correctly refused to upgrade the actual installed
  production tree as unknown drift, so the released lineage was self-blocking. The incoming
  unreleased tree stays out of the registry, and so does the hand-enriched pre-migration tree,
  which is now asserted absent. Two new tests pin the registry to exactly the tagged release
  trees plus the pre-tag line, and drive the real upgrade from a live tree materialized from the
  `v1.3.0` tag: it installs cleanly, while hand drift on top of that same released tree still
  fails closed with per-file diagnostics and zero mutation.
- Do not migrate the retirement adapter onto upstream's `branch-retirement-verify` yet, and record
  why in the boundary matrix: the controller this environment can actually reach predates the verb
  and does not advertise it, the same-day in-flight upstream experiment moved that surface to
  contract v3 and storage layout v2 while still advertising verify version 1, and no controller-free
  verdict exists for the
  documented independent path. The verb is also not a drop-in replacement: it observes state before
  the lock and never compares the plan's identity fields against caller intent, so the convergence
  shape is a pre-lock `verify` observation plus an in-lock plan-field recheck, not the removal of
  independent verification. The earlier guess that its shared lock would deadlock against the
  caller's exclusive `flock` is wrong — advisory locks do not conflict — and is not part of the
  reasoning. Those conditions are the precise preconditions for the switch; the digest mirror stays
  the fail-closed authority, unchanged and unextended.
- State the per-surface exit-code mapping in the published contract, and remove two claims that
  the implementation contradicts: `1` is reachable on the exec-forwarded commands through the
  companion guard and uncaught tracebacks, and a `BLOCK_REMOTE_MISMATCH` refusal can be reported
  with rc 2 after the Gitea repository has already been created, which `bootstrap.executed` — not
  the exit code — discloses. The bootstrap companion has no interactive confirmation gate at all,
  which the previous wording implied. The tests now reject those lies directly: they require the
  contract to name `status=blocked`, to point mutation truth at `bootstrap.executed`, to mention
  the pre-execution guard, and to avoid any ordering or "never returns" guarantee, while binding
  the classification line and the post-create mismatch site in the companion source.
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
