"""PDF extraction, on PDFs generated here with fpdf2: no third-party PDFs in the repository."""

import io
import re

import pytest
from fpdf import FPDF
from fpdf.enums import EncryptionMethod
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image
from pypdf import PdfWriter

from evfind.extract import extract_file, extract_pdf

IMAGE = "<image>"


def make_pdf(pages, *, owner_password=None, user_password=None) -> bytes:
    """A PDF with one page per item: text, None for a blank page, or IMAGE for a page that is only a picture."""
    pdf = FPDF()
    pdf.set_font("helvetica", size=11)
    for page in pages:
        pdf.add_page()
        if page == IMAGE:
            picture = io.BytesIO()
            Image.new("RGB", (200, 100), (40, 90, 160)).save(picture, format="PNG")
            pdf.image(picture, x=10, y=10, w=150)
        elif page:
            pdf.multi_cell(0, 6, page)
    if owner_password:
        pdf.set_encryption(owner_password=owner_password, user_password=user_password,
                           encryption_method=EncryptionMethod.RC4)
    return bytes(pdf.output())


def save(tmp_path, content: bytes, name="doc.pdf"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


PAGE_ONE = "Retrieval quality is measured with recall.\n\nA second paragraph on page one."
PAGE_TWO = "Page two talks about reciprocal rank fusion."


def test_text_pages_become_passages_with_page_numbers(tmp_path):
    result = extract_pdf(save(tmp_path, make_pdf([PAGE_ONE, PAGE_TWO])))
    assert not result.unusable and not result.problems
    assert [p.page for p in result.passages] == [1, 2]
    assert "recall" in result.passages[0].text and "second paragraph" in result.passages[0].text
    assert "reciprocal rank fusion" in result.passages[1].text
    assert all(p.section == "" for p in result.passages)
    # a PDF passage's reference is its page: extracted line breaks needn't match the page's lines
    assert all(p.line_start is None and p.line_end is None for p in result.passages)


def test_pages_without_text_are_listed(tmp_path):
    result = extract_pdf(save(tmp_path, make_pdf([PAGE_ONE, None, IMAGE, PAGE_TWO])))
    assert not result.unusable
    assert [p.page for p in result.passages] == [1, 4]
    assert result.problems == ["2 of 4 pages have no extractable text (pages 2, 3); "
                               "they may be scanned images and weren't indexed"]


def test_many_empty_pages_are_summarised(tmp_path):
    result = extract_pdf(save(tmp_path, make_pdf([PAGE_ONE] + [None] * 12)))
    assert "12 of 13 pages" in result.problems[0] and "(pages 2, 3, 4, 5, 6, 7, 8, 9, 10, 11 and more)" in result.problems[0]


@pytest.mark.parametrize("pages", [[IMAGE], [IMAGE, None], [None]])
def test_a_pdf_with_no_text_is_flagged_as_needing_ocr(tmp_path, pages):
    result = extract_pdf(save(tmp_path, make_pdf(pages)))
    assert result.passages == []
    assert "probably a scanned image" in result.unusable and "OCR" in result.unusable


def test_a_pdf_encrypted_only_against_copying_is_read(tmp_path):
    result = extract_pdf(save(tmp_path, make_pdf([PAGE_TWO], owner_password="owner-only")))
    assert not result.unusable
    assert "reciprocal rank fusion" in result.passages[0].text


def test_a_pdf_that_needs_a_password_is_refused(tmp_path):
    result = extract_pdf(save(tmp_path, make_pdf([PAGE_TWO], owner_password="owner", user_password="secret")))
    assert result.unusable == "the PDF needs a password to open, and password-protected PDFs aren't supported"
    assert result.passages == []


def test_an_aes_pdf_explains_the_missing_package(tmp_path, monkeypatch):
    from pypdf import PdfReader
    from pypdf.errors import DependencyError

    def needs_cryptography(self, password):
        raise DependencyError("cryptography is required for AES algorithm")

    monkeypatch.setattr(PdfReader, "decrypt", needs_cryptography)
    result = extract_pdf(save(tmp_path, make_pdf([PAGE_TWO], owner_password="owner")))
    assert "AES-encrypted" in result.unusable and "pip install cryptography" in result.unusable


def test_a_pdf_with_no_pages_is_refused(tmp_path):
    out = io.BytesIO()
    PdfWriter().write(out)
    assert extract_pdf(save(tmp_path, out.getvalue())).unusable == "the PDF has no pages"


def test_a_pdf_with_too_many_pages_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("evfind.extract.MAX_PDF_PAGES", 2)
    result = extract_pdf(save(tmp_path, make_pdf([PAGE_ONE, PAGE_TWO, PAGE_ONE])))
    assert result.unusable == "the PDF has 3 pages; the limit is 2"


def test_a_page_that_fails_to_extract_is_reported_and_the_rest_kept(tmp_path, monkeypatch):
    from pypdf import PageObject
    from pypdf.errors import PdfStreamError

    original = PageObject.extract_text

    def flaky(self, *args, **kwargs):
        if "Page two" in original(self, *args, **kwargs):
            raise PdfStreamError("broken content stream")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PageObject, "extract_text", flaky)
    result = extract_pdf(save(tmp_path, make_pdf([PAGE_ONE, PAGE_TWO])))
    assert [p.page for p in result.passages] == [1]
    assert result.problems == ["1 of 2 pages couldn't be read (pages 2) and weren't indexed"]


def test_a_pdf_whose_pages_are_all_blank_or_broken_is_unusable(tmp_path, monkeypatch):
    from pypdf import PageObject
    from pypdf.errors import PdfStreamError

    original = PageObject.extract_text

    def broken_if_text(self, *args, **kwargs):
        if original(self, *args, **kwargs):
            raise PdfStreamError("broken content stream")
        return ""

    monkeypatch.setattr(PageObject, "extract_text", broken_if_text)
    result = extract_pdf(save(tmp_path, make_pdf([PAGE_ONE, None])))
    assert result.passages == [] and "OCR" in result.unusable


@pytest.mark.parametrize("content", [b"", b"not a pdf at all", b"%PDF-1.7\n%%EOF", b"%PDF-1.4\n1 0 obj << /Type"])
def test_a_file_that_isnt_a_readable_pdf_is_refused_with_the_reason(tmp_path, content):
    result = extract_pdf(save(tmp_path, content))
    assert result.passages == []
    assert result.unusable.startswith(("the PDF can't be read", "no page could be read", "the PDF has no pages"))


def test_extract_file_sends_pdfs_to_the_pdf_reader(tmp_path):
    result = extract_file(save(tmp_path, make_pdf([PAGE_TWO]), name="Paper.PDF"))
    assert result.passages[0].page == 1


# Three crashes the 40,000-file fuzzing run found, reproduced exactly: pypdf raises
# NotImplementedError for each, and each must become a reason, not an exception.
@pytest.mark.parametrize("damage", [
    lambda pdf: re.sub(rb"/V\s+\d", b"/V 0", pdf, count=1),        # "Encryption V=0 NOT supported"
    lambda pdf: pdf.replace(b"/Standard", b"/Standarx", 1),           # "only Standard ... handler"
], ids=["encryption-version", "encryption-handler"])
def test_unsupported_encryption_is_a_reason_not_a_crash(tmp_path, damage):
    damaged = damage(make_pdf([PAGE_TWO], owner_password="o"))
    result = extract_pdf(save(tmp_path, damaged))
    assert result.unusable.startswith("the PDF can't be read (NotImplementedError")


def test_an_unknown_compression_filter_is_a_reason_not_a_crash(tmp_path):
    damaged = make_pdf([PAGE_ONE, PAGE_TWO]).replace(b"/FlateDecode", b"/FlateDecodX")
    assert extract_pdf(save(tmp_path, damaged)).unusable == "no page could be read: the PDF is probably damaged"


# Damaging an encrypted PDF is what found NotImplementedError (an unsupported encryption handler).
SEEDS = [make_pdf([PAGE_ONE, PAGE_TWO]), make_pdf([PAGE_ONE, IMAGE, None]), make_pdf([PAGE_ONE], owner_password="o")]


@st.composite
def damaged_pdf(draw):
    data = bytearray(draw(st.sampled_from(SEEDS)))
    for position, value in draw(st.lists(st.tuples(st.integers(0, len(data) - 1), st.integers(0, 255)),
                                         min_size=1, max_size=30)):
        data[position % len(data)] = value
    return bytes(data[: draw(st.integers(1, len(data)))])


@settings(deadline=None, max_examples=300, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(damaged_pdf())
def test_a_damaged_pdf_never_crashes_extraction(tmp_path, content):
    """Corrupt bytes anywhere, or cut the file short: the result is passages or a reason, never an exception."""
    result = extract_pdf(save(tmp_path, content))
    assert result.passages or result.unusable
