import hashlib

import pytest

from papervault.core import State, VaultError, atomic_write, encode
from papervault.drive import Drive


class Response:
    def __init__(self, status=200, data=None, headers=None, content=b""):
        self.status_code = status
        self.payload = data
        self.headers = headers or {}
        self.content = content

    def json(self):
        return self.payload

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def iter_content(self, n):
        yield self.content


class ScriptedSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def file_meta(data, checksum=True):
    m = {"id": "file-id", "size": str(len(data)), "trashed": False}
    if checksum:
        m["sha256Checksum"] = hashlib.sha256(data).hexdigest()
    return m


def test_upload_resumes_saved_offset(tmp_path):
    state = State(tmp_path / "home")
    data = b"abcdefgh"
    p = tmp_path / "paper.pdf"
    p.write_bytes(data)
    url = "https://www.googleapis.com/upload/drive/v3/files?upload_id=SESSION"
    session = ScriptedSession(
        [
            Response(404),
            Response(308, headers={"Range": "bytes=0-3"}),
            Response(200, file_meta(data)),
            Response(200, file_meta(data)),
            Response(200, file_meta(data)),
        ]
    )
    d = Drive(state, session)
    atomic_write(
        d.sessions / "file-id.json",
        encode({"url": url, "sha256": hashlib.sha256(data).hexdigest(), "size": 8}),
    )
    d.upload("file-id", p, "hash.pdf", "parent", "application/pdf", {})
    assert session.calls[2][2]["data"] == b"efgh"
    assert session.calls[2][2]["headers"]["Content-Range"] == "bytes 4-7/8"
    assert not (d.sessions / "file-id.json").exists()


def test_upload_lost_chunk_response_probes_before_retry(tmp_path):
    import requests

    state = State(tmp_path / "home")
    data = b"abcdefgh"
    p = tmp_path / "p.pdf"
    p.write_bytes(data)
    url = "https://www.googleapis.com/upload/drive/v3/files?upload_id=SESSION"
    session = ScriptedSession(
        [
            Response(404),
            Response(200, headers={"Location": url}),
            Response(308),
            requests.ConnectionError("Lost response"),
            Response(200, file_meta(data)),
            Response(200, file_meta(data)),
            Response(200, file_meta(data)),
        ]
    )
    d = Drive(state, session)
    d.upload("file-id", p, "hash.pdf", "parent", "application/pdf", {})
    assert session.calls[4][2]["headers"]["Content-Range"] == "bytes */8"
    assert sum(c[0] == "POST" for c in session.calls) == 1


def test_missing_remote_checksum_downloads_for_verification(tmp_path):
    data = b"actual bytes"
    session = ScriptedSession(
        [
            Response(200, file_meta(data, False)),
            Response(200, file_meta(data, False)),
            Response(200, content=data),
        ]
    )
    d = Drive(State(tmp_path), session)
    assert d.verify("file-id", hashlib.sha256(data).hexdigest(), len(data)) == "download-sha256"
    assert session.calls[-1][2]["params"] == {"alt": "media"}


def test_metadata_property_hash_is_not_used_as_checksum(tmp_path):
    data = b"wrong bytes"
    meta = file_meta(data, False)
    meta["appProperties"] = {"sha256": "a" * 64}
    d = Drive(
        State(tmp_path),
        ScriptedSession([Response(200, meta), Response(200, meta), Response(200, content=data)]),
    )
    with pytest.raises(VaultError):
        d.verify("file-id", "a" * 64, len(data))


def test_pagination_and_incomplete_search(tmp_path):
    d = Drive(
        State(tmp_path),
        ScriptedSession(
            [
                Response(200, {"files": [{"id": "one"}], "nextPageToken": "next"}),
                Response(200, {"files": [{"id": "two"}]}),
            ]
        ),
    )
    assert [f["id"] for f in d.list("trashed = false")] == ["one", "two"]
    assert d.session.calls[1][2]["params"]["pageToken"] == "next"
    d.session = ScriptedSession([Response(200, {"files": [], "incompleteSearch": True})])
    with pytest.raises(VaultError):
        d.list("trashed = false")


def test_unsafe_session_url_never_receives_token(tmp_path):
    session = ScriptedSession([])
    d = Drive(State(tmp_path), session)
    with pytest.raises(VaultError):
        d.request("PUT", "https://attacker.example/upload")
    assert not session.calls


def test_existing_remote_id_is_verified_not_uploaded_again(tmp_path):
    data = b"bytes"
    p = tmp_path / "p.pdf"
    p.write_bytes(data)
    session = ScriptedSession([Response(200, file_meta(data)), Response(200, file_meta(data))])
    d = Drive(State(tmp_path / "state"), session)
    d.upload("file-id", p, "p.pdf", "parent", "application/pdf", {})
    assert all(c[0] == "GET" for c in session.calls)
