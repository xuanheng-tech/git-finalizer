# Release governance

How a Git Finalizer version becomes a published release, on which host, and what must be true
before and after. This file is the authority for release *process*; `AGENTS.md` and
`CONTRIBUTING.md` cover the version batch a contributor must prepare, and `SECURITY.md` covers the
supported-version policy.

## Roles

| Concern | Owner |
| --- | --- |
| What a release *is* | An annotated tag `vX.Y.Z` whose tree passes `scripts/release.py preflight` |
| Who authorises a release | The executor checks explicit task authority: a continuing grant may include normal tags, Releases and activation within existing channels and visibility; a commit/push-only grant does not, see [authorization-boundary.md](authorization-boundary.md) |
| Build recipe and verification gates | `scripts/release.py` — the only implementation, shared by both hosts |
| Public distribution for outside users | **GitHub** `xuanheng-tech/git-finalizer` (releases, release notes, artifacts, checksums) |
| Governed daily delivery and the historical release record | **Gitea** `xuanheng-tech/git-finalizer` (the ten releases `v0.4.0` – `v1.4.0` were published here; `v1.5.0` onward is GitHub) |
| Production installation on this machine | `tool-skill-sync` bundles built from the canonical source repository — it does **not** consume a release artifact from either host |

One release authority per version: **by default a version is published on exactly one host**. Publishing the
same tag on both hosts is a deliberate mirroring act, not a default, and it requires the digest
comparison in [Cross-host parity](#cross-host-parity). A published version is never rewritten, so
releases are forward-only — that is a rule this project follows, and no workflow here replaces a
published tag or asset, but neither host cryptographically enforces it: see
[Publishing permissions](#publishing-permissions) for what the hosts actually require.

## Tag triggers

Both hosts evaluate a release workflow **from the tree of the pushed tag's commit**:

- Tags whose commit tree contains no workflow directory do not trigger a release run. Observed:
  pushing the existing fifteen tags to GitHub produced zero release runs and zero releases, because
  none of those commits contained `.github/workflows`.
- The first tag created from a commit that *does* contain `.github/workflows/release.yml` will create
  a real public GitHub Release. Treat that as irreversible.
- The trigger glob is wider than the release contract: `v*.*.*` also matches `v1.6.0-rc.1` and
  `v1.6.0.1`, both of which `preflight` rejects. Pushing one buys a red release run for a version
  that will never ship, and the tag then stays. Release tags are `vX.Y.Z` and nothing else.
- Publication mechanics differ, and with them the post-publish guarantee:
  - Gitea: `GET` the tag's release first and refuse if one exists → `POST` draft → upload and
    re-download each asset, checking its pre-upload digest and the exact inventory →
    `PATCH {"draft":false}` → prints `published_release=<tag> assets=<n>`.
    Failed verification retains the draft.
  - GitHub: `gh release create` publishes immediately with both assets attached, then a following
    step re-downloads the assets and verifies them.
  Both hosts run the same gates before publishing. Gitea verifies while the release is still a draft;
  GitHub verifies after publication. Existing records survive failed verification.

Before tag creation, the maintainer runs `just release-check` in the clean candidate checkout.
It runs lint and all existing checks with `--require-controller`: the real Controller retirement
suite must report exactly one executed PASS. Missing, skipped, duplicate or failed Controller
results are fatal. Record the installed Controller version and contract with the candidate evidence.
Hosted checks keep Controller optional so a standalone runner still reports its absence as SKIP;
hosted green checks do not substitute for the mandatory maintainer gate.

Both hosted jobs run, in this order and on both hosts: `preflight` → `just lint` → `just check` → `build` →
`verify` → release-notes extraction → publish. A tag whose version batch disagrees fails at
`preflight` before anything is published.

Every build/version gate reads the **working tree**, not the tag object: those gates do not
inspect `git status` or compare a file against `HEAD`. The separate `publication-status` command
observes remote tag and Release identity; it does not establish checkout cleanliness. The release jobs are
safe because they check out the tag commit and assert `HEAD` equals it before running anything, so a
manual run has to happen in a clean checkout of the commit being tagged.

## Version batch (what preflight enforces)

`scripts/release.py preflight vX.Y.Z` refuses the release unless all of the following agree:

- the tag name equals the `git-finalize` `VERSION`, and `VERSION` equals the README `当前版本`
  declaration (the English `Current source version:` line is bound by `just check`, not here);
- all three governance companions declare the same `VERSION`;
- `tool_cli_contract.json` and `tool_skill_manifest.json` declare the same `tool_version`, and
  `contract_version` matches `toolchain_compatibility.json`;
- the manifest's `public_cli_contract_sha256` equals the bytes of the shipped
  `tool_cli_contract.json`;
- the root manifest **and both sibling mirrors** bind the actual Skill payload tree hash. A mirror's
  own `public_cli_contract_sha256` is that sibling's contract digest, which this repository does not
  ship and therefore does not verify — never overwrite it with Git Finalizer's digest;
- `LICENSE` is present in the packaged surface;
- the `CHANGELOG.md` section is **not** checked here: `just check` requires the declared version to
  have a section, and the release job fails when `scripts/changelog.py extract` returns nothing.

## Artifact and checksum consistency

In a fresh Git checkout, one commit yields one byte stream: sorted member names, `ustar`,
owner/group `0` with numeric ids, member mtime pinned to the commit timestamp, `gzip -n`, and
staged directory modes pinned to `0755`. Directory-mode pinning matters: before it was added the
build depended on the builder's umask, so two hosts could have produced different bytes from the
same commit.

Regular-file read permissions still come from the builder's checkout; Git records only their
executable bit. Altered read permissions can therefore change archive bytes even when `git status`
is clean. For `v1.8.6`, the canonical checkout had `quick_validate.py` at `0600` while a fresh checkout
used `0644`: file contents were identical, and the fresh-checkout digest matched the published
artifact. Use a fresh checkout when comparing builds across hosts.

Per-run guarantees:

- `build` produces the tarball, rebuilds it in the same job, and fails if the two differ; it is the
  only step allowed to author `dist/SHA256SUMS.txt`;
- `verify` re-opens the artifact and fails unless the file set equals the installation contract, no
  member path is absolute or traverses `..`, the entry point is `0755`, and its embedded `VERSION`
  equals the tag. It compares against the checksum file `build` wrote and **refuses to create one**,
  because a check that can write its own expected value is not a check;
- the GitHub job additionally **downloads the assets back from the published release** and compares
  the downloaded tarball against the digest recorded *before* the upload, asserts that the published
  release carries exactly `SHA256SUMS.txt` plus the tarball, and re-checks the checksum file itself —
  so the value proven is not one that arrived in the same download.

<a name="cross-host-parity"></a>
### Cross-host parity

Known-good anchor: the published `v1.4.0` artifact — `ae1d29683ca36a911993eb138b769577961f7708d61e8eeb47ac402419b7f0f9`,
157304 bytes, Gitea-only release — was re-downloaded from that host and matched its own published
`SHA256SUMS.txt` under `sha256sum --check`, and the same bytes are reproduced by
`scripts/release.py` (which first shipped in `v1.5.0`, so it is run *against* the `v1.4.0` tree
rather than checked out with it) under `umask` 022, 002 and 077.

Cross-host equality is established for a version only when both hosts independently build that
same candidate and the downloaded release assets match. What `v1.5.0` proved was host independence
of the recipe: the digest the GitHub runner
built and published equals, byte for byte, the digest `scripts/release.py selfcheck` produced
locally from the same commit (see the release record below), on different machines with different
images. When the same version is ever published on both hosts, the two `git-finalizer-<version>.tar.gz`
digests must be compared; a mismatch is a release defect (differing `tar`/`gzip` builds, locale
sort order, or runner image), not something to paper over by publishing one host's file on the other.

## Publishing permissions

- `ci.yml` and `quality.yml` (`.github/workflows/ci.yml`, `.gitea/workflows/quality.yml`):
  `permissions: contents: read`.
- `release.yml` (both hosts): `permissions: contents: write` only — no packages, no pull-request
  writes, no id-token.
- GitHub organization setting for this repository: default workflow permissions `read`, and
  workflows may not approve pull requests.
- No repository secrets are used or required: each platform injects its own token
  (`GITHUB_TOKEN` / `GITEA_TOKEN`). Adding a secret to make a release work is a design smell here.
  GitHub's pinned `setup-just` uses the built-in `github.token` for GitHub release metadata,
  avoiding shared-IP anonymous API limits. Gitea keeps that input empty so its platform token is
  never forwarded to GitHub. Action pins, build-tool versions and job permissions stay the same.
- What `main` actually requires, measured when `v1.5.0` was released: one required status context,
  `check` — the single `ci.yml` job, which runs `just lint` and `just check` inside itself — with
  `strict` on. There is no separate required `lint` context, no required pull-request review, no
  repository or organization ruleset, `enforce_admins` is off, and tags are annotated but not signed.
  So the contract gate does block a merge that fails it, but shell lint is not independently
  required and tag immutability is a convention backed by the no-force-push rule, not by the host.

## Runbook: first public GitHub release

The heading is history: this began as the first-public-release runbook and is now the runbook for
every release. Tag publication and activation require task authority that covers those actions;
a continuing automatic-delivery grant may supply it. Source maintenance alone is not a release.
Production activation on the maintainer machine is a **separate** authorised step that follows a
successful release, and it uses `tool-skill-sync install --activate-production` from the canonical
source repository rather than the release artifact.

### Before

1. A release-preparation batch is merged to `main` and contains: version bump in `git-finalize` and
   the three companions, README source-version declarations, `tool_version` in contract and manifest,
   a `CHANGELOG.md` section for that version, and any manifest/contract re-pins made in the same
   commit.
2. Decide the host. For the first public release the host is **GitHub**; do not also publish that
   version on Gitea unless you intend to mirror it and compare digests.
3. Confirm the candidate:

   ```bash
   VERSION=$(sed -n 's/^readonly VERSION="\(.*\)"$/\1/p' git-finalize)
   git fetch --all --tags --prune
   git log --oneline -3 github/main   # or origin/main, the branch being tagged
   git status --porcelain=v1          # must be empty in the checkout used for tagging
   just release-check                # real Controller suite must execute and pass
   python3 -B scripts/release.py preflight "v$VERSION"    # exact new tag name
   python3 -B scripts/release.py selfcheck "v$VERSION"    # record digest from this clean candidate
   python3 -B scripts/release.py publication-status "v$VERSION" \
     --remote github --candidate "$(git rev-parse HEAD)" \
     --expected-sha256 '<digest-from-selfcheck>'
   ```

   Run these gates in a fresh clean checkout with normal Git checkout modes. Continue to new tag
   creation only for `state=unpublished`; every other state follows the recovery table below.
   The explicit `github` remote must have one identical, credential-free fetch/push endpoint on
   github.com (HTTPS or Git SSH). Observation uses the existing authorised `gh` client and makes
   no tag, ref, Release or asset changes. This guard observes GitHub only and does not replace
   either workflow's concurrency control or Gitea's pre-create lookup.

   GitHub's by-tag endpoint documents published Releases. A 404 therefore requires a complete
   paginated listing with draft visibility before absence can be established. Read access alone,
   authentication/transport failures and malformed metadata produce a blocker, not `unpublished`.
   See the [GitHub Releases API](https://docs.github.com/en/rest/releases/releases).

4. Announce nothing before the tag exists; the tag is the release trigger.
5. Record the release decision in the release request: candidate SHA, the `preflight` line it
   printed, the `selfcheck` digest, which host publishes, and that no other host will publish the
   same version unless the digest comparison below is performed afterwards. The approved decisions
   become a row in [Release record](#release-record) once the release exists. A digest is a property
   of the commit it was built from: run `selfcheck` while standing on the candidate, because a later
   commit — even a docs-only one — yields a different number for the same version.

The first release out of this model was `v1.5.0`: the open-sourcing, maintenance-governance and CI
batch that follows the released `1.4.0` line. `v1.4.0` stays exactly as published on Gitea and is
**never** re-created, re-tagged or re-published on GitHub — the GitHub history for it is the tag and
the source, not a release object.

### Publish

`<candidate-sha>` is the tip of `main` at the moment the release is approved — never a re-created
commit, and never a commit that `preflight` has not been run against.

```bash
git tag -a "v$VERSION" <candidate-sha> -m "v$VERSION — <one-line summary>"
git rev-parse "v$VERSION^{commit}"    # must print <candidate-sha>
git push github "v$VERSION"           # normal push; never --force, never re-create a moved tag
```

Then watch the run:

```bash
gh run list --repo xuanheng-tech/git-finalizer --workflow release --limit 3
gh run watch <run-id> --repo xuanheng-tech/git-finalizer --exit-status
```

### After

```bash
VERSION=$(sed -n 's/^readonly VERSION="\(.*\)"$/\1/p' git-finalize)
gh release view "v$VERSION" --repo xuanheng-tech/git-finalizer \
  --json tagName,isDraft,isPrerelease,assets,targetCommitish
```

Accept the release only when all of these hold:

- `isDraft=false`, `isPrerelease=false`, `tagName` equals the tag;
- `targetCommitish` equals the candidate SHA and equals `git rev-parse "v$VERSION^{commit}"`;
- exactly two assets: `git-finalizer-$VERSION.tar.gz` and `SHA256SUMS.txt`;
- release notes are the CHANGELOG section for that version (non-empty);
- independent re-download verification passes:

  ```bash
  work=$(mktemp -d)
  gh release download "v$VERSION" --repo xuanheng-tech/git-finalizer --dir "$work"
  (cd "$work" && sha256sum --check SHA256SUMS.txt)
  ```

### If it fails

Re-run `publication-status` with the same tag, candidate and independent clean-build digest before
retrying an external action. Its exit code is 0 for an observed state and 2 for blocked observation;
0 alone is not permission to create a tag or Release. The observation is not an atomic lock, so
recheck immediately before an external mutation and preserve a changed/unknown result.

| Observed state | Recovery |
| --- | --- |
| `unpublished` | After the clean candidate gates, create the new annotated tag through the supported workflow. |
| `tagged_unpublished` | Inspect the existing workflow run; retry that run only when it can safely build/publish the same candidate. Keep the tag. |
| `draft_incomplete` | Inspect the existing draft and its uploaded bytes. Resume it only through a supported, authorised recovery path; do not create another Release or overwrite assets. |
| `published_assets_verified` | Compare against `selfcheck` from a fresh checkout of the exact candidate; a downloaded checksum alone is not source proof. |
| `published_verified` | Record the existing publication; no publication retry is needed. |
| `published_incomplete`, `conflict`, `unavailable` | Preserve tag, Release and assets; investigate before any dependent mutation. Fix code/bytes in the next version. |

- Workflow failure before publication: retry a transient failed run for the same candidate. A code
  change belongs to a new commit and new version; the existing tag stays on its original candidate.
- A re-run of a job that already published will not quietly republish: GitHub's create call refuses a
  second release for the same tag, and Gitea refuses earlier still at its pre-create lookup. Both
  release jobs group concurrency by tag, so two runs for one tag cannot overlap. Read the existing
  release object before assuming the publication is broken — usually it is fine and only the job went
  red.
- Wrong published bytes or assets: preserve the observed publication and ship a corrected new
  version. Retraction of an existing public Release is an incident with separate authority; this
  maintenance runbook never deletes or recreates it.
- **Never**: force-push a tag, move/delete-and-recreate a published tag, rewrite history, or edit a
  released `CHANGELOG.md` section. Published versions are immutable; corrections land in the next
  version's section.
- A release whose existence you must retract is an incident, not a maintenance action: say so
  explicitly in the next release notes.

## Verifying a release without trusting this project

There is no build attestation, no signature and no SBOM, and the packaged manifests carry the
placeholder `tool_commit: "@release"` because a tracked file cannot contain its own commit id. So a
third party's verification procedure is reproducible by construction:

```bash
git clone --branch v1.5.0 https://github.com/xuanheng-tech/git-finalizer.git && cd git-finalizer
python3 -B scripts/release.py build v1.5.0
sha256sum dist/git-finalizer-1.5.0.tar.gz   # must equal the published SHA256SUMS.txt entry
```

If the two differ, the difference is in the build environment, and the recipe is deliberately narrow
enough to make that diagnosable: `tar` and `gzip` behaviour and locale sort order are the variables,
which is why the member metadata is pinned in the recipe (sorted names, `ustar`, numeric uid/gid `0`,
mtime from the commit, `gzip -n`, staged directories `0755`). The runner image is part of that
environment: GitHub pins `ubuntu-24.04`, while the Gitea job asks for `ubuntu-latest`, so a Gitea
digest is only comparable once the two hosts are known to run the same tools — an open item in
[maintenance-backlog.md](maintenance-backlog.md).

## Release record

One row per published version. The Host column names its single host or an explicitly verified
`GitHub + Gitea` mirror; a mirror row requires the same candidate, tag object, artifact digest and
size on both hosts. Every identifier here is measured rather than remembered: the candidate
commit is `git rev-parse 'vX.Y.Z^{commit}'`, the tag object is `git rev-parse vX.Y.Z`, and each
digest comes from the tarball as served by every host named in that row.
`tests/test_public_docs.py` re-derives the two Git columns from the repository's own refs, so a row
that drifts from the tags fails the contract gate. A row whose tag is absent is accepted only when it
belongs solely to the *other* host; a mirrored row requires the tag on either host. Production activation
is tracked by `tool-skill-sync` on the maintainer machine and is deliberately **not** part of this
record.

| Version | Host | Candidate commit | Tag object | Artifact SHA-256 | Bytes |
| --- | --- | --- | --- | --- | --- |
| `v0.4.0` | Gitea | `9b7a9e54aec20890b3c31c7600a2d55f5ad37d87` | `644600f74c51199787447473ff241ce81e1a66b8` | `1bba32d1cca447ab6f8f07c166593b4476be3cf55950f563243ad9d86224e658` | 35582 |
| `v0.4.1` | Gitea | `25cb70eeb1d6ac2432ffa9c6c1efa12431fd3844` | `8084f7ac1c338335ce07e855a41399d74cf13cf5` | `6b2cea4d58f4ef352b9c458e3f01044af71f6d2b21c4e8430c8c3de800c0b546` | 35998 |
| `v0.4.2` | Gitea | `0efa3203ab61d330a5bbb407607194caf5bf9d79` | `4db79d99213a7ba47db8e7aa44885f5a4dff53b0` | `a00932b935134c88a91538bc2b7b48873b9f4a0206e109f47896ba595edf3c49` | 39562 |
| `v0.5.0` | Gitea | `61fc4de868b358c5c3e84132bad1d01341f6927e` | `e8c284314b87947cbca0fcf1b310d9b14bf1f0e1` | `b583fe5a38570dcb10a75bdb69239bdc3a5bd9ea276335d201252928e1ef8a6d` | 42837 |
| `v0.6.0` | Gitea | `8197f9c6367fb7c26f811fefc892a5983fb4d67c` | `f798b2fb00c33bac6394bd56a0886ba8f6414ba0` | `dbffc690def904198010b43d74a52fc5e77ab36a9f1d8694a18f55ffb0ddf44d` | 47662 |
| `v1.1.0` | Gitea | `8a0326e20a43dfab08d876575b1306112a926c03` | `6acee193d1feda44ee6cfaa9fd94e2e9b3c7779d` | `0136a1f6bb0369f098cd29008456646d1549e4608d5a6d8bd7eac986a32a6d6d` | 144155 |
| `v1.1.1` | Gitea | `44e9d7741750c2355ab538ed78969621df0ce323` | `f566c678a5f513124fdd71d795fb12e35123c4c6` | `997f98fb4a38a3d48e10d7052239b9b60295f970a7b1f7ca4da598cc2473fbbe` | 144732 |
| `v1.2.0` | Gitea | `c5f1f96dbc7d26e87ea5d9c25d6bef483ceede4f` | `1ebde18fa9431e33ffcf8b833302c90d4f9defa3` | `e4ec484703873b667f9f9a9ab245c357cb17db75468434bd5e289f02e270f40d` | 149317 |
| `v1.3.0` | Gitea | `cfbc6d05d125f852786e6e5dacb681db13f9695e` | `f06b1745d37acdb254018ce36fa7ae0ecb200454` | `3502230ecbd1213042443fd44351068dcd321940f9f6d1e30449e132f3e6d3e9` | 150148 |
| `v1.4.0` | Gitea | `64806dae0805b3dc707e7eab848c8b40ee952ad3` | `f768a6197268dc4a9be1b5d466464a3c2f6bf73c` | `ae1d29683ca36a911993eb138b769577961f7708d61e8eeb47ac402419b7f0f9` | 157304 |
| `v1.5.0` | GitHub | `a59773ae5ea4aca781152ac289d46afcc91a93b9` | `12cc0f60d0a04296e1cf8527aa14673c3cb42cfb` | `8e16fc878de5e2ff715d035dde74292a240842a3bc296a4c4755c061f18df77c` | 160480 |
| `v1.6.0` | GitHub | `930c5ec3e489fe9a160349d4dc9fad9390697877` | `e0aa67de0e22aebd93ecccab6868c48bef89e017` | `70741393ffbc837d44b82cac9031be2dfe0aac5c3661802b42c7110752c769eb` | 164273 |
| `v1.8.5` | GitHub | `32ebbcb94d1ecf4468db72dfe6e4b196d9660fb9` | `44129cf58c7f470ee9add8d1eac55e4338f243ae` | `c15274ee104988b512ef3cfd2c36b6ca8227720943b29abe7d9cc0c10753de51` | 195094 |
| `v1.8.6` | GitHub | `17d15bf343e64e06840b520b7b3509ba0ec4f3cd` | `762188cade263a862b8a1dd1b352f2ebd9c1471c` | `0f3161b42c3140b10dd5dd7dad158c84dc3ea8487969cb129e0f8c5f2ac2069d` | 195192 |
| `v1.8.7` | GitHub | `96bbd0f1add33f817b10ea0fa1fc03f12a02a5f8` | `6ab2c2b04625dcbfbc0876a47ea8efacc347153f` | `d08e678d03047a13c4930d26abad9864d132489bf14ed849d31ef7b7fd8772d2` | 196087 |
| `v1.8.8` | GitHub | `16778ab07b051981c8e294dd23eff2a6c7c8e81c` | `e290125fa40263a79da43a53504aafd4e6d09f39` | `5325706c8d049f5097547f89c954090e8bbac23a9ef5c3cf2d09548f8a168542` | 196457 |
| `v1.8.9` | GitHub | `54d2f7f80bfb0392bea79561311551c7528cd3b6` | `8149d8e5dbf804a70f0cab1df94c728c08d61df9` | `23ed4bf2459568f7fc3b1705358be26ecf18dcf24611bd5eaadfe1d0e5116f33` | 196825 |

`v0.4.0` – `v0.6.0` shipped their artifact under the pre-rename name `codex-git-finalizer-*`. Not
every version string in this repository was ever released: thirteen sections of `CHANGELOG.md`
(`0.2.2`, `0.6.1`, the `0.7.x` – `0.10.x` line and `1.0.0`) have no tag at all, and five tags
(`v0.2.0`, `v0.2.1`, `v0.2.4`, `v0.3.0`, `v0.3.1`) carry no release object on either host. There is
therefore no `v1.0.0` release anywhere, and a range such as "v1.0.0 – v1.4.0" must not be used for the
Gitea line.

`v1.5.0` was the first release published through this model and the repository's first GitHub
Release: the tag went to GitHub only and `.github/workflows/release.yml` (run `36245062595`) built,
published and re-verified it, with **no** Gitea release object for that version and no mirror.
`v1.6.0` repeated that decision rather than reopening it: annotated tag pushed to GitHub only,
published by run `36305958586`, and its published digest equals the digest measured locally from the
same commit before tagging, so the two independent witnesses agree to the byte. Gitea again carries no
release object for it. The Gitea release
job last ran at `v1.4.0`, on workflow bytes that predate the current
`.gitea/workflows/release.yml` — that file was last edited by the `v1.5.0` candidate itself, so its
publish path has never executed as shipped and its first real test is the next Gitea release. A future
version may be mirrored on the second host only together with the digest comparison in
[Cross-host parity](#cross-host-parity).

## Support boundary of the release contract

Released guarantees cover the standalone CLI and its machine-readable output
([agent-contract.md](agent-contract.md)) plus the confinement rules in
[integration-boundaries.md](integration-boundaries.md). Optional integrations are guaranteed only to
fail closed with a precise blocker; an upstream project's unpublished generation is not part of any
Git Finalizer contract, and no release is held for an upstream change.
