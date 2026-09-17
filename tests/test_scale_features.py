import pytest
from conftest import DOI, TITLE, metadata

from papervault.core import VaultError


def test_duplicate_bypasses_parser_crossref_and_full_scan(env, pdf, monkeypatch):
    v, drive, provider = env
    v.import_file(pdf, "first")
    provider.fail = True
    monkeypatch.setattr("papervault.service.extract", lambda *a: pytest.fail("duplicate was parsed"))
    monkeypatch.setattr(v, "sync", lambda: pytest.fail("full cloud scan"))
    monkeypatch.setattr(v.index, "all", lambda: pytest.fail("full local scan"))
    j = v.import_file(pdf, "second")
    assert j["status"] == "archived" and j["skipped_processing"]
    assert drive.uploaded_blobs == 1


def test_new_doi_hot_path_never_scans_entire_library(env, pdf, monkeypatch):
    v, _, _ = env
    monkeypatch.setattr(v, "sync", lambda: pytest.fail("full cloud scan"))
    monkeypatch.setattr(v.index, "all", lambda: pytest.fail("full local scan"))
    assert v.import_file(pdf, "first")["status"] == "archived"


def test_default_same_doi_different_bytes_skips(env, pdf):
    v, drive, _ = env
    first = v.import_file(pdf, "batch")
    pdf.write_bytes(pdf.read_bytes() + b"\n% different bytes\n")
    j = v.import_file(pdf, "batch")
    assert j["status"] == "skipped_existing_doi" and drive.uploaded_blobs == 1
    assert v.one(DOI)["files"][0]["sha256"] == first["sha256"]
    assert v.release("batch")[1]["action"] == "skipped"


def test_force_same_bytes_reuploads_then_trashes_old(env, pdf):
    v, drive, _ = env
    old = v.import_file(pdf, "batch")
    new = v.import_file(pdf, "batch", force=True)
    assert new["status"] == "archived", new
    assert drive.uploaded_blobs == 2
    assert drive.get(old["blob_id"])["trashed"]
    assert not drive.get(new["blob_id"])["trashed"]
    assert new["sha256"] == old["sha256"] and new["blob_id"] != old["blob_id"]
    current = v.one(DOI)
    assert len(current["files"]) == 1
    assert drive.get(current["files"][0]["csl_file_id"])["trashed"] is False
    assert v.sync()["works"] == 1


def test_failed_replace_does_not_trash_old_and_can_resume(env, pdf):
    v, drive, _ = env
    old = v.import_file(pdf, "batch")
    pdf.write_bytes(pdf.read_bytes() + b"\n% replacement\n")
    drive.fail_record_once = True
    j = v.import_file(pdf, "batch", force=True)
    assert j["status"] == "retryable_error"
    assert not drive.get(old["blob_id"])["trashed"]
    j = v.run_job(v.index.job(j["job_id"]))
    assert j["status"] == "archived", j
    assert drive.get(old["blob_id"])["trashed"]
    assert drive.uploaded_blobs == 2


def test_remove_preview_and_tombstone_rebuild(env, pdf):
    v, drive, _ = env
    j = v.import_file(pdf, "batch")
    before = len(drive.files)
    sidecar_id = v.one(DOI)["files"][0]["csl_file_id"]
    plan = v.remove(DOI)
    assert not plan["apply"] and len(drive.files) == before
    v.remove(DOI, apply=True)
    assert drive.get(j["blob_id"])["trashed"]
    assert drive.get(sidecar_id)["trashed"]
    assert v.index.find(DOI) == []
    assert v.sync()["works"] == 0
    assert v.index.find(DOI) == []
    with pytest.raises(VaultError):
        v.get(DOI, pdf.parent / "out")


def test_remove_trash_failure_is_recoverable(env, pdf, monkeypatch):
    v, drive, _ = env
    j = v.import_file(pdf, "batch")
    original = drive.trash

    def fail(fid):
        raise VaultError("simulated trash timeout", 3)

    monkeypatch.setattr(drive, "trash", fail)
    with pytest.raises(VaultError):
        v.remove(DOI, apply=True)
    assert v.index.find(DOI) == []
    assert not drive.get(j["blob_id"])["trashed"]
    monkeypatch.setattr(drive, "trash", original)
    assert len(v.recover_operations()) == 1
    assert drive.get(j["blob_id"])["trashed"]


def test_remove_one_version_keeps_other(env, pdf):
    v, drive, _ = env
    first = v.import_file(pdf, "batch")
    pdf.write_bytes(pdf.read_bytes() + b"\n% version two\n")
    second = v.import_file(pdf, "batch", keep_version=True)
    v.remove(DOI, sha=first["sha256"], apply=True)
    assert len(v.one(DOI)["files"]) == 1
    assert not drive.get(second["blob_id"])["trashed"]


def test_text_resolution_and_ambiguity(env, pdf):
    v, _, provider = env
    v.import_file(pdf, "batch")
    assert v.resolve_doi(TITLE) == DOI
    other = "10.1234/other"
    provider.entries[other] = metadata(other)
    with pytest.raises(VaultError):
        v.resolve_doi(TITLE)
    assert v.resolve_doi(TITLE, selected=DOI) == DOI
    with pytest.raises(VaultError):
        v.resolve_doi("Vague citation fragment")


def test_sharded_layout_and_legacy_rebuild(env, pdf):
    v, drive, _ = env
    j = v.import_file(pdf, "batch")
    blob = drive.get(j["blob_id"])
    parent = drive.get(blob["parents"][0])
    assert parent["name"] == j["sha256"][:2]
    assert parent["parents"] == [v.state.config["vault"]["objects_id"]]
    r = v.one(DOI)
    record = drive.get(r["record_file_id"])
    assert record["parents"] != [v.state.config["vault"]["records_id"]]
    # v0.1 flat records remain readable during explicit full rebuild.
    drive.files[r["record_file_id"]]["meta"]["parents"] = [v.state.config["vault"]["records_id"]]
    assert v.sync()["works"] == 1


def test_reimport_removed_doi_creates_live_successor(env, pdf):
    v, _, _ = env
    v.import_file(pdf, "batch")
    v.remove(DOI, apply=True)
    new = v.import_file(pdf, "batch")
    assert new["status"] == "archived", new
    assert not v.one(DOI).get("deleted")
    assert v.sync()["works"] == 1
