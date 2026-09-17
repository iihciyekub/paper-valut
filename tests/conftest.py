import copy
import hashlib
import json
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from papervault.core import State, VaultError, uid
from papervault.service import Vault

DOI = "10.1234/example"
TITLE = "A careful investigation of scholarly document storage"


def metadata(doi=DOI, title=TITLE):
    raw = {
        "DOI": doi,
        "type": "journal-article",
        "title": [title],
        "author": [{"family": "Smith", "given": "Jane"}],
        "issued": {"date-parts": [[2024]]},
    }
    csl = {
        "DOI": doi,
        "type": "article-journal",
        "title": title,
        "author": raw["author"],
        "issued": raw["issued"],
    }
    return {
        "doi": doi,
        "raw": raw,
        "csl": csl,
        "retrieved_at": "2026-09-13",
        "source_url": "https://api.crossref.org/works/" + doi,
    }


class Provider:
    def __init__(self):
        self.entries = {DOI: metadata()}
        self.fail = False

    def lookup(self, doi, refresh=False):
        if self.fail:
            raise VaultError("Network unavailable", 3)
        if doi not in self.entries:
            raise VaultError("No Crossref record", 2)
        return copy.deepcopy(self.entries[doi])

    def search(self, query):
        return [copy.deepcopy(m["raw"]) for m in self.entries.values()]


class FakeDrive:
    def __init__(self):
        self.files = {}
        self.fail_record_once = False
        self.lose_record_response = False
        self.uploaded_blobs = 0
        self.download_count = 0
        self.corrupt_download = False

    def new_id(self):
        return uid()

    def get(self, fid, missing_ok=False):
        if fid not in self.files:
            if missing_ok:
                return None
            raise VaultError("Missing remote file")
        return copy.deepcopy(self.files[fid]["meta"])

    def folder(self, fid, name, parent=None, props=None):
        self.files.setdefault(
            fid,
            {
                "data": b"",
                "meta": {
                    "id": fid,
                    "name": name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent] if parent else [],
                    "appProperties": props or {},
                    "trashed": False,
                },
            },
        )
        return self.get(fid)

    def children(self, folder):
        return [
            self.get(fid)
            for fid, item in self.files.items()
            if folder in item["meta"]["parents"] and not item["meta"]["trashed"]
        ]

    def list(self, q):
        import re

        result = []
        properties = re.findall(r"key='([^']+)' and value='([^']+)'", q)
        parent = re.search(r"'([^']+)' in parents", q)
        name = re.search(r"name = '([^']+)'", q)
        mime = re.search(r"mimeType = '([^']+)'", q)
        for fid, item in self.files.items():
            meta = item["meta"]
            if meta["trashed"]:
                continue
            if parent and parent[1] not in meta["parents"]:
                continue
            if name and meta["name"] != name[1]:
                continue
            if mime and meta["mimeType"] != mime[1]:
                continue
            if any(meta.get("appProperties", {}).get(k) != v for k, v in properties):
                continue
            result.append(self.get(fid))
        return result

    def iter_files(self, q):
        yield from self.list(q)

    def trash(self, fid):
        self.files[fid]["meta"]["trashed"] = True
        return self.get(fid)

    def upload(self, fid, path, name, parent, mime, props):
        data = Path(path).read_bytes()
        if fid not in self.files:
            if mime == "application/pdf":
                self.uploaded_blobs += 1
            self.files[fid] = {
                "data": data,
                "meta": {
                    "id": fid,
                    "name": name,
                    "mimeType": mime,
                    "parents": [parent],
                    "size": str(len(data)),
                    "sha256Checksum": hashlib.sha256(data).hexdigest(),
                    "appProperties": props,
                    "trashed": False,
                },
            }
        else:
            self.verify(fid, hashlib.sha256(data).hexdigest(), len(data))
        return self.get(fid)

    def upload_json(self, fid, value, name, parent, props):
        if props.get("kind") == "record" and self.fail_record_once:
            self.fail_record_once = False
            raise VaultError("Record upload failed", 3)
        from tempfile import TemporaryDirectory

        from papervault.core import encode

        with TemporaryDirectory() as td:
            path = Path(td) / "body.json"
            path.write_bytes(encode(value))
            self.upload(fid, path, name, parent, "application/json", props)
        if props.get("kind") == "record" and self.lose_record_response:
            self.lose_record_response = False
            raise VaultError("Response lost after remote commit", 3)

    def read_json(self, fid):
        if self.get(fid)["trashed"]:
            raise VaultError("Trashed")
        return json.loads(self.files[fid]["data"])

    def download(self, fid, sink=None, expected=None):
        self.download_count += 1
        if self.get(fid)["trashed"]:
            raise VaultError("Trashed")
        data = b"corrupt" if self.corrupt_download else self.files[fid]["data"]
        h = hashlib.sha256(data).hexdigest()
        if sink:
            sink.write(data)
        if expected and h != expected:
            raise VaultError("Hash mismatch")
        return {"sha256": h, "size": len(data)}

    def verify(self, fid, sha, size, full=False):
        meta = self.get(fid)
        if meta["trashed"] or int(meta["size"]) != size or meta["sha256Checksum"] != sha:
            raise VaultError("Remote mismatch")
        if full:
            self.download(fid, expected=sha)
        return "download-sha256" if full else "drive-sha256"


@pytest.fixture
def env(tmp_path):
    state = State(tmp_path / "state")
    state.save()
    drive = FakeDrive()
    provider = Provider()
    vault = Vault(state, drive=drive, provider=provider)
    vault.init()
    return vault, drive, provider


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "paper.pdf"
    c = canvas.Canvas(str(p))
    c.setTitle(TITLE)
    c.drawString(40, 780, TITLE)
    c.drawString(40, 760, "Jane Smith - 2024")
    c.drawString(40, 740, "https://doi.org/" + DOI)
    c.drawString(40, 700, "Abstract: archival integrity of scholarly documents.")
    c.save()
    return p
