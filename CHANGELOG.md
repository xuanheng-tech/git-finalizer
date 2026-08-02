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
