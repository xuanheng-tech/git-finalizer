# Maintenance backlog

Deferred work, each item with the condition that unblocks it. None of these is a defect in a released
version: they are deferred because doing them now would change a sibling project, activate
production, alter a published artifact's file set, or add a feature. The rules that keep them out of
ordinary batches are in [release-governance.md](release-governance.md) and
[integration-boundaries.md](integration-boundaries.md).

## 1. Hand branch-retirement verification back to the Worktree Controller

Today Git Finalizer verifies a retirement plan itself: `git-finalize-retirement-plan.py` recomputes
the controller's state digest, a path its own module docstring marks as legacy compatibility rather
than published contract. It exists because no frozen, released controller generation offers a verifier
Git Finalizer could consume.

- Unblock: a released controller generation whose `capabilities --json` attests a stable
  `branch_retirement_verify_version`, with the digest recipe published as part of that contract.
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

The maintainer machine runs an older release than the published one; that is the designed state, not
a defect, because publication and activation are separate authorised actions.

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
- Nothing lets a third party trace a published tarball back to its source commit: packaged manifests
  carry the literal `tool_commit: "@release"` by design and there are no build attestations, so the
  measured table in [release-governance.md](release-governance.md) is currently the only bridge.
- The `docs/...` links in the shipped `README.md` resolve in the repository, not inside the tarball,
  because `docs/` is not part of the package. Either outcome is fine, but choosing it changes the
  artifact file set, so it belongs to a version batch.
- `.gitea/workflows/release.yml` has not run as shipped since the `v1.5.0` candidate edited it: the
  Gitea release job last executed on older bytes, so the next Gitea release is its first real test.

## 5. Dependency automation (Renovate)

`renovate.json` is published and pins the same preset shape as the sibling repositories, but nothing
observed on GitHub acts on it: no bot commits, no bot pull requests, and the configured
`local>xuanheng-tech/renovate-config` preset is not reachable by an unauthenticated or member-level
read.

- Unblock: an organisation admin installs the Renovate app on this repository and publishes or
  relocates the preset, then merges the onboarding pull request.
- Never: repoint `extends` at a public preset just to make the file look satisfied. That hides the
  blocker while leaving the automation inactive, and the pinned action SHAs in
  `.github/workflows` are exactly what the automation exists to keep current.
