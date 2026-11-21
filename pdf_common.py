"""pdf_common.py - shared Simandhar-branded PDF styles and table helpers (used by the integrity report)."""
from datetime import datetime

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Table, TableStyle

from brand import FONT, FONT_BOLD, GREY, LINE, MAROON, MAROON_LIGHT

NAVY = MAROON
LIGHT = MAROON_LIGHT
ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=ss["Title"], fontName=FONT_BOLD, fontSize=16, textColor=NAVY, alignment=0,
                    spaceAfter=2)
H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName=FONT_BOLD, fontSize=11.5, textColor=NAVY,
                    spaceBefore=10, spaceAfter=4)
B = ParagraphStyle("b", parent=ss["BodyText"], fontName=FONT, fontSize=8.5, leading=11)
SM = ParagraphStyle("s", parent=B, fontSize=7.5, textColor=GREY)
C = ParagraphStyle("c", parent=B, fontSize=7.8, leading=9.6)
CH = ParagraphStyle("ch", parent=C, fontName=FONT_BOLD, textColor=colors.white)


def _esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;")


def _table(df, widths=None):
    if df is None or df.empty:
        return Paragraph("No data for this selection.", B)
    data = [[Paragraph(_esc(c), CH) for c in df.columns]]
    for _, r in df.iterrows():
        data.append([Paragraph(_esc("-" if pd.isna(v) else (f"{v:g}" if isinstance(v, float) else v)), C) for v in r])
    t = Table(data, colWidths=widths, repeatRows=1)
    st = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE), ("TOPPADDING", (0, 0), (-1, -1), 3),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    for i in range(2, len(data), 2):
        st.append(("BACKGROUND", (0, i), (-1, i), LIGHT))
    t.setStyle(TableStyle(st))
    return t


def _cards(items, width):
    big = ParagraphStyle("big", parent=B, fontName=FONT_BOLD, fontSize=17, leading=20, textColor=NAVY)
    cells = [[Paragraph(v, big) for _, v in items], [Paragraph(k, SM) for k, _ in items]]
    t = Table(cells, colWidths=[width / len(items)] * len(items))
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("LINEAFTER", (0, 0), (-2, -1), 2, colors.white),
                           ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def _footer(institute, label):
    def f(canv, doc):
        canv.saveState()
        canv.setFont(FONT, 7)
        canv.setFillColor(GREY)
        canv.drawString(14 * mm, 8 * mm, f"{institute}  |  {label}  |  generated {datetime.now():%d %b %Y %H:%M}")
        canv.drawRightString(doc.pagesize[0] - 14 * mm, 8 * mm, f"Page {doc.page}")
        canv.restoreState()
    return f
