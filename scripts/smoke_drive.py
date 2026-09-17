"""Real Drive transport test. Isolated scratch folder is moved to Trash afterwards.

No scholarly record is added to the library. Run with `uv run python scripts/smoke_drive.py`.
"""

import json
import tempfile
from pathlib import Path

from reportlab.pdfgen import canvas

from papervault.core import State, digest, now
from papervault.drive import BASE, Drive

state = State()
with state.lock():
    v = state.require_vault()
    drive = Drive(state)
    folder = drive.new_id()
    drive.folder(folder, "PaperVault transport self-test (temporary)", v["root_id"], {"kind": "selftest"})
    try:
        with tempfile.TemporaryDirectory(prefix="pv-smoke-") as td:
            p = Path(td) / "transport-test.pdf"
            c = canvas.Canvas(str(p))
            c.setTitle("PaperVault transport self-test - not a scholarly article")
            c.drawString(40, 780, "PaperVault transport self-test")
            c.drawString(40, 750, "Synthetic test file. Not a scholarly article.")
            c.save()
            # Force a multi-chunk upload; trailing PDF comments are inert test padding.
            with p.open("ab") as f:
                f.write(b"\n%" + b"x" * (5 * 1024 * 1024) + b"\n")
            sha = digest(p)
            fid = drive.new_id()
            drive.upload(fid, p, sha + ".pdf", folder, "application/pdf", {"kind": "selftest", "sha256": sha})
            drive.verify(fid, sha, p.stat().st_size, full=True)
            downloaded = Path(td) / "downloaded.pdf"
            with downloaded.open("wb") as sink:
                drive.download(fid, sink, expected=sha)
            assert digest(downloaded) == sha
            # Repeat the same upload ID: must reuse the object and verify it.
            drive.upload(fid, p, sha + ".pdf", folder, "application/pdf", {"kind": "selftest", "sha256": sha})
            rid = drive.new_id()
            record = {"purpose": "transport self-test only", "sha256": sha, "file_id": fid}
            drive.upload_json(rid, record, "transport-test.json", folder, {"kind": "selftest"})
            assert drive.read_json(rid) == record
            assert len(drive.children(folder)) == 2
            report = {
                "at": now(),
                "ok": True,
                "pdf_size": p.stat().st_size,
                "checks": ["multi_chunk_upload", "download_sha256", "idempotent_upload", "json_readback"],
                "scratch_folder_id": folder,
            }
    finally:
        drive.request("PATCH", BASE + "/files/" + folder, json={"trashed": True})
    report["scratch_moved_to_trash"] = drive.get(folder)["trashed"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
