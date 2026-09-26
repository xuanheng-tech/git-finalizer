## What changes

<!-- One paragraph: the behaviour, not the file list. -->

## Affected surface

- [ ] CLI flags, modes or defaults
- [ ] `--summary` output, `status` / `final_phase` / `next_action` / `push.result` vocabulary
- [ ] Exit codes or exec-forwarded companion receipts
- [ ] Integration adapter (Worktree Controller, Snapshot Runner, Gitea bootstrap)
- [ ] Skill payload (`skills/git-change-delivery/**`, which is hash-bound)
- [ ] Release procedure, CI or governance documents
- [ ] Nothing user-visible (internal/test only)

## Checklist

- [ ] `just check` passes on a clean checkout, and `just lint` passes
- [ ] A **negative** test exists for every new gate: it refuses, it refuses before writing, and the
      blocker text is specific
- [ ] The standalone contract still holds (no sibling, controller, Skill or private host needed)
- [ ] Any new external-layout knowledge sits in the marked `GF-INTEGRATION-ADAPTER` site **and** in
      `docs/integration-boundaries.md`
- [ ] Hash-bound files updated together: `tool_cli_contract.json` ↔ manifest pin, Skill payload ↔
      root manifest + both mirrors, version literals ↔ README declarations ↔ `CHANGELOG.md`
- [ ] Released `CHANGELOG.md` sections and pushed tags were not edited
- [ ] No secrets, personal paths or private hosting endpoints in the diff
- [ ] If this is version-relevant, the batch declares the new version and its CHANGELOG section

## Notes for the reviewer

<!-- Verification actually performed, and anything intentionally left as a recorded blocker. -->
