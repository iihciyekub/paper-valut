from __future__ import annotations

import copy
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from send2trash import send2trash

from .core import VaultError, atomic_write, digest, encode, now, uid
from .drive import Drive, quoted, valid_id
from .extract import extract
from .index import Index, compact, validate_record
from .metadata import Crossref, doi_key, identify, normalize_doi, text_key, title_of


def fingerprint(path):
    st = path.lstat()
    if path.is_symlink() or not path.is_file():
        raise VaultError("只接受普通文件，符号链接不参与导入或清理。", 2)
    return {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def summary(r):
    return {
        "doi": r["doi"],
        "title": title_of(r["metadata"]["raw"]),
        "work_id": r["work_id"],
        "files": len(r["files"]),
        "primary_sha256": r.get("primary_sha256"),
        "archived": True,
    }


class Vault:
    def __init__(self, state, drive=None, provider=None):
        self.state = state
        self.index = Index(state)
        self._drive = drive
        self._provider = provider
        if state.config.get("vault"):
            self.index.bind(state.config["vault"]["vault_id"])

    @property
    def drive(self):
        if self._drive is None:
            self._drive = Drive(self.state)
        return self._drive

    @property
    def provider(self):
        if self._provider is None:
            self._provider = Crossref(self.state)
        return self._provider

    def init(self, name="PaperVault"):
        if self.state.config.get("vault"):
            return self.state.config["vault"]
        v = self.state.config.get("pending_vault")
        if not v:
            v = {
                "schema_version": 1,
                "vault_id": uid(),
                "writer_id": self.state.config["device_id"],
                "name": name,
                "root_id": self.drive.new_id(),
                "objects_id": self.drive.new_id(),
                "records_id": self.drive.new_id(),
                "config_id": self.drive.new_id(),
                "created_at": now(),
            }
            self.state.config["pending_vault"] = v
            self.state.save()
        props = {"vault_id": v["vault_id"], "kind": "vault"}
        self.drive.folder(v["root_id"], v["name"], props=props)
        self.drive.folder(v["objects_id"], "objects", v["root_id"])
        self.drive.folder(v["records_id"], "records", v["root_id"])
        self.drive.upload_json(v["config_id"], v, "vault.json", v["root_id"], props)
        self.state.config["vault"] = v
        self.state.config.pop("pending_vault", None)
        self.state.save()
        self.index.bind(v["vault_id"])
        return v

    def discover(self):
        files = self.drive.list(
            "trashed = false and mimeType = 'application/json' and appProperties has { key='kind' and value='vault' }"
        )
        return [
            {"config_id": f["id"], "name": f["name"], "vault_id": f.get("appProperties", {}).get("vault_id")}
            for f in files
        ]

    def connect(self, config_id):
        if self.state.config.get("vault"):
            raise VaultError("此配置目录已连接文献库；连接其他库请指定 --home。", 4)
        v = self.drive.read_json(valid_id(config_id))
        try:
            if v["schema_version"] != 1 or v["config_id"] != config_id:
                raise ValueError()
            import uuid

            uuid.UUID(v["vault_id"])
            uuid.UUID(v["writer_id"])
            for k in ("root_id", "objects_id", "records_id"):
                valid_id(v[k])
        except (KeyError, TypeError, ValueError):
            raise VaultError("不是支持的 PaperVault 配置文件。") from None
        self.state.config["vault"] = v
        self.state.save()
        self.sync()
        return {"vault": v, "read_only": v["writer_id"] != self.state.config["device_id"]}

    def check_writer(self):
        v = self.state.require_vault(write=True)
        remote = self.drive.read_json(v["config_id"])
        if remote != v:
            raise VaultError("云端文献库配置已变化，停止写入；请同步配置并检查写入设备。")
        return v

    def record_query(self, doi=None):
        v = self.state.require_vault()
        q = f"trashed = false and appProperties has {{ key='vault_id' and value={quoted(v['vault_id'])} }} and appProperties has {{ key='kind' and value='record' }}"
        if doi:
            q += f" and appProperties has {{ key='doi_key' and value={quoted(doi_key(doi))} }}"
        return q

    def read_record(self, f):
        v = self.state.require_vault()
        r = self.drive.read_json(f["id"])
        validate_record(r, v["vault_id"])
        if r.get("record_file_id") != f["id"]:
            raise VaultError("文献记录的 fileId 不一致。")
        return r

    def sync(self):
        v = self.state.require_vault()

        def records():
            from collections import deque

            folders = deque([v["records_id"]])
            while folders:
                folder = folders.popleft()
                for f in self.drive.iter_files(f"{quoted(folder)} in parents and trashed = false"):
                    if f.get("mimeType") == "application/vnd.google-apps.folder":
                        folders.append(f["id"])
                    elif f.get("mimeType") == "application/json":
                        yield self.read_record(f)
                    else:
                        raise VaultError("records 中存在非档案文件。")

        return self.index.rebuild_stream(records(), v["vault_id"])

    def sync_work(self, doi):
        doi = normalize_doi(doi)
        records = [self.read_record(f) for f in self.drive.iter_files(self.record_query(doi))]
        if not records:
            if self.index.get_doi(doi, include_deleted=True):
                raise VaultError("云端文献档案不可见或已被外部删除；请检查权限，未静默使用旧索引。")
            return None
        if any(r["doi"] != doi for r in records):
            raise VaultError("DOI 查询返回不一致的文献档案。")
        return self.index.apply_chain(records, self.state.require_vault()["vault_id"])

    def shard(self, kind, key):
        v = self.state.require_vault()
        prefix = key[:2]
        cache_key = f"{kind}:{prefix}"
        cache = self.state.config.setdefault("shards", {})
        if cache_key not in cache:
            parent = v[kind + "_id"]
            matches = self.drive.list(
                f"{quoted(parent)} in parents and trashed = false and name = {quoted(prefix)} and mimeType = 'application/vnd.google-apps.folder'"
            )
            if len(matches) > 1:
                raise VaultError("分片目录重名，请检查云盘。")
            cache[cache_key] = matches[0]["id"] if matches else self.drive.new_id()
            self.state.save()  # Persist pre-generated ID before creating the folder.
        self.drive.folder(
            cache[cache_key], prefix, v[kind + "_id"], {"vault_id": v["vault_id"], "kind": "shard"}
        )
        return cache[cache_key]

    @staticmethod
    def sidecar_csl(metadata):
        csl = copy.deepcopy(metadata["csl"])
        csl["id"] = metadata["doi"]
        return csl

    def upload_sidecar(self, file_id, sha256, csl):
        v = self.state.require_vault()
        self.drive.upload_json(
            file_id,
            csl,
            sha256 + ".csl.json",
            self.shard("objects", sha256),
            {"vault_id": v["vault_id"], "kind": "csl", "sha256": sha256, "schema": "1"},
        )

    def verify_sidecar(self, file_entry, metadata):
        fid = file_entry.get("csl_file_id")
        if not fid:
            return False
        v = self.state.require_vault()
        meta = self.drive.get(fid)
        props = meta.get("appProperties", {})
        if (
            meta.get("trashed")
            or meta.get("mimeType") != "application/json"
            or props.get("vault_id") != v["vault_id"]
            or props.get("kind") != "csl"
            or props.get("sha256") != file_entry["sha256"]
        ):
            raise VaultError("PDF 的 CSL sidecar 不存在、已删除或归属信息不一致。")
        if self.drive.read_json(fid) != self.sidecar_csl(metadata):
            raise VaultError("PDF 的 CSL sidecar 与当前文献元数据不一致。")
        return True

    def verify_record_sidecars(self, record):
        for f in record["files"]:
            if record.get("schema_version", 1) >= 3 or f.get("csl_file_id"):
                self.verify_sidecar(f, record["metadata"])

    def finish_sidecar_trash(self, sidecars, current):
        active_ids = {f.get("csl_file_id") for f in current["files"] if f.get("csl_file_id")}
        v = self.state.require_vault()
        for item in sidecars:
            fid = item.get("csl_file_id")
            if not fid:
                continue
            if fid in active_ids:
                raise VaultError("目标 CSL sidecar 仍由当前文献记录引用，拒绝移除。")
            meta = self.drive.get(fid)
            props = meta.get("appProperties", {})
            if (
                props.get("vault_id") != v["vault_id"]
                or props.get("kind") != "csl"
                or props.get("sha256") != item["sha256"]
            ):
                raise VaultError("目标文件不是本库受管的 CSL sidecar，拒绝移除。")
            if not meta.get("trashed"):
                self.drive.trash(fid)
            if not self.drive.get(fid).get("trashed"):
                raise VaultError("Drive 未确认 CSL sidecar 移入回收站。", 3)

    def find(self, query, refresh=False):
        try:
            doi = normalize_doi(query)
        except VaultError:
            doi = None
        if doi and (refresh or not self.index.get_doi(doi)):
            self.sync_work(doi)
        elif refresh:
            raise VaultError(
                "标题查询不会触发全库同步；请用 pv resolve 查询 Crossref，或显式 pv rebuild。", 2
            )
        return self.index.find(query)

    def resolve(self, query):
        try:
            doi = normalize_doi(query)
        except VaultError:
            doi = None
        candidates = [self.provider.lookup(doi)["raw"]] if doi else self.provider.search(query)
        return [
            {
                "doi": normalize_doi(r["DOI"]),
                "title": title_of(r),
                "exact_title": text_key(title_of(r)) == text_key(query),
                "archived_in_local_index": bool(self.index.get_doi(r["DOI"])),
            }
            for r in candidates
            if r.get("DOI")
        ]

    def resolve_doi(self, query, selected=None):
        candidates = self.resolve(query)
        if selected:
            doi = normalize_doi(selected)
            if doi not in [r["doi"] for r in candidates]:
                raise VaultError("指定 DOI 不在 Crossref 候选中。", 2)
            return doi
        try:
            return normalize_doi(query)
        except VaultError:
            pass
        exact = [r for r in candidates if r["exact_title"]]
        if len(exact) != 1 or len(text_key(query)) < 30:
            raise VaultError(
                "文本匹配有歧义，请先 pv resolve 查看候选，再 get --resolve --select-doi 指定 DOI。", 2
            )
        return exact[0]["doi"]

    def one(self, query):
        rows = self.find(query)
        if not rows:
            raise VaultError("本库没有对应文献。Crossref 元数据存在不等于 PDF 已归档。", 2)
        if len(rows) != 1:
            raise VaultError("匹配到多篇文献，请改用明确 DOI。", 2)
        return rows[0]

    def make_job(self, path, batch, supplied_doi=None, role="unspecified"):
        path = Path(os.path.abspath(Path(path).expanduser()))
        before = fingerprint(path)
        if before["size"] > self.state.config["max_pdf_mb"] * 1024 * 1024 or before["size"] == 0:
            raise VaultError("PDF 为空或超过配置的大小上限。", 2)
        jid = uid()
        folder = self.state.home / "staging"
        folder.mkdir(exist_ok=True, mode=0o700)
        snap = folder / (jid + ".pdf")
        with path.open("rb") as source, snap.open("xb") as dest:
            os.chmod(snap, 0o600)
            shutil.copyfileobj(source, dest, 1024 * 1024)
        if fingerprint(path) != before:
            snap.unlink(missing_ok=True)
            raise VaultError("复制期间源文件发生变化，请关闭编辑器后重试。", 2)
        return {
            "job_id": jid,
            "batch_id": batch,
            "status": "discovered",
            "source": str(path),
            "fingerprint": before,
            "snapshot": str(snap),
            "sha256": digest(snap),
            "size": before["size"],
            "supplied_doi": supplied_doi,
            "role": role,
            "created_at": now(),
        }

    def import_file(
        self,
        path,
        batch,
        supplied_doi=None,
        dry_run=False,
        role="unspecified",
        force=False,
        keep_version=False,
        replace_sha=None,
    ):
        # Hash before copying/parsing or contacting Crossref. Exact duplicates take a bounded lookup.
        path = Path(os.path.abspath(Path(path).expanduser()))
        before = fingerprint(path)
        sha = digest(path)
        if fingerprint(path) != before:
            raise VaultError("哈希计算期间文件改变。", 2)
        existing = self.index.blob(sha)
        if existing and not force:
            if supplied_doi and normalize_doi(supplied_doi) != existing["doi"]:
                raise VaultError("相同字节已关联不同 DOI。")
            self.sync_work(existing["doi"])
            existing = self.index.blob(sha)
            if existing:
                self.drive.verify(existing["drive_file_id"], sha, before["size"])
                j = {
                    "job_id": uid(),
                    "batch_id": batch,
                    "status": "accepted" if dry_run else "archived",
                    "source": str(path),
                    "fingerprint": before,
                    "sha256": sha,
                    "size": before["size"],
                    "doi": existing["doi"],
                    "blob_id": existing["drive_file_id"],
                    "duplicate": True,
                    "skipped_processing": True,
                    "snapshot": "",
                    "created_at": now(),
                }
                if not dry_run:
                    self.index.save_job(j)
                return j
        job = self.make_job(path, batch, supplied_doi, role)
        job.update(force=force, keep_version=keep_version, replace_sha=replace_sha)
        if not dry_run:
            self.index.save_job(job)
        try:
            return self.run_job(job, dry_run=dry_run)
        finally:
            if dry_run:
                Path(job["snapshot"]).unlink(missing_ok=True)

    def run_job(self, job, dry_run=False, manual_doi=None, reason=None):
        save = (lambda: None) if dry_run else (lambda: self.index.save_job(job))
        try:
            if job["status"] in ("archived", "local_released", "skipped_existing_doi"):
                return job
            snap = Path(job["snapshot"])
            if not snap.is_file() or digest(snap) != job["sha256"]:
                raise VaultError("导入快照缺失或已改变；请重新导入源文件。")
            if manual_doi and (job.get("pending_record") or job.get("blob_id")):
                raise VaultError("上传已开始，不能在恢复任务中更换 DOI。")
            if not job.get("match") or manual_doi or job["match"].get("status") != "accepted":
                extraction = extract(snap)
                job["extraction"] = extraction
                job["match"] = identify(
                    extraction, self.provider, manual_doi or job.get("supplied_doi"), reason
                )
                job["status"] = job["match"]["status"]
                save()
            if job["status"] == "needs_review" or job["match"]["status"] != "accepted":
                return job
            if dry_run:
                job["status"] = "accepted"
                return job
            self.archive(job)
            job.pop("error", None)
            job.pop("error_code", None)
            save()
            snap.unlink(missing_ok=True)
            return job
        except VaultError as e:
            job["status"] = "retryable_error" if e.code == 3 else "rejected" if e.code == 2 else "blocked"
            job["error"] = str(e)
            job["error_code"] = e.code
            save()
            return job

    def archive(self, job):
        v = self.check_writer()
        meta = job["match"]["metadata"]
        doi = meta["doi"]
        old = self.sync_work(doi)
        # Persisted remote commit may have succeeded before a lost response/local failure.
        if job.get("pending_record"):
            pending = job["pending_record"]
            if old and self.index.commit_record(pending["commit_id"]) == compact(pending):
                self.choose_file(old, job["sha256"])
                self.drive.verify(job["blob_id"], job["sha256"], job["size"])
                self.verify_record_sidecars(old)
                job.update(status="archived", doi=doi, record_file_id=old["record_file_id"])
                self.finish_trash(job.get("trash_after", []), old)
                self.index.save_job(job)
                return
            if (old or {}).get("commit_id") != pending.get("parent_commit_id"):
                raise VaultError("恢复任务的父记录已变化，停止提交以避免分叉。")
        existing = self.index.blob(job["sha256"])
        if existing and existing["doi"] != doi:
            raise VaultError("同一文件已关联另一 DOI，需要人工纠错。")
        if old and not old.get("deleted") and not job.get("pending_record") and not job.get("force"):
            if existing:
                self.drive.verify(existing["drive_file_id"], job["sha256"], job["size"])
                if existing.get("csl_file_id"):
                    self.verify_sidecar(existing, old["metadata"])
                job.update(
                    status="archived",
                    doi=doi,
                    blob_id=existing["drive_file_id"],
                    record_file_id=old["record_file_id"],
                    duplicate=True,
                )
                self.index.save_job(job)
                return
            if not job.get("keep_version"):
                job.update(
                    status="skipped_existing_doi",
                    doi=doi,
                    reason="DOI 已有文件；使用 --force 替换，或 --keep-version 保留另一个版本。",
                )
                self.index.save_job(job)
                return
        if not job.get("blob_id"):
            q = f"trashed = false and appProperties has {{ key='vault_id' and value={quoted(v['vault_id'])} }} and appProperties has {{ key='kind' and value='blob' }} and appProperties has {{ key='sha256' and value={quoted(job['sha256'])} }}"
            candidates = [] if job.get("force") else self.drive.list(q)
            if len(candidates) > 1:
                raise VaultError("云端存在重复哈希对象，需先检查，不会任意选择。")
            job["blob_id"] = candidates[0]["id"] if candidates else self.drive.new_id()
        if not job.get("csl_id"):
            if job.get("pending_record"):
                pending_file = self.choose_file(job["pending_record"], job["sha256"])
                job["csl_id"] = pending_file.get("csl_file_id") or self.drive.new_id()
            else:
                job["csl_id"] = self.drive.new_id()
        job["status"] = "uploading"
        self.index.save_job(job)
        props = {"vault_id": v["vault_id"], "kind": "blob", "sha256": job["sha256"], "schema": "1"}
        self.drive.upload(
            job["blob_id"],
            Path(job["snapshot"]),
            job["sha256"] + ".pdf",
            self.shard("objects", job["sha256"]),
            "application/pdf",
            props,
        )
        verification = self.drive.verify(job["blob_id"], job["sha256"], job["size"])
        csl_value = self.sidecar_csl(meta)
        self.upload_sidecar(job["csl_id"], job["sha256"], csl_value)
        job["status"] = "blob_verified"
        self.index.save_job(job)
        if not job.get("pending_record"):
            r = (
                copy.deepcopy(old)
                if old
                else {
                    "schema_version": 3,
                    "vault_id": v["vault_id"],
                    "work_id": uid(),
                    "doi": doi,
                    "files": [],
                }
            )
            r.update(
                commit_id=uid(),
                parent_commit_id=old["commit_id"] if old else None,
                writer_id=self.state.config["device_id"],
                record_file_id=self.drive.new_id(),
                metadata=meta,
                decision=job["match"]["decision"],
                updated_at=now(),
            )
            r["deleted"] = False
            if old and job.get("force") and old["files"]:
                target = self.choose_file(old, job.get("replace_sha"))
                # A forced upload of an existing alternative also replaces that hash's old location.
                retired = [f for f in r["files"] if f["sha256"] in {target["sha256"], job["sha256"]}]
                job["trash_after"] = retired
                r["files"] = [f for f in r["files"] if f not in retired]
                r["removed_files"] = retired
                r["primary_sha256"] = job["sha256"]
            backfill = job.setdefault("backfill_csl_ids", {})
            for f in r["files"]:
                if f.get("csl_file_id"):
                    continue
                fid = backfill.get(f["sha256"])
                if not fid:
                    fid = self.drive.new_id()
                    backfill[f["sha256"]] = fid
                    self.index.save_job(job)
                self.upload_sidecar(fid, f["sha256"], csl_value)
                f["csl_file_id"] = fid
            r["files"].append(
                {
                    "sha256": job["sha256"],
                    "drive_file_id": job["blob_id"],
                    "csl_file_id": job["csl_id"],
                    "size": job["size"],
                    "mime_type": "application/pdf",
                    "original_filename": Path(job["source"]).name,
                    "role": job["role"],
                    "verified_at": now(),
                    "verification": verification,
                }
            )
            r["schema_version"] = 3
            if not old or old.get("deleted"):
                r["primary_sha256"] = job["sha256"]
            job["pending_record"] = r
            self.index.save_job(job)
        self.commit(job["pending_record"])
        self.finish_trash(job.get("trash_after", []), job["pending_record"])
        job.update(status="archived", doi=doi, record_file_id=job["pending_record"]["record_file_id"])
        self.index.save_job(job)

    def commit(self, record):
        v = self.check_writer()
        validate_record(record, v["vault_id"])
        self.drive.upload_json(
            record["record_file_id"],
            record,
            f"{record['work_id']}.{record['commit_id']}.json",
            self.shard("records", doi_key(record["doi"])),
            {
                "vault_id": v["vault_id"],
                "kind": "record",
                "doi_key": doi_key(record["doi"]),
                "work_id": record["work_id"],
                "commit_id": record["commit_id"],
            },
        )
        self.sync_work(record["doi"])

    def choose_file(self, r, sha=None):
        target = sha or r.get("primary_sha256")
        matches = [f for f in r["files"] if f["sha256"] == target] if target else r["files"]
        if len(matches) != 1:
            raise VaultError("请用 --sha256 指定一个完整哈希，或先设置 primary 文件。", 2)
        return matches[0]

    def get(self, query, output, sha=None):
        r = self.one(query)
        f = self.choose_file(r, sha)
        directory = Path(output).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / (f["sha256"] + ".pdf")
        if final.exists() or final.is_symlink():
            if not final.is_symlink() and final.is_file() and digest(final) == f["sha256"]:
                return {"path": str(final), "sha256": f["sha256"], "existing": True}
            raise VaultError("输出路径已有不同内容，拒绝覆盖。")
        fd, temp = tempfile.mkstemp(prefix=".pv-", suffix=".part", dir=directory)
        try:
            with os.fdopen(fd, "wb") as sink:
                result = self.drive.download(f["drive_file_id"], sink, expected=f["sha256"])
                sink.flush()
                os.fsync(sink.fileno())
            if result["size"] != f["size"]:
                raise VaultError("下载大小与文献档案不一致。")
            # link is atomic and fails if another process creates the destination; no overwrite race.
            os.link(temp, final)
            return {"path": str(final), "sha256": f["sha256"], "existing": False}
        finally:
            Path(temp).unlink(missing_ok=True)

    def open(self, query, sha=None):
        result = self.get(query, self.state.home / "cache", sha)
        self.index.put_cache({**result, "last_used": now(), "pinned": True})
        if sys.platform == "darwin":
            subprocess.run(["open", result["path"]], check=True)
        elif sys.platform == "win32":
            os.startfile(result["path"])
        else:
            subprocess.run(["xdg-open", result["path"]], check=True)
        result["cache_pinned"] = True
        return result

    def verify_all(self):
        self.sync()
        results = []
        for r in self.index.iter_works():
            for f in r["files"]:
                try:
                    self.drive.verify(f["drive_file_id"], f["sha256"], f["size"], full=True)
                    if f.get("csl_file_id"):
                        self.verify_sidecar(f, r["metadata"])
                    results.append({"doi": r["doi"], "sha256": f["sha256"], "ok": True})
                except VaultError as e:
                    results.append(
                        {
                            "doi": r["doi"],
                            "sha256": f["sha256"],
                            "ok": False,
                            "error": str(e),
                            "error_code": e.code,
                        }
                    )
        return results

    def release(self, batch, apply=False):
        jobs = self.index.jobs(batch)
        if not jobs:
            raise VaultError("批次不存在。", 2)
        results = []
        for job in jobs:
            item = {"job_id": job["job_id"], "source": job["source"], "action": "skipped"}
            try:
                if job["status"] != "archived":
                    raise VaultError("任务未归档或已经清理。", 2)
                path = Path(job["source"])
                if fingerprint(path) != job["fingerprint"] or digest(path) != job["sha256"]:
                    raise VaultError("本地原件已改变，不能清理。")
                self.sync_work(job["doi"])
                r = self.one(job["doi"])
                f = self.choose_file(r, job["sha256"])
                self.drive.verify(f["drive_file_id"], f["sha256"], f["size"], full=True)
                if f.get("csl_file_id"):
                    self.verify_sidecar(f, r["metadata"])
                if apply:
                    # Recheck after the network call; never remove a subsequently edited file.
                    if fingerprint(path) != job["fingerprint"] or digest(path) != job["sha256"]:
                        raise VaultError("清理前原件发生变化。")
                    send2trash(str(path))
                    job.update(status="local_released", released_at=now())
                    self.index.save_job(job)
                    item["action"] = "moved_to_trash"
                else:
                    item["action"] = "eligible"
            except (VaultError, FileNotFoundError) as e:
                item["reason"] = str(e)
            results.append(item)
        return results

    def prune_cache(self, unpin=False):
        items = self.index.cache()
        total = sum(Path(i["path"]).stat().st_size for i in items if Path(i["path"]).is_file())
        limit = self.state.config["cache_limit_mb"] * 1024 * 1024
        result = []
        for i in sorted(items, key=lambda x: x["last_used"]):
            p = Path(i["path"])
            if p.parent != self.state.home / "cache" or p.is_symlink():
                continue
            if not p.exists():
                self.index.drop_cache(i["sha256"])
                continue
            if digest(p) != i["sha256"]:
                result.append({"path": str(p), "status": "modified_preserved"})
                continue
            if unpin:
                i["pinned"] = False
                self.index.put_cache(i)
            if not i.get("pinned") and total > limit:
                total -= p.stat().st_size
                p.unlink()
                self.index.drop_cache(i["sha256"])
                result.append({"path": str(p), "status": "removed"})
        return {"bytes_remaining": total, "limit_bytes": limit, "items": result}

    def revise(self, query, primary=None, refresh=False):
        self.check_writer()
        candidate = self.one(query)
        old = self.sync_work(candidate["doi"])
        if not old or old.get("deleted"):
            raise VaultError("文献已移除。", 2)
        r = copy.deepcopy(old)
        if primary:
            self.choose_file(r, primary)
            r["primary_sha256"] = primary
        if refresh:
            r["metadata"] = self.provider.lookup(r["doi"], refresh=True)
            r["metadata_refresh_at"] = now()
        r.update(
            commit_id=uid(),
            parent_commit_id=old["commit_id"],
            record_file_id=self.drive.new_id(),
            updated_at=now(),
        )
        if refresh:
            csl_value = self.sidecar_csl(r["metadata"])
            old_sidecars = []
            sidecars = []
            for f in r["files"]:
                if f.get("csl_file_id"):
                    old_sidecars.append({"csl_file_id": f["csl_file_id"], "sha256": f["sha256"]})
                new_id = self.drive.new_id()
                f["csl_file_id"] = new_id
                sidecars.append({"csl_file_id": new_id, "sha256": f["sha256"], "csl": csl_value})
            r["schema_version"] = 3
            operation = {
                "kind": "metadata_refresh",
                "record": r,
                "sidecars": sidecars,
                "trash_sidecars_after": old_sidecars,
            }
            path = self.state.home / "pending-ops" / (r["commit_id"] + ".json")
            atomic_write(path, encode(operation))
            for item in sidecars:
                self.upload_sidecar(item["csl_file_id"], item["sha256"], item["csl"])
            self.commit(r)
            current = self.sync_work(r["doi"])
            self.finish_sidecar_trash(old_sidecars, current)
            path.unlink(missing_ok=True)
            return summary(r)
        # Keep an exact retryable commit body before touching the network.
        pending = self.state.home / "pending-records" / (r["commit_id"] + ".json")
        atomic_write(pending, encode(r))
        self.commit(r)
        pending.unlink(missing_ok=True)
        return summary(r)

    def finish_trash(self, files, current):
        active_ids = {f["drive_file_id"] for f in current["files"]}
        v = self.state.require_vault()
        for f in files:
            fid = f["drive_file_id"]
            if fid in active_ids:
                raise VaultError("目标文件仍由当前文献记录引用，拒绝移除。")
            meta = self.drive.get(fid)
            props = meta.get("appProperties", {})
            if (
                props.get("vault_id") != v["vault_id"]
                or props.get("kind") != "blob"
                or props.get("sha256") != f["sha256"]
            ):
                raise VaultError("目标文件不属于本库的受管 PDF，拒绝移除。")
            if not meta.get("trashed"):
                self.drive.trash(fid)
            if not self.drive.get(fid).get("trashed"):
                raise VaultError("Drive 未确认移入回收站。", 3)
        self.finish_sidecar_trash(
            [{"csl_file_id": f.get("csl_file_id"), "sha256": f["sha256"]} for f in files], current
        )

    def remove(self, query, sha=None, apply=False):
        self.check_writer()
        found = self.one(query)
        old = self.sync_work(found["doi"])
        if not old or old.get("deleted"):
            raise VaultError("文献已移除或不存在。", 2)
        targets = [self.choose_file(old, sha)] if sha else old["files"]
        plan = {"doi": old["doi"], "files": targets, "action": "trash_files", "apply": apply}
        if not apply:
            return plan
        r = copy.deepcopy(old)
        r["files"] = [f for f in old["files"] if f not in targets]
        r["deleted"] = not r["files"]
        if r.get("primary_sha256") in {f["sha256"] for f in targets}:
            r["primary_sha256"] = None
        r.update(
            schema_version=max(2, old.get("schema_version", 1)),
            removed_files=targets,
            commit_id=uid(),
            parent_commit_id=old["commit_id"],
            record_file_id=self.drive.new_id(),
            updated_at=now(),
        )
        operation = {"record": r, "trash_after": targets}
        path = self.state.home / "pending-ops" / (r["commit_id"] + ".json")
        atomic_write(path, encode(operation))
        self.commit(r)  # Tombstone becomes durable before moving any original blob to Trash.
        self.finish_trash(targets, r)
        path.unlink(missing_ok=True)
        return {**plan, "action": "moved_to_trash", "deleted_work": r["deleted"]}

    def recover_operations(self):
        from .core import read_json

        out = []
        for path in sorted((self.state.home / "pending-ops").glob("*.json")):
            op = read_json(path)
            r = op["record"]
            current = self.sync_work(r["doi"])
            kind = op.get("kind", "remove")
            committed = self.index.commit_record(r["commit_id"]) == compact(r)
            if kind == "metadata_refresh" and not committed:
                for item in op.get("sidecars", []):
                    self.upload_sidecar(item["csl_file_id"], item["sha256"], item["csl"])
            if not committed:
                if not current or current["commit_id"] != r["parent_commit_id"]:
                    raise VaultError("待恢复操作的父版本已变化，需人工检查。")
                self.commit(r)
                current = self.sync_work(r["doi"])
            if kind == "metadata_refresh":
                self.verify_record_sidecars(current)
                self.finish_sidecar_trash(op.get("trash_sidecars_after", []), current)
            else:
                self.finish_trash(op["trash_after"], current)
            path.unlink()
            out.append({"commit_id": r["commit_id"], "recovered": True})
        return out
