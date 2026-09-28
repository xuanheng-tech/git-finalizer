# Maintenance backlog

Remaining integration debt and operational gates, each with its actual prerequisite. Implementation
fixes completed in this branch are recorded under Unreleased in the Changelog. The boundaries for
sibling changes, production activation and artifact contracts are in [release-governance.md](release-governance.md) and
[integration-boundaries.md](integration-boundaries.md).

## 1. Hand branch-retirement verification back to the Worktree Controller

Today Git Finalizer verifies a retirement plan itself: `git-finalize-retirement-plan.py` recomputes
the controller's state digest, a path its own module docstring marks as legacy compatibility rather
than published contract. A released Controller now advertises verifier and execution contracts.
The verifier takes a shared repository lock, so calling it from Finalizer's exclusive lock would
deadlock. An earlier lock-free verdict cannot substitute for the mutation guard either.

- Next: consume the complete producer `branch-retirement-execution/v1` authorization/completion
  protocol, with interruption recovery and compatibility for existing plan-only callers.
- Then: delete the mirrored recipe, keep the fail-closed legacy-namespace migration blocker, and
  re-derive the retirement rows of the boundary matrix in
  [integration-boundaries.md](integration-boundaries.md) plus
  `tests/test_integration_boundaries.sh`.
- Never meanwhile: relax the digest comparison, or release a version that trusts an unpublished
  upstream generation.

## 2. Sibling pairing (Snapshot Runner, Context Loader)

`manifests/snapshot-runner/tool_skill_manifest.json` and
`manifests/context-loader/tool_skill_manifest.json` pin the sibling generations Git Finalizer is
verified against, and `scripts/release.py preflight` binds both mirrors and the root manifest to the
actual Skill payload tree.

- Unblock: a sibling release that Git Finalizer genuinely depends on. Pairing maintenance is never
  folded into release preparation.
- Then: bump the mirror, re-bind all three manifests in the same commit, and ship it as its own
  version batch — both mirrors are inside the release package, so the artifact file set is a
  contract.

## 3. Production activation of a published release

Publication and production activation are separate authorised actions. A source branch or a published
artifact does not establish which bytes a maintainer currently runs.

- Requires: explicit authorisation for each activation, then `tool-skill-sync install
  --activate-production` from the canonical source repository — never from a release artifact.
- Afterwards: check the `CURRENT`/`PREVIOUS` pointers, the live canonical payload digest, and
  `tool-skill-sync check`; a payload that a released ref carries must already be listed in
  `RELEASED_CANONICAL_SKILL_SHA256`, otherwise the next activation treats its own released state as
  unknown drift (guarded by `tests/test_tool_skill_sync.py`).

## 4. Release-process hardening

Small, unscheduled, each one a real gap rather than a polish item.

- `preflight` accepts a tag that already exists, so double publication is stopped only by the person
  running the runbook. A mechanical guard has to decide which host to ask, because a checkout of one
  host cannot see a tag published only on the other.
- Nothing lets a third party *cryptographically* trace a published tarball back to its source commit:
  packaged manifests carry the literal `tool_commit: "@release"` by design and there are no build
  attestations. The bridge that does exist is reproducible construction — build from the tag and
  compare digests, as [release-governance.md](release-governance.md) documents. An attestation step
  would need `id-token`/`attestations` permissions, which the governance document currently forbids.
- Both jobs compare downloaded assets with pre-upload digests. Gitea does so while the release is
  still a draft and checks the asset inventory before making it public. The transport regressions
  use a disposable local HTTP fixture; a real hosted release remains a separately authorised test.
- The `docs/...` links in the shipped `README.md` resolve in the repository, not inside the tarball,
  because `docs/` is not part of the package. Either outcome is fine, but choosing it changes the
  artifact file set, so it belongs to a version batch.
- The jobs record the actual Python/Git/tar/gzip/locale and no longer install an unused interpreter.
  Different runner images can still produce different bytes; the cross-host comparison remains
  the acceptance gate when moving a release between hosts.
- `just check` runs the same commands on both hosts but not always the same set of suites: a few
  groups are gated on an optional integration being installed on `PATH` and report themselves as
  skipped rather than failing. That is intended, but it means a green check on one host is not
  proof that the other executed the same number of groups, so read the skipped-group counts in the
  job log instead of assuming parity. Making that comparison mechanical (a recorded expected count
  per host) needs a decision about which host owns which expectation.

## 5. Dependency automation (Renovate)

`renovate.json` uses built-in public presets and carries its conservative schedule, rate limits,
release age and manual merge policy locally. It no longer requires a private Gitea preset.

- Account prerequisite: a repository administrator must enable the chosen Renovate service and
  complete its onboarding. A valid configuration is not evidence that a bot is installed or active.
- Verification: read actual bot runs and proposed updates on each host. Changes to action pins must
  keep both hosts consistent, as enforced by `tests/test_release_procedure.py`.
