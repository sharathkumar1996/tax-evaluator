"""Builds the per-student evaluation report as a PDF (bytes)."""
import io
import re
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)
from reportlab.graphics.shapes import Drawing, Rect

from grader_core import fmt

NAVY = colors.HexColor("#1F3864")
GREY = colors.HexColor("#595959")
LIGHT = colors.HexColor("#F2F4F8")
LINE = colors.HexColor("#D9DDE5")
RED = colors.HexColor("#C0392B")
AMBER = colors.HexColor("#B7791F")
GREEN = colors.HexColor("#2E7D4F")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=16, textColor=NAVY,
                    alignment=0, spaceAfter=2)
H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=11.5, textColor=NAVY,
                    spaceBefore=10, spaceAfter=5)
BODY = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=8.5, leading=11)
SMALL = ParagraphStyle("s", parent=BODY, fontSize=7.5, leading=9.5, textColor=GREY)
CELL = ParagraphStyle("c", parent=BODY, fontSize=8, leading=10)
CELL_R = ParagraphStyle("cr", parent=CELL, alignment=TA_RIGHT)
HEAD = ParagraphStyle("h", parent=CELL, fontName="Helvetica-Bold", textColor=colors.white)
HEAD_R = ParagraphStyle("hr", parent=HEAD, alignment=TA_RIGHT)


def _p(t, st=CELL):
    return Paragraph(str(t).replace("&", "&amp;").replace("<", "&lt;"), st)


def _bar(pct, w=40 * mm, h=3.2 * mm):
    d = Drawing(w, h)
    d.add(Rect(0, 0, w, h, fillColor=LIGHT, strokeColor=None))
    col = GREEN if pct >= 0.8 else AMBER if pct >= 0.5 else RED
    d.add(Rect(0, 0, max(0, min(1, pct)) * w, h, fillColor=col, strokeColor=None))
    return d


def _grid(data, widths, right_cols=(), zebra=True):
    t = Table(data, colWidths=widths, repeatRows=1)
    st = [("BACKGROUND", (0, 0), (-1, 0), NAVY),
          ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
          ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
          ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    if zebra:
        for r in range(2, len(data), 2):
            st.append(("BACKGROUND", (0, r), (-1, r), LIGHT))
    t.setStyle(TableStyle(st))
    return t


def _lineref(r):
    ln = str(r["Line"])
    if ln.upper() == "FS":
        return f"Form {r['Form']} - filing status" if str(r["Form"])[:1].isdigit() else f"{r['Form']} - filing status"
    if re.match(r"^\d{1,3}[a-zA-Z]{0,2}$", ln) or re.match(r"^[A-J]$", ln):
        return f"{r['Form']} line {ln}"
    return f"{r['Form']} - {r['Description']}" if not ln.lower().startswith(("text:", "check:")) else f"{r['Form']}"


def build_report(student_name, student_id, key, rows, cats, total, out_of, file_name, institute):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=16 * mm,
                            title=f"Evaluation report - {student_id} - {key.case_id}", author=institute)
    W = A4[0] - 28 * mm
    story = []

    story.append(_p(institute.upper(), ParagraphStyle("inst", parent=SMALL, fontName="Helvetica-Bold",
                                                       textColor=NAVY, fontSize=8)))
    story.append(Paragraph("Tax Return Evaluation Report", H1))
    story.append(_p(f"Case {key.case_id}: {key.title}  |  Tax year {key.tax_year}", SMALL))
    story.append(Spacer(1, 6))

    info = [[_p("Student"), _p(student_name or "-"), _p("Student ID"), _p(student_id or "-")],
            [_p("Return file"), _p(file_name), _p("Evaluated on"), _p(datetime.now().strftime("%d %b %Y, %H:%M"))]]
    t = Table(info, colWidths=[22 * mm, W / 2 - 22 * mm, 24 * mm, W / 2 - 24 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), LIGHT), ("BACKGROUND", (2, 0), (2, -1), LIGHT),
                           ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [t, Spacer(1, 8)]

    pct = total / out_of if out_of else 0
    n_ok = sum(r["Result"] == "Correct" for r in rows)
    n_bad = sum(r["Result"] == "Wrong" for r in rows)
    n_unx = sum(r["Result"] == "Unexpected entry" for r in rows)
    n_keyed = len(rows) - n_unx
    big = ParagraphStyle("big", parent=BODY, fontName="Helvetica-Bold", fontSize=22, leading=26, textColor=NAVY)
    extra = f" &nbsp; <font color='#C0392B'>{n_unx} unexpected entr{'y' if n_unx == 1 else 'ies'}</font>" if n_unx else ""
    score = Table([[Paragraph(f"{total:g} / {out_of:g}", big),
                    Paragraph(f"<b>{pct:.0%}</b> overall<br/><font color='#2E7D4F'>{n_ok} correct</font> &nbsp; "
                              f"<font color='#C0392B'>{n_bad} wrong</font> &nbsp; of {n_keyed} items checked{extra}", BODY)]],
                  colWidths=[45 * mm, W - 45 * mm])
    score.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                               ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    story.append(score)

    # ---- category-wise marks
    story.append(Paragraph("Marks by category", H2))
    data = [[_p("Category", HEAD), _p("Marks", HEAD_R), _p("Out of", HEAD_R), _p("%", HEAD_R), _p("", HEAD),
             _p("Mistakes", HEAD_R)]]
    for c in cats:
        has_max = c["Marks"] > 0
        data.append([_p(c["Category"]), _p(f"{c['Awarded']:g}", CELL_R), _p(f"{c['Marks']:g}" if has_max else "-", CELL_R),
                     _p(f"{c['Pct']:.0%}" if has_max else "-", CELL_R), _bar(c["Pct"]) if has_max else _p(""),
                     _p(c["Mistakes"], CELL_R)])
    story.append(_grid(data, [W - 120 * mm, 18 * mm, 18 * mm, 14 * mm, 48 * mm, 22 * mm]))

    # ---- mistakes
    wrong = [r for r in rows if r["Result"] != "Correct"]
    story.append(Paragraph("Where you lost marks", H2))
    if not wrong:
        story.append(_p("No mistakes found. Well done.", BODY))
    else:
        data = [[_p("#", HEAD), _p("Where", HEAD), _p("What was checked", HEAD), _p("Your entry", HEAD_R),
                 _p("Correct", HEAD_R), _p("Lost", HEAD_R), _p("Feedback", HEAD)]]
        for r in wrong:
            fb = r["Feedback"] or ""
            if r["Note"]:
                fb = (fb + " " if fb else "") + f"[{r['Note']}]"
            corr = fmt(r["Correct Value"]) if str(r["Correct Value"]).strip() != "" else "(blank)"
            mine = fmt(r["Student Value"]) if str(r["Student Value"]).strip() != "" else "(blank)"
            data.append([_p(r["Step"]), _p(_lineref(r)), _p(r["Description"]), _p(mine, CELL_R),
                         _p(corr, CELL_R), _p(f"{r['Marks'] - r['Awarded']:g}", CELL_R), _p(fb)])
        story.append(_grid(data, [8 * mm, 27 * mm, 32 * mm, 22 * mm, 22 * mm, 10 * mm, W - 121 * mm]))

    # ---- full detail
    story.append(Paragraph("All steps checked", H2))
    data = [[_p("#", HEAD), _p("Category", HEAD), _p("Where", HEAD), _p("What was checked", HEAD),
             _p("Your entry", HEAD_R), _p("Correct", HEAD_R), _p("Marks", HEAD_R), _p("Result", HEAD)]]
    colour = {"Correct": GREEN}
    for r in rows:
        res = ParagraphStyle("res", parent=CELL, textColor=colour.get(r["Result"], RED), fontName="Helvetica-Bold")
        data.append([_p(r["Step"]), _p(r["Category"]), _p(_lineref(r)), _p(r["Description"]),
                     _p(fmt(r["Student Value"]) if str(r["Student Value"]).strip() != "" else "(blank)", CELL_R),
                     _p(fmt(r["Correct Value"]) if str(r["Correct Value"]).strip() != "" else "(blank)", CELL_R),
                     _p(f"{r['Awarded']:g}/{r['Marks']:g}" if r["Marks"] else f"{r['Awarded']:g}", CELL_R),
                     _p(r["Result"], res)])
    story.append(_grid(data, [8 * mm, 28 * mm, 30 * mm, W - 152 * mm, 20 * mm, 20 * mm, 14 * mm, 32 * mm]))

    def footer(canv, doc_):
        canv.saveState()
        canv.setFont("Helvetica", 7)
        canv.setFillColor(GREY)
        canv.drawString(14 * mm, 9 * mm, f"{institute}  |  {student_id}  |  Case {key.case_id}  |  "
                                         "Figures read automatically from the uploaded return PDF")
        canv.drawRightString(A4[0] - 14 * mm, 9 * mm, f"Page {doc_.page}")
        canv.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
