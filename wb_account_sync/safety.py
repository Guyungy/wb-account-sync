"""Explicit identity, offline guards and durable local metadata."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from contextlib import contextmanager
import uuid


class SafetyError(Exception):
    """An expected refusal; no unsafe fallback is permitted."""


def canonical(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(data):
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def valid_uid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise SafetyError("Account IDs must be complete lowercase UUIDs.") from None
    return value


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise SafetyError("Expected a regular JSON file smaller than 32 MiB.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise SafetyError("Invalid JSON metadata.") from None


def require_plain_path(path):
    path = Path(path).expanduser().absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            # macOS /tmp and /var are system aliases: callers should resolve them first.
            raise SafetyError("Symlink paths are not accepted; use an explicit canonical path.")
    return path


def identity(home):
    home = require_plain_path(home)
    db = home / "workbuddy.db"
    if not home.is_dir() or db.is_symlink() or not db.is_file():
        raise SafetyError("Explicit --home must contain a regular workbuddy.db.")
    stat = db.stat()
    return {"home": str(home.resolve()), "device": stat.st_dev, "inode": stat.st_ino}


def require_no_sidecars(home):
    db = Path(home) / "workbuddy.db"
    for suffix in ("-wal", "-journal"):
        p = Path(str(db) + suffix)
        if p.is_symlink() or (p.exists() and p.stat().st_size):
            raise SafetyError("Active WAL/journal found. Quit the client normally; never delete its sidecars.")


def snapshot_identity(home):
    path = Path(home) / "storage" / "skeleton" / "account-snapshot.json"
    require_plain_path(path)
    data = read_json(path)
    try:
        uid = valid_uid(data["primary"]["uid"])
    except (KeyError, TypeError):
        raise SafetyError("Cannot confirm the current account from account-snapshot.json.") from None
    return uid, file_digest(path)


def require_offline():
    """Fail closed. Never spawn a helper to evade process/sandbox restrictions."""
    if sys.platform != "darwin":
        raise SafetyError("Writes are supported only on macOS; other platforms are read-only.")
    try:
        proc = subprocess.run(["/bin/ps", "-Ao", "pid=,command="], capture_output=True,
                              text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        raise SafetyError("Cannot inspect processes. Use a permitted system terminal after quitting the client.") from None
    if proc.returncode or not proc.stdout.strip():
        raise SafetyError("Process inspection denied; refusing writes.")
    current_pid = os.getpid()
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            raise SafetyError("Unexpected process output; refusing writes.")
        if int(parts[0]) == current_pid:
            continue
        command = parts[1]
        if (re.search(r"/[^/]*WorkBuddy[^/]*\.app/Contents/", command, re.I)
                or re.search(r"wb-account-sync\.py\s+live(?:\s|$)", command)
                or re.search(r"wb-account-sync(?:\.sh)?\s+live(?:\s|$)", command)):
            raise SafetyError("WorkBuddy or the legacy sync daemon is running. Stop it before writes.")


def fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, data, *, exclusive=False):
    """0600 metadata; fsync data and parent. Never overwrite an explicit export."""
    path = Path(path)
    require_plain_path(path)
    payload = (canonical(data) + "\n").encode("utf-8")
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(tmp, path)
            tmp.unlink()
        else:
            if path.is_symlink():
                raise SafetyError("Refusing to replace a symlink.")
            os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        if tmp.exists():
            tmp.unlink()


def state_root(path, home):
    path = require_plain_path(path)
    home = Path(home).resolve()
    if path == home or home in path.parents or path in home.parents:
        raise SafetyError("State directory must be outside the client home, and not its ancestor.")
    marker = path / ".wb-account-sync-state"
    if path.exists():
        if not path.is_dir() or (any(path.iterdir()) and not marker.is_file()):
            raise SafetyError("Use a new dedicated state directory, not an existing personal folder.")
        if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise SafetyError("State directory must belong to you and have mode 0700.")
    else:
        path.mkdir(mode=0o700)  # parent must already exist
        fsync_dir(path.parent)
    expected = {"format": 1, "home": str(home)}
    if marker.exists():
        if read_json(marker) != expected:
            raise SafetyError("State directory belongs to a different client home.")
    else:
        atomic_json(marker, expected, exclusive=True)
    return path


@contextmanager
def state_lock(root, home):
    import fcntl
    token = hashlib.sha256(str(Path(home).resolve()).encode()).hexdigest()
    # A fixed per-home lock prevents two independently chosen state directories racing.
    # Located in the user's cache only for writes; never created during plan/doctor.
    base = require_plain_path(Path.home() / ".wb-account-sync-locks")
    if not base.exists():
        base.mkdir(mode=0o700)
    if base.stat().st_uid != os.getuid() or base.stat().st_mode & 0o077:
        raise SafetyError("Unsafe lock directory permissions.")
    lock_path = base / (token + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafetyError("Another operation holds this client home's lock.") from None
        yield
    finally:
        os.close(fd)
