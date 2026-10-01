# Agent notes

`scripts/release.py preflight <tag>` binds the literals it can read: the entry-script `VERSION` and
the three companion `VERSION` literals, the README `当前版本` declaration, `tool_version` in
`tool_cli_contract.json` and `tool_skill_manifest.json`, `contract_version` against
`toolchain_compatibility.json`, the manifest's `public_cli_contract_sha256` binding, all three
Skill-payload pins (root manifest plus both mirrors), and `LICENSE` presence. It does **not** read
`CHANGELOG.md`.

The rest of the version batch is bound by other gates: the English current-release declaration and
the matching `CHANGELOG.md` section by `just check` (`tests/run.sh`, `tests/test_changelog.py`), and
the release notes themselves by `scripts/changelog.py extract <tag>` in the release workflow, which
fails before publishing if the section is missing. Every gate reads the working tree, so they must be
run in a clean checkout of the commit being tagged. See README "Development and releases" and
`docs/release-governance.md` for the authoritative list.
Before creating a release tag, run `just release-check` from that clean candidate: lint and the
full check runner must pass with the real Controller suite executed. Hosted `just check` retains
its optional Controller SKIP behavior and does not replace this maintainer gate.

Tool/Skill changes also require the source-only manifest check. Deployment closes only after the
installed `tool-skill-sync doctor --repo <checkout> --summary` passes the relevant components.
A clean source-only gate does not prove installed versions or instruction integration. Preserve
parallel tool candidates; use an explicit published Python `--source-ref` instead of touching a dirty checkout.
