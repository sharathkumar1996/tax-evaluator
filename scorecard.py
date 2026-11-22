"""Final course scorecard (one Simandhar-branded PDF per student) and the batch results workbook."""
import io
from datetime import datetime

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from brand import FONT, FONT_BOLD, GREEN, GREY, LINE, MAROON, MAROON_LIGHT, RED, ZEBRA
from finalscore import FINAL

_ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=_ss["Title"], fontName=FONT_BOLD, fontSize=17, leading=21, textColor=MAROON,
                    alignment=0, spaceAfter=2)
H2 = ParagraphStyle("h2", parent=_ss["Heading2"], fontName=FONT_BOLD, fontSize=11.5, textColor=MAROON,
                    spaceBefore=8, spaceAfter=4)
BODY = ParagraphStyle("b", parent=_ss["BodyText"], fontName=FONT, fontSize=9, leading=11.5)
SMALL = ParagraphStyle("s", parent=BODY, fontSize=7.5, leading=9.5, textColor=GREY)
CELL = ParagraphStyle("c", parent=BODY, fontSize=8.5, leading=10.5)
HEAD = ParagraphStyle("h", parent=CELL, fontName=FONT_BOLD, textColor=colors.white)


def _p(t, st=CELL):
    return Paragraph(str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), st)


def _grid(data, widths):
    t = Table([[_p(c, HEAD) for c in data[0]]] + [[_p(c) for c in r] for r in data[1:]], colWidths=widths,
              repeatRows=1, hAlign="LEFT")
    style = [("BACKGROUND", (0, 0), (-1, 0), MAROON), ("BOX", (0, 0), (-1, -1), 0.5, LINE),
             ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    style += [("BACKGROUND", (0, i), (-1, i), ZEBRA) for i in range(2, len(data), 2)]
    t.setStyle(TableStyle(style))
    return t


def _num(x):
    return "-" if x is None or (isinstance(x, float) and pd.isna(x)) else f"{float(x):g}"


def scorecard_pdf(row, returns, settings, institute, view=FINAL):
    """row: one student's row from finalscore.final_scores; returns: [{case_id, title, pct, on_time}] for the
    course's mock returns (not yet evaluated ones included with pct None)."""
    s = settings
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"Final scorecard - {row['Student ID']}", author=institute)
    W = A4[0] - 32 * mm
    final = view == FINAL
    story = [_p(institute.upper(), ParagraphStyle("i", parent=SMALL, fontName=FONT_BOLD, textColor=MAROON)),
             Paragraph("Final Course Scorecard" if final else "Course Progress Scorecard", H1),
             _p("Practical tax return training: mock returns and MCQ tests", SMALL)]
    rule = Table([[""]], colWidths=[W], rowHeights=[2])
    rule.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.8, MAROON)]))
    story += [rule, Spacer(1, 6)]

    info = Table([[_p("Student"), _p(row["Name"] or "-"), _p("Student ID"), _p(row["Student ID"])],
                  [_p("Batch"), _p(row["Batch"] or "-"), _p("Issued on"), _p(datetime.now().strftime("%d %b %Y"))]],
                 colWidths=[22 * mm, W / 2 - 22 * mm, 24 * mm, W / 2 - 24 * mm])
    info.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), MAROON_LIGHT), ("BACKGROUND", (2, 0), (2, -1), MAROON_LIGHT),
                              ("BOX", (0, 0), (-1, -1), 0.5, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE)]))
    story += [info, Spacer(1, 10)]

    total, result = row["Total / 100"], row["Result"]
    good = result in ("Pass", "On track")
    big = ParagraphStyle("big", parent=BODY, fontName=FONT_BOLD, fontSize=26, leading=30, textColor=MAROON)
    res = ParagraphStyle("res", parent=BODY, fontName=FONT_BOLD, fontSize=15, leading=18,
                         textColor=GREEN if good else RED)
    head = Table([[Paragraph(f"{total:g} <font size=12>/ 100</font>", big), Paragraph(result.upper(), res),
                   _p(f"Pass mark: {s['pass_mark']:g} / 100" + ("" if final else
                      ". Progress so far: based only on the returns and tests your batch has done."), SMALL)]],
                 colWidths=[50 * mm, 35 * mm, W - 85 * mm])
    head.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), MAROON_LIGHT), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 8),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [head, Spacer(1, 4)]

    story.append(Paragraph("How the score is made up", H2))
    acc_k, ont_k, mcq_k = (f"Accuracy / {s['w_accuracy']:g}", f"On-time / {s['w_on_time']:g}", f"MCQ / {s['w_mcq']:g}")
    comp = [["Component", "How it is worked out", "Your marks", "Out of"],
            ["Return accuracy", f"Average score of the mock returns ({row['Returns evaluated']} evaluated): "
                                f"{_num(row['Average return score'])}%", _num(row[acc_k]), f"{s['w_accuracy']:g}"],
            ["On-time submission", f"{row['On time']} returns on time, {row['Late']} late",
             _num(row[ont_k]), f"{s['w_on_time']:g}"],
            ["MCQ tests", f"Marks {row['MCQ marks']}", _num(row[mcq_k]), f"{s['w_mcq']:g}"]]
    if final:
        comp.append(["Total", "", _num(total), "100"])
    story.append(_grid(comp, [34 * mm, W - 34 * mm - 44 * mm, 22 * mm, 22 * mm]))

    story.append(Paragraph("Mock returns", H2))
    data = [["Case", "Title", "Score / 100", "Submission"]]
    for r in returns:
        status = ("-" if r.get("pct") is None else
                  {1: "On time", 0: "Late"}.get(r.get("on_time"), "Not set"))
        data.append([r["case_id"], r.get("title") or "", "Not submitted" if r.get("pct") is None and final else
                     ("-" if r.get("pct") is None else f"{100 * float(r['pct']):.1f}"), status])
    story.append(_grid(data, [22 * mm, W - 22 * mm - 56 * mm, 28 * mm, 28 * mm]))

    story.append(Paragraph("MCQ tests", H2))
    mq = [["Test", "Marks"]] + [[f"Test {t}", f"{_num(row.get(f'MCQ {t}'))} / {s['mcq_out_of']:g}"]
                                for t in range(1, int(s["n_mcq"]) + 1)]
    story.append(_grid(mq, [40 * mm, 40 * mm]))
    story += [Spacer(1, 8), _p(
        f"Scoring: return accuracy {s['w_accuracy']:g} marks (all {s['n_mocks']:g} mock returns, a return not "
        f"submitted counts 0), on-time submission {s['w_on_time']:g} marks (a late return earns no on-time marks), "
        f"MCQ tests {s['w_mcq']:g} marks ({s['n_mcq']:g} tests of {s['mcq_out_of']:g} marks). "
        f"Pass mark {s['pass_mark']:g}.", SMALL)]

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont(FONT, 7)
        canvas.setFillColor(GREY)
        canvas.drawString(16 * mm, 9 * mm, f"{institute}  •  Final course scorecard  •  {row['Student ID']}")
        canvas.drawRightString(A4[0] - 16 * mm, 9 * mm, f"Page {d.page}")
        canvas.restoreState()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def results_workbook(df, settings, history, view=FINAL):
    """Excel: the results sheet, the scoring settings and the submission-status change history."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Final results" if view == FINAL else "Progress so far", index=False)
        pd.DataFrame([(k, v) for k, v in settings.items()], columns=["Setting", "Value"]).to_excel(
            xw, sheet_name="Scoring", index=False)
        pd.DataFrame(history or [], columns=["student_id", "case_id", "old_status", "new_status", "changed_by",
                                             "reason", "changed_at"]).to_excel(xw, sheet_name="Status changes",
                                                                               index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(
                    max(len(str(c.value or "")) for c in col) + 2, 40)
    return buf.getvalue()
