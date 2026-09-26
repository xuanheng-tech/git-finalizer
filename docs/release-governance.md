# Release governance

How a Git Finalizer version becomes a published release, on which host, and what must be true
before and after. This file is the authority for release *process*; `AGENTS.md` and
`CONTRIBUTING.md` cover the version batch a contributor must prepare, and `SECURITY.md` covers the
supported-version policy.

## Roles

| Concern | Owner |
| --- | --- |
| What a release *is* | An annotated tag `vX.Y.Z` whose tree passes `scripts/release.py preflight` |
| Build recipe and verification gates | `scripts/release.py` — the only implementation, shared by both hosts |
| Public distribution for outside users | **GitHub** `xuanheng-tech/git-finalizer` (releases, release notes, artifacts, checksums) |
| Governed daily delivery and the historical release record | **Gitea** `xuanheng-tech/git-finalizer` (v1.0.0 – v1.4.0 were published here) |
| Production installation on this machine | `tool-skill-sync` bundles built from the canonical source repository — it does **not** consume a release artifact from either host |

One release authority per version: **a version is published on exactly one host**. Publishing the
same tag on both hosts is a deliberate mirroring act, not a default, and it requires the digest
comparison in [Cross-host parity](#cross-host-parity). Nothing on either host can rewrite a published
version: releases are forward-only.

## Tag triggers

Both hosts evaluate a release workflow **from the tree of the pushed tag's commit**:

- Tags whose commit tree contains no workflow directory do not trigger a release run. Observed:
  pushing the existing fifteen tags to GitHub produced zero release runs and zero releases, because
  none of those commits contained `.github/workflows`.
- The first tag created from a commit that *does* contain `.github/workflows/release.yml` will create
  a real public GitHub Release. Treat that as irreversible.
- Trigger shape differs only in publication mechanics, never in gating:
  - Gitea: `POST` draft release → upload assets from `dist/` → `PATCH {"draft":false}` →
    prints `published_release=<tag> assets=<n>`.
  - GitHub: `gh release create` publishes immediately with both assets attached, then a following
    step re-downloads the published assets and verifies them.

Both run, in this order and on both hosts: `preflight` → `just lint` → `just check` → `build` →
`verify` → release-notes extraction → publish. A tag whose version batch disagrees fails at
`preflight` before anything is published.

## Version batch (what preflight enforces)

`scripts/release.py preflight vX.Y.Z` refuses the release unless all of the following agree:

- the tag name equals the `git-finalize` `VERSION`, and `VERSION` equals the README declarations;
- all three governance companions declare the same `VERSION`;
- `tool_cli_contract.json` and `tool_skill_manifest.json` declare the same `tool_version`, and
  `contract_version` matches `toolchain_compatibility.json`;
- the manifest's `public_cli_contract_sha256` equals the bytes of the shipped
  `tool_cli_contract.json`;
- the root manifest **and both sibling mirrors** bind the actual Skill payload tree hash;
- `LICENSE` is present in the packaged surface.

It also refuses to run against a dirty worktree implicitly: the workflow checks out the exact tag
commit, so the gate always sees the tagged bytes.

## Artifact and checksum consistency

The recipe is fixed so that one commit yields one byte stream: sorted member names, `ustar`,
owner/group `0` with numeric ids, member mtime pinned to the commit timestamp, `gzip -n`, and
staged directory modes pinned to `0755`. Directory-mode pinning matters: before it was added the
build depended on the builder's umask, so two hosts could have produced different bytes from the
same commit.

Per-run guarantees:

- `build` produces the tarball, rebuilds it in the same job, and fails if the two differ;
- `verify` re-opens the artifact and fails unless the file set equals the installation contract, no
  member path is absolute or traverses `..`, the entry point is `0755`, and its embedded `VERSION`
  equals the tag;
- `dist/SHA256SUMS.txt` is written from the artifact bytes and checked with `sha256sum --check`;
- the GitHub job additionally **downloads the assets back from the published release** and verifies
  the digest, so a checksum is never only self-declared.

<a name="cross-host-parity"></a>
### Cross-host parity

Known-good anchor: the published `v1.4.0` artifact (`ae1d29683ca36a91…`, Gitea-only release) is
reproduced byte-for-byte by `scripts/release.py` at that commit under `umask` 022, 002 and 077.

GitHub has not published a release yet, so **cross-host byte equality is expected, not yet
proven**. When the same version is ever published on both hosts, the two `git-finalizer-<version>.tar.gz`
digests must be compared; a mismatch is a release defect (differing `tar`/`gzip` builds, locale
sort order, or runner image), not something to paper over by publishing one host's file on the other.

## Publishing permissions

- `ci.yml` / `quality.yml`: `permissions: contents: read`.
- `release.yml` (both hosts): `permissions: contents: write` only — no packages, no pull-request
  writes, no id-token.
- GitHub organization setting for this repository: default workflow permissions `read`, and
  workflows may not approve pull requests.
- No repository secrets are used or required: each platform injects its own token
  (`GITHUB_TOKEN` / `GITEA_TOKEN`). Adding a secret to make a release work is a design smell here.

## Runbook: first public GitHub release

Do not execute any of this as part of a maintenance change; it is a deliberate release action.
Production activation on the maintainer machine is a **separate** authorised step that follows a
successful release, and it uses `tool-skill-sync install --activate-production` from the canonical
source repository rather than the release artifact.

### Before

1. A release-preparation batch is merged to `main` and contains: version bump in `git-finalize` and
   the three companions, README current-release declarations, `tool_version` in contract and manifest,
   a dated `CHANGELOG.md` section, and any manifest/contract re-pins made in the same commit.
2. Decide the host. For the first public release the host is **GitHub**; do not also publish that
   version on Gitea unless you intend to mirror it and compare digests.
3. Confirm the candidate:

   ```bash
   git fetch --all --tags --prune
   git log --oneline -3 github/main   # or origin/main, the branch being tagged
   git status --porcelain=v1          # must be empty in the checkout used for tagging
   python3 -B scripts/release.py preflight v1.5.0   # exact new tag name
   python3 -B scripts/release.py selfcheck v1.5.0   # deterministic build, scratch only
   ```

4. Announce nothing before the tag exists; the tag is the release trigger.
5. Record the release decision in the release request: candidate SHA, the `preflight` line it
   printed, the `selfcheck` digest, which host publishes, and that no other host will publish the
   same version unless the digest comparison below is performed afterwards.

The first release out of this model is `v1.5.0`: the open-sourcing, maintenance-governance and
CI batch that follows the released `1.4.0` line. `v1.4.0` stays exactly as published on Gitea and is
**never** re-created, re-tagged or re-published on GitHub — the GitHub history for it is the tag and
the source, not a release object.

### Publish

`<candidate-sha>` is the tip of `main` at the moment the release is approved — never a re-created
commit, and never a commit that `preflight` has not been run against.

```bash
git tag -a v1.5.0 <candidate-sha> -m 'v1.5.0 — <one-line summary>'
git rev-parse 'v1.5.0^{commit}'    # must print <candidate-sha>
git push github v1.5.0             # normal push; never --force, never re-create a moved tag
```

Then watch the run:

```bash
gh run list --repo xuanheng-tech/git-finalizer --workflow release --limit 3
gh run watch <run-id> --repo xuanheng-tech/git-finalizer --exit-status
```

### After

```bash
gh release view v1.5.0 --repo xuanheng-tech/git-finalizer \
  --json tagName,isDraft,isPrerelease,assets,targetCommitish
```

Accept the release only when all of these hold:

- `isDraft=false`, `isPrerelease=false`, `tagName` equals the tag;
- `targetCommitish` equals the candidate SHA and equals `git rev-parse 'v1.5.0^{commit}'`;
- exactly two assets: `git-finalizer-1.5.0.tar.gz` and `SHA256SUMS.txt`;
- release notes are the CHANGELOG section for that version (non-empty);
- independent re-download verification passes:

  ```bash
  work=$(mktemp -d)
  gh release download v1.5.0 --repo xuanheng-tech/git-finalizer --dir "$work"
  (cd "$work" && sha256sum --check SHA256SUMS.txt)
  ```

### If it fails

- Workflow failure: fix forward with a normal commit and re-run the job. The tag may stay.
- Wrong bytes or wrong assets attached: delete **the release object** (`gh release delete v1.5.0
  --repo … --cleanup-history` is history cleanup of a *release*, never of the repository) and
  republish after the fix.
- **Never**: force-push a tag, move/delete-and-recreate a published tag, rewrite history, or edit a
  released `CHANGELOG.md` section. Published versions are immutable; corrections land in the next
  version's section.
- A release whose existence you must retract is an incident, not a maintenance action: say so
  explicitly in the next release notes.

## Support boundary of the release contract

Released guarantees cover the standalone CLI and its machine-readable output
([agent-contract.md](agent-contract.md)) plus the confinement rules in
[integration-boundaries.md](integration-boundaries.md). Optional integrations are guaranteed only to
fail closed with a precise blocker; an upstream project's unpublished generation is not part of any
Git Finalizer contract, and no release is held for an upstream change.
