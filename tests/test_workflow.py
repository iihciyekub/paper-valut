import copy
import json
from pathlib import Path

import pytest
from conftest import DOI, metadata

from papervault.core import VaultError, digest, uid
from papervault.index import heads
from papervault.service import Vault


def test_archive_dedupe_download_rebuild(env, pdf, tmp_path):
    v, drive, _ = env
    j = v.import_file(pdf, "batch")
    assert j["status"] == "archived", j
    assert pdf.exists()
    assert not Path(j["snapshot"]).exists()
    assert drive.uploaded_blobs == 1
    record = v.one(DOI)
    entry = record["files"][0]
    sidecar = drive.get(entry["csl_file_id"])
    assert sidecar["name"] == entry["sha256"] + ".csl.json"
    assert sidecar["parents"] == [drive.get(entry["drive_file_id"])["parents"][0]]
    assert sidecar["appProperties"] == {
        "vault_id": record["vault_id"],
        "kind": "csl",
        "sha256": entry["sha256"],
        "schema": "1",
    }
    csl = drive.read_json(entry["csl_file_id"])
    assert csl["id"] == DOI and csl["DOI"] == DOI and csl["title"] == record["metadata"]["csl"]["title"]
    again = v.import_file(pdf, "batch")
    assert again["duplicate"] and drive.uploaded_blobs == 1
    assert len(v.index.all()) == 1
    out = v.get(DOI, tmp_path / "download")
    assert digest(Path(out["path"])) == digest(pdf)
    with v.index.db:
        v.index.db.execute("DELETE FROM works")
        v.index.db.execute("DELETE FROM commits")
    assert v.sync()["works"] == 1
    assert v.one(DOI)["files"][0]["sha256"] == digest(pdf)


def test_pdf_versions_preserve_primary(env, pdf):
    v, drive, _ = env
    first = v.import_file(pdf, "batch")
    # A trailing PDF comment changes the original bytes without altering its title.
    with pdf.open("ab") as f:
        f.write(b"\n% annotation variant\n")
    second = v.import_file(pdf, "batch", role="annotated", keep_version=True)
    assert second["status"] == "archived", second
    r = v.one(DOI)
    assert len(r["files"]) == 2 and drive.uploaded_blobs == 2
    assert r["primary_sha256"] == first["sha256"]
    v.revise(DOI, primary=second["sha256"])
    assert v.one(DOI)["primary_sha256"] == second["sha256"]
    assert v.sync()["records"] == 3


@pytest.mark.parametrize("fault", ["fail_record_once", "lose_record_response"])
def test_failed_record_or_lost_response_recovery(env, pdf, fault):
    v, drive, _ = env
    setattr(drive, fault, True)
    j = v.import_file(pdf, "batch")
    assert j["status"] == "retryable_error"
    assert pdf.exists() and Path(j["snapshot"]).exists()
    recovered = v.run_job(v.index.job(j["job_id"]))
    assert recovered["status"] == "archived", recovered
    assert drive.uploaded_blobs == 1
    assert len(v.one(DOI)["files"]) == 1


def test_crossref_outage_not_rejection(env, pdf):
    v, _, provider = env
    provider.fail = True
    j = v.import_file(pdf, "batch")
    assert j["status"] == "retryable_error" and pdf.exists()
    provider.fail = False
    assert v.run_job(j)["status"] == "archived"


def test_same_hash_other_doi_blocked(env, pdf):
    v, _, provider = env
    v.import_file(pdf, "batch")
    other = "10.1234/other"
    provider.entries[other] = metadata(other)
    j = v.make_job(pdf, "batch", other)
    v.index.save_job(j)
    j = v.run_job(j, manual_doi=other, reason="Explicitly reviewed")
    assert j["status"] == "blocked"
    assert len(v.index.all()) == 1


def test_corrupt_download_never_published(env, pdf, tmp_path):
    v, drive, _ = env
    v.import_file(pdf, "batch")
    drive.corrupt_download = True
    with pytest.raises(VaultError):
        v.get(DOI, tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []


def test_download_does_not_overwrite(env, pdf, tmp_path):
    v, _, _ = env
    j = v.import_file(pdf, "batch")
    target = tmp_path / (j["sha256"] + ".pdf")
    target.write_bytes(b"USER CHANGES")
    with pytest.raises(VaultError):
        v.get(DOI, tmp_path)
    assert target.read_bytes() == b"USER CHANGES"


def test_release_checks_remote_and_modified_original(env, pdf, monkeypatch):
    v, drive, _ = env
    j = v.import_file(pdf, "batch")
    removed = []
    monkeypatch.setattr("papervault.service.send2trash", lambda p: removed.append(p))
    before = drive.download_count
    assert v.release("batch")[0]["action"] == "eligible"
    assert drive.download_count > before and not removed
    pdf.write_bytes(pdf.read_bytes() + b"\n% edited\n")
    assert v.release("batch", True)[0]["action"] == "skipped"
    assert not removed and pdf.exists()
    assert v.index.job(j["job_id"])["status"] == "archived"


def test_release_corrupt_remote_and_success(env, pdf, monkeypatch):
    v, drive, _ = env
    v.import_file(pdf, "batch")
    removed = []
    monkeypatch.setattr("papervault.service.send2trash", lambda p: removed.append(p))
    drive.corrupt_download = True
    assert v.release("batch", True)[0]["action"] == "skipped" and not removed
    drive.corrupt_download = False
    assert v.release("batch", True)[0]["action"] == "moved_to_trash"
    assert removed == [str(pdf)]


def test_metadata_refresh_replaces_sidecar_after_record_commit(env, pdf):
    v, drive, provider = env
    v.import_file(pdf, "batch")
    before = v.one(DOI)
    old_csl_id = before["files"][0]["csl_file_id"]
    provider.entries[DOI]["csl"]["container-title"] = "Updated Journal"
    v.revise(DOI, refresh=True)
    after = v.one(DOI)
    new_csl_id = after["files"][0]["csl_file_id"]
    assert new_csl_id != old_csl_id
    assert drive.get(old_csl_id)["trashed"]
    assert not drive.get(new_csl_id)["trashed"]
    assert drive.read_json(new_csl_id)["container-title"] == "Updated Journal"
    assert after["schema_version"] == 3


def test_metadata_refresh_record_failure_recovers_sidecars(env, pdf):
    v, drive, provider = env
    v.import_file(pdf, "batch")
    old = v.one(DOI)
    old_csl_id = old["files"][0]["csl_file_id"]
    provider.entries[DOI]["csl"]["container-title"] = "Recovered Journal"
    drive.fail_record_once = True
    with pytest.raises(VaultError):
        v.revise(DOI, refresh=True)
    assert not drive.get(old_csl_id)["trashed"]
    assert len(v.recover_operations()) == 1
    current = v.one(DOI)
    new_csl_id = current["files"][0]["csl_file_id"]
    assert new_csl_id != old_csl_id
    assert drive.get(old_csl_id)["trashed"]
    assert drive.read_json(new_csl_id)["container-title"] == "Recovered Journal"


def test_dry_run_no_cloud_mutation_or_jobs(env, pdf):
    v, drive, _ = env
    before = copy.deepcopy(drive.files)
    assert v.import_file(pdf, "batch", dry_run=True)["status"] == "accepted"
    assert drive.files == before and v.index.jobs() == []
    assert not list((v.state.home / "staging").iterdir())


def test_rebuild_rejects_branch_preserves_local_index(env, pdf):
    v, drive, _ = env
    v.import_file(pdf, "batch")
    root = v.one(DOI)
    for _ in range(2):
        r = copy.deepcopy(root)
        r.update(commit_id=uid(), parent_commit_id=root["commit_id"], record_file_id=drive.new_id())
        drive.upload_json(
            r["record_file_id"], r, "record.json", v.state.config["vault"]["records_id"], {"kind": "record"}
        )
    with pytest.raises(VaultError):
        v.sync()
    assert v.one(DOI)["commit_id"] == root["commit_id"]


def test_missing_parent_and_bad_schema(env, pdf):
    v, _, _ = env
    v.import_file(pdf, "batch")
    r = v.one(DOI)
    r["parent_commit_id"] = uid()
    with pytest.raises(VaultError):
        heads([r], r["vault_id"])
    r["parent_commit_id"] = None
    r["files"][0]["sha256"] = "../escape"
    with pytest.raises(VaultError):
        heads([r], r["vault_id"])


def test_readonly_new_device(env, tmp_path):
    v, drive, provider = env
    from papervault.core import State

    state = State(tmp_path / "another-device")
    state.save()
    other = Vault(state, drive, provider)
    result = other.connect(v.state.config["vault"]["config_id"])
    assert result["read_only"]
    with pytest.raises(VaultError):
        other.check_writer()


def test_modified_cache_is_never_evicted(env, tmp_path):
    v, _, _ = env
    folder = v.state.home / "cache"
    folder.mkdir()
    p = folder / ("a" * 64 + ".pdf")
    p.write_bytes(b"original")
    sha = digest(p)
    v.index.put_cache({"path": str(p), "sha256": sha, "last_used": "2026", "pinned": True})
    p.write_bytes(b"USER NOTES")
    v.state.config["cache_limit_mb"] = 0
    result = v.prune_cache(unpin=True)
    assert p.exists() and result["items"][0]["status"] == "modified_preserved"


def test_trashed_remote_prevents_cleanup(env, pdf, monkeypatch):
    v, drive, _ = env
    j = v.import_file(pdf, "batch")
    drive.files[j["blob_id"]]["meta"]["trashed"] = True
    monkeypatch.setattr("papervault.service.send2trash", lambda p: pytest.fail("must not trash"))
    assert v.release("batch", True)[0]["action"] == "skipped"


def test_lost_response_can_recover_after_later_revision(env, pdf):
    v, drive, _ = env
    drive.lose_record_response = True
    j = v.import_file(pdf, "batch")
    assert j["status"] == "retryable_error"
    v.revise(DOI, primary=j["sha256"])
    result = v.run_job(j)
    assert result["status"] == "archived" and "error" not in result
    assert drive.uploaded_blobs == 1


def test_cached_record_rechecked_when_remote_bytes_change(env, pdf):
    v, drive, _ = env
    v.import_file(pdf, "batch")
    old = v.one(DOI)
    fid = old["record_file_id"]
    import hashlib

    changed = copy.deepcopy(old)
    changed["doi"] = "10.1234/unrelated"
    data = json.dumps(changed).encode()
    drive.files[fid]["data"] = data
    drive.files[fid]["meta"]["sha256Checksum"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(VaultError):
        v.sync()
    assert v.one(DOI)["doi"] == DOI
