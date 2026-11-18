"""Document generation.

Renders offer letters as PDFs in memory with reportlab — no temp files, no disk
writes, so it works the same on a laptop or a read-only container filesystem.
The route streams the returned bytes straight to the client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)


def build_offer_letter_pdf(
    *,
    full_name: str,
    job_title: str,
    salary_display: str,
    company: str = "Onboarding Automation",
) -> bytes:
    """Return a formatted offer-letter PDF as bytes."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        topMargin=1 * inch,
        bottomMargin=1 * inch,
        leftMargin=1 * inch,
        rightMargin=1 * inch,
        title=f"Offer Letter — {full_name}",
        author=company,
    )

    styles = getSampleStyleSheet()
    heading = ParagraphStyle(
        "OfferHeading",
        parent=styles["Title"],
        alignment=TA_CENTER,
        fontSize=18,
        spaceAfter=6,
    )
    subtitle = ParagraphStyle(
        "OfferSubtitle",
        parent=styles["Normal"],
        alignment=TA_CENTER,
        textColor="#666666",
        fontSize=10,
        spaceAfter=24,
    )
    body = ParagraphStyle(
        "OfferBody",
        parent=styles["Normal"],
        fontSize=11,
        leading=16,
        spaceAfter=12,
    )

    today = datetime.now(UTC).date().isoformat()
    story = [
        Paragraph("Offer of Employment", heading),
        Paragraph(company, subtitle),
        Paragraph(f"Date: {today}", body),
        Paragraph(f"Dear {full_name},", body),
        Paragraph(
            f"We are delighted to offer you the position of "
            f"<b>{job_title}</b>. We were impressed throughout the interview "
            f"process and believe you will be a strong addition to the team.",
            body,
        ),
        Paragraph(
            f"<b>Compensation:</b> {salary_display}.",
            body,
        ),
        Paragraph(
            "This letter confirms the primary terms of your offer. Your "
            "onboarding coordinator will follow up with start-date logistics, "
            "benefits enrollment, and any documents that require your signature.",
            body,
        ),
        Spacer(1, 24),
        Paragraph("Warm regards,", body),
        Paragraph(f"The {company} Team", body),
    ]

    doc.build(story)
    return buffer.getvalue()
