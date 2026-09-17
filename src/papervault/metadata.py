from __future__ import annotations

import hashlib
import html
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote, unquote, urlparse

import requests

from .core import VaultError, atomic_write, encode, now, read_json

DOI_RE = re.compile(r'10\.\d{4,9}/[^\s<>"\u201c\u201d]+', re.IGNORECASE)


def normalize_doi(value):
    s = str(value).strip()
    if s.lower().startswith(
        ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/")
    ):
        s = unquote(urlparse(s).path.lstrip("/"))
    s = re.sub(r"^doi\s*:\s*", "", s, flags=re.IGNORECASE).strip().lower()
    if not DOI_RE.fullmatch(s) or len(s) > 2048:
        raise VaultError("无效 DOI，请输入 10.xxxx/… 或 doi.org 链接。", 2)
    return s


def doi_key(value):
    return hashlib.sha256(normalize_doi(value).encode()).hexdigest()


def text_key(value):
    s = unicodedata.normalize("NFKC", html.unescape(re.sub(r"<[^>]+>", "", str(value)))).casefold()
    return "".join(c for c in s if c.isalnum())


def doi_candidates(text):
    results = []
    for match in DOI_RE.finditer(text):
        raw = match.group(0)
        # Keep the raw DOI, and test a conservative punctuation variant separately.
        candidates = [raw]
        trimmed = raw.rstrip(".,;")
        if trimmed.endswith(")") and trimmed.count(")") > trimmed.count("("):
            trimmed = trimmed[:-1]
        if trimmed != raw:
            candidates.append(trimmed)
        for s in candidates:
            try:
                d = normalize_doi(s)
                if d not in results:
                    results.append(d)
            except VaultError:
                pass
    return results[:12]


def title_of(raw):
    t = raw.get("title") or []
    return t[0] if isinstance(t, list) and t else str(t) if isinstance(t, str) else ""


def normalize_csl(provider):
    """Crossref currently returns journal-article even from its CSL transform route.

    Preserve the exact provider response separately; normalize only the export schema.
    """
    if not isinstance(provider, dict):
        raise VaultError("Crossref CSL 响应不是 JSON 对象。", 2)
    fields = {
        "id",
        "type",
        "DOI",
        "title",
        "title-short",
        "container-title",
        "container-title-short",
        "author",
        "editor",
        "translator",
        "issued",
        "accessed",
        "original-date",
        "submitted",
        "page",
        "volume",
        "issue",
        "number",
        "publisher",
        "publisher-place",
        "URL",
        "ISSN",
        "ISBN",
        "abstract",
        "language",
        "note",
        "edition",
        "event-title",
        "event-place",
    }
    result = {k: v for k, v in provider.items() if k in fields}
    if result.get("type") == "journal-article":
        result["type"] = "article-journal"
    for key in ("title", "container-title", "container-title-short", "ISSN", "ISBN"):
        if isinstance(result.get(key), list):
            result[key] = "; ".join(str(x) for x in result[key])
    name_fields = {
        "family",
        "given",
        "literal",
        "dropping-particle",
        "non-dropping-particle",
        "suffix",
        "comma-suffix",
        "static-ordering",
    }
    for key in ("author", "editor", "translator"):
        if key in result:
            result[key] = [
                {k: v for k, v in n.items() if k in name_fields} for n in result[key] if isinstance(n, dict)
            ]
    return result


def validate_metadata(doi, raw, csl):
    if not isinstance(raw, dict) or not isinstance(csl, dict):
        raise VaultError("Crossref 返回数据结构不正确。", 2)
    if normalize_doi(raw.get("DOI", "")) != doi or normalize_doi(csl.get("DOI", "")) != doi:
        raise VaultError("Crossref 原始元数据与 CSL DOI 不一致。", 2)
    if not title_of(raw) or not csl.get("title") or not raw.get("author") or not csl.get("author"):
        raise VaultError("元数据缺少标题或作者，严格模式不予归档。", 2)
    if raw.get("type") != "journal-article" or csl.get("type") != "article-journal":
        raise VaultError("首版严格模式仅收录期刊论文（journal-article）。", 2)
    dates = csl.get("issued", {}).get("date-parts", [])
    if not dates or not dates[0] or not isinstance(dates[0][0], int):
        raise VaultError("CSL 缺少有效出版年份。", 2)


class Crossref:
    def __init__(self, state, session=None):
        self.state = state
        self.session = session or requests.Session()
        self.cache = state.home / "crossref"
        self.cache.mkdir(exist_ok=True, mode=0o700)
        self.session.headers["User-Agent"] = "PaperVault/0.1 (personal PDF archive)"
        self.mailto = state.config.get("mailto")
        self.last = 0.0

    def get(self, path, params=None, accept=None):
        time.sleep(max(0, 0.25 - (time.monotonic() - self.last)))
        params = dict(params or {})
        if self.mailto:
            params["mailto"] = self.mailto
        for attempt in range(4):
            self.last = time.monotonic()
            try:
                r = self.session.get(
                    "https://api.crossref.org" + path,
                    params=params,
                    headers={"Accept": accept or "application/json"},
                    timeout=(10, 40),
                )
            except requests.RequestException:
                if attempt == 3:
                    raise VaultError("Crossref 网络请求失败，请稍后重试。", 3) from None
                time.sleep(2**attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == 3:
                    raise VaultError("Crossref 限流或暂时不可用，请重试。", 3)
                delay = r.headers.get("Retry-After", "")
                time.sleep(min(int(delay) if delay.isdigit() else 2**attempt, 30))
                continue
            if r.status_code != 200:
                raise VaultError(f"Crossref 返回 HTTP {r.status_code}。", 3)
            try:
                return r.json()
            except ValueError:
                raise VaultError("Crossref 返回的内容不是有效 JSON。", 3) from None

    def lookup(self, value, refresh=False):
        doi = normalize_doi(value)
        key = doi_key(doi)
        legacy = self.cache / (key + ".json")
        p = legacy if legacy.exists() else self.cache / key[:2] / (key + ".json")
        data = read_json(p)
        if not refresh and data and time.time() - p.stat().st_mtime < 30 * 86400:
            validate_metadata(doi, data["raw"], data["csl"])
            return data
        path = "/works/" + quote(doi, safe="")
        response = self.get(path)
        if response is None:
            raise VaultError("Crossref 未收录该 DOI，或 DOI 提取有误。", 2)
        raw = response.get("message", {})
        csl_provider = self.get(
            path + "/transform/application/vnd.citationstyles.csl+json",
            accept="application/vnd.citationstyles.csl+json",
        )
        csl = normalize_csl(csl_provider)
        validate_metadata(doi, raw, csl)
        data = {
            "doi": doi,
            "raw": raw,
            "csl": csl,
            "csl_provider": csl_provider,
            "csl_normalization_version": 1,
            "retrieved_at": now(),
            "source_url": "https://api.crossref.org" + path,
        }
        atomic_write(p, encode(data))
        return data

    def search(self, query):
        result = self.get("/works", {"query.bibliographic": query, "rows": 5})
        return (result or {}).get("message", {}).get("items", [])


def match_evidence(extraction, raw):
    title = text_key(title_of(raw))
    page = text_key(extraction.get("first_page", ""))
    titles = extraction.get("titles", [])
    ratio = max((SequenceMatcher(None, title, text_key(t)).ratio() for t in titles), default=0)
    # Long, exact first-page titles are the primary automatic signal; fuzzy scores alone never suffice.
    exact = len(title) >= 30 and title in page
    author = any(
        text_key(a.get("family", "")) in page
        for a in raw.get("author", [])
        if len(text_key(a.get("family", ""))) >= 3
    )
    years = {
        str(d["date-parts"][0][0])
        for k in ("issued", "published", "published-print", "published-online")
        if isinstance((d := raw.get(k)), dict) and d.get("date-parts") and d["date-parts"][0]
    }
    year = any(y in extraction.get("first_page", "") for y in years)
    accepted = exact and author and (year or ratio >= 0.98)
    return {
        "automatic_accept": accepted,
        "title_exact_on_first_page": exact,
        "title_similarity": round(ratio, 4),
        "author_on_first_page": author,
        "year_on_first_page": year,
        "rule_version": 1,
    }


def identify(extraction, provider, supplied_doi=None, manual_reason=None):
    explicit = normalize_doi(supplied_doi) if supplied_doi else None
    candidates = [explicit] if explicit else extraction.get("dois", [])
    evaluated = []
    invalid = []
    seen = set()

    def evaluate(doi):
        if doi in seen:
            return
        seen.add(doi)
        try:
            meta = provider.lookup(doi)
        except VaultError as e:
            if e.code != 2:
                raise
            invalid.append({"doi": doi, "reason": str(e)})
            return
        evidence = match_evidence(extraction, meta["raw"])
        evaluated.append((meta, evidence))

    for d in candidates[:12]:
        evaluate(d)
    if not explicit and not any(e["automatic_accept"] for _, e in evaluated):
        titles = extraction.get("titles", [])
        if titles:
            for raw in provider.search(titles[0]):
                try:
                    evaluate(normalize_doi(raw.get("DOI", "")))
                except VaultError as e:
                    if e.code != 2:
                        raise
    acceptable = [(m, e) for m, e in evaluated if e["automatic_accept"]]
    summaries = [{"doi": m["doi"], "title": title_of(m["raw"]), **e} for m, e in evaluated]
    if manual_reason and explicit and evaluated:
        meta, ev = evaluated[0]
        decision = {**ev, "method": "manual", "reason": manual_reason, "at": now()}
        return {"status": "accepted", "metadata": meta, "decision": decision, "candidates": summaries}
    if len(acceptable) == 1:
        meta, ev = acceptable[0]
        # Another valid first-page DOI is a conflict, even if its title extraction is weaker.
        extracted_valid = [m["doi"] for m, _ in evaluated if m["doi"] in extraction.get("dois", [])]
        if len(set(extracted_valid)) <= 1:
            return {
                "status": "accepted",
                "metadata": meta,
                "decision": {**ev, "method": "automatic", "at": now()},
                "candidates": summaries,
            }
    return {
        "status": "needs_review",
        "reason": "需核对 PDF 与 DOI 的对应关系，或元数据不满足严格准入条件。",
        "candidates": summaries,
        "invalid_candidates": invalid,
    }
