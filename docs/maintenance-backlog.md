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
- Nothing lets a third party *cryptographically* trace a published tarball back to its source commit:
  packaged manifests carry the literal `tool_commit: "@release"` by design and there are no build
  attestations. The bridge that does exist is reproducible construction — build from the tag and
  compare digests, as [release-governance.md](release-governance.md) documents. An attestation step
  would need `id-token`/`attestations` permissions, which the governance document currently forbids.
- Only the GitHub job checks the bytes after publication. The Gitea publisher creates a draft, uploads
  `dist/` and flips it public, then prints an asset count — it never re-downloads what it published,
  so a partially uploaded or replaced Gitea release can pass. Give the Gitea job the same
  re-download-and-compare step the GitHub job runs, against the digest recorded before upload.
- The `docs/...` links in the shipped `README.md` resolve in the repository, not inside the tarball,
  because `docs/` is not part of the package. Either outcome is fine, but choosing it changes the
  artifact file set, so it belongs to a version batch.
- `.gitea/workflows/release.yml` has not run as shipped since the `v1.5.0` candidate edited it: the
  Gitea release job last executed on older bytes, so the next Gitea release is its first real test.
- The release jobs do not record the environment that produced a digest. Both install a pinned Python
  that nothing then executes (every gate calls `python3`, and the shipped CLI hardcodes
  `/usr/bin/python3`), the GitHub runner image is pinned while the Gitea runner asks for
  `ubuntu-latest`, and `tar`/`gzip`/locale are the variables the byte recipe cannot control. Print
  `python3 -V`, `tar --version` and `gzip --version` into the job log, or drop the unused interpreter
  install, so a cross-host divergence is attributable instead of arguable.
- `just check` runs the same commands on both hosts but not always the same set of suites: a few
  groups are gated on an optional integration being installed on `PATH` and report themselves as
  skipped rather than failing. That is intended, but it means a green check on one host is not
  proof that the other executed the same number of groups, so read the skipped-group counts in the
  job log instead of assuming parity. Making that comparison mechanical (a recorded expected count
  per host) needs a decision about which host owns which expectation.

## 5. Dependency automation (Renovate)

`renovate.json` is published and extends `local>xuanheng-tech/renovate-config`. That preset exists
only on the private Gitea instance and returns "not found" on GitHub, and GitHub shows no Renovate
activity at all: no bot commit, no bot pull request. On Gitea the same configuration is alive — it
opened the still-unmerged proposal to move the `astral-sh/setup-uv` pin forward, which is also how
one learns that the pins in the workflows do drift.

- Unblock: an organisation admin installs the Renovate app on the GitHub repository and publishes or
  relocates the preset so the `extends` target resolves there, then merges the onboarding pull
  request.
- Never: repoint `extends` at a public preset just to satisfy the file. That hides the blocker while
  leaving the automation inactive, and the pinned action SHAs in `.github/workflows` are exactly what
  the automation exists to keep current.
