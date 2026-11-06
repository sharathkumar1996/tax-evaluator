"""Management PDF, student consolidated PDF and management Excel workbook."""
import io
from datetime import datetime

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from metrics import CRITICAL_TEXT

NAVY = colors.HexColor("#1F3864")
LIGHT = colors.HexColor("#F2F4F8")
LINE = colors.HexColor("#D9DDE5")
GREY = colors.HexColor("#595959")
ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=16, textColor=NAVY, alignment=0,
                    spaceAfter=2)
H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=11.5, textColor=NAVY,
                    spaceBefore=10, spaceAfter=4)
B = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=8.5, leading=11)
SM = ParagraphStyle("s", parent=B, fontSize=7.5, textColor=GREY)
C = ParagraphStyle("c", parent=B, fontSize=7.8, leading=9.6)
CH = ParagraphStyle("ch", parent=C, fontName="Helvetica-Bold", textColor=colors.white)


def _pct(x):
    return "-" if x is None or pd.isna(x) else f"{x * 100:.1f}%"


def _num(x, d=1):
    return "-" if x is None or pd.isna(x) else f"{x:.{d}f}"


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
    big = ParagraphStyle("big", parent=B, fontName="Helvetica-Bold", fontSize=17, leading=20, textColor=NAVY)
    cells = [[Paragraph(v, big) for _, v in items], [Paragraph(k, SM) for k, _ in items]]
    t = Table(cells, colWidths=[width / len(items)] * len(items))
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("LINEAFTER", (0, 0), (-2, -1), 2, colors.white),
                           ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def _footer(institute, label):
    def f(canv, doc):
        canv.saveState()
        canv.setFont("Helvetica", 7)
        canv.setFillColor(GREY)
        canv.drawString(14 * mm, 8 * mm, f"{institute}  |  {label}  |  generated {datetime.now():%d %b %Y %H:%M}")
        canv.drawRightString(doc.pagesize[0] - 14 * mm, 8 * mm, f"Page {doc.page}")
        canv.restoreState()
    return f


def management_pdf(institute, client, scope, head, by_batch, by_case, top, watch):
    buf = io.BytesIO()
    size = landscape(A4)
    doc = SimpleDocTemplate(buf, pagesize=size, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=14 * mm, title="Practical tax training - management report")
    W = size[0] - 28 * mm
    s = [Paragraph(f"{_esc(institute.upper())}  |  {_esc(client)}", SM),
         Paragraph("Practical Tax Return Training - Management Report", H1),
         Paragraph(_esc(scope), SM), Spacer(1, 6)]
    prog = f"{head['returns']:,}" + (f" of {head['expected']:,}" if head.get("expected") else "")
    s.append(_cards([("Returns evaluated", prog), ("Students", f"{head['students']:,}"),
                     ("File-ready rate", _pct(head["file_ready_rate"])),
                     ("Bottom-line accuracy", _pct(head["bottom_line_accuracy"])),
                     ("Avg errors per return", _num(head["avg_errors"])), ("Average score", _pct(head["avg_score"]))], W))
    s.append(Paragraph("<b>File-ready</b>: no mistakes on critical items - " + _esc(CRITICAL_TEXT) +
                       ". <b>Bottom-line accuracy</b>: refund / amount owed exactly right. "
                       "<b>Errors</b>: items wrong plus unexpected entries. Each case is marked on its own answer key.", SM))
    s += [Paragraph("By batch", H2), _table(by_batch)]
    s += [Paragraph("By case", H2), _table(by_case)]
    s += [Paragraph("Most common mistakes", H2),
          _table(top, [16 * mm, 24 * mm, 16 * mm, W - 116 * mm, 30 * mm, 30 * mm] if top is not None and not top.empty else None)]
    s += [Paragraph(f"Students needing coaching ({0 if watch is None else len(watch)})", H2),
          Paragraph("Average score below 60% or file-ready rate below 50% across the returns evaluated so far.", SM),
          _table(watch)]
    doc.build(s, onFirstPage=_footer(institute, "Management report"), onLaterPages=_footer(institute, "Management report"))
    return buf.getvalue()


def student_pdf(institute, student, evals, mistakes_by_case):
    """evals: list of evaluation dicts for one student; mistakes_by_case: {case_id: [mistake dicts]}."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=14 * mm, title=f"Consolidated report {student['student_id']}")
    W = A4[0] - 28 * mm
    ev = pd.DataFrame(evals)
    s = [Paragraph(_esc(institute.upper()), SM), Paragraph("Consolidated Evaluation Report", H1),
         Paragraph(f"{_esc(student.get('name') or '')}  |  Student ID {_esc(student['student_id'])}  |  "
                   f"Batch {_esc(student.get('batch') or '-')}", B), Spacer(1, 6)]
    if ev.empty:
        s.append(Paragraph("No returns evaluated yet.", B))
    else:
        bl = ev["bottom_line"].dropna()
        s.append(_cards([("Returns evaluated", str(len(ev))), ("Average score", _pct(ev["pct"].mean())),
                         ("File-ready", f"{int(ev['file_ready'].sum())} of {len(ev)}"),
                         ("Bottom line right", f"{int(bl.sum())} of {len(bl)}" if len(bl) else "-"),
                         ("Avg errors per return", _num(ev["errors"].mean()))], W))
        tbl = pd.DataFrame({
            "Case": ev["case_id"], "Score": [f"{a:g} / {b:g}" for a, b in zip(ev["score"], ev["out_of"])],
            "%": [f"{p * 100:.0f}%" for p in ev["pct"]],
            "File-ready": ["Yes" if x else "No" for x in ev["file_ready"]],
            "Bottom line": ["-" if pd.isna(x) else ("Right" if x else "Wrong") for x in ev["bottom_line"]],
            "Errors": ev["errors"], "Evaluated": ev["evaluated_at"].str[:10]})
        s += [Paragraph("Results by case", H2), _table(tbl)]
        s.append(Paragraph("Mistakes by case", H2))
        for case in ev["case_id"]:
            ms = [m for m in mistakes_by_case.get(case, []) if m["result"] != "Correct"]
            s.append(Paragraph(f"<b>{_esc(case)}</b> - {len(ms)} mistake(s)", B))
            if ms:
                df = pd.DataFrame([{"Where": f"{m['form']} {m['line']}", "What was checked": m["description"],
                                    "Your entry": m["student_value"] if m["student_value"] not in (None, "") else "(blank)",
                                    "Correct": m["correct_value"] if m["correct_value"] not in (None, "") else "(blank)",
                                    "Lost": f"{(m['marks'] or 0) - (m['awarded'] or 0):g}"} for m in ms])
                s.append(_table(df, [32 * mm, W - 102 * mm, 25 * mm, 25 * mm, 20 * mm]))
            s.append(Spacer(1, 4))
    doc.build(s, onFirstPage=_footer(institute, f"Student {student['student_id']}"),
              onLaterPages=_footer(institute, f"Student {student['student_id']}"))
    return buf.getvalue()


def management_excel(head, by_batch, by_case, top, students, evals, scope):
    buf = io.BytesIO()
    summary = pd.DataFrame([
        ("Scope", scope), ("Returns evaluated", head["returns"]), ("Students", head["students"]),
        ("File-ready rate", _pct(head["file_ready_rate"])), ("Bottom-line accuracy", _pct(head["bottom_line_accuracy"])),
        ("Average errors per return", _num(head["avg_errors"])), ("Average score", _pct(head["avg_score"])),
        ("Critical items (file-ready)", CRITICAL_TEXT), ("Generated", f"{datetime.now():%d %b %Y %H:%M}")],
        columns=["Measure", "Value"])
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        for name, df in [("By batch", by_batch), ("By case", by_case), ("Top mistakes", top), ("Students", students),
                         ("All evaluations", evals)]:
            (df if df is not None and not df.empty else pd.DataFrame({"Note": ["No data"]})).to_excel(xw, sheet_name=name, index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(
                    60, max(10, max(len(str(c.value or "")) for c in col[:200]) + 2))
            ws.freeze_panes = "A2"
    return buf.getvalue()
