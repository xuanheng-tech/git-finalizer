# Git Finalizer

Authorization-boundary Git delivery for developers and coding agents: stage exactly the files you
name, create the commit, push only when the remote provably accepts a non-force fast-forward, and
then verify the live remote instead of trusting the push exit status.

Git Finalizer does not run tests, review code, resolve conflicts, call a model, create releases, or
decide on your behalf. It performs one Git write you explicitly authorized and reports what actually
happened in machine-readable JSON. Authorization comes from the caller: an existing task grant can
cover consecutive deliveries within its stated scope; passing tests does not create a new grant.

Current source version: **1.7.1**.

当前版本：`1.7.1`

This is the source version. Source delivery and local installation do not imply a GitHub tag,
release or downloadable asset.

## Why it exists

Ordinary `git commit && git push` does not check whether the staged files match the reviewed scope,
whether hooks changed that content, or whether earlier unpublished commits are safe to publish.
Git Finalizer turns these into explicit gates with precise blockers, so both a human in a
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


Exact content review is optional: `--fixture-exceptions /absolute/review.json` is accepted by normal
publication and configured-upstream `--resume-publish`, as well as existing branch/history modes.
Local `verify-only` and `commit-only` also accept exact `credential-assignment-v1` reviews for
single-level `backend/tests/test_*.py` files; they read the existing local `origin` push URL only to
bind repository identity and perform no remote operation. These local reviews require unchanged
raw/clean-filter blob identity and, for an already-staged change, the same index blob. They cannot
review `CHANGELOG.md`, other test paths, known tokens, private keys or SSH keys.
The original signature matchers remain in force. Every review binds the repository root commit, push URL hash,
exact path, raw blob OID, SHA-256 and detector; other detectors still apply. Normal publication requires
HEAD to match upstream and checks the same raw blob again after staging. Test reviews are limited to
`tests/` and the backend assignment fixtures described above. The sole documentation exception is `CHANGELOG.md` with `private-key-header-v1`: only an
already-published, inline-backtick header example may be preserved, and its following historical text
must remain byte-for-byte unchanged. New headers, closing markers and other document paths are refused.

The exact public Controller retirement capability description is classified from its matching JSON
schema, independent of filename or whitespace. Only its unique fixed description can be masked;
other raw and decoded assignments remain checked. Decoded known-token, private-key and SSH-key
signatures are checked separately from credential-assignment reviews in both worktree and blob
scans. Fully escaped sensitive keys remain checked, and classifier failure refuses the candidate.

Python prose reviews are limited to already-published literal refusal-reason pairs in module-level
`*_REASONS` dictionaries. The full raw key/value pair must be preserved, with only whitespace and a
colon between the literals; other assignments and all other detectors remain mandatory.
Local verify-only and commit-only modes do not accept these Python prose reviews.

## Requirements

- GNU Bash, Git and GNU command-line utilities (`realpath`, `stat`, `grep`, `sed`). Verified baseline:
  **Ubuntu 24.04 LTS** with its system Git. Plan-bound retirement also requires a Controller that
  advertises the public verification and execution protocols. Integration publication requires
  `integration_publication_execution_version` 1 and the public locked stdio session.
- Python **3.11 or newer**, with the standard library only. The entrypoint resolves `python3` from
  `PATH` once and uses that interpreter for every companion and JSON summary. Verified baseline:
  **Python 3.12**; no Debian-specific interpreter path is required.
- Run as an ordinary user. Git Finalizer has no privilege escalation and no daemon, database or
  plugin framework.
- Nothing else. No remote hosting service, no Worktree Controller, no Snapshot Runner, no Context
  Loader, no installed Skill and no agent runtime is required for the core lifecycle. See
  [Standalone contract](#standalone-contract).

## Installation

There is no package index. Releases are published as GitHub Release assets:
each one carries `git-finalizer-<version>.tar.gz` plus a `SHA256SUMS.txt` that names it, from
<https://github.com/xuanheng-tech/git-finalizer/releases>. GitHub carries every release from `v1.5.0`
onward; earlier versions were published on the governed delivery remote described in
[docs/release-governance.md](docs/release-governance.md), so they are not listed here. Fetch both,
verify the artifact, then install it. Select an actual published release from that page and set
`GF_RELEASE_VERSION` to its version number without the `v` prefix; it can differ from the source
version above.

```bash
GF_RELEASE_VERSION='<published-version>'
curl -fsSLO "https://github.com/xuanheng-tech/git-finalizer/releases/download/v${GF_RELEASE_VERSION}/SHA256SUMS.txt"
curl -fsSLO "https://github.com/xuanheng-tech/git-finalizer/releases/download/v${GF_RELEASE_VERSION}/git-finalizer-${GF_RELEASE_VERSION}.tar.gz"
sha256sum --check SHA256SUMS.txt
```

From the verified tarball (files `git-finalize*` must stay in one directory, because the entrypoint
locates its companions relative to itself):

```bash
tar -xzf "git-finalizer-${GF_RELEASE_VERSION}.tar.gz"
install -d -m 0755 "$HOME/.local/bin"
cp "git-finalizer-${GF_RELEASE_VERSION}/git-finalize" \
    "git-finalizer-${GF_RELEASE_VERSION}"/git-finalize-*.py "$HOME/.local/bin/"
chmod 0755 "$HOME/.local/bin/git-finalize"
export PATH="$HOME/.local/bin:$PATH"
git-finalize --version
```

The bundle ships the CLI, its companions and the shared Skill. The `docs/…` links elsewhere in this
README resolve in the source repository; they are not inside the unpacked bundle.

From a checkout:

```bash
./git-finalize --version
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
- Which operation class needs which approval, and who asks the human:
  [docs/authorization-boundary.md](docs/authorization-boundary.md)
- Work deferred on purpose, each entry with the condition that unblocks it:
  [docs/maintenance-backlog.md](docs/maintenance-backlog.md)

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

Each integration's knowledge of another project's layout lives only in the files that carry its
`# GF-INTEGRATION-ADAPTER` marker — one marker per adapter file, and retirement uses two because both
the CLI and the authorization/completion companion speak to that integration. That confinement is machine-checked by
`tests/test_integration_boundaries.sh`.

## Coding agents

The CLI uses the same arguments and JSON results for every caller; it does not inspect a model
provider, an agent's private session state or its approval settings. A shell-capable agent can use
the standalone lifecycle without the optional four-tool workflow.

For Skills, [Codex discovers `.agents/skills`](https://developers.openai.com/codex/skills), while
[Claude Code discovers `.claude/skills`](https://code.claude.com/docs/en/skills). The shared personal
payload can be linked into each client's discovery directory; keep one source and preserve existing
links. Project instructions remain in `AGENTS.md`. This repository supplies a `CLAUDE.md`
import (`@AGENTS.md`) for older Claude clients; the [Claude memory documentation](https://code.claude.com/docs/en/memory)
describes both the import and newer native discovery. Other clients can read the same instructions
explicitly according to their own discovery rules.

`tool-skill-sync doctor` observes instruction fingerprints and pending changes. It supports
`CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `--codex-home` and `--claude-home`; `--agents-root` controls the
shared payload without moving either client's configuration. A passing check cannot prove that an
already-running agent reread its context.

## Security model

- Branch retirement refuses a protected branch name or the remote's live default branch, and
  publishing a *first* branch refuses protected names; updating the upstream of the branch you
  already stand on is deliberately allowed, because that is the ordinary delivery path. Two
  interfaces — `--publish-existing-history` and its resume form — may target a protected branch,
  since the branch already exists there.
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
- Pending merge, cherry-pick, revert, rebase, sequencer or bisect state blocks commit lifecycle
  operations. Hooks remain enabled; a created commit must match the checked index tree and parent
  before publication. Hook failures preserve any commit already created and report its OID.
- Publication checks earlier local commits that are absent from the target remote, as well as the
  newly staged files. Content signatures are heuristic checks, including quoted JSON assignments;
  they are not a guarantee that every secret format can be detected.

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
Different gates own different parts of that list: `scripts/release.py preflight <tag>` binds the
version literals, the contract digest and the Skill-payload pins, while `just check` binds the README
declarations and the presence of the `CHANGELOG.md` section, which the release job then extracts as
the release notes. [docs/release-governance.md](docs/release-governance.md) is the authoritative
list. `CHANGELOG.md` is the authoritative record and the source of release notes. **Merging to the
default branch is not a release**: a release happens only when a separately created annotated tag is
pushed, which triggers the release workflow. That workflow re-verifies the version batch, builds a
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
