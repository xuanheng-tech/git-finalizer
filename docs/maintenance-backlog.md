# Maintenance backlog

Remaining integration debt and operational gates, each with its actual prerequisite. Versioned
implementation changes are recorded in the Changelog. The boundaries for
sibling changes, production activation and artifact contracts are in [release-governance.md](release-governance.md) and
[integration-boundaries.md](integration-boundaries.md).

## 1. Sibling pairing (Snapshot Runner, Context Loader)

`manifests/snapshot-runner/tool_skill_manifest.json` and
`manifests/context-loader/tool_skill_manifest.json` pin the sibling generations Git Finalizer is
verified against, and `scripts/release.py preflight` binds both mirrors and the root manifest to the
actual Skill payload tree.

- Unblock: a sibling release that Git Finalizer genuinely depends on. Pairing maintenance is never
  folded into release preparation.
- Then: bump the mirror, re-bind all three manifests in the same commit, and ship it as its own
  version batch — both mirrors are inside the release package, so the artifact file set is a
  contract.

## 2. Production activation of a published release

Publication and production activation are separate authorised actions. A source branch or a published
artifact does not establish which bytes a maintainer currently runs.

- Requires: explicit task authority covering activation, including a still-valid continuing grant,
  then `tool-skill-sync install --activate-production` from the canonical source repository using
  an explicit published `--source-ref` when parallel candidates exist — never from a release artifact.
- Afterwards: check the `CURRENT`/`PREVIOUS` pointers, the live canonical payload digest, and
  `tool-skill-sync check`; a payload that a released ref carries must already be listed in
  `RELEASED_CANONICAL_SKILL_SHA256`, otherwise the next activation treats its own released state as
  unknown drift (guarded by `tests/test_tool_skill_sync.py`).

## 3. Release-process hardening

Small, unscheduled, each one a real gap rather than a polish item.

- GitHub preparation now has a read-only `publication-status` guard with an explicit remote,
  paginated draft visibility and asset/source-digest checks. The runbook recovers existing records
  without recreating tags or Releases. It is an observation, not an atomic publication lock, and
  does not inspect Gitea; Gitea keeps its existing workflow lookup and draft verification guards.
- Nothing lets a third party *cryptographically* trace a published tarball back to its source commit:
  packaged manifests carry the literal `tool_commit: "@release"` by design and there are no build
  attestations. The bridge that does exist is reproducible construction — build from the tag and
  compare digests, as [release-governance.md](release-governance.md) documents. An attestation step
  would need `id-token`/`attestations` permissions, which the governance document currently forbids.
- Both jobs compare downloaded assets with pre-upload digests. Gitea does so while the release is
  still a draft and checks the asset inventory before making it public. The transport regressions
  use a disposable local HTTP fixture; a real hosted release remains a separately authorised test.
- The packaged Markdown files link to documentation at the corresponding public release tag.
  `docs/` remains outside the package and source-checkout links remain relative; the artifact file
  set stays unchanged.
- The jobs record the actual Python/Git/tar/gzip/locale. Both quality jobs now exercise a verified
  source-built release installation on Python 3.11 and 3.12 with optional tools and personal
  configuration absent. Different runner images can still produce different bytes; the cross-host
  comparison remains the acceptance gate when moving a release between hosts.
- `just check` ends with one PASS/SKIP/FAIL table and skip reasons on both hosts. Real-Controller
  retirement is optional when the Controller is absent from `PATH`; unit-test skips are also
  listed individually. Green checks prove the reported executed groups passed, not cross-host
  coverage parity. Before tag creation, `just release-check` requires the real-Controller suite to
  execute and pass in the clean candidate checkout. Hosted checks remain standalone and optional.

## 4. Dependency automation (Renovate)

`renovate.json` uses built-in public presets and carries its conservative schedule, rate limits,
release age and manual merge policy locally. It no longer requires a private Gitea preset.

- Account prerequisite: a repository administrator must enable the chosen Renovate service and
  complete its onboarding. A valid configuration is not evidence that a bot is installed or active.
- Verification: read actual bot runs and proposed updates on each host. Changes to action pins must
  keep both hosts consistent, as enforced by `tests/test_release_procedure.py`.

Maintainer readback on 2026-10-02 confirmed the Gitea automation: onboarding PR #1 was merged,
Dependency Dashboard #3 is open, and update PRs #9 (`setup-uv`) and #11 (`uv`) were refreshed that
day. Both proposals cover quality and release workflows under `.gitea/` and `.github/`.
The reviewed updates pin `setup-uv` v10.2.0 to the official tag's commit and `uv` to 0.12.19 in all
four files. The shared workflow checks passed for each update; source-built release installation
with uv 0.12.19 passed on Python 3.11 and 3.12. Further proposals follow the same review and normal
Controller integration gates. This closes the Gitea activation uncertainty.

GitHub had no issues or PRs at that readback. The current CLI authentication received HTTP 403
when listing GitHub App installations, so an independent GitHub bot installation remains
unverified. An empty PR list is not evidence that a bot is disabled. Gitea's existing updates
already cover both workflow sets; the GitHub mirror receives accepted changes through the normal
main synchronization. If independent GitHub automation is desired, an administrator should verify
its repository selection through the service's
[installation and onboarding procedure](https://docs.renovatebot.com/getting-started/installing-onboarding/)
and then check actual processing results.
