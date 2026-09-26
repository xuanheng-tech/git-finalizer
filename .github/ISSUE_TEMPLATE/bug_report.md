---
name: Bug report
about: A command behaved differently from the documented contract
labels: bug
---

## What happened

<!-- Expected versus actual, in a few sentences. -->

## Environment

- `git-finalize --version`:
- OS and Git version:
- Command line (redact repository identifiers you do not want public):

## Machine-readable result

Paste the `--summary` JSON for the failing run. It is the fastest way to diagnose, because
`status`, `final_phase`, `next_action` and `reason` are the tool's own account of what it refused and
why. Check that it contains no token, private hostname or path you do not want published.

```json

```

## Reproduction

<!-- A scratch repository under /tmp plus the exact commands, if you can. -->

## What the documentation says

<!-- Link the section of README.md, docs/agent-contract.md or docs/integration-boundaries.md that the
behaviour appears to contradict. If the documentation is simply unclear, say so and file it as
documentation instead. -->
