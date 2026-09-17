import pytest
from conftest import DOI, TITLE, Provider, metadata
from reportlab.pdfgen import canvas

from papervault.core import VaultError
from papervault.extract import extract
from papervault.metadata import doi_candidates, identify, normalize_doi, validate_metadata


def test_doi_normalization_and_punctuation():
    assert normalize_doi("https://doi.org/10.1234%2FABC") == "10.1234/abc"
    assert normalize_doi(" DOI: 10.1234/AbC ") == "10.1234/abc"
    assert normalize_doi("10.1234/example(a)") == "10.1234/example(a)"
    assert doi_candidates("See doi:10.1234/ABC).") == ["10.1234/abc).", "10.1234/abc"]
    with pytest.raises(VaultError):
        normalize_doi("https://evil.test/paper")


def test_reference_doi_excluded(tmp_path):
    p = tmp_path / "references.pdf"
    c = canvas.Canvas(str(p))
    c.setTitle("An unrelated paper title with no DOI match")
    c.drawString(40, 780, "An unrelated paper title with no DOI match")
    c.drawString(40, 760, "References")
    c.drawString(40, 740, DOI)
    c.save()
    e = extract(p)
    assert DOI not in e["dois"]
    assert identify(e, Provider())["status"] == "needs_review"


def test_explicit_doi_does_not_bypass_pdf_match():
    e = {
        "first_page": "A completely unrelated article",
        "titles": ["A completely unrelated article"],
        "dois": [],
    }
    assert identify(e, Provider(), DOI)["status"] == "needs_review"
    assert identify(e, Provider(), DOI, "Compared title manually")["status"] == "accepted"


def test_metadata_must_have_crossref_required_fields():
    m = metadata()
    m["csl"]["DOI"] = "10.1234/wrong"
    with pytest.raises(VaultError):
        validate_metadata(DI := DOI, m["raw"], m["csl"])
    m = metadata()
    m["csl"]["author"] = []
    with pytest.raises(VaultError):
        validate_metadata(DI, m["raw"], m["csl"])


def test_ambiguous_multiple_dois_need_review():
    provider = Provider()
    other = "10.1234/second"
    provider.entries[other] = metadata(other)
    e = {"first_page": TITLE + " Jane Smith 2024", "titles": [TITLE], "dois": [DOI, other]}
    assert identify(e, provider)["status"] == "needs_review"


def test_invalid_pdf_rejected(tmp_path):
    p = tmp_path / "notpdf.pdf"
    p.write_text("<html>Publisher login page</html>")
    with pytest.raises(VaultError):
        extract(p)


def test_actual_crossref_type_mapping_preserves_provider():
    from papervault.metadata import normalize_csl

    provider = {
        "DOI": DOI,
        "type": "journal-article",
        "title": TITLE,
        "author": [{"given": "Jane", "family": "Smith", "ORCID": "https://orcid.org/example"}],
        "issued": {"date-parts": [[2024]]},
        "container-title": ["Journal of Examples"],
        "reference": [{"DOI": "10.1234/cited"}],
    }
    csl = normalize_csl(provider)
    assert csl["type"] == "article-journal"
    assert provider["type"] == "journal-article"
    assert csl["container-title"] == "Journal of Examples"
    assert "reference" not in csl and "ORCID" not in csl["author"][0]
    validate_metadata(DOI, metadata()["raw"], csl)
