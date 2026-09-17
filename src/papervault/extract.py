from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from .core import VaultError
from .metadata import doi_candidates


def extract(path: Path):
    try:
        result = subprocess.run(
            [sys.executable, "-m", "papervault.extract", str(path)],
            capture_output=True,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise VaultError("PDF 解析超过 45 秒，需人工检查。", 2) from None
    if result.returncode:
        raise VaultError("PDF 无法解析、已加密或不是有效 PDF。", 2)
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise VaultError("PDF 提取结果无效。", 2) from None


def worker(path):
    from pypdf import PdfReader

    with open(path, "rb") as f:
        if not f.read(1024).lstrip().startswith(b"%PDF-"):
            raise ValueError("Not PDF")
    reader = PdfReader(path, strict=False)
    if reader.is_encrypted or len(reader.pages) > 2000:
        raise ValueError("Encrypted or too many pages")
    pages = [(p.extract_text() or "")[:30000] for p in reader.pages[:3]]
    if not pages:
        raise ValueError("No pages")
    # Ignore reference lists even when they appear on an early page.
    pages = [re.split(r"(?im)^\s*(?:references|bibliography|参考文献)\s*$", p)[0] for p in pages]
    first = pages[0]
    meta = reader.metadata or {}
    metadata_text = "\n".join(str(v)[:2000] for v in meta.values())
    xmp_titles = []
    try:
        xmp = reader.xmp_metadata
        if xmp:
            xmp_titles = list((xmp.dc_title or {}).values())
            metadata_text += "\n" + str(xmp.dc_identifier or "") + "\n" + str(xmp.pdf_keywords or "")
    except (ValueError, AttributeError):
        pass  # Malformed optional XMP must not hide otherwise readable first-page evidence.
    links = []
    for page in reader.pages[:2]:
        for annotation in page.get("/Annots", []):
            try:
                link = annotation.get_object().get("/A", {}).get("/URI", "")
                if "doi.org/" in str(link):
                    links.append(str(link))
            except (AttributeError, TypeError):
                continue
    title = str(meta.get("/Title", "")).strip()
    titles = []
    if 15 < len(title) < 600 and not re.search(r"(?i)untitled|microsoft word|\.pdf$", title):
        titles.append(title)
    for candidate in xmp_titles:
        if isinstance(candidate, str) and 15 < len(candidate) < 600 and candidate not in titles:
            titles.append(candidate)
    lines = [s.strip() for s in first.splitlines() if len(s.strip()) > 12]
    for i, line in enumerate(lines[:18]):
        if re.search(r"(?i)https?://|doi:|copyright|issn|all rights|www\.", line):
            continue
        for n in (1, 2, 3):
            candidate = " ".join(lines[i : i + n])
            if 20 < len(candidate) < 400 and candidate not in titles:
                titles.append(candidate)
    return {
        "first_page": first[:16000],
        "titles": titles[:35],
        "dois": doi_candidates(metadata_text + "\n" + "\n".join(pages) + "\n" + "\n".join(links)),
        "pages": len(reader.pages),
        "has_text": len(first.strip()) > 40,
    }


if __name__ == "__main__":
    try:
        # Limit CPU even if the parent disappears; wall-clock timeout lives in the parent.
        if sys.platform != "win32":
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
        print(json.dumps(worker(sys.argv[1]), ensure_ascii=False))
    except Exception:  # noqa: BLE001 -- Isolated parser reports a bounded failure to its parent.
        sys.exit(2)
