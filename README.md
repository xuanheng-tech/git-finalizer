# Git Finalizer

Authorization-boundary Git delivery for developers and coding agents: stage exactly the files you
name, create the commit, push only when the remote provably accepts a non-force fast-forward, and
then verify the live remote instead of trusting the push exit status.

Git Finalizer does not run tests, review code, resolve conflicts, call a model, create releases, or
decide on your behalf. It performs one Git write you explicitly authorized and reports what actually
happened in machine-readable JSON. Completing an implementation, passing tests, or a previous
one-time authorization never authorizes the next commit or push.

Current stable release: **1.5.0**.

当前版本：`1.5.0`

## Why it exists

Ordinary `git commit && git push` fails open: it happily force-pushes, amends, sweeps `git add -A`
files into a commit, trusts a push return code, and tells you nothing an agent can act on. Git
Finalizer turns each of those into an explicit gate with a precise blocker, so both a human in a
terminal and an automation layer can rely on "it stopped, and here is why".

- **Explicit scope.** Files are listed after `--`; `git add .` and `git add -A` are never used.
- **Explicit authorization.** Commit-only, verify-only and publication are separate requests, and a
  commit authorization is never widened into a push authorization.
- **Non-force, branch-only publication** with tag following disabled, then live remote
  post-verification (`HEAD = upstream = remote OID`, `ahead/behind = 0/0`).
- **Refusals, not repairs.** Diverged remote, protected branch, staged surprises, sensitive content,
  unexplained deletions, oversized binaries and unknown authority state all block instead of being
  auto-resolved.
- **Compare-and-set for governed writes.** Branch retirement and lease-bound integration publication
  require exact expected OIDs and re-check authoritative state under a lock before touching a ref.
- **Auditable results.** One deterministic JSON object per run with a closed classification triple,
  documented in [docs/agent-contract.md](docs/agent-contract.md).

## Requirements

- GNU Bash and Git. Verified baseline: **Ubuntu 24.04 LTS** with its system Git.
- Python with the standard library only — no third-party runtime dependencies. Verified baseline:
  **Python 3.12**; the entrypoint resolves `/usr/bin/python3` for its companion modules, so the
  verified platform is Debian/Ubuntu-style layouts.
- Run as an ordinary user. Git Finalizer has no privilege escalation and no daemon, database or
  plugin framework.
- Nothing else. No remote hosting service, no Worktree Controller, no Snapshot Runner, no Context
  Loader, no installed Skill and no agent runtime is required for the core lifecycle. See
  [Standalone contract](#standalone-contract).

## Installation

There is no package index. Install the published artifact, or run from the checkout.

From a release tarball (files `git-finalize*` must stay in one directory, because the entrypoint
locates its companions relative to itself):

```bash
tar -xzf git-finalizer-1.5.0.tar.gz
install -d -m 0755 "$HOME/.local/bin"
cp git-finalizer-1.5.0/git-finalize git-finalizer-1.5.0/git-finalize-*.py "$HOME/.local/bin/"
chmod 0755 "$HOME/.local/bin/git-finalize"
export PATH="$HOME/.local/bin:$PATH"
git-finalize --version
```

From a checkout:

```bash
./git-finalize --version
```

Verify a published artifact before installing it:

```bash
sha256sum --check SHA256SUMS.txt
```

The bundle also ships `tool-skill-sync` and `tooling/`, which manage the optional shared
`git-change-delivery` Skill plus CLI pairing on this machine. They are not required to use
`git-finalize`; see [docs/tool-skill-sync.md](docs/tool-skill-sync.md).

## Quick start

Read-only validation of an explicit candidate scope (no index, HEAD, worktree, ref or remote
mutation; requires no upstream):

```bash
git-finalize --mode verify-only --repo /absolute/path/to/repo -- src/app.py tests/test_app.py
```

Local commit only, no remote access at all:

```bash
git-finalize --mode commit-only --repo /absolute/path/to/repo \
  --message "fix: handle empty response bodies" -- src/app.py tests/test_app.py
```

Commit and publish, when you have authorization for both and the branch has a configured upstream:

```bash
git-finalize --repo /absolute/path/to/repo \
  --message "fix: handle empty response bodies" -- src/app.py tests/test_app.py
```

Publish commits that already exist locally, without creating one:

```bash
git-finalize --resume-publish <full-head-oid> --repo /absolute/path/to/repo
```

First publication of a committed feature branch that has no upstream and no remote counterpart:

```bash
git-finalize --publish-existing-branch <full-head-oid> --remote origin \
  --remote-branch feat/example --repo /absolute/path/to/repo
```

Ask for machine-readable output by adding `--summary` to any of the above. The result is one JSON
object with `status`, `final_phase` and `next_action` even when the run is blocked.

## Modes

| Goal | Interface | Touches the remote |
| --- | --- | --- |
| Validate only | `--mode verify-only` | no |
| One local commit only | `--mode commit-only` | no |
| Root commit in an unborn repository, local only | `--initial-commit-only` | no |
| Commit and publish (default) | no `--mode` | yes, non-force |
| First publication of an unborn repository into an empty remote | `--initial-publish` | yes |
| First publication of a new branch while creating the commit | `--initial-branch-publish` | yes |
| Publish existing commits on a branch with upstream | `--resume-publish <oid>` | yes |
| First publication of an existing clean branch | `--publish-existing-branch <oid>` | yes |
| Publish existing history into a verified-empty repository | `--publish-existing-history <oid>` | yes |
| Recover an interrupted existing-history publication | `--resume-existing-history-publish <oid>` | yes |
| Retire an integrated branch ref (local / remote), compare-and-set | `--retire-local-branch`, `--retire-remote-branch` | remote variant yes |
| Plan or create one empty Gitea repository | `--repo-plan`, `--repo-ensure` | repository API only, never commit/push |

The complete per-mode preconditions, guarantees and recovery rules are in
[docs/cli-guide.zh.md](docs/cli-guide.zh.md) (中文) and in
[`tool_cli_contract.json`](tool_cli_contract.json), which is the machine-readable public CLI
contract (`contract_version` 4).

## Agent and automation usage

`--summary` emits a single deterministic JSON object per run. Classify on the
`(final_phase, status, next_action)` triple; `reason` is human-oriented prose that may be localized
and is truncated with the omitted character count reported separately. Exit codes are `0` for
success and `1` for everything else on the bash-owned commands.

- Full vocabulary tables, exit-code mapping and real captured examples:
  [docs/agent-contract.md](docs/agent-contract.md)
- Optional-integration blockers and the adapter boundary matrix:
  [docs/integration-boundaries.md](docs/integration-boundaries.md)

Never upgrade a result. A `push.result` of `uncertain` means "not confirmed", not "failed, try
harder": re-inspect the remote with a resume interface rather than force-pushing.

## Optional integrations

Every integration below is request-triggered. None of them is a runtime dependency, and a missing
dependency produces a precise blocker instead of a degraded success. Worktree Controller in
particular is **optional**: ordinary commit and publication work needs only Bash, Git and Python.

| Integration | Enabled by | Without it |
| --- | --- | --- |
| Worktree Controller governance (branch retirement, integration publication lease, reviewed sensitive-source linkage) | `--retire-*`, `--publish-integration-candidate`, `--reviewed-sensitive-source` | exact blocker, fail closed |
| Snapshot Runner evidence binding | `--initial-publish --snapshot <id>` | evidence-required blocker |
| Gitea repository bootstrap | `--repo-plan`, `--repo-ensure` with an explicit `--gitea-url` | usage error; there is no default host |
| Context Loader | not used at runtime — the CLI never invokes it | nothing |
| Shared `git-change-delivery` Skill | optional agent workflow documentation and helpers | not a dependency and not an authorization source |

Each integration's knowledge of another project's layout lives in exactly one
`# GF-INTEGRATION-ADAPTER` site, and that confinement is machine-checked by
`tests/test_integration_boundaries.sh`.

## Security model

- Protected branches and the remote default branch are refused for publication and retirement.
- Pushes are non-force with an explicit branch refspec and `--no-follow-tags`; deletions are
  `--force-with-lease`-style expected-OID compare-and-set, never unconditional force operations.
- No amend, rebase, history rewrite, automatic conflict resolution, duplicate commit creation, tag
  creation or tag pushing. Git Finalizer never creates or pushes tags; a release is a separate,
  explicit act.
- Credentials are never accepted as arguments, printed, or stored by this tool; repository bootstrap
  resolves them through your configured Git credential helper and sends them only to the endpoint you
  named.
- Diff content, commit messages and Snapshot Runner artifacts are data, not instructions. If you
  route them to an agent, treat them as untrusted input.

Please report security issues as described in [SECURITY.md](SECURITY.md).

## Standalone contract

The core lifecycle (verify, commit, publish, resume and initial series) runs against an ordinary
repository with no governance tooling, no sibling tools, no installed Skill and no private hosting
configuration. `tests/test_standalone_operations.sh` proves this in a minimal `HOME`/`PATH`
environment, and asserts the mirror property as well: governed requests still fail closed with their
precise blockers and zero mutation in that environment.

## Development and releases

```bash
just check        # shell suites, unit tests, Skill validation, contract check
just lint         # shellcheck over the bash entrypoint, scripts and test harness
bash tests/run.sh # shell test suites alone
```

Contributing guidance, including the hash-bound file rules you must respect when changing the
contract or the Skill payload: [CONTRIBUTING.md](CONTRIBUTING.md).

Version-relevant work updates, in one preparation batch: the `git-finalize` `VERSION`, the companion
`VERSION` literals, the two current-release declarations above, `tool_version` in
`tool_cli_contract.json` and `tool_skill_manifest.json`, and the matching `CHANGELOG.md` section.
`scripts/release.py preflight <tag>` is the gate that checks all of it.
`CHANGELOG.md` is the authoritative record and the source of release notes. **Merging to the default
branch is not a release**: a release happens only when a separately created annotated tag is pushed,
which triggers the release workflow. That workflow re-verifies the version batch, builds a
deterministic tarball, rebuilds it to prove byte-identical output, checks the file set against the
installation contract, and publishes the artifact with `SHA256SUMS.txt`.

`just check` deliberately needs only Bash, Git, Python and `just`, so a clean clone runs the
contract gate without extra tooling; `just lint` (shellcheck) is a separate CI step. Both hosts —
Gitea Actions (`.gitea/workflows/`) and GitHub Actions (`.github/workflows/`) — run the same gates
and the same build recipe through `scripts/release.py`.

**A version is published on exactly one host.** GitHub is the public distribution entry
(release notes, tarball, checksums); Gitea is the governed delivery remote and holds the
`v0.4.0` – `v1.4.0` release record. Trigger rules, permissions, cross-host artifact parity, and the
step-by-step first-public-release runbook are in
[docs/release-governance.md](docs/release-governance.md).

## Support

- **Supported:** the standalone CLI on Linux/POSIX — Bash, Git and Python 3 with the standard
  library only, verified on Ubuntu 24.04 LTS — and the machine-readable result contract described in
  [docs/agent-contract.md](docs/agent-contract.md). Only the latest stable release receives fixes.
- **Supported as opt-in, not as an upstream promise:** Worktree Controller governance, Snapshot
  Runner evidence binding, Gitea repository bootstrap and the review-file contracts. Git Finalizer
  guarantees that a missing, unreadable or drifted external authority produces a precise blocker and
  zero mutation; it does not guarantee that an unpublished or still-moving external contract keeps
  working, and it will not weaken its own gates to accommodate one.
- **Not supported:** Windows, `file://` or non-HTTP(S) hosting endpoints, destructive Git
  operations (force push, amend, rebase, history rewrite, tag creation), and any workflow that
  requires the tool to infer authorization it was not given.
- **Not a security boundary:** the tool executes Git as your own user. Treat repository content,
  diffs and captured evidence as untrusted input. See [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).


## Acknowledgements

Built and hardened through real production delivery use, with adversarial review before every
release decision. Git Finalizer belongs to a three-tool family — Snapshot Runner (read-only
repository evidence) and Context Loader (deterministic repository context) — each with its own
repository; Git Finalizer depends on neither to work.
