"""CSV/PDF export of a comparison run — something a judge or a real
site-selection team can actually walk away with, not just a browser tab.
"""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle


def build_csv_bytes(df: pd.DataFrame) -> bytes:
    """Full comparison table (site x architecture x annual cost) as CSV."""
    table = df.pivot_table(index="site", columns="architecture", values="annual_cost_usd")
    return table.to_csv().encode("utf-8")


def build_pdf_bytes(
    df: pd.DataFrame,
    headline_text: str,
    facility_mw: float,
    rate: float,
    methodology_notes: list[tuple[str, str]],
) -> bytes:
    """One-page-ish PDF: headline finding, full comparison table, methodology."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("DCTitle", parent=styles["Title"], fontSize=18, spaceAfter=4)
    meta_style = ParagraphStyle("DCMeta", parent=styles["Normal"], textColor=colors.grey, spaceAfter=16)
    headline_style = ParagraphStyle("DCHeadline", parent=styles["Heading2"], spaceAfter=14)
    body_style = styles["Normal"]

    story = [
        Paragraph("Data-Centre Siting &amp; Cooling-Cost Engine", title_style),
        Paragraph(
            f"Generated {date.today().isoformat()} &middot; {facility_mw:.1f} MW planned IT load "
            f"&middot; ${rate:.2f}/kWh electricity rate",
            meta_style,
        ),
        Paragraph(headline_text, headline_style),
        Spacer(1, 6),
    ]

    table_df = df.pivot_table(index="site", columns="architecture", values="annual_cost_usd")
    header = ["Site"] + list(table_df.columns)
    rows = [header]
    for site, row in table_df.iterrows():
        rows.append([site] + [f"${v:,.0f}" for v in row])

    t = Table(rows, hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b0b0b")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 20))

    story.append(Paragraph("Methodology &amp; sources", styles["Heading3"]))
    for label, source in methodology_notes:
        story.append(Paragraph(f"<b>{label}</b> &mdash; {source}", body_style))
        story.append(Spacer(1, 6))

    doc.build(story)
    return buf.getvalue()
