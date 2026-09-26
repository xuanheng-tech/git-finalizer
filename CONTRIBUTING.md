# Contributing

Thanks for working on Git Finalizer. This repository is small but deliberately strict: most of
its value is that a command refuses to do something unless the evidence supports it. The rules
below exist so a change cannot quietly weaken that.

## Ground rules

- **Explicit authorization stays explicit.** No change may infer a commit, push, retirement or
  publication from context, history, a passing test or a previous one-time authorization.
- **Fail closed.** Missing authority, unreadable capability, drifted plan, unsafe lock,
  incomplete installation or unknown content drift must produce a precise blocker and zero
  mutation. Never add an implicit fallback, a "best effort" downgrade, or a silent skip.
- **No destructive Git.** Force push, amend, rebase, history rewrite, automatic conflict
  resolution, tag creation/publishing and duplicate commits remain refused. Refactors must not
  loosen this.
- **One adapter per integration.** Knowledge about another project's file layout, lock name,
  artifact names or API surface belongs in the single `# GF-INTEGRATION-ADAPTER: <name>` site for
  that integration. Adding the same fact elsewhere is rejected by
  `tests/test_integration_boundaries.sh`; if the fact is genuinely new, disclose it in
  `docs/integration-boundaries.md` in the same commit.
- **Docs describe verified behaviour.** Every claim in `README.md` and `docs/` must be traceable
  to code, a test, or a released artifact. Do not document intent.

## Environment

Verified baseline: **Ubuntu 24.04 LTS** with system Bash, Git and Python 3.12, plus `just`,
`shellcheck` and `python3` on `PATH`. Runtime code depends on the standard library only.

```bash
just check        # full gate: shell suites, unit tests, Skill validation, contract check
just lint         # shellcheck over git-finalize, tool-skill-sync, scripts/ and tests/
just skill-check  # source/live Skill consistency (diagnostic only, writes nothing)
bash tests/run.sh # the shell test suites on their own
```

`just check` is the portable contract gate: it needs only Bash, Git, Python and `just`, so it must
keep running on a clean clone with no extra tooling. Static lint of the shell surface is a separate
`just lint` step that CI runs in addition; `scripts/lint.sh` resolves a usable `shellcheck` (a
present-but-unusable binary falls back to `uvx --from shellcheck-py`) and fails with a clear message
when neither is available.

The gate reads the repository's annotated tags — the release record in
[docs/release-governance.md](docs/release-governance.md) is checked against them — so a shallow clone
has to be completed first: `git fetch --unshallow --tags`. CI checks out with `fetch-depth: 0` for
the same reason.

## How a change is expected to look

1. **State the surface.** Which command, mode or contract element changes? Public CLI flags,
   `--summary` fields, exit codes and status enums are the Agent-facing contract.
2. **Add the negative test too.** Every new gate needs a case proving it refuses, that it
   refuses *before* writing anything, and that the refusal message is specific. Look at
   `tests/test_standalone_operations.sh` and `tests/test_integration_boundaries.sh` for the
   expected style: isolated `HOME`/`XDG_STATE_HOME`/`PATH`, `/tmp` scratch repositories and local
   bare remotes only.
3. **Keep the standalone contract.** The core lifecycle must keep working with no Worktree
   Controller, no Snapshot Runner, no Context Loader, no installed Skill and no private Git
   hosting. If a change makes a governed integration reachable without requesting it, that is a
   bug.
4. **Update the machine contract in the same commit** when output changes. The summary key set,
   `status`, `final_phase` and `next_action` vocabularies are extracted from source and asserted
   against `docs/integration-boundaries.md` and `docs/agent-contract.md`; adding a key or value
   without documenting it fails the build.

## Hash-bound files

These are byte-bound and must be updated together, in one commit:

| File | Bound to |
| --- | --- |
| `tool_cli_contract.json` | `public_cli_contract_sha256` in `tool_skill_manifest.json` |
| `skills/git-change-delivery/` payload files | `canonical_skill_sha256` in the root manifest **and** both mirrors under `manifests/` |
| `git-finalize` + companion `VERSION` literals | README current-release declarations, `CHANGELOG.md` section, `tool_version` in contract and manifest |

A released `CHANGELOG.md` section and any pushed tag are history: they are never edited or
rewritten. Correct the record in a new section instead.

## Version and release flow

See [docs/release-governance.md](docs/release-governance.md) for the authoritative process and the
first-public-release runbook, and [docs/maintenance-backlog.md](docs/maintenance-backlog.md) for work
that is deferred on purpose: each entry names the condition that unblocks it, so an ordinary change
must not quietly do part of it.

- Version-relevant work updates the entrypoint `VERSION`, the companions, the README
  declarations, `tool_cli_contract.json`/`tool_skill_manifest.json`, and the matching
  `CHANGELOG.md` section in one preparation batch.
- Merging to the default branch is **not** a release. A release happens only when an approved
  annotated tag is pushed, which triggers the release workflow. Tags are annotated and carry the
  release summary; they are not GPG-signed, which is why immutability is a process rule rather than a
  host guarantee (see the release governance document).
- The release job re-verifies the whole version batch, builds a deterministic tarball, rebuilds
  it for comparison, verifies the file set against the installation contract, and publishes the
  artifact with `SHA256SUMS.txt`. The tool itself never creates or pushes tags.

## Commit messages

Conventional-commit style, imperative, scoped when useful:
`fix(retirement): attest controller capability and name namespace blockers`. The body explains
why the change was needed and what was verified; release notes come from `CHANGELOG.md`, not
from commit messages.

## What will not be accepted

- Features that call a model, run tests, or make review decisions — those belong to other tools.
- Long-running daemons, databases, plugin frameworks, or a rewrite of the argument parser.
- Pairing bumps of sibling projects bundled with unrelated work.
- Changes that trade a precise blocker for a generic error, or a refusal for a warning.
