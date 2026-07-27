from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import stat
import tempfile
import unittest


DEPLOY_PATH = Path(__file__).with_name("deploy.py")
SPEC = importlib.util.spec_from_file_location("git_finalizer_hook_deploy", DEPLOY_PATH)
assert SPEC is not None and SPEC.loader is not None
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class HookDeploymentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.live = self.root / ".codex" / "hooks"
        self.source.mkdir()
        self.live.mkdir(parents=True)
        self.payloads = {
            "codex_git_finalize_bridge.py": b"versioned bridge\n",
            "test_codex_git_finalize_bridge.py": b"versioned tests\n",
        }
        for name, payload in self.payloads.items():
            (self.source / name).write_bytes(payload)

    def run_main(self, action: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = deploy.main(
                (
                    action,
                    "--source-dir",
                    str(self.source),
                    "--live-dir",
                    str(self.live),
                )
            )
        return status, stdout.getvalue(), stderr.getvalue()

    def populate_live(self) -> None:
        for name, payload in self.payloads.items():
            (self.live / name).write_bytes(payload)

    def test_check_reports_consistent_fixture(self) -> None:
        self.populate_live()

        status, stdout, stderr = self.run_main("check")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count("ok "), 2)
        self.assertNotIn("drift ", stdout)

    def test_check_detects_fixture_drift(self) -> None:
        self.populate_live()
        (self.live / "codex_git_finalize_bridge.py").write_bytes(b"drifted\n")

        status, stdout, stderr = self.run_main("check")

        self.assertEqual(status, 1)
        self.assertEqual(stderr, "")
        self.assertIn("drift codex_git_finalize_bridge.py", stdout)
        self.assertIn("ok test_codex_git_finalize_bridge.py", stdout)

    def test_install_is_atomic_preserves_modes_and_does_not_touch_hooks_json(self) -> None:
        bridge = self.live / "codex_git_finalize_bridge.py"
        tests = self.live / "test_codex_git_finalize_bridge.py"
        bridge.write_bytes(b"old bridge\n")
        tests.write_bytes(b"old tests\n")
        bridge.chmod(0o600)
        tests.chmod(0o664)
        hooks_json = self.live.parent / "hooks.json"
        hooks_json.write_bytes(b'{"unrelated":"preserve exactly"}\n')
        hooks_json_before = (
            hashlib.sha256(hooks_json.read_bytes()).hexdigest(),
            hooks_json.stat().st_mtime_ns,
        )

        status, stdout, stderr = self.run_main("install")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("installed codex_git_finalize_bridge.py", stdout)
        self.assertIn("installed test_codex_git_finalize_bridge.py", stdout)
        for name, payload in self.payloads.items():
            self.assertEqual((self.live / name).read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(bridge.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(tests.stat().st_mode), 0o664)
        self.assertEqual(
            (
                hashlib.sha256(hooks_json.read_bytes()).hexdigest(),
                hooks_json.stat().st_mtime_ns,
            ),
            hooks_json_before,
        )
        self.assertEqual(
            [path for path in self.live.iterdir() if path.name.startswith(".")],
            [],
        )

    def test_install_recovers_missing_files_with_safe_default_modes(self) -> None:
        status, stdout, stderr = self.run_main("install")

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count("installed "), 2)
        for name, payload in self.payloads.items():
            target = self.live / name
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(deploy.sha256(target), deploy.sha256(self.source / name))
        self.assertEqual(
            stat.S_IMODE((self.live / "codex_git_finalize_bridge.py").stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(
                (self.live / "test_codex_git_finalize_bridge.py").stat().st_mode
            ),
            0o644,
        )


if __name__ == "__main__":
    unittest.main()
