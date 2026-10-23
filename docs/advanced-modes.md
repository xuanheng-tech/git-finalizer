# Advanced modes

These modes and classifiers extend the ordinary lifecycle in the [README](../README.md).
Each keeps the explicit-scope, content, history and remote checks.

## Preserving unified-diff artifacts

A complete `.patch` artifact may contain a line consisting of the single space required for
an empty context line. The whitespace check recognizes only that exact marker inside a counted
unified-diff hunk, after Git parses the whole artifact with `apply --numstat` without applying it.
That same required marker may end the patch; its accompanying Git EOF diagnostic is checked
against the exact final context line. Extra blank lines outside a hunk still fail.
The same classifier checks working candidates, staged blobs and unpublished commit ranges.
It preserves the artifact bytes and adds no bypass flag or repository whitespace configuration.

Ordinary trailing whitespace, added patch lines with trailing spaces, out-of-hunk spaces,
malformed/oversized patches and ambiguous diagnostics still fail. Clean-filter output is checked
again from the staged blob. Scope, secret-content, history and remote checks remain mandatory.

## Completing an already reviewed merge locally

After the caller resolves conflicts, `--merge-parent <full-oid>` lets `--mode verify-only`
or `--mode commit-only` inspect one non-empty, resolved two-parent merge. It requires one regular
`MERGE_HEAD` matching the exact parent, no unmerged index entries or other pending Git operations,
and the usual explicit file scope and content checks. Verification preserves Git state; commit
checks the original HEAD, both parents and reviewed index tree. It never publishes or resolves
conflicts. Missing/multiple/drifted parents, unknown staged files and hook drift still fail closed.
Both SHA-1 and SHA-256 repositories require their own complete parent OID.

## Synchronizing two existing remotes

`--sync-published-branch <full-published-oid>` copies one already-published branch by a normal,
non-force push. Supply `--source-remote`, `--remote`, the same `--remote-branch` on both existing
remotes, and `--expected-target-oid`. Both remotes must have one identical fetch/push endpoint;
the live source must equal the exact OID and the target must be its ancestor. The existing commit
range safety scan still runs. `--dry-run` validates the complete transaction without pushing.

The checkout can stay on another clean attached branch: HEAD, index, local branches, tags and
upstream stay unchanged. No branch is created, merged, deleted or force-pushed. A target already
at the source OID is verified without another push, including recovery after an uncertain outcome.
Source/target drift blocks the action. The pre-push expected-target check is not a server-side CAS;
the server's ordinary non-force check and independent post-verification remain mandatory. A
concurrent change requires fresh observation, never an automatic merge or history rewrite.
This interface grants no authorization and does not replace Controller integration of new code.
