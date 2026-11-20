"""Exercise NUL transport and producer failure cleanly in pure Python."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from git_finalizer.git_io import parse_nul_records


class NulRecordsTests(unittest.TestCase):
    def test_literal_records_include_empty_tabs_and_newlines(self) -> None:
        raw = b"tab\tname\x00line\nname\x00\x00"
        self.assertEqual(parse_nul_records(raw), ["tab\tname", "line\nname", ""])

    def test_empty_buffer_returns_empty_list(self) -> None:
        self.assertEqual(parse_nul_records(b""), [])

    def test_unterminated_trailing_record_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_nul_records(b"partial\x00unterminated")

    def test_fast_repeated_parsing_remains_exact(self) -> None:
        payload = b"refs/heads/main\x00"
        for _ in range(2048):
            self.assertEqual(parse_nul_records(payload), ["refs/heads/main"])
