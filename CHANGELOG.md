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
