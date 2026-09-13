from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import re
import tempfile
import unittest

from scripts import changelog


ROOT = Path(__file__).resolve().parents[1]
VALID_CHANGELOG = """# Changelog

## Unreleased

## 1.2.3 - 2026-08-01

- Changed: target notes

## 1.2.2

- Fixed: adjacent notes
"""


class ChangelogTests(unittest.TestCase):
    def test_current_source_version_has_one_nonempty_changelog_section(self) -> None:
        script_match = re.search(
            r'^readonly VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$',
            (ROOT / "git-finalize").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        readme_match = re.search(
            r"^当前版本：`([0-9]+\.[0-9]+\.[0-9]+)`$",
            (ROOT / "README.md").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        self.assertIsNotNone(script_match)
        self.assertIsNotNone(readme_match)
        assert script_match is not None
        assert readme_match is not None
        self.assertEqual(script_match.group(1), readme_match.group(1))

        body = changelog.validate_version(
            (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), script_match.group(1)
        )
        self.assertTrue(body)

    def test_invalid_or_duplicate_headings_are_rejected(self) -> None:
        cases = (
            (
                VALID_CHANGELOG + "\n## 1.2.3\n\n- Fixed: duplicate\n",
                "duplicate version section",
            ),
            (VALID_CHANGELOG + "\n## Unreleased\n", "exactly one Unreleased section"),
            (
                VALID_CHANGELOG.replace("## 1.2.3 - 2026-08-01", "## 01.2.3"),
                "invalid level-two heading",
            ),
        )
        for text, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                changelog.ChangelogError, message
            ):
                changelog.parse_changelog(text)

    def test_missing_empty_and_mismatched_sections_are_rejected(self) -> None:
        with self.assertRaisesRegex(changelog.ChangelogError, "missing version section: 1.2.4"):
            changelog.validate_version(VALID_CHANGELOG, "1.2.4")

        empty = VALID_CHANGELOG.replace("- Changed: target notes", "")
        with self.assertRaisesRegex(changelog.ChangelogError, "version section is empty: 1.2.3"):
            changelog.validate_version(empty, "1.2.3")

        with self.assertRaisesRegex(changelog.ChangelogError, "missing version section: 1.2.4"):
            changelog.extract_tag(VALID_CHANGELOG, "v1.2.4")

    def test_release_tag_extraction_does_not_include_adjacent_versions(self) -> None:
        self.assertEqual(
            changelog.extract_tag(VALID_CHANGELOG, "v1.2.3"), "- Changed: target notes"
        )

    def test_release_workflow_cli_extracts_notes_and_fails_on_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHANGELOG.md"
            path.write_text(VALID_CHANGELOG, encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = changelog.main(
                    ["extract", "v1.2.3", "--changelog", str(path)]
                )
            self.assertEqual(result, 0)
            self.assertEqual(stdout.getvalue(), "- Changed: target notes\n")
            self.assertEqual(stderr.getvalue(), "")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = changelog.main(
                    ["extract", "v1.2.4", "--changelog", str(path)]
                )
            self.assertEqual(result, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("missing version section: 1.2.4", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
