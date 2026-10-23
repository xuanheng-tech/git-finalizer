"""Exercise NUL transport and producer failure without repository or caller state."""

from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class NulRecordsTests(unittest.TestCase):
    def run_shell(self, body: str) -> subprocess.CompletedProcess[str]:
        source = (ROOT / "git-finalize").read_text(encoding="utf-8")
        match = re.search(r"(?ms)^read_nul_records\(\) \{\n.*?^\}\n", source)
        self.assertIsNotNone(match)
        return subprocess.run(
            ["/bin/bash", "--noprofile", "--norc"],
            input="set -Eeuo pipefail\n" + match[0] + body,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "BASH_ENV": "/dev/null"},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_literal_records_include_empty_tabs_and_newlines(self):
        result = self.run_shell(r'''
values=(stale)
read_nul_records values printf '%s\0' $'tab\tname' $'line\nname' ''
printf '%s\0' "${values[@]}"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "tab\tname\0line\nname\0\0")

    def test_failed_producer_discards_partial_records(self):
        result = self.run_shell(r'''
producer() { printf 'partial\0'; return 7; }
values=(stale)
if read_nul_records values producer; then exit 90; fi
printf '%s\n' "${#values[@]}"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0\n")

    def test_fast_producers_do_not_spuriously_fail(self):
        result = self.run_shell(r'''
values=()
for ((i=0; i<2048; i++)); do
    read_nul_records values printf 'refs/heads/main\0'
    [[ ${#values[@]} == 1 && ${values[0]} == refs/heads/main ]]
done
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lastpipe_and_monitor_options_are_restored(self):
        result = self.run_shell(r'''
values=()
for lastpipe in on off; do
    if [[ $lastpipe == on ]]; then shopt -s lastpipe; else shopt -u lastpipe; fi
    for monitor in on off; do
        if [[ $monitor == on ]]; then set -m; else set +m; fi
        before_lastpipe=$(shopt -p lastpipe || :)
        before_flags=$-
        read_nul_records values printf 'one\0'
        [[ $(shopt -p lastpipe || :) == "$before_lastpipe" && $- == "$before_flags" ]]
        [[ ${#values[@]} == 1 && ${values[0]} == one ]]
    done
done
''')
        self.assertEqual(result.returncode, 0, result.stderr)
