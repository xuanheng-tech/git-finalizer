#!/usr/bin/python3
"""Create and safely clean registered task directories under /tmp."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any


TMP_ROOT = Path("/tmp")
STATE_ROOT = Path.home() / ".local/state/tool-temp-dir"
RECORD_VERSION = 2
TOOL_VERSION = "1.0.2"
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
NAME_RE = re.compile(
    r"^tool-task-(?P<prefix>[A-Za-z0-9][A-Za-z0-9_-]{0,63})-"
    r"(?P<random>[0-9a-f]{16})$"
)
MAX_RECORD_BYTES = 16_384
FIXTURE_PREFIX_TOKENS = frozenset(
    {"fixture", "profile", "profiling", "pytest", "test", "tests"}
)


class TempDirError(Exception):
    """A safety invariant rejected the requested operation."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class CleanupValidation:
    path: Path
    record_path: Path
    prefix: str
    uid: int
    device: int
    inode: int
    entries: tuple["CleanupEntry", ...]


@dataclass(frozen=True)
class CleanupEntry:
    relative_path: str
    kind: str
    mode: int
    uid: int
    device: int
    inode: int
    size: int


def _private_mode(path: Path, expected_mode: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise TempDirError("registry_missing", f"登记路径不存在：{path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise TempDirError("registry_unsafe", f"登记路径不是普通目录：{path}")
    if metadata.st_uid != os.getuid():
        raise TempDirError("registry_unsafe", f"登记路径不属于当前 UID：{path}")
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise TempDirError(
            "registry_unsafe",
            f"登记路径权限必须为 {expected_mode:04o}：{path}",
        )
    return metadata


def _registry_dir(*, create: bool) -> Path:
    records = STATE_ROOT / "records"
    if create:
        old_umask = os.umask(0o077)
        try:
            STATE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
            records.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise TempDirError("registry_unavailable", f"无法创建登记目录：{exc}") from exc
        finally:
            os.umask(old_umask)
    _private_mode(STATE_ROOT, 0o700)
    _private_mode(records, 0o700)
    return records


def _record_path(path: Path, *, create_registry: bool = False) -> Path:
    try:
        records = _registry_dir(create=create_registry)
    except TempDirError as exc:
        if not create_registry and exc.code == "registry_missing":
            raise TempDirError(
                "not_registered", "目标不存在于本工具的创建登记中"
            ) from exc
        raise
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()
    return records / f"{digest}.json"


def _write_record(record_path: Path, record: dict[str, Any]) -> None:
    payload = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    old_umask = os.umask(0o077)
    try:
        descriptor = os.open(record_path, flags, 0o600)
    except OSError as exc:
        raise TempDirError("registration_failed", f"无法创建登记记录：{exc}") from exc
    finally:
        os.umask(old_umask)

    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise TempDirError("registration_failed", "登记记录权限不是 0600")
    finally:
        os.close(descriptor)


def _read_record(record_path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(record_path, flags)
    except FileNotFoundError as exc:
        raise TempDirError("not_registered", "目标不存在于本工具的创建登记中") from exc
    except OSError as exc:
        raise TempDirError("registry_unsafe", f"无法安全打开登记记录：{exc}") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise TempDirError("registry_unsafe", "登记记录不是普通文件")
        if metadata.st_uid != os.getuid():
            raise TempDirError("registry_unsafe", "登记记录不属于当前 UID")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise TempDirError("registry_unsafe", "登记记录权限必须为 0600")
        if metadata.st_size > MAX_RECORD_BYTES:
            raise TempDirError("registry_corrupt", "登记记录超过大小限制")
        chunks: list[bytes] = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)

    raw = b"".join(chunks)
    if len(raw) > MAX_RECORD_BYTES:
        raise TempDirError("registry_corrupt", "登记记录超过大小限制")
    try:
        record = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TempDirError("registry_corrupt", "登记记录不是有效 JSON") from exc
    if not isinstance(record, dict):
        raise TempDirError("registry_corrupt", "登记记录必须是 JSON 对象")
    return record


def _validate_prefix(task_prefix: str) -> None:
    if not task_prefix:
        raise TempDirError("invalid_prefix", "task-prefix 不得为空")
    if PREFIX_RE.fullmatch(task_prefix) is None:
        raise TempDirError(
            "invalid_prefix",
            "task-prefix 仅允许 1–64 个 ASCII 字母、数字、下划线或连字符，且首字符必须为字母或数字",
        )


def create_directory(task_prefix: str) -> dict[str, Any]:
    _validate_prefix(task_prefix)
    records = _registry_dir(create=True)
    old_umask = os.umask(0o077)
    try:
        for _attempt in range(128):
            path = TMP_ROOT / f"tool-task-{task_prefix}-{secrets.token_hex(8)}"
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                continue
            break
        else:
            raise TempDirError("create_failed", "无法生成未占用的随机目录名")
    except OSError as exc:
        raise TempDirError("create_failed", f"无法在 /tmp 创建目录：{exc}") from exc
    finally:
        os.umask(old_umask)

    metadata = path.lstat()
    record_path = records / f"{hashlib.sha256(os.fsencode(path)).hexdigest()}.json"
    try:
        if path.parent != TMP_ROOT or NAME_RE.fullmatch(path.name) is None:
            raise TempDirError("create_failed", "创建结果不符合固定 /tmp 命名合同")
        if path.resolve(strict=True) != path:
            raise TempDirError("create_failed", "创建结果无法规范化为原始路径")
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise TempDirError("create_failed", "创建结果不是普通目录")
        if metadata.st_uid != os.getuid():
            raise TempDirError("create_failed", "创建目录不属于当前 UID")
        os.chmod(path, 0o700)
        metadata = path.lstat()
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise TempDirError("create_failed", "创建目录权限不是 0700")
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record = {
            "version": RECORD_VERSION,
            "path": str(path),
            "basename": path.name,
            "uid": metadata.st_uid,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "created_at": created_at,
            "prefix": task_prefix,
        }
        _write_record(record_path, record)
    except Exception:
        try:
            current = path.lstat()
            if (
                stat.S_ISDIR(current.st_mode)
                and not stat.S_ISLNK(current.st_mode)
                and current.st_dev == metadata.st_dev
                and current.st_ino == metadata.st_ino
                and not any(path.iterdir())
            ):
                path.rmdir()
        except OSError:
            pass
        raise

    return {
        "ok": True,
        "command": "create",
        "path": str(path),
        "record": str(record_path),
        "created_at": created_at,
    }


def _normalize_cleanup_path(raw_path: str) -> Path:
    if not raw_path or not os.path.isabs(raw_path):
        raise TempDirError("invalid_path", "cleanup 目标必须是绝对路径")
    if any(character in raw_path for character in ("\x00", "\n", "\r")):
        raise TempDirError("invalid_path", "cleanup 目标包含非法控制字符")
    normalized = os.path.normpath(raw_path)
    if normalized != raw_path:
        raise TempDirError("path_not_canonical", "cleanup 目标必须已规范化，不接受路径穿越或多余分隔符")
    path = Path(normalized)
    if path.parent != TMP_ROOT:
        raise TempDirError("outside_tmp", "cleanup 目标必须直接位于 /tmp 下")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,160}", path.name) is None:
        raise TempDirError("name_mismatch", "cleanup 目标 basename 不符合登记名称合同")
    return path


def _decode_mount_path(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _read_mount_points() -> set[str]:
    mount_points: set[str] = set()
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            for line in stream:
                fields = line.split()
                if len(fields) < 5:
                    raise TempDirError("mountinfo_invalid", "mountinfo 行格式异常")
                mount_points.add(os.path.normpath(_decode_mount_path(fields[4])))
    except OSError as exc:
        raise TempDirError("mountinfo_unavailable", f"无法读取 mountinfo：{exc}") from exc
    return mount_points


def _reject_mounts(path: Path, mount_points: set[str]) -> None:
    target = str(path)
    if target in mount_points:
        raise TempDirError("target_is_mount", "cleanup 目标本身是 mount point")
    prefix = target + os.sep
    nested = sorted(point for point in mount_points if point.startswith(prefix))
    if nested:
        raise TempDirError("nested_mount", f"目录树包含挂载点：{nested[0]}")


def _entry_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISCHR(mode):
        return "character device"
    if stat.S_ISBLK(mode):
        return "block device"
    return "unsupported file type"


def _prefix_allows_fixture_fifo(prefix: str) -> bool:
    tokens = frozenset(part for part in re.split(r"[-_]", prefix) if part)
    return bool(tokens & FIXTURE_PREFIX_TOKENS)


def _allowed_entry_kind(
    mode: int,
    relative_path: str,
    *,
    allow_owned_fixture_fifo: bool,
    fifo_requires_pytest_subtree: bool = False,
) -> str:
    kind = _entry_kind(mode)
    if kind == "FIFO" and allow_owned_fixture_fifo and fifo_requires_pytest_subtree:
        first, separator, _rest = relative_path.partition("/")
        if not separator or not first.startswith("pytest-") or first == "pytest-":
            raise TempDirError(
                "fifo_scope_rejected",
                f"FIFO 不在 pytest 临时子目录中：{relative_path}",
            )
    allowed = {"regular file", "directory", "symlink"}
    if allow_owned_fixture_fifo:
        allowed.add("FIFO")
    if kind not in allowed:
        raise TempDirError(
            "unsafe_entry",
            f"目录树包含不允许的 {kind}：{relative_path}",
        )
    return kind


def _open_directory(path: str | bytes, *, dir_fd: int | None = None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        return os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise TempDirError("directory_changed", f"无法无跟随打开目录：{exc}") from exc


def _scan_tree(
    descriptor: int,
    *,
    expected_uid: int,
    expected_device: int,
    allow_owned_fixture_fifo: bool,
    fifo_requires_pytest_subtree: bool = False,
    relative_root: str = "",
    entries: list[CleanupEntry] | None = None,
) -> list[CleanupEntry]:
    if entries is None:
        entries = []
    try:
        with os.scandir(descriptor) as iterator:
            names = sorted(entry.name for entry in iterator)
    except OSError as exc:
        raise TempDirError("scan_failed", f"无法遍历目录树：{exc}") from exc

    for name in names:
        relative = f"{relative_root}/{name}" if relative_root else name
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise TempDirError("directory_changed", f"遍历期间条目发生变化：{relative}: {exc}") from exc
        if metadata.st_uid != expected_uid:
            raise TempDirError("owner_mismatch", f"目录树条目不属于登记 UID：{relative}")
        if metadata.st_dev != expected_device:
            raise TempDirError("cross_device_entry", f"目录树条目跨越登记 filesystem：{relative}")
        kind = _allowed_entry_kind(
            metadata.st_mode,
            relative,
            allow_owned_fixture_fifo=allow_owned_fixture_fifo,
            fifo_requires_pytest_subtree=fifo_requires_pytest_subtree,
        )
        entries.append(
            CleanupEntry(
                relative_path=relative,
                kind=kind,
                mode=stat.S_IMODE(metadata.st_mode),
                uid=metadata.st_uid,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                size=metadata.st_size,
            )
        )
        if kind != "directory":
            continue
        child = _open_directory(name, dir_fd=descriptor)
        try:
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise TempDirError("directory_changed", f"遍历期间目录 inode 发生变化：{relative}")
            _scan_tree(
                child,
                expected_uid=expected_uid,
                expected_device=expected_device,
                allow_owned_fixture_fifo=allow_owned_fixture_fifo,
                fifo_requires_pytest_subtree=fifo_requires_pytest_subtree,
                relative_root=relative,
                entries=entries,
            )
        finally:
            os.close(child)
    return entries


def _validate_record(
    record: dict[str, Any],
    *,
    path: Path,
    metadata: os.stat_result,
) -> None:
    expected_keys = {
        "version",
        "path",
        "basename",
        "uid",
        "device",
        "inode",
        "created_at",
        "prefix",
    }
    if set(record) != expected_keys:
        raise TempDirError("registry_corrupt", "登记记录字段集合不符合合同")
    if record.get("version") != RECORD_VERSION or record.get("path") != str(path):
        raise TempDirError("registry_corrupt", "登记记录版本或路径不匹配")
    prefix = record.get("prefix")
    # The exact registered basename survives a namespace migration. Cleanup never
    # guesses another tool's directory format or accepts an unregistered path.
    if (
        record.get("basename") != path.name
        or not isinstance(prefix, str)
        or PREFIX_RE.fullmatch(prefix) is None
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}-" + re.escape(prefix)
                        + r"-[0-9a-f]{16}", path.name) is None
    ):
        raise TempDirError("registry_corrupt", "登记前缀或精确名称与目标不匹配")
    created_at = record.get("created_at")
    if not isinstance(created_at, str):
        raise TempDirError("registry_corrupt", "登记创建时间格式异常")
    try:
        datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise TempDirError("registry_corrupt", "登记创建时间不是 ISO-8601") from exc
    expected_identity = (
        record.get("uid"),
        record.get("device"),
        record.get("inode"),
    )
    current_identity = (metadata.st_uid, metadata.st_dev, metadata.st_ino)
    if expected_identity != current_identity:
        raise TempDirError("identity_mismatch", "当前目录 UID、device 或 inode 与创建登记不一致")
    if metadata.st_uid != os.getuid():
        raise TempDirError("owner_mismatch", "cleanup 目标不属于当前 UID")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise TempDirError("mode_mismatch", "cleanup 目标目录权限必须保持为 0700")


def validate_cleanup(
    raw_path: str,
    *,
    allow_owned_fixture_fifo: bool = False,
) -> CleanupValidation:
    path = _normalize_cleanup_path(raw_path)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise TempDirError("target_missing", "cleanup 目标不存在") from exc
    except OSError as exc:
        raise TempDirError("target_unreadable", f"无法读取 cleanup 目标：{exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise TempDirError("target_symlink", "cleanup 目标本身不得是 symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise TempDirError("target_not_directory", "cleanup 目标不是目录")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise TempDirError("path_unresolvable", f"无法规范化 cleanup 目标：{exc}") from exc
    if resolved != path or resolved.parent != TMP_ROOT:
        raise TempDirError("path_not_canonical", "规范化后目标不再直接位于 /tmp")

    record_path = _record_path(path)
    record = _read_record(record_path)
    _validate_record(record, path=path, metadata=metadata)
    prefix = record["prefix"]
    fifo_requires_pytest_subtree = (
        allow_owned_fixture_fifo and not _prefix_allows_fixture_fifo(prefix)
    )
    mount_points = _read_mount_points()
    _reject_mounts(path, mount_points)

    descriptor = _open_directory(str(path))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_uid, opened.st_dev, opened.st_ino) != (
            metadata.st_uid,
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise TempDirError("identity_mismatch", "打开后的目标 identity 与登记不一致")
        entries = _scan_tree(
            descriptor,
            expected_uid=metadata.st_uid,
            expected_device=metadata.st_dev,
            allow_owned_fixture_fifo=allow_owned_fixture_fifo,
            fifo_requires_pytest_subtree=fifo_requires_pytest_subtree,
        )
        if fifo_requires_pytest_subtree and not any(
            entry.kind == "FIFO" for entry in entries
        ):
            raise TempDirError(
                "fifo_scope_rejected",
                "普通临时根仅可显式清理 pytest 子目录中的 FIFO",
            )
    finally:
        os.close(descriptor)

    return CleanupValidation(
        path=path,
        record_path=record_path,
        prefix=prefix,
        uid=metadata.st_uid,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        entries=tuple(entries),
    )


def _delete_contents(
    descriptor: int,
    *,
    expected_uid: int,
    expected_device: int,
    allow_owned_fixture_fifo: bool,
    fifo_requires_pytest_subtree: bool = False,
    relative_root: str = "",
) -> None:
    directory = os.fstat(descriptor)
    if not stat.S_ISDIR(directory.st_mode):
        raise TempDirError("directory_changed", f"待删除条目不再是目录：{relative_root}")
    if directory.st_uid != expected_uid:
        raise TempDirError("owner_mismatch", f"目录树条目不属于登记 UID：{relative_root}")
    if directory.st_dev != expected_device:
        raise TempDirError("cross_device_entry", f"目录树条目跨越登记 filesystem：{relative_root}")
    if not directory.st_mode & stat.S_IWUSR:
        try:
            os.fchmod(descriptor, stat.S_IMODE(directory.st_mode) | stat.S_IWUSR)
        except OSError as exc:
            raise TempDirError(
                "cleanup_failed", f"无法写入待删除目录：{relative_root}: {exc}"
            ) from exc
    try:
        with os.scandir(descriptor) as iterator:
            names = sorted(entry.name for entry in iterator)
    except OSError as exc:
        raise TempDirError("cleanup_failed", f"无法读取待删除目录：{exc}") from exc

    for name in names:
        relative = f"{relative_root}/{name}" if relative_root else name
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise TempDirError("directory_changed", f"清理期间条目发生变化：{relative}: {exc}") from exc
        if metadata.st_uid != expected_uid:
            raise TempDirError("owner_mismatch", f"目录树条目不属于登记 UID：{relative}")
        if metadata.st_dev != expected_device:
            raise TempDirError("cross_device_entry", f"目录树条目跨越登记 filesystem：{relative}")
        kind = _allowed_entry_kind(
            metadata.st_mode,
            relative,
            allow_owned_fixture_fifo=allow_owned_fixture_fifo,
            fifo_requires_pytest_subtree=fifo_requires_pytest_subtree,
        )
        try:
            if kind == "directory":
                child = _open_directory(name, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise TempDirError(
                            "directory_changed",
                            f"清理期间目录 inode 发生变化：{relative}",
                        )
                    _delete_contents(
                        child,
                        expected_uid=expected_uid,
                        expected_device=expected_device,
                        allow_owned_fixture_fifo=allow_owned_fixture_fifo,
                        fifo_requires_pytest_subtree=fifo_requires_pytest_subtree,
                        relative_root=relative,
                    )
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=descriptor)
            else:
                os.unlink(name, dir_fd=descriptor)
        except TempDirError:
            raise
        except OSError as exc:
            raise TempDirError("cleanup_failed", f"删除失败：{relative}: {exc}") from exc


def cleanup_directory(
    raw_path: str,
    *,
    allow_owned_fixture_fifo: bool = False,
) -> dict[str, Any]:
    validation = validate_cleanup(
        raw_path,
        allow_owned_fixture_fifo=allow_owned_fixture_fifo,
    )
    fifo_requires_pytest_subtree = (
        allow_owned_fixture_fifo
        and not _prefix_allows_fixture_fifo(validation.prefix)
    )
    _reject_mounts(validation.path, _read_mount_points())
    descriptor = _open_directory(str(validation.path))
    try:
        metadata = os.fstat(descriptor)
        if (metadata.st_uid, metadata.st_dev, metadata.st_ino) != (
            validation.uid,
            validation.device,
            validation.inode,
        ):
            raise TempDirError("identity_mismatch", "删除前目标 identity 与登记不一致")
        _scan_tree(
            descriptor,
            expected_uid=validation.uid,
            expected_device=validation.device,
            allow_owned_fixture_fifo=allow_owned_fixture_fifo,
            fifo_requires_pytest_subtree=fifo_requires_pytest_subtree,
        )
        _delete_contents(
            descriptor,
            expected_uid=validation.uid,
            expected_device=validation.device,
            allow_owned_fixture_fifo=allow_owned_fixture_fifo,
            fifo_requires_pytest_subtree=fifo_requires_pytest_subtree,
        )
    finally:
        os.close(descriptor)

    try:
        final_metadata = validation.path.lstat()
        if (
            final_metadata.st_uid,
            final_metadata.st_dev,
            final_metadata.st_ino,
        ) != (validation.uid, validation.device, validation.inode):
            raise TempDirError("identity_mismatch", "删除根目录前目标 identity 发生变化")
        validation.path.rmdir()
    except TempDirError:
        raise
    except OSError as exc:
        raise TempDirError("cleanup_failed", f"无法删除根目录：{exc}") from exc

    try:
        validation.record_path.unlink()
    except OSError as exc:
        raise TempDirError(
            "record_cleanup_failed",
            f"目录已删除，但登记记录保留且无法移除：{exc}",
        ) from exc
    return {
        "ok": True,
        "command": "cleanup",
        "path": str(validation.path),
        "record_removed": True,
    }


def dry_run_cleanup(
    raw_path: str,
    *,
    allow_owned_fixture_fifo: bool = False,
) -> dict[str, Any]:
    validation = validate_cleanup(
        raw_path,
        allow_owned_fixture_fifo=allow_owned_fixture_fifo,
    )
    entries = [
        {
            "device": entry.device,
            "inode": entry.inode,
            "kind": entry.kind,
            "mode": f"{entry.mode:04o}",
            "path": entry.relative_path,
            "size": entry.size,
            "uid": entry.uid,
        }
        for entry in validation.entries
    ]
    return {
        "ok": True,
        "command": "dry-run-cleanup",
        "path": str(validation.path),
        "record": str(validation.record_path),
        "prefix": validation.prefix,
        "entry_count": len(entries),
        "regular_file_bytes": sum(
            entry.size for entry in validation.entries if entry.kind == "regular file"
        ),
        "entries": entries,
        "delete_root": True,
        "delete_record": True,
    }


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tool-temp-dir")
    parser.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("task_prefix")
    fifo_help = "allow owned FIFOs in fixture task roots or pytest-* subtrees"
    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("absolute_path")
    cleanup_parser.add_argument(
        "--allow-owned-fixture-fifo", action="store_true", help=fifo_help
    )
    dry_run_parser = subparsers.add_parser("dry-run-cleanup")
    dry_run_parser.add_argument("absolute_path")
    dry_run_parser.add_argument(
        "--allow-owned-fixture-fifo", action="store_true", help=fifo_help
    )
    validate_parser = subparsers.add_parser("validate-cleanup")
    validate_parser.add_argument("absolute_path")
    validate_parser.add_argument(
        "--allow-owned-fixture-fifo", action="store_true", help=fifo_help
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "create":
            result = create_directory(arguments.task_prefix)
        elif arguments.command == "cleanup":
            result = cleanup_directory(
                arguments.absolute_path,
                allow_owned_fixture_fifo=arguments.allow_owned_fixture_fifo,
            )
        elif arguments.command == "dry-run-cleanup":
            result = dry_run_cleanup(
                arguments.absolute_path,
                allow_owned_fixture_fifo=arguments.allow_owned_fixture_fifo,
            )
        else:
            validation = validate_cleanup(
                arguments.absolute_path,
                allow_owned_fixture_fifo=arguments.allow_owned_fixture_fifo,
            )
            result = {
                "ok": True,
                "command": "validate-cleanup",
                "path": str(validation.path),
                "record": str(validation.record_path),
            }
    except TempDirError as exc:
        _emit(
            {
                "ok": False,
                "command": arguments.command,
                "code": exc.code,
                "reason": exc.reason,
            }
        )
        return 2
    except OSError as exc:
        _emit(
            {
                "ok": False,
                "command": arguments.command,
                "code": "os_error",
                "reason": str(exc),
            }
        )
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
