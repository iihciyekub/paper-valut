from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock, Timeout


class VaultError(Exception):
    def __init__(self, message: str, code: int = 5):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(UTC).isoformat()


def uid():
    return str(uuid.uuid4())


def digest(path: Path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".pv-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


class State:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("PAPERVAULT_HOME", "~/.papervault")).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        self.config_path = self.home / "config.json"
        self.config = read_json(self.config_path, {})
        self.config.setdefault("device_id", uid())
        self.config.setdefault("cache_limit_mb", 2048)
        self.config.setdefault("max_pdf_mb", 200)

    def save(self):
        atomic_write(self.config_path, encode(self.config))

    def lock(self):
        lock = FileLock(self.home / "vault.lock", timeout=0)
        try:
            lock.acquire()
        except Timeout:
            raise VaultError("另一个 pv 进程正在操作本地文献库，请稍后重试。", 4) from None
        return lock

    def require_vault(self, write=False):
        v = self.config.get("vault")
        if not v:
            raise VaultError("请先运行 pv init 或 pv connect。", 4)
        if write and v["writer_id"] != self.config["device_id"]:
            raise VaultError("此设备是只读设备；请在原写入设备操作。唯一写入设备迁移方法见 README。", 4)
        return v
