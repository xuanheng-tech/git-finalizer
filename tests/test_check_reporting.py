"""Keep skipped execution and failed groups visible at the existing check entry point."""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

CHECK = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/check.py"))


class CheckReportingTests(unittest.TestCase):
    def test_entry_preserves_repository_imports_in_an_isolated_python_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gf-check-entry-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            tests = root / "tests"
            skill = root / "skills/git-change-delivery"
            for directory in (scripts, tests, skill):
                directory.mkdir(parents=True)
            shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/check.py", scripts / "check.py")
            (root / "required_module.py").write_text("value = 1\n")
            (tests / "test_import.py").write_text(
                "import required_module, unittest\n"
                "class ImportTest(unittest.TestCase):\n"
                " def test_import(self): self.assertEqual(required_module.value, 1)\n")
            (skill / "test_skill.py").write_text(
                "import unittest\nclass SkillTest(unittest.TestCase):\n"
                " def test_fixture(self): self.assertTrue(True)\n")
            (skill / "quick_validate.py").write_text("print('fixture validation')\n")
            (tests / "run.sh").write_text(
                "#!/bin/sh\nprintf '%s\\n' "
                "'{\"status\":\"PASS\",\"group\":\"shell fixture\",\"reason\":\"\"}' "
                ">\"$GF_TEST_GROUP_RESULTS\"\n")
            (root / "tool-skill-sync").write_text("#!/bin/sh\nexit 0\n")
            (root / "tool-skill-sync").chmod(0o755)
            result = subprocess.run([sys.executable, "-I", "-B", str(scripts / "check.py")],
                                    cwd=root, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Summary: 5 PASS, 0 SKIP, 0 FAIL", result.stdout)

    def run_units(self, cases: list[unittest.TestCase]) -> tuple[bool, list[dict[str, str]], str]:
        results: list[dict[str, str]] = []
        output = io.StringIO()
        with patch.object(unittest.TestLoader, "discover", return_value=unittest.TestSuite(cases)):
            with redirect_stdout(output), redirect_stderr(output):
                passed = CHECK["unit_group"](results, "fixture", Path("unused"))
                CHECK["summary"](results)
        return passed, results, output.getvalue()

    def test_skipped_tests_keep_their_reason_and_are_not_counted_as_execution(self) -> None:
        class Cases(unittest.TestCase):
            def test_pass(self) -> None:
                pass

            @unittest.skip("optional dependency unavailable")
            def test_skip(self) -> None:
                self.fail("must not execute")

        passed, results, output = self.run_units([Cases("test_pass"), Cases("test_skip")])
        self.assertTrue(passed)
        self.assertEqual([result["status"] for result in results], ["SKIP", "PASS"])
        self.assertIn("optional dependency unavailable", output)
        self.assertIn("1 executed, 1 skipped", output)

    def test_empty_discovery_fails_and_all_skipped_is_explicit(self) -> None:
        passed, results, _ = self.run_units([])
        self.assertFalse(passed)
        self.assertEqual(results[-1]["status"], "FAIL")
        case = unittest.FunctionTestCase(lambda: None)
        case.setUp = lambda: case.skipTest("fixture unavailable")
        passed, results, output = self.run_units([case])
        self.assertTrue(passed)
        self.assertEqual(results[-1]["status"], "SKIP")
        self.assertIn("0 executed, 1 skipped", output)

    def test_failed_unit_group_never_reports_pass(self) -> None:
        case = unittest.FunctionTestCase(lambda: self.fail("intentional fixture failure"))
        passed, results, output = self.run_units([case])
        self.assertFalse(passed)
        self.assertEqual(results[-1]["status"], "FAIL")
        self.assertIn("0 PASS", output)

    def test_failed_external_group_keeps_nonzero_status(self) -> None:
        results: list[dict[str, str]] = []
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 7)):
            with redirect_stdout(io.StringIO()):
                self.assertFalse(CHECK["command_group"](results, "fixture", ["unused"]))
        self.assertEqual(results, [{"status": "FAIL", "group": "fixture", "reason": "exit 7"}])

    def test_shell_skip_is_retained_and_nonzero_exit_cannot_become_pass(self) -> None:
        def run(command, **kwargs):
            record = Path(kwargs["env"]["GF_TEST_GROUP_RESULTS"])
            record.write_text(json.dumps({"status": "SKIP", "group": "real Controller",
                                          "reason": "worktree-controller unavailable on PATH"}) + "\n")
            return subprocess.CompletedProcess(command, exit_code)

        for exit_code in (0, 4):
            with self.subTest(exit_code=exit_code), patch.object(subprocess, "run", side_effect=run):
                results: list[dict[str, str]] = []
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(CHECK["shell_groups"](results), exit_code == 0)
                    CHECK["summary"](results)
                self.assertEqual(results[0]["status"], "SKIP")
                self.assertIn("worktree-controller unavailable on PATH", output.getvalue())
                self.assertNotIn("PASS |", output.getvalue())
                if exit_code:
                    self.assertEqual(results[-1]["status"], "FAIL")
