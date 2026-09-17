from __future__ import annotations

import copy
import json
import sqlite3

from .core import VaultError, now
from .drive import valid_id
from .metadata import normalize_doi, text_key, validate_metadata


def validate_record(r, vault_id):
    import re
    import uuid

    try:
        if r["schema_version"] not in (1, 2, 3) or r["vault_id"] != vault_id:
            raise ValueError("Schema or vault mismatch")
        for key in ("work_id", "commit_id", "writer_id"):
            uuid.UUID(r[key])
        if r.get("parent_commit_id"):
            uuid.UUID(r["parent_commit_id"])
        if normalize_doi(r["doi"]) != r["doi"]:
            raise ValueError("DOI not normalized")
        validate_metadata(r["doi"], r["metadata"]["raw"], r["metadata"]["csl"])
        if r["metadata"]["doi"] != r["doi"]:
            raise ValueError("Metadata DOI mismatch")
        if not r["files"] and not r.get("deleted"):
            raise ValueError("Empty files")
        hashes = set()
        for f in r["files"]:
            if not re.fullmatch("[a-f0-9]{64}", f["sha256"]) or f["sha256"] in hashes:
                raise ValueError("Invalid or duplicate hash")
            hashes.add(f["sha256"])
            valid_id(f["drive_file_id"])
            if r["schema_version"] >= 3 or f.get("csl_file_id"):
                valid_id(f["csl_file_id"])
            if type(f["size"]) is not int or f["size"] <= 0 or not f.get("verified_at"):
                raise ValueError("Invalid file size or verification")
        if r.get("primary_sha256") and r["primary_sha256"] not in hashes:
            raise ValueError("Primary missing")
        if r["decision"]["method"] not in ("manual", "automatic"):
            raise ValueError("No matching evidence")
    except (KeyError, TypeError, ValueError, VaultError):
        raise VaultError("文献档案 schema、DOI、文件关联或准入依据校验失败。") from None
    return r


def heads(records, vault_id):
    commits = {}
    for r in records:
        validate_record(r, vault_id)
        cid = r["commit_id"]
        if cid in commits and commits[cid] != r:
            raise VaultError("相同 commit_id 对应不同内容。")
        commits[cid] = r
    children = {}
    by_work = {}
    for cid, r in commits.items():
        by_work.setdefault(r["work_id"], []).append(cid)
        parent = r.get("parent_commit_id")
        if parent:
            if parent not in commits or commits[parent]["work_id"] != r["work_id"]:
                raise VaultError("记录父链缺失或指向另一篇文献，保留旧索引。")
            children.setdefault(parent, []).append(cid)
    result = []
    dois = set()
    blobs = {}
    for ids in by_work.values():
        roots = [cid for cid in ids if not commits[cid].get("parent_commit_id")]
        tips = [cid for cid in ids if not children.get(cid)]
        if len(roots) != 1 or len(tips) != 1 or any(len(children.get(cid, [])) > 1 for cid in ids):
            raise VaultError("云端记录有分叉或循环，需要人工处理；未覆盖旧索引。")
        visited = set()
        tip = tips[0]
        while tip:
            if tip in visited:
                raise VaultError("云端记录父链循环。")
            visited.add(tip)
            tip = commits[tip].get("parent_commit_id")
        if visited != set(ids):
            raise VaultError("存在未连接的记录版本。")
        r = commits[tips[0]]
        if r["doi"] in dois:
            raise VaultError("一个 DOI 对应多个文献记录，拒绝静默合并。")
        dois.add(r["doi"])
        for f in r["files"]:
            previous = blobs.get(f["sha256"])
            if previous and previous != (r["doi"], f["drive_file_id"], f["size"]):
                raise VaultError("同一文件哈希存在冲突关联。")
            blobs[f["sha256"]] = (r["doi"], f["drive_file_id"], f["size"])
        result.append(r)
    return result


def compact(record):
    r = copy.deepcopy(record)
    meta = r.get("metadata", {})
    raw = meta.get("raw", {})
    meta["raw"] = {k: raw[k] for k in ("DOI", "title", "type", "author", "issued") if k in raw}
    meta.pop("csl_provider", None)
    return r


class Index:
    def __init__(self, state):
        self.db = sqlite3.connect(state.home / "index.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS works(work_id TEXT PRIMARY KEY, doi TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS commits(commit_id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cache(sha TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS remote_records(file_id TEXT PRIMARY KEY, sha TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.db.commit()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS blobs(sha TEXT PRIMARY KEY, work_id TEXT NOT NULL, doi TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS blobs_work ON blobs(work_id);
            CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch_id);
            CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
            CREATE VIRTUAL TABLE IF NOT EXISTS search_tri USING fts5(text, work_id UNINDEXED, tokenize='trigram');
            CREATE VIRTUAL TABLE IF NOT EXISTS search_words USING fts5(text, work_id UNINDEXED, tokenize='unicode61');
        """)
        if not self.db.execute("SELECT 1 FROM settings WHERE key='scale_index_v2'").fetchone():
            with self.db:
                for (body,) in self.db.execute("SELECT body FROM works"):
                    self._project(json.loads(body))
                self.db.execute("INSERT INTO settings VALUES('scale_index_v2','1')")

    def _project(self, r):
        r = compact(r)
        wid = r["work_id"]
        self.db.execute("DELETE FROM blobs WHERE work_id=?", (wid,))
        old = self.db.execute("SELECT rowid FROM works WHERE work_id=?", (wid,)).fetchone()
        if old:
            self.db.execute("DELETE FROM search_tri WHERE rowid=?", old)
            self.db.execute("DELETE FROM search_words WHERE rowid=?", old)
        self.db.execute(
            "INSERT INTO works(work_id,doi,body) VALUES(?,?,?) ON CONFLICT(work_id) DO UPDATE SET doi=excluded.doi,body=excluded.body",
            (wid, r["doi"], json.dumps(r)),
        )
        rowid = self.db.execute("SELECT rowid FROM works WHERE work_id=?", (wid,)).fetchone()[0]
        if r.get("deleted"):
            return
        self.db.executemany(
            "INSERT INTO blobs VALUES(?,?,?,?)",
            [(f["sha256"], wid, r["doi"], json.dumps(f)) for f in r["files"]],
        )
        csl = r["metadata"]["csl"]
        searchable = (
            str(csl.get("title", ""))
            + " "
            + " ".join(
                str(a.get("family", "")) + " " + str(a.get("given", "")) for a in csl.get("author", [])
            )
            + " "
            + r["doi"]
        )
        self.db.execute(
            "INSERT INTO search_tri(rowid,text,work_id) VALUES(?,?,?)", (rowid, text_key(searchable), wid)
        )
        self.db.execute(
            "INSERT INTO search_words(rowid,text,work_id) VALUES(?,?,?)", (rowid, searchable, wid)
        )

    def apply_chain(self, records, vault_id):
        current = heads(records, vault_id)
        if len(current) != 1:
            raise VaultError("DOI 查询未得到唯一文献历史链。")
        r = current[0]
        row = self.db.execute("SELECT work_id FROM works WHERE doi=?", (r["doi"],)).fetchone()
        if row and row[0] != r["work_id"]:
            raise VaultError("DOI 对应不同内部文献 ID。")
        try:
            with self.db:
                self._project(r)
                self.db.executemany(
                    "INSERT OR REPLACE INTO commits VALUES(?,?)",
                    [(x["commit_id"], json.dumps(compact(x))) for x in records],
                )
        except sqlite3.IntegrityError:
            raise VaultError("文件哈希或 DOI 与其他记录冲突。") from None
        return r

    def blob(self, sha):
        row = self.db.execute("SELECT doi,body FROM blobs WHERE sha=?", (sha,)).fetchone()
        return {"doi": row[0], **json.loads(row[1])} if row else None

    def get_doi(self, doi, include_deleted=False):
        row = self.db.execute("SELECT body FROM works WHERE doi=?", (normalize_doi(doi),)).fetchone()
        r = json.loads(row[0]) if row else None
        return r if r and (include_deleted or not r.get("deleted")) else None

    def iter_works(self, include_deleted=False):
        for (body,) in self.db.execute("SELECT body FROM works ORDER BY doi"):
            r = json.loads(body)
            if include_deleted or not r.get("deleted"):
                yield r

    def rebuild_stream(self, records, vault_id):
        # Stage on disk, validate one work's history at a time, then swap in a transaction.
        self.bind(vault_id)
        self.db.executescript(
            "DROP TABLE IF EXISTS rebuild_commits; CREATE TABLE rebuild_commits(cid TEXT PRIMARY KEY,wid TEXT,body TEXT); CREATE INDEX rebuild_work ON rebuild_commits(wid);"
        )
        count = 0
        for r in records:
            validate_record(r, vault_id)
            try:
                self.db.execute(
                    "INSERT INTO rebuild_commits VALUES(?,?,?)",
                    (r["commit_id"], r["work_id"], json.dumps(compact(r))),
                )
            except sqlite3.IntegrityError:
                raise VaultError("重复的云端 commit_id。") from None
            count += 1
            if count % 500 == 0:
                self.db.commit()
        self.db.commit()
        n = 0
        try:
            with self.db:
                self.db.execute("DELETE FROM works")
                self.db.execute("DELETE FROM commits")
                self.db.execute("DELETE FROM blobs")
                self.db.execute("DELETE FROM search_tri")
                self.db.execute("DELETE FROM search_words")
                for (wid,) in self.db.execute("SELECT DISTINCT wid FROM rebuild_commits"):
                    chain = [
                        json.loads(x[0])
                        for x in self.db.execute("SELECT body FROM rebuild_commits WHERE wid=?", (wid,))
                    ]
                    current = heads(chain, vault_id)
                    if len(current) != 1:
                        raise VaultError("重建发现文献历史冲突。")
                    r = current[0]
                    if self.db.execute("SELECT 1 FROM works WHERE doi=?", (r["doi"],)).fetchone():
                        raise VaultError("重建发现重复 DOI。")
                    self._project(r)
                    n += not r.get("deleted", False)
                self.db.execute("INSERT INTO commits SELECT cid,body FROM rebuild_commits")
        except sqlite3.IntegrityError:
            raise VaultError("重建发现文件哈希冲突，保留旧索引。") from None
        with self.db:
            self.db.execute("DROP TABLE rebuild_commits")
        return {"works": n, "records": count, "rebuilt_at": now()}

    def bind(self, vault_id):
        row = self.db.execute("SELECT value FROM settings WHERE key='vault_id'").fetchone()
        if row and row[0] != vault_id:
            raise VaultError("本地索引属于其他文献库；请使用不同的 --home 目录。", 4)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES('vault_id', ?)", (vault_id,))

    def replace(self, records, vault_id):
        self.rebuild_stream(iter(records), vault_id)
        return self.all()

    def all(self):
        return list(self.iter_works())

    def commit_record(self, commit_id):
        row = self.db.execute("SELECT body FROM commits WHERE commit_id=?", (commit_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def cached_remote(self, file_id, sha):
        if not sha:
            return None
        row = self.db.execute(
            "SELECT body FROM remote_records WHERE file_id=? AND sha=?", (file_id, sha)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def cache_remote(self, file_id, sha, body):
        if sha:
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO remote_records VALUES(?,?,?)", (file_id, sha, json.dumps(body))
                )

    def find(self, query, limit=100):
        try:
            d = normalize_doi(query)
        except VaultError:
            d = None
        if d:
            r = self.get_doi(d)
            return [r] if r else []
        terms = [text_key(t) for t in query.split() if text_key(t)]
        if not terms:
            return []
        table = "search_tri" if all(len(t) >= 3 for t in terms) else "search_words"
        expression = " AND ".join('"' + t + '"' for t in terms)
        rows = self.db.execute(
            f"SELECT w.body FROM {table} s JOIN works w ON w.work_id=s.work_id WHERE {table} MATCH ? LIMIT ?",
            (expression, limit),
        )
        return [json.loads(r[0]) for r in rows]

    def save_job(self, job):
        job["updated_at"] = now()
        stored = copy.deepcopy(job)
        if job["status"] in ("archived", "local_released", "skipped_existing_doi"):
            for k in ("extraction", "match", "pending_record"):
                stored.pop(k, None)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO jobs VALUES(?,?,?,?)",
                (job["job_id"], job["batch_id"], job["status"], json.dumps(stored)),
            )

    def job(self, jid):
        row = self.db.execute("SELECT body FROM jobs WHERE job_id=?", (jid,)).fetchone()
        if not row:
            raise VaultError("导入任务不存在。", 2)
        return json.loads(row[0])

    def jobs(self, batch=None):
        rows = self.db.execute(
            "SELECT body FROM jobs" + (" WHERE batch_id=?" if batch else ""), (batch,) if batch else ()
        )
        return [json.loads(row[0]) for row in rows]

    def put_cache(self, item):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?)", (item["sha256"], json.dumps(item)))

    def cache(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT body FROM cache")]

    def drop_cache(self, sha):
        with self.db:
            self.db.execute("DELETE FROM cache WHERE sha=?", (sha,))
