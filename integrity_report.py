"""Integrity (copying) report: PDF for management and an Excel workbook with all the evidence."""
import io
from datetime import datetime

import pandas as pd
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

from integrity import LEVELS, SIGNALS
from mgmt_report import H1, H2, SM, B, _cards, _esc, _footer, _table

CAUTION = ("These are signals for a conversation, not proof. Before any action, open both returns, compare the "
           "evidence listed, and hear the students. Students in the same lab may legitimately share a computer "
           "or save at the same time; common mistakes are already filtered out.")


def _counts(res):
    st = res["students"]
    n = len(st)
    c = {lv: int((st["Integrity"] == lv).sum()) if n else 0 for _, lv in LEVELS}
    return n, c


def integrity_pdf(institute, scope, res):
    buf = io.BytesIO()
    size = landscape(A4)
    doc = SimpleDocTemplate(buf, pagesize=size, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=14 * mm, title="Integrity report")
    W = size[0] - 28 * mm
    n, c = _counts(res)
    s = [Paragraph(_esc(institute.upper()), SM), Paragraph("Practical Tax Return Training - Integrity Report", H1),
         Paragraph(_esc(scope), SM), Spacer(1, 6),
         _cards([("Students checked", f"{n:,}"), ("Very likely copied", str(c["Very likely copied"])),
                 ("Likely copied", str(c["Likely copied"])), ("Review", str(c["Review"])),
                 ("Groups", str(len(res["groups"]))), ("Built on trainer's file", str(len(res["trainer"])))], W),
         Paragraph(_esc(CAUTION), SM)]
    s += [Paragraph("By batch", H2), _table(res["batches"])]
    if not res["trainer"].empty:
        s += [Paragraph("Returns built on the trainer's correct-return file", H2), _table(res["trainer"])]
    s += [Paragraph("Groups of linked students (likely or stronger)", H2),
          _table(res["groups"], [14 * mm, 14 * mm, 16 * mm, 60 * mm, W - 164 * mm, 40 * mm, 20 * mm]
                 if not res["groups"].empty else None)]
    pairs = res["pairs"]
    s += [PageBreak(), Paragraph(f"Flagged pairs ({len(pairs)})", H2)]
    if not pairs.empty:
        show = pairs[["Level", "Score", "Case", "Student A", "Name A", "Student B", "Name B", "Evidence"]]
        s.append(_table(show, [26 * mm, 12 * mm, 12 * mm, 18 * mm, 28 * mm, 18 * mm, 28 * mm, W - 142 * mm]))
    else:
        s.append(Paragraph("No pairs of returns share suspicious evidence.", B))
    flagged = res["students"][res["students"]["Integrity"] != "No concerns"] if not res["students"].empty else res["students"]
    s += [Paragraph(f"Students with concerns ({len(flagged)})", H2),
          _table(flagged.drop(columns=["Returns"]) if not flagged.empty else flagged)]
    if not res["id_checks"].empty:
        s += [Paragraph("Student ID checks", H2), _table(res["id_checks"])]
    s += [Paragraph("How the score is made", H2),
          Paragraph(_esc("Each piece of evidence adds points (capped at 100): " +
                         "; ".join(f"{t} ({p})" for p, t in SIGNALS.values()) +
                         "; the same unusual wrong answers (up to 70, rarer mistakes count more); saved within "
                         "2 minutes of each other (10). 90+ = very likely copied, 60+ = likely, 30+ = review."), SM)]
    doc.build(s, onFirstPage=_footer(institute, "Integrity report"), onLaterPages=_footer(institute, "Integrity report"))
    return buf.getvalue()


def integrity_excel(scope, res):
    buf = io.BytesIO()
    n, c = _counts(res)
    summary = pd.DataFrame([("Scope", scope), ("Students checked", n),
                            ("Very likely copied", c["Very likely copied"]), ("Likely copied", c["Likely copied"]),
                            ("Review", c["Review"]), ("Groups of linked students", len(res["groups"])),
                            ("Built on trainer's file", len(res["trainer"])), ("Note", CAUTION),
                            ("Generated", f"{datetime.now():%d %b %Y %H:%M}")], columns=["Measure", "Value"])
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        for name, key in [("By batch", "batches"), ("Students", "students"), ("Flagged pairs", "pairs"),
                          ("Groups", "groups"), ("Trainer file", "trainer"), ("ID checks", "id_checks")]:
            df = res[key]
            (df if not df.empty else pd.DataFrame({"Note": ["Nothing found"]})).to_excel(xw, sheet_name=name, index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(
                    80, max(10, max(len(str(cell.value or "")) for cell in col[:200]) + 2))
            ws.freeze_panes = "A2"
    return buf.getvalue()
