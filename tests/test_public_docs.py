from __future__ import annotations

import re
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]

# Every markdown file this repository publishes. The set is derived rather than curated: an authored
# list silently stops covering new documents, which is how docs/process/ and the shipped Skill
# references ended up outside the gate.
DOCUMENTED_SUBTREES = (
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "AGENTS.md",
    "docs/",
    ".github/",
    "skills/",
)

# Released CHANGELOG sections quote the private paths they documented as removed, and a published
# section is immutable. Only the Unreleased heading of these files is scannable.
HISTORY_DOCUMENTS = frozenset({ROOT / "CHANGELOG.md"})

PRIVATE_MARKERS = (
    "127.0.0.1:3000",
    "localhost:3000",
    "/home/hsd",
    "refs/pull/",
    "claude.ai",
)


def link_targets(text: str) -> list[str]:
    return [
        target
        for target in re.findall(r"\]\(([^)\s]+)\)", text)
        if not target.startswith(("http://", "https://", "mailto:", "#"))
    ]


def git_run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=check,
    )


def git_output(*arguments: str) -> str:
    return git_run(*arguments).stdout.strip()


def public_markdown() -> list[Path]:
    """Every tracked markdown file, i.e. everything a release publishes as readable text."""
    listed = git_run("ls-files", "-z", "--", "*.md").stdout
    return sorted(ROOT / entry for entry in listed.split("\0") if entry)


def marker_scannable_text(document: Path) -> str:
    text = document.read_text(encoding="utf-8")
    if document not in HISTORY_DOCUMENTS:
        return text
    pending = re.search(r"^## Unreleased\b.*?(?=^## )", text, re.MULTILINE | re.DOTALL)
    return pending.group(0) if pending else ""


def marker_scannable_text(document: Path) -> str:
    if document not in HISTORY_DOCUMENTS:
        return document.read_text(encoding="utf-8")
    text = document.read_text(encoding="utf-8")
    pending = re.search(r"^## Unreleased\b.*?(?=^## )", text, re.M | re.S)
    return pending.group(0) if pending else ""


class PublicDocumentationTests(unittest.TestCase):
    def test_every_published_markdown_has_a_documented_home(self) -> None:
        documents = public_markdown()
        self.assertGreaterEqual(len(documents), 15, "the markdown inventory shrank unexpectedly")
        for document in documents:
            relative = str(document.relative_to(ROOT))
            with self.subTest(document=relative):
                self.assertTrue(
                    relative.startswith(DOCUMENTED_SUBTREES),
                    f"{relative} is published but belongs to no documented subtree: decide where it "
                    "belongs and list that subtree here",
                )

    def test_relative_links_resolve(self) -> None:
        for document in public_markdown():
            with self.subTest(document=str(document.relative_to(ROOT))):
                self.assertTrue(document.is_file(), f"missing public document: {document}")
                for target in link_targets(document.read_text(encoding="utf-8")):
                    path = target.split("#", 1)[0]
                    if not path:
                        continue
                    resolved = (document.parent / path).resolve()
                    self.assertTrue(
                        resolved.exists(),
                        f"{document.name} links to a missing file: {target}",
                    )

    def test_public_documents_hold_no_private_infrastructure(self) -> None:
        for document in public_markdown():
            with self.subTest(document=str(document.relative_to(ROOT))):
                text = marker_scannable_text(document)
                for marker in PRIVATE_MARKERS:
                    self.assertNotIn(
                        marker, text, f"{document.name} exposes {marker!r}"
                    )

    def test_release_governance_is_the_single_process_authority(self) -> None:
        governance = (ROOT / "docs" / "release-governance.md").read_text(encoding="utf-8")
        for required in (
            "exactly one host",
            "preflight",
            "Cross-host parity",
            "permissions: contents: write",
            "Runbook: first public GitHub release",
            "Never",
        ):
            self.assertIn(required, governance, f"governance lost: {required}")
        for source in (
            "README.md",
            "CONTRIBUTING.md",
            ".gitea/workflows/release.yml",
            ".github/workflows/release.yml",
        ):
            self.assertIn(
                "release-governance.md",
                (ROOT / source).read_text(encoding="utf-8"),
                f"{source} no longer points at release governance",
            )

    def test_release_record_matches_the_repository_refs(self) -> None:
        governance = (ROOT / "docs" / "release-governance.md").read_text(encoding="utf-8")
        self.assertIn("## Release record", governance, "release record section vanished")
        section = governance.split("## Release record", 1)[1].split("\n## ", 1)[0]
        rows = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in section.splitlines()
            if line.startswith("| `v")
        ]
        self.assertGreaterEqual(len(rows), 2, "release record lost its rows")
        tags = set(git_output("tag", "--list").split())
        origin = git_run("remote", "get-url", "origin", check=False).stdout.strip()
        from_github = "github.com" in origin
        if not tags:
            self.fail(
                "this checkout carries no tags, so the record cannot be checked: clone without "
                "--depth, or run `git fetch --unshallow --tags`"
            )
        seen: set[str] = set()
        for version, host, commit, tag_object, digest, size in rows:
            tag = version.strip("`")
            with self.subTest(version=tag):
                self.assertNotIn(tag, seen, f"{tag} is recorded twice")
                seen.add(tag)
                if tag not in tags:
                    # The one-host rule means a published tag can live on exactly one host, so a
                    # checkout of the other host cannot see it; a missing tag on its own host does
                    # not.
                    self.assertNotEqual(
                        host == "GitHub",
                        from_github,
                        f"{tag} is recorded for {host}, which is the host this checkout came from, "
                        "yet the tag is absent",
                    )
                    continue
                self.assertIn(host, ("GitHub", "Gitea"), "release record host column drifted")
                self.assertEqual(
                    git_output("rev-parse", tag),
                    tag_object.strip("`"),
                    "recorded tag object does not match the repository ref",
                )
                self.assertEqual(
                    git_output("rev-parse", f"{tag}^{{commit}}"),
                    commit.strip("`"),
                    "recorded candidate commit does not match the peeled tag",
                )
                self.assertRegex(digest.strip("`"), r"^[0-9a-f]{64}$")
                self.assertRegex(size, r"^[0-9]+$")

    def test_support_boundary_is_stated(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Support", readme)
        section = readme.split("## Support", 1)[1].split("\n## ", 1)[0]
        for required in ("Supported:", "Not supported:", "latest stable release"):
            self.assertIn(required, section, f"support boundary lost {required!r}")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("Only the latest stable release is supported", security)

    def test_templates_exist_and_carry_the_review_gate(self) -> None:
        pull_request = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(
            encoding="utf-8"
        )
        for required in ("just check", "just lint", "negative", "Hash-bound", "CHANGELOG"):
            self.assertIn(required, pull_request, f"PR template lost {required!r}")
        bug_report = (
            ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.md"
        ).read_text(encoding="utf-8")
        self.assertIn("--summary", bug_report)
        self.assertIn("--version", bug_report)
        config = (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("blank_issues_enabled", config)
        self.assertIn("SECURITY.md", config)

    def test_release_procedure_is_documented_for_contributors(self) -> None:
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        for required in (
            "just check",
            "just lint",
            "GF-INTEGRATION-ADAPTER",
            "is **not** a release",
            "never edited or",
        ):
            self.assertIn(required, contributing, f"CONTRIBUTING lost {required!r}")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Current stable release", readme)
        self.assertIn("当前版本", readme)


if __name__ == "__main__":
    unittest.main()
