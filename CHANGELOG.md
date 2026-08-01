# Changelog

This file records only behavior verified from this repository's Git history. Dates on tagged
versions are the annotated tag dates. A source-version date is the commit date and does not claim
that a release tag already exists.

## Unreleased

- Add a tag-only Gitea Actions release workflow for a deterministic script bundle and SHA-256
  verification. Git Finalizer's branch-only publish behavior is unchanged.

## 0.3.1 - 2026-08-01

Source version commit: `4d88db801fa58fbb5bfbf49d683c01cbe1279b2b`; release tag not yet present.

- Run repository Python entry points with `-B` so Finalizer and Hook checks do not write bytecode.

## 0.3.0 - 2026-08-01

Source version commit: `cd4b16330bf0ec4b41c78b844b3577440adea629`; release tag not yet present.

- Add optional initial-publish binding to complete Snapshot Runner evidence, including path, size,
  SHA-256, executable-bit, index, and root-tree checks.

## 0.2.4 - 2026-07-27

- Preserve an explicitly staged deletion when an ignored local copy exists at the same path.
- Add the versioned Codex PreToolUse bridge, its tests, and explicit deployment checks.

There is no 0.2.3 source version or tag; the repository moved directly from 0.2.2 to 0.2.4.

## 0.2.2 - 2026-07-21

Source version commit: `f86c2169fcd9fac36be57bd10588c6dd64177058`; release tag not present.

- Allow explicitly named large binary files within fixed size and safety limits.

## 0.2.1 - 2026-07-20

- Accept a configured but not yet resolvable upstream when publishing or resuming an empty-remote
  clone.

## 0.2.0 - 2026-07-20

- Add initial publication to an empty remote and exact resume of a root commit after an uncertain
  push.
- Validate remote state before creating the commit.

## 0.1 - 2026-07-18

Source version commit: `7dd9a6af1dbe07c365120a6460bc92b3384d6cc5`; release tag not present.

- Introduce explicit-path staging, commit creation, fast-forward-safe branch push, and post-push
  verification.
