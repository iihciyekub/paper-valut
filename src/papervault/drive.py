from __future__ import annotations

import hashlib
import random
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from google.auth.transport.requests import AuthorizedSession

from .auth import credentials
from .core import VaultError, atomic_write, digest, encode, read_json

BASE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FIELDS = "id,name,mimeType,size,sha256Checksum,trashed,parents,appProperties,capabilities(canDownload)"
CHUNK = 4 * 1024 * 1024


def quoted(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise VaultError("无效的 Drive fileId。")
    return value


class Drive:
    def __init__(self, state, session=None):
        self.state = state
        self.session = session or AuthorizedSession(credentials(state))
        self.sessions = state.home / "uploads"
        self.sessions.mkdir(exist_ok=True, mode=0o700)

    def request(self, method, url, *, allowed=(), retries=4, **kwargs):
        # Only Google receives the OAuth bearer token. Never load arbitrary URLs from records.
        p = urlparse(url)
        if p.scheme != "https" or p.hostname != "www.googleapis.com":
            raise VaultError("拒绝非 Google API 地址。")
        for attempt in range(retries + 1):
            try:
                r = self.session.request(method, url, timeout=(15, 90), allow_redirects=False, **kwargs)
            except requests.RequestException:
                if attempt == retries:
                    raise VaultError("Google 网络请求失败；任务已保留，可以重试。", 3) from None
                time.sleep(min(2**attempt + random.random(), 10))
                continue
            if r.status_code in allowed or 200 <= r.status_code < 300:
                return r
            reason = ""
            try:
                errors = r.json().get("error", {}).get("errors", [])
                reason = errors[0].get("reason", "") if errors else ""
            except (ValueError, AttributeError, IndexError):
                pass
            retryable = r.status_code in {408, 429, 500, 502, 503, 504} or reason in {
                "rateLimitExceeded",
                "userRateLimitExceeded",
                "backendError",
            }
            if retryable and attempt < retries:
                delay = r.headers.get("Retry-After", "")
                time.sleep(min(float(delay) if delay.isdigit() else 2**attempt + random.random(), 30))
                r.close()
                continue
            status = r.status_code
            r.close()
            if status == 401:
                raise VaultError("Google 登录已失效，请重新运行 pv auth login。", 4)
            if status == 403 and not retryable:
                raise VaultError("Drive 拒绝操作：请检查 API 是否启用、授权权限和云盘剩余容量。", 4)
            raise VaultError(f"Drive 请求失败（HTTP {status}）。", 3 if retryable else 5)

    def new_id(self):
        return self.request(
            "GET", BASE + "/files/generateIds", params={"count": 1, "space": "drive", "type": "files"}
        ).json()["ids"][0]

    def get(self, file_id, missing_ok=False):
        r = self.request(
            "GET", BASE + "/files/" + valid_id(file_id), params={"fields": FIELDS}, allowed=(404,)
        )
        if r.status_code == 404:
            if missing_ok:
                return None
            raise VaultError("云端文件不存在或当前应用无权访问。")
        return r.json()

    def iter_files(self, q):
        token = None
        while True:
            args = {
                "q": q,
                "spaces": "drive",
                "pageSize": 1000,
                "fields": f"nextPageToken,incompleteSearch,files({FIELDS})",
            }
            if token:
                args["pageToken"] = token
            data = self.request("GET", BASE + "/files", params=args).json()
            if data.get("incompleteSearch"):
                raise VaultError("Drive 搜索不完整，拒绝使用不完整结果。", 3)
            yield from data.get("files", [])
            token = data.get("nextPageToken")
            if not token:
                return

    def list(self, q):
        return list(self.iter_files(q))

    def trash(self, file_id):
        return self.request("PATCH", BASE + "/files/" + valid_id(file_id), json={"trashed": True}).json()

    def children(self, folder):
        return self.list(f"{quoted(folder)} in parents and trashed = false")

    def folder(self, file_id, name, parent=None, props=None):
        old = self.get(file_id, missing_ok=True)
        if old:
            if old.get("trashed") or old.get("mimeType") != "application/vnd.google-apps.folder":
                raise VaultError("预分配文件夹 ID 对应的对象类型不符或已删除。")
            return old
        body = {
            "id": file_id,
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "appProperties": props or {},
        }
        if parent:
            body["parents"] = [parent]
        r = self.request("POST", BASE + "/files", json=body, params={"fields": FIELDS}, allowed=(409,))
        return self.get(file_id) if r.status_code == 409 else r.json()

    def download(self, file_id, sink=None, expected=None):
        meta = self.get(file_id)
        if meta.get("trashed") or meta.get("capabilities", {}).get("canDownload") is False:
            raise VaultError("云端文件已删除或禁止下载。")
        h = hashlib.sha256()
        size = 0
        try:
            with self.request(
                "GET", BASE + "/files/" + valid_id(file_id), params={"alt": "media"}, stream=True
            ) as r:
                for chunk in r.iter_content(CHUNK):
                    if not chunk:
                        continue
                    h.update(chunk)
                    size += len(chunk)
                    if sink is not None:
                        sink.write(chunk)
        except requests.RequestException:
            raise VaultError("下载中断，未生成最终文件；请重试。", 3) from None
        sha = h.hexdigest()
        if expected and sha != expected:
            raise VaultError("下载 SHA-256 不匹配，文件不能使用或用于清理原件。")
        if meta.get("size") and size != int(meta["size"]):
            raise VaultError("下载大小与 Drive 元数据不一致。")
        return {"sha256": sha, "size": size}

    def read_json(self, file_id):
        # Bound remote record reads. They are data, never executable instructions.
        meta = self.get(file_id)
        if meta.get("trashed") or int(meta.get("size", 0)) > 10 * 1024 * 1024:
            raise VaultError("云端 JSON 已删除或超过 10 MB 限制。")
        import io
        import json

        class Bounded(io.BytesIO):
            def write(self, b):
                if self.tell() + len(b) > 10 * 1024 * 1024:
                    raise VaultError("云端 JSON 超过 10 MB 限制。")
                return super().write(b)

        sink = Bounded()
        self.download(file_id, sink)
        try:
            return json.loads(sink.getvalue())
        except ValueError:
            raise VaultError("云端 JSON 无法解析。") from None

    def verify(self, file_id, sha, size, full=False):
        meta = self.get(file_id)
        if meta.get("trashed") or int(meta.get("size", -1)) != size:
            raise VaultError("云端文件不存在、已删除或大小不匹配。")
        remote = meta.get("sha256Checksum")
        if remote and remote != sha:
            raise VaultError("云端 SHA-256 不匹配。")
        if full or not remote:
            self.download(file_id, expected=sha)
            return "download-sha256"
        return "drive-sha256"

    def upload(self, file_id, path: Path, name, parent, mime, props):
        sha, size = digest(path), path.stat().st_size
        old = self.get(file_id, missing_ok=True)
        if old:
            self.verify(file_id, sha, size)
            return old
        progress_path = self.sessions / (valid_id(file_id) + ".json")
        saved = read_json(progress_path, {})
        if saved and (saved.get("sha256") != sha or saved.get("size") != size):
            raise VaultError("上传恢复记录与源文件不符。")
        url = saved.get("url")
        for restart in range(3):
            if not url:
                body = {
                    "id": file_id,
                    "name": name,
                    "parents": [parent],
                    "mimeType": mime,
                    "appProperties": props,
                }
                r = self.request(
                    "POST",
                    UPLOAD,
                    params={"uploadType": "resumable", "fields": FIELDS},
                    json=body,
                    headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
                    allowed=(409,),
                )
                if r.status_code == 409:
                    self.verify(file_id, sha, size)
                    return self.get(file_id)
                url = r.headers.get("Location")
                if not url:
                    raise VaultError("Drive 未返回续传地址。", 3)
                atomic_write(progress_path, encode({"url": url, "sha256": sha, "size": size}))
            # Probe the server, including after process restarts or lost responses.
            r = self.request(
                "PUT",
                url,
                data=b"",
                headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"},
                allowed=(308, 404, 410),
            )
            if r.status_code in (200, 201):
                self.verify(file_id, sha, size)
                progress_path.unlink(missing_ok=True)
                return self.get(file_id)
            if r.status_code in (404, 410):
                old = self.get(file_id, missing_ok=True)
                if old:
                    self.verify(file_id, sha, size)
                    progress_path.unlink(missing_ok=True)
                    return old
                url = None
                continue
            offset = (
                int(r.headers.get("Range", "bytes=0--1").split("-")[-1]) + 1 if r.headers.get("Range") else 0
            )
            with path.open("rb") as f:
                while offset < size:
                    f.seek(offset)
                    block = f.read(min(CHUNK, size - offset))
                    end = offset + len(block) - 1
                    try:
                        r = self.request(
                            "PUT",
                            url,
                            data=block,
                            headers={"Content-Type": mime, "Content-Range": f"bytes {offset}-{end}/{size}"},
                            allowed=(308, 404, 410),
                            retries=0,
                        )
                    except VaultError as e:
                        if e.code != 3:
                            raise
                        break  # Next loop probes authoritative server progress.
                    if r.status_code in (200, 201):
                        self.verify(file_id, sha, size)
                        progress_path.unlink(missing_ok=True)
                        return self.get(file_id)
                    if r.status_code in (404, 410):
                        url = None
                        break
                    new_offset = int(r.headers["Range"].split("-")[-1]) + 1 if r.headers.get("Range") else 0
                    if new_offset <= offset or new_offset > size:
                        raise VaultError("Drive 续传进度异常，请重试。", 3)
                    offset = new_offset
        raise VaultError("上传未完成，已保存续传状态；可运行 pv jobs retry。", 3)

    def upload_json(self, file_id, value, name, parent, props):
        p = self.sessions / (valid_id(file_id) + ".body.json")
        atomic_write(p, encode(value))
        try:
            self.upload(file_id, p, name, parent, "application/json", props)
            if self.read_json(file_id) != value:
                raise VaultError("云端记录读回校验失败。")
        finally:
            p.unlink(missing_ok=True)
