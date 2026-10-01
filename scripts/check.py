#!/usr/bin/env python3
"""Run the existing check groups and distinguish execution, skips and failures."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def report(results: list[dict[str, str]], status: str, group: str, reason: str = "") -> None:
    results.append({"status": status, "group": group, "reason": reason})
    print(f"{status} | {group}" + (f" | {reason}" if reason else ""), flush=True)


def command_group(results: list[dict[str, str]], name: str, command: list[str]) -> bool:
    code = subprocess.run(command, cwd=ROOT, check=False).returncode
    report(results, "PASS" if code == 0 else "FAIL", name, f"exit {code}" if code else "")
    return code == 0


def unit_group(results: list[dict[str, str]], name: str, directory: Path) -> bool:
    suite = unittest.TestLoader().discover(str(directory), pattern="test_*.py")
    outcome = unittest.TextTestRunner().run(suite)
    for test, reason in outcome.skipped:
        report(results, "SKIP", f"{name}: {test.id()}", reason)
    executed = outcome.testsRun - len(outcome.skipped)
    status = "PASS" if outcome.wasSuccessful() else "FAIL"
    if outcome.testsRun == 0:
        status = "FAIL"
    elif executed == 0 and status == "PASS":
        status = "SKIP"
    report(results, status, name, f"{executed} executed, {len(outcome.skipped)} skipped")
    return status != "FAIL"


def shell_groups(results: list[dict[str, str]], *, require_controller: bool = False) -> bool:
    with tempfile.TemporaryDirectory(prefix="git-finalizer-check-") as temporary:
        record = Path(temporary) / "shell-groups.jsonl"
        environment = dict(os.environ, GF_TEST_GROUP_RESULTS=str(record))
        code = subprocess.run(["bash", "tests/run.sh"], cwd=ROOT, env=environment,
                              check=False).returncode
        groups = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()] \
            if record.exists() else []
        results.extend(groups)
        if require_controller:
            controller = [group for group in groups
                          if group["group"] == "test_retire_branch_real_controller"]
            if len(controller) != 1 or controller[0]["status"] != "PASS":
                reason = ("expected exactly one executed PASS result" if len(controller) != 1
                          else f"{controller[0]['status']}: {controller[0]['reason']}")
                report(results, "FAIL", "required real-Controller integration", reason)
                return False
        if code != 0 or not groups:
            if not any(group["status"] == "FAIL" for group in groups):
                report(results, "FAIL", "shell integrations", f"exit {code}; incomplete results")
            return False
        return not any(group["status"] == "FAIL" for group in groups)


def summary(results: list[dict[str, str]]) -> None:
    print("\nCheck results (groups and individually skipped unit tests):", flush=True)
    for result in results:
        print(f"{result['status']} | {result['group']}"
              + (f" | {result['reason']}" if result["reason"] else ""), flush=True)
    counts = {status: sum(result["status"] == status for result in results)
              for status in ("PASS", "SKIP", "FAIL")}
    print("Summary: " + ", ".join(f"{count} {status}" for status, count in counts.items()),
          flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-controller", action="store_true",
                        help="require real Controller retirement tests to execute and pass")
    arguments = parser.parse_args(argv)
    results: list[dict[str, str]] = []
    python = sys.executable
    groups = (
        lambda: shell_groups(results, require_controller=arguments.require_controller),
        lambda: unit_group(results, "Python tests", ROOT / "tests"),
        lambda: command_group(results, "Skill validation", [python, "-B",
                              "skills/git-change-delivery/quick_validate.py", "skills/git-change-delivery"]),
        lambda: unit_group(results, "Skill tests", ROOT / "skills/git-change-delivery"),
        lambda: command_group(results, "source-only manifest", [str(ROOT / "tool-skill-sync"),
                              "--source-repo", f"git-finalizer={ROOT}", "check", "--source-only",
                              "git-finalizer"]),
    )
    try:
        for group in groups:
            if not group():
                return 1
        return 0
    except (OSError, ValueError, ImportError) as error:
        report(results, "FAIL", "check runner", str(error))
        return 1
    finally:
        summary(results)


if __name__ == "__main__":
    raise SystemExit(main())
