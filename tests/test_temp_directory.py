from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock


TOOL_PATH = Path(__file__).resolve().parents[1] / "tooling/tool_temp_dir.py"
OWNED_TEST_PREFIX = "tool-task-unittest-"


def load_script(name: str, path: Path) -> Any:
    loader = SourceFileLoader(name, str(path))
    specification = importlib.util.spec_from_loader(name, loader)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


temp_tool = load_script("tool_temp_dir_test_target", TOOL_PATH)


def remove_owned_test_path(path: Path) -> None:
    """Best-effort cleanup restricted to this test suite's explicit /tmp names."""
    if path.parent != Path("/tmp") or not (
        path.name.startswith(OWNED_TEST_PREFIX)
        or path.name.startswith("archive-task-unittest-")
        or path.name.startswith("tool-task-integration-")
        or path.name.startswith("tool-temp-dir-unittest-")
    ):
        raise AssertionError(f"refusing test cleanup outside owned scope: {path}")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        path.unlink()
        return
    with os.scandir(path) as iterator:
        children = [Path(entry.path) for entry in iterator]
    for child in children:
        child_metadata = child.lstat()
        if stat.S_ISDIR(child_metadata.st_mode) and not stat.S_ISLNK(
            child_metadata.st_mode
        ):
            remove_owned_tree_child(child)
        else:
            child.unlink()
    path.rmdir()


def remove_owned_tree_child(path: Path) -> None:
    with os.scandir(path) as iterator:
        children = [Path(entry.path) for entry in iterator]
    for child in children:
        metadata = child.lstat()
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            remove_owned_tree_child(child)
        else:
            child.unlink()
    path.rmdir()


class TempDirToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_state = tempfile.TemporaryDirectory(
            prefix="tool-temp-dir-state-test-",
        )
        self.addCleanup(self.temporary_state.cleanup)
        self.state_root = Path(self.temporary_state.name) / "state"
        self.state_patch = mock.patch.object(temp_tool, "STATE_ROOT", self.state_root)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def create(self, suffix: str) -> tuple[dict[str, Any], Path]:
        result = temp_tool.create_directory(f"unittest-{suffix}")
        path = Path(result["path"])
        self.addCleanup(remove_owned_test_path, path)
        return result, path

    def assert_error(self, code: str, operation: Any, *arguments: Any, **keywords: Any) -> None:
        with self.assertRaises(temp_tool.TempDirError) as caught:
            operation(*arguments, **keywords)
        self.assertEqual(caught.exception.code, code)

    def test_create_validate_cleanup_and_record_lifecycle(self) -> None:
        result, path = self.create("normal")
        record = Path(result["record"])
        records_dir = self.state_root / "records"

        self.assertEqual(path.parent, Path("/tmp"))
        self.assertRegex(path.name, temp_tool.NAME_RE)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.state_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(records_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)

        stored = json.loads(record.read_text(encoding="utf-8"))
        metadata = path.stat()
        self.assertEqual(stored["path"], str(path))
        self.assertEqual(stored["uid"], os.getuid())
        self.assertEqual(stored["device"], metadata.st_dev)
        self.assertEqual(stored["inode"], metadata.st_ino)
        self.assertEqual(stored["prefix"], "unittest-normal")
        self.assertIsInstance(stored["created_at"], str)

        child = path / "child"
        child.mkdir()
        (child / "ordinary.txt").write_text("ordinary", encoding="utf-8")
        (path / "root.txt").write_text("root", encoding="utf-8")
        (child / "external-link").symlink_to("/etc")

        record_before = record.read_bytes()
        validation = temp_tool.validate_cleanup(str(path))
        self.assertEqual(validation.path, path)
        self.assertEqual(record.read_bytes(), record_before)
        self.assertTrue((child / "ordinary.txt").exists())
        self.assertTrue((child / "external-link").is_symlink())

        cleaned = temp_tool.cleanup_directory(str(path))
        self.assertTrue(cleaned["record_removed"])
        self.assertFalse(path.exists())
        self.assertFalse(record.exists())
        self.assertTrue(Path("/etc").is_dir())

    def test_invalid_prefixes_are_rejected_without_creation(self) -> None:
        invalid = (
            "",
            "bad/name",
            "bad\\name",
            "../bad",
            "bad space",
            "-bad",
            "bad.$",
            "a" * 65,
        )
        for prefix in invalid:
            with self.subTest(prefix=prefix):
                self.assert_error(
                    "invalid_prefix", temp_tool.create_directory, prefix
                )

    def test_non_tmp_unregistered_and_name_mismatch_are_rejected(self) -> None:
        self.assert_error("outside_tmp", temp_tool.validate_cleanup, str(Path.home()))

        unregistered = Path(
            f"/tmp/{OWNED_TEST_PREFIX}unregistered-{secrets.token_hex(8)}"
        )
        unregistered.mkdir(mode=0o700)
        self.addCleanup(remove_owned_test_path, unregistered)
        self.assert_error(
            "not_registered", temp_tool.validate_cleanup, str(unregistered)
        )
        self.assertTrue(unregistered.is_dir())

        mismatch = Path(f"/tmp/tool-temp-dir-unittest-{secrets.token_hex(8)}")
        mismatch.mkdir(mode=0o700)
        self.addCleanup(remove_owned_test_path, mismatch)
        self.assert_error("not_registered", temp_tool.validate_cleanup, str(mismatch))
        self.assertTrue(mismatch.is_dir())

    def test_path_traversal_is_rejected_and_target_remains(self) -> None:
        _result, path = self.create("traversal")
        traversal = f"/tmp/../tmp/{path.name}"

        self.assert_error(
            "path_not_canonical", temp_tool.validate_cleanup, traversal
        )
        self.assertTrue(path.is_dir())
        temp_tool.cleanup_directory(str(path))

    def test_inode_replacement_is_rejected_and_record_is_preserved(self) -> None:
        result, path = self.create("inode")
        record = Path(result["record"])
        original = Path(
            f"/tmp/{OWNED_TEST_PREFIX}inode-original-{secrets.token_hex(8)}"
        )
        path.rename(original)
        self.addCleanup(remove_owned_test_path, original)
        path.mkdir(mode=0o700)

        self.assert_error("identity_mismatch", temp_tool.validate_cleanup, str(path))
        self.assertTrue(path.is_dir())
        self.assertTrue(record.is_file())
        path.rmdir()
        original.rmdir()

    def test_symlink_target_is_rejected_without_touching_referent(self) -> None:
        result, path = self.create("target-symlink")
        record = Path(result["record"])
        referent = Path(
            f"/tmp/{OWNED_TEST_PREFIX}symlink-referent-{secrets.token_hex(8)}"
        )
        referent.mkdir(mode=0o700)
        self.addCleanup(remove_owned_test_path, referent)
        marker = referent / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        path.rmdir()
        path.symlink_to(referent, target_is_directory=True)

        self.assert_error("target_symlink", temp_tool.validate_cleanup, str(path))
        self.assertTrue(path.is_symlink())
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertTrue(record.is_file())
        path.unlink()

    def test_target_and_nested_mount_points_are_rejected(self) -> None:
        _result, path = self.create("mount")
        keep = path / "keep.txt"
        keep.write_text("keep", encoding="utf-8")
        with mock.patch.object(
            temp_tool, "_read_mount_points", return_value={str(path)}
        ):
            self.assert_error(
                "target_is_mount", temp_tool.validate_cleanup, str(path)
            )
        self.assertTrue(keep.is_file())

        child = path / "nested"
        child.mkdir()
        with mock.patch.object(
            temp_tool, "_read_mount_points", return_value={str(child)}
        ):
            self.assert_error("nested_mount", temp_tool.validate_cleanup, str(path))
        self.assertTrue(keep.is_file())
        self.assertTrue(child.is_dir())
        temp_tool.cleanup_directory(str(path))

    def test_socket_and_fifo_reject_before_any_content_is_deleted(self) -> None:
        _result, path = self.create("specials")
        keep = path / "keep.txt"
        keep.write_text("keep", encoding="utf-8")
        fifo = path / "pipe"
        os.mkfifo(fifo, 0o600)
        socket_path = path / "socket"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(socket_path))

        self.assert_error("unsafe_entry", temp_tool.validate_cleanup, str(path))
        self.assertEqual(keep.read_text(encoding="utf-8"), "keep")
        self.assertTrue(fifo.exists())
        self.assertTrue(socket_path.exists())

        server.close()
        socket_path.unlink()
        fifo.unlink()
        temp_tool.cleanup_directory(str(path))

    def test_character_and_block_device_modes_are_rejected(self) -> None:
        for mode, label in (
            (stat.S_IFCHR | 0o600, "character device"),
            (stat.S_IFBLK | 0o600, "block device"),
        ):
            with self.subTest(label=label):
                self.assertEqual(temp_tool._entry_kind(mode), label)
                self.assert_error(
                    "unsafe_entry",
                    temp_tool._allowed_entry_kind,
                    mode,
                    "synthetic-device",
                    allow_owned_fixture_fifo=False,
                )

    def test_registered_exact_basename_survives_namespace_migration(self) -> None:
        result, path = self.create("migration")
        old_record = Path(result["record"])
        record = json.loads(old_record.read_text())
        migrated = path.with_name(path.name.replace("tool-task-", "archive-task-", 1))
        path.rename(migrated)
        self.addCleanup(remove_owned_test_path, migrated)
        record.update(path=str(migrated), basename=migrated.name)
        new_record = temp_tool._record_path(migrated)
        temp_tool._write_record(new_record, record)
        old_record.unlink()
        self.assertIsNone(temp_tool.NAME_RE.fullmatch(migrated.name))
        checked = temp_tool.validate_cleanup(str(migrated))
        self.assertEqual(checked.inode, record["inode"])
        temp_tool.cleanup_directory(str(migrated))
        self.assertFalse(migrated.exists())

    def test_mismatched_registered_basename_fails_before_deletion(self) -> None:
        result, path = self.create("basename")
        record_path = Path(result["record"])
        record = json.loads(record_path.read_text())
        record["basename"] = "another-directory"
        record_path.write_text(json.dumps(record))
        (path / "keep").write_text("untouched")
        self.assert_error("registry_corrupt", temp_tool.cleanup_directory, str(path))
        self.assertEqual((path / "keep").read_text(), "untouched")

    def test_fixture_fifo_requires_the_explicit_scoped_option(self) -> None:
        _result, path = self.create("fixture")
        os.mkfifo(path / "fixture-pipe", mode=0o600)
        self.assert_error("unsafe_entry", temp_tool.cleanup_directory, str(path))
        temp_tool.cleanup_directory(str(path), allow_owned_fixture_fifo=True)
        self.assertFalse(path.exists())
        _result, ordinary = self.create("ordinary")
        self.assert_error("fifo_scope_rejected", temp_tool.cleanup_directory, str(ordinary),
                          allow_owned_fixture_fifo=True)

    def test_pytest_subtree_fifo_in_ordinary_root_requires_exact_scope(self) -> None:
        _result, path = self.create("ordinary")
        marker = path / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        pytest_root = path / "pytest-candidate"
        pytest_root.mkdir()
        os.mkfifo(pytest_root / "pipe", mode=0o600)
        outside = path / "ordinary"
        outside.mkdir()
        os.mkfifo(outside / "pipe", mode=0o600)

        self.assert_error("unsafe_entry", temp_tool.cleanup_directory, str(path))
        self.assert_error("fifo_scope_rejected", temp_tool.cleanup_directory, str(path),
                          allow_owned_fixture_fifo=True)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

        (outside / "pipe").unlink()
        preview = temp_tool.dry_run_cleanup(
            str(path), allow_owned_fixture_fifo=True
        )
        self.assertEqual(
            [entry["path"] for entry in preview["entries"] if entry["kind"] == "FIFO"],
            ["pytest-candidate/pipe"],
        )
        temp_tool.cleanup_directory(str(path), allow_owned_fixture_fifo=True)
        self.assertFalse(path.exists())

    def test_cleanup_owned_readonly_subdirectory_with_scoped_fifo(self) -> None:
        _result, path = self.create("readonly")
        pytest_root = path / "pytest-candidate"
        pytest_root.mkdir()
        os.mkfifo(pytest_root / "pipe", mode=0o600)
        readonly = pytest_root / "readonly"
        readonly.mkdir()
        (readonly / "manifest.json").write_text("{}", encoding="utf-8")
        readonly.chmod(0o500)
        self.addCleanup(lambda: readonly.chmod(0o700) if readonly.exists() else None)

        preview = temp_tool.dry_run_cleanup(
            str(path), allow_owned_fixture_fifo=True
        )
        self.assertTrue(preview["ok"])
        temp_tool.cleanup_directory(str(path), allow_owned_fixture_fifo=True)
        self.assertFalse(path.exists())

    def test_owned_fifo_below_custom_pytest_basetemp(self) -> None:
        for basetemp in ("pytest", "p2", "short-test-root"):
            with self.subTest(basetemp=basetemp):
                _result, path = self.create("custom-basetemp")
                fixture = path / basetemp / "test_compute_logs_reject_fifo_0"
                fixture.mkdir(parents=True)
                os.mkfifo(fixture / "fifo.out", mode=0o600)

                self.assert_error("unsafe_entry", temp_tool.cleanup_directory, str(path))
                preview = temp_tool.dry_run_cleanup(
                    str(path), allow_owned_fixture_fifo=True
                )
                self.assertEqual(
                    [entry["path"] for entry in preview["entries"] if entry["kind"] == "FIFO"],
                    [f"{basetemp}/test_compute_logs_reject_fifo_0/fifo.out"],
                )
                temp_tool.cleanup_directory(str(path), allow_owned_fixture_fifo=True)
                self.assertFalse(path.exists())

    def test_custom_basetemp_rejects_fifo_outside_numbered_pytest_node(self) -> None:
        for relative in ("p2/pipe", "p2/test_fixture/pipe", "p2/test_fixture0"):
            with self.subTest(relative=relative):
                _result, path = self.create("custom-basetemp-rejection")
                marker = path / "keep.txt"
                marker.write_text("keep", encoding="utf-8")
                fifo = path / relative
                fifo.parent.mkdir(parents=True, exist_ok=True)
                os.mkfifo(fifo, mode=0o600)

                self.assert_error(
                    "fifo_scope_rejected", temp_tool.cleanup_directory, str(path),
                    allow_owned_fixture_fifo=True,
                )
                self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
                self.assertTrue(fifo.exists())

    def test_owned_fifo_below_nested_pytest_basetemp(self) -> None:
        for relative in (
            "project/pytest/test_absent_completion_preserv0/preservations/tree/pipe",
            "project/pytest-candidate/test_fixture0/pipe",
            "acceptance/project/pytest/test_fixture1/tree/pipe",
        ):
            with self.subTest(relative=relative):
                result, path = self.create("nested-basetemp")
                record = Path(result["record"])
                record_before = record.read_bytes()
                fifo = path / relative
                fifo.parent.mkdir(parents=True)
                os.mkfifo(fifo, mode=0o600)

                self.assert_error("unsafe_entry", temp_tool.cleanup_directory, str(path))
                preview = temp_tool.dry_run_cleanup(
                    str(path), allow_owned_fixture_fifo=True
                )
                self.assertEqual(
                    [entry["path"] for entry in preview["entries"] if entry["kind"] == "FIFO"],
                    [relative],
                )
                self.assertTrue(fifo.exists())
                self.assertEqual(record.read_bytes(), record_before)
                temp_tool.cleanup_directory(str(path), allow_owned_fixture_fifo=True)
                self.assertFalse(path.exists())
                self.assertFalse(record.exists())

    def test_nested_pytest_scope_rejects_unrelated_fifo_before_deletion(self) -> None:
        for relative in (
            "project/pytest/pipe",
            "project/pytest/test_fixture/pipe",
            "project/pytest/test_fixture0",
            "project/pytest-/test_fixture0/pipe",
            "project/not-pytest/test_fixture0/pipe",
            "project/p2/test_fixture0/pipe",
            "project/pytest/test_fixture0-suffix/pipe",
            "unrelated/pipe",
        ):
            with self.subTest(relative=relative):
                result, path = self.create("nested-rejection")
                record = Path(result["record"])
                record_before = record.read_bytes()
                marker = path / "keep.txt"
                marker.write_text("keep", encoding="utf-8")
                accepted = path / "project/pytest/test_accepted1/pipe"
                accepted.parent.mkdir(parents=True)
                os.mkfifo(accepted, mode=0o600)
                rejected = path / relative
                rejected.parent.mkdir(parents=True, exist_ok=True)
                os.mkfifo(rejected, mode=0o600)

                self.assert_error(
                    "fifo_scope_rejected", temp_tool.cleanup_directory, str(path),
                    allow_owned_fixture_fifo=True,
                )
                self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
                self.assertEqual(record.read_bytes(), record_before)
                self.assertTrue(accepted.exists())
                self.assertTrue(rejected.exists())

    def test_nested_pytest_fifo_still_requires_registered_owner(self) -> None:
        _result, path = self.create("nested-owner")
        fifo = path / "project/pytest/test_fixture0/pipe"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(fifo, mode=0o600)
        original_stat = os.stat

        def foreign_pipe_stat(*arguments: Any, **keywords: Any) -> os.stat_result:
            metadata = original_stat(*arguments, **keywords)
            if arguments[0] == "pipe" and keywords.get("dir_fd") is not None:
                fields = list(metadata)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return metadata

        with mock.patch.object(temp_tool.os, "stat", side_effect=foreign_pipe_stat):
            self.assert_error(
                "owner_mismatch", temp_tool.cleanup_directory, str(path),
                allow_owned_fixture_fifo=True,
            )
        self.assertTrue(fifo.exists())
        temp_tool.cleanup_directory(str(path), allow_owned_fixture_fifo=True)

    def test_nested_pytest_cleanup_does_not_follow_symlink(self) -> None:
        _result, path = self.create("nested-symlink")
        fixture = path / "project/pytest/test_fixture0"
        fixture.mkdir(parents=True)
        os.mkfifo(fixture / "pipe", mode=0o600)
        external = Path(self.temporary_state.name) / "external"
        external.mkdir()
        marker = external / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        os.mkfifo(external / "pipe", mode=0o600)
        (fixture / "external").symlink_to(external, target_is_directory=True)

        temp_tool.cleanup_directory(str(path), allow_owned_fixture_fifo=True)
        self.assertFalse(path.exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertTrue((external / "pipe").exists())

    def test_cli_uses_only_neutral_state_in_an_empty_home(self) -> None:
        home = Path(self.temporary_state.name) / "empty-home"
        home.mkdir()
        environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}

        def run(*arguments: str) -> dict[str, Any]:
            result = subprocess.run([sys.executable, str(TOOL_PATH), *arguments],
                                    env=environment, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

        created = run("create", "unittest-isolated")
        path = Path(created["path"])
        self.addCleanup(remove_owned_test_path, path)
        self.assertTrue(run("validate-cleanup", str(path))["ok"])
        self.assertTrue(run("cleanup", str(path))["record_removed"])
        self.assertEqual(list(home.iterdir()), [home / ".local"])
        self.assertFalse(path.exists())
