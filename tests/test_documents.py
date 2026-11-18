"""Tests for the PDF document service."""

from app.services.documents import build_offer_letter_pdf


def test_offer_letter_pdf_is_valid() -> None:
    pdf = build_offer_letter_pdf(
        full_name="Alex Rivera",
        job_title="Senior Software Engineer",
        salary_display="USD 145,000 base + bonus eligible",
    )
    # PDF magic header and non-trivial size.
    assert pdf[:4] == b"%PDF"
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 500


def test_offer_letter_pdf_handles_special_characters() -> None:
    # Names/titles with characters that could break naive string building.
    pdf = build_offer_letter_pdf(
        full_name="José O'Brien-Núñez",
        job_title="Lead & Principal <Engineer>",
        salary_display="€120,000",
    )
    assert pdf[:4] == b"%PDF"
