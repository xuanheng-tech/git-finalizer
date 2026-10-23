# Content reviews and scanner classifications

Content signatures, sensitive assignments and oversized binaries block by default. The
exceptions below are exact, reviewed and narrow; nothing is allowed implicitly. See also the
[agent contract](agent-contract.md) for how refusals are reported.

Exact content review is optional: `--fixture-exceptions /absolute/review.json` is accepted by normal
publication and configured-upstream `--resume-publish`, as well as existing branch/history modes.
When an initial publication retained its root commit after a failed push, an explicitly reviewed
`tests/` fixture may be passed again with `--resume-initial-publish <exact-root-oid>
--allow-test-fixture tests/<exact-path>`. Each canonical path must name a regular blob in that exact
root; the summary records its path and blob identity. No fixture is allowed implicitly. Other
files, scanner failures, clean-state and remote-identity guards remain checked, and other resume
modes still refuse this legacy flag. Recovery reuses the same root without creating a commit.

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
Duplicate keys, non-finite constants and malformed JSON containers do not disable scanning of
escaped string literals. Worktree, index and history checks use the same scanner.

Static Python credential directory references have one narrow non-secret classification.
Dictionary items require a directly quoted ASCII key that is either an uppercase namespace ending
in `_CREDENTIALS` or exactly `container_credentials`; a module-level plain assignment may instead
have the single name target `CREDENTIALS`. The directly quoted value must be exactly
`/run/credentials/<unit>.service`. The unit is a single ASCII
component starting with an alphanumeric character, followed by alphanumerics, `_`, `.`, `@` or `-`,
with a total unit-name length of at most 255 characters. This describes a systemd credential
directory reference, not its contents; the scanner never reads that directory. It requires parsed
Python containing a declaration, requires distinct static string dictionary keys, and accepts no string prefixes,
escapes, concatenation, interpolation, traversal or child-file paths. JSON and prose, other assignments
(including nested, chained and annotated `CREDENTIALS` declarations), and other sensitive keys retain
their existing checks. These excluded forms receive no directory
classification; this does not add Python runtime evaluation or a new dynamic-expression detector.
All other assignments and raw/decoded known
token, private-key and SSH-key signatures remain mandatory. No fixture exception or new flag is needed.

The secret-key prefix must start outside an ASCII word: the suffix of a task or risk identifier
does not count as a key prefix. Standalone keys and keys after punctuation or whitespace remain
checked in raw content, decoded JSON strings and Git clean-filter output.

Python prose reviews are limited to already-published literal refusal-reason pairs in module-level
`*_REASONS` dictionaries. The full raw key/value pair must be preserved, with only whitespace and a
colon between the literals; other assignments and all other detectors remain mandatory.
Local verify-only and commit-only modes do not accept these Python prose reviews.

## Resuming with reviewed sources and large binaries

`--resume-publish` accepts the same exact `--reviewed-sensitive-source` review and Controller
linkage used for a retained commit, with its reviewed scope after `--`. The review is revalidated
against HEAD and every unpublished revision must contain that same reviewed blob. Existing
branch/history publication and resume modes also accept explicit `--allow-large-binary <path>`:
only the exact regular HEAD blob above 5 MiB and at most 25 MiB is approved. Other historical
blobs, unused exceptions and content signatures still block. These interfaces reuse commits.
