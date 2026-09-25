# Security policy

## Reporting a vulnerability

Please report security issues through a **private security advisory** on the repository's
Security tab rather than a public issue. Include the Git Finalizer version (`git-finalize
--version`), the exact command line with any repository-specific identifiers removed, and the
`--summary` JSON output if you used it.

Only the latest stable release is supported. Fixes are shipped in a new release; released tags
and published artifacts are never rewritten.

## What the tool is designed to refuse

Git Finalizer is an authorization-boundary tool. These behaviours are deliberate, tested, and
treated as security properties rather than bugs:

- **No inferred authorization.** Completing an implementation, passing tests, or a previous
  one-time authorization never authorizes a commit or push. Each Git write needs explicit
  authorization for that action, repository and scope.
- **No destructive history operations.** Force push, amend, rebase, history rewrite, automatic
  conflict resolution, tag creation/publication and duplicate commit creation are refused.
  Pushes are non-force and tag-following is disabled.
- **Protected branches and remote state.** Publishing to a protected branch is refused before
  any state preflight; a diverged remote blocks publication instead of reconciling it.
- **Compare-and-set only.** Branch retirement and lease-bound integration publication require an
  expected OID and re-check authoritative state under a lock; a mismatch aborts instead of
  writing.
- **Fail closed on unknown state.** Missing authority, unreadable capability, drifted plan,
  unsafe lock file, incomplete installation or unknown content drift produce a precise blocker
  and no mutation. There is no implicit downgrade path and no fallback to non-authoritative
  state.
- **Credential handling.** The tool never accepts, prints or stores tokens. Repository bootstrap
  resolves credentials through the configured Git credential helper and only sends them to the
  explicitly provided HTTP(S) endpoint; the endpoint has no built-in default.
- **Result reporting.** Success claims require verified evidence. `remote_verified` is reported
  only after live remote OID comparison; a push that was not verified stays `remote_pushed`.

## Trust boundaries when you run it

- The tool executes `git` as your own user with your own permissions. It does not sandbox
  itself, and it is not a defence against a malicious repository that exploits Git itself:
  review repository contents before running any Git command against them.
- Diff content, commit messages found in history, and Snapshot Runner artifacts are treated as
  data. If you feed `--summary` output or evidence to an AI agent, that material is untrusted
  input, not instructions.
- The optional `git-change-delivery` Skill is workflow documentation for agents. It is not an
  authorization source and grants nothing the CLI would otherwise refuse.

## Reporting an unsafe assumption

Findings such as "the tool trusts a value it should re-check", "a blocker can be bypassed", or
"an error path mutates state" are security-relevant even when they look like ordinary bugs.
Prefer a private advisory for those.
