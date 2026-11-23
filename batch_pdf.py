"""Consolidated batch report for a trainer: one Simandhar-branded PDF and one Excel workbook per batch."""
import io
from datetime import datetime

import pandas as pd
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing, Line, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from brand import AMBER, FONT, FONT_BOLD, GREEN, GREY, LINE, MAROON, MAROON_LIGHT, RED, ZEBRA
from scorecard import BODY, H1, H2, HEAD, SMALL

TINY = ParagraphStyle("t", parent=BODY, fontSize=7.4, leading=9)
TINY_HEAD = ParagraphStyle("th", parent=TINY, fontName=FONT_BOLD, textColor=colors.white)
STATUS_COLOR = {"On track": GREEN, "Pass": GREEN, "At risk": RED, "Fail": RED, "Not started": GREY,
                "Very likely copied": RED, "Likely copied": AMBER, "Review": GREY}


def _esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _table(df, widths, W, status_col=None, max_rows=None):
    """A branded table from a DataFrame. Long text is cut so one row never fills a page."""
    if df is None or df.empty:
        return Paragraph("None.", SMALL)
    rows = df.head(max_rows) if max_rows else df
    data = [[Paragraph(_esc(c), TINY_HEAD) for c in rows.columns]]
    for _, r in rows.iterrows():
        cells = []
        for c in rows.columns:
            v = _fmt(r[c])
            v = v if len(v) <= 260 else v[:257] + "..."
            if c == status_col and r[c] in STATUS_COLOR:
                cells.append(Paragraph(f"<font color='#{STATUS_COLOR[r[c]].hexval()[2:]}'><b>{_esc(v)}</b></font>",
                                       TINY))
            else:
                cells.append(Paragraph(_esc(v), TINY))
        data.append(cells)
    total = sum(widths)
    t = Table(data, colWidths=[w * W / total for w in widths], repeatRows=1, hAlign="LEFT")
    style = [("BACKGROUND", (0, 0), (-1, 0), MAROON), ("BOX", (0, 0), (-1, -1), 0.5, LINE),
             ("INNERGRID", (0, 0), (-1, -1), 0.3, LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    style += [("BACKGROUND", (0, i), (-1, i), ZEBRA) for i in range(2, len(data), 2)]
    t.setStyle(TableStyle(style))
    out = [t]
    if max_rows and len(df) > max_rows:
        out.append(Paragraph(f"... and {len(df) - max_rows} more (see the Excel workbook).", SMALL))
    return out if len(out) > 1 else t


def _tiles(items, W):
    """A row of KPI tiles: [(label, value, note)]."""
    big = ParagraphStyle("kv", parent=BODY, fontName=FONT_BOLD, fontSize=15, leading=18, textColor=MAROON)
    lab = ParagraphStyle("kl", parent=SMALL, fontSize=7, leading=8.5)
    cells = [[Paragraph(_esc(v), big), Paragraph(_esc(lbl) + (f"<br/>{_esc(n)}" if n else ""), lab)]
             for lbl, v, n in items]
    t = Table([[Table([[c[0]], [c[1]]], colWidths=[W / len(items) - 6]) for c in cells]],
              colWidths=[W / len(items)] * len(items))
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), MAROON_LIGHT), ("LINEAFTER", (0, 0), (-2, -1), 2,
                                                                             colors.white),
                           ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def _bar_chart(mocks, W, cohort_avg=None):
    """Average score per mock (one series, so no legend), values on the bars, all-batch average as a line."""
    h = 52 * mm
    d = Drawing(W, h)
    if mocks.empty:
        return None
    vals = [float(x or 0) for x in mocks["Average %"]]
    bc = VerticalBarChart()
    bc.x, bc.y, bc.width, bc.height = 24, 18, W - 40, h - 34
    bc.data = [vals]
    bc.categoryAxis.categoryNames = list(mocks["Case"])
    bc.categoryAxis.labels.fontName, bc.categoryAxis.labels.fontSize = FONT, 7
    bc.categoryAxis.strokeColor = LINE
    bc.valueAxis.valueMin, bc.valueAxis.valueMax, bc.valueAxis.valueStep = 0, 100, 25
    bc.valueAxis.labels.fontName, bc.valueAxis.labels.fontSize = FONT, 7
    bc.valueAxis.strokeColor = colors.white
    bc.valueAxis.visibleGrid, bc.valueAxis.gridStrokeColor, bc.valueAxis.gridStrokeWidth = True, LINE, 0.4
    bc.bars[0].fillColor, bc.bars[0].strokeColor = MAROON, None
    bc.barWidth, bc.groupSpacing = 10, 10
    bc.barLabelFormat, bc.barLabels.fontName, bc.barLabels.fontSize = "%.0f", FONT, 7
    bc.barLabels.nudge = 6
    d.add(bc)
    if cohort_avg is not None:
        y = bc.y + bc.height * cohort_avg / 100
        d.add(Line(bc.x, y, bc.x + bc.width, y, strokeColor=GREY, strokeWidth=0.8, strokeDashArray=[3, 2]))
    d.add(String(bc.x, h - 9, "Average score per mock return (%)", fontName=FONT_BOLD, fontSize=8, fillColor=GREY))
    if cohort_avg is not None:
        d.add(String(bc.x + bc.width, h - 9, f"- - -  all batches average {cohort_avg:.0f}%", fontName=FONT,
                     fontSize=7, fillColor=GREY, textAnchor="end"))
    return d


def batch_report_pdf(batch, trainer, summary, students, mocks, missing, mistakes, integ, cfg, institute,
                     cohort_avg=None, integrity_ran=True, caution="", improper=None):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=13 * mm,
                            bottomMargin=15 * mm, title=f"Batch report - {batch}", author=institute)
    W = A4[0] - 28 * mm
    s = summary
    story = [Paragraph(_esc(institute.upper()), ParagraphStyle("i", parent=SMALL, fontName=FONT_BOLD,
                                                                 textColor=MAROON)),
             Paragraph(f"Batch Report - {_esc(batch)}", H1),
             Paragraph(f"Trainer: <b>{_esc(trainer or 'not set')}</b> &nbsp;·&nbsp; {s['Students']} students "
                       f"&nbsp;·&nbsp; {s['Mocks done']} of {cfg['n_mocks']:g} mock returns done &nbsp;·&nbsp; "
                       f"as of {datetime.now().strftime('%d %b %Y')}", SMALL)]
    rule = Table([[""]], colWidths=[W], rowHeights=[2])
    rule.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.8, MAROON)]))
    story += [rule, Spacer(1, 6)]

    pct = lambda v: "-" if v is None or pd.isna(v) else f"{v:g}%"
    story.append(_tiles([("Submission rate", pct(s["Submission rate %"]),
                          f"{s['Returns received']} of {s['Returns expected']} returns"),
                         ("On-time rate", pct(s["On-time rate %"]), f"{s['Status not set']} not set"
                          if s["Status not set"] else ""),
                         ("Average return score", pct(s["Average score %"]), f"median {pct(s['Median score %'])}"),
                         ("MCQ average", pct(s["MCQ average %"]), ""),
                         ("Integrity flags", str(s["Integrity flags"]) if integrity_ran else "-",
                          "likely or very likely copied" if integrity_ran else "not run")], W))
    story.append(Spacer(1, 6))
    ch = _bar_chart(mocks, W, cohort_avg)
    if ch:
        story.append(ch)

    story.append(Paragraph("Mock returns", H2))
    story.append(_table(mocks.drop(columns=["Students"]), [9, 26, 9, 11, 8, 7, 10, 10, 10, 10, 10], W))

    story.append(CondPageBreak(60 * mm))
    story.append(Paragraph("Students - progress so far", H2))
    pa, po = cfg["w_accuracy"] / cfg["n_mocks"], cfg["w_on_time"] / cfg["n_mocks"]
    story.append(Paragraph(f"Marks out of 100 earned so far. Each mock return is worth {pa:.2f} accuracy marks (in "
                           f"proportion to its score) and {po:.2f} on-time marks; each MCQ test "
                           f"{cfg['w_mcq'] / cfg['n_mcq'] if cfg['n_mcq'] else 0:.2f} marks. 'Possible' = the marks "
                           f"the returns and tests done so far were worth. Lowest first, so the students who need help are at the top.", SMALL))
    st_cols = ["Student ID", "Name", "Returns evaluated", "Average return score", "On time", "Late",
               "Total / 100", "Marks possible so far", "% of possible so far"]
    stab = students[st_cols].sort_values(["% of possible so far", "Total / 100"]).rename(
        columns={"Returns evaluated": "Returns", "Average return score": "Avg return %", "Total / 100": "Marks",
                 "Marks possible so far": "Possible", "% of possible so far": "% of possible"})
    story.append(_table(stab, [14, 30, 10, 11, 8, 7, 10, 10, 11], W))

    story.append(CondPageBreak(50 * mm))
    story.append(Paragraph("Returns not received", H2))
    story.append(_table(missing.drop(columns=["Batch"]) if not missing.empty else missing, [14, 30, 9, 60], W))

    if improper is not None:
        story.append(CondPageBreak(40 * mm))
        story.append(Paragraph("Improper submissions", H2))
        story.append(Paragraph("Returns that came in but could not be evaluated (unreadable, no PDF, wrong file "
                               "type) or were evaluated but not named StudentID_CaseID.", SMALL))
        story.append(_table(improper[["Student ID", "Name", "Case", "Problem", "Details"]] if not improper.empty
                            else improper, [14, 26, 9, 22, 50], W))

    story.append(CondPageBreak(50 * mm))
    story.append(Paragraph("Most common mistakes", H2))
    story.append(Paragraph("Lines most often wrong in this batch's returns - good topics for a revision class.", SMALL))
    story.append(_table(mistakes, [8, 8, 9, 32, 10, 10, 11, 12], W, max_rows=15))

    story.append(CondPageBreak(60 * mm))
    story.append(Paragraph("Integrity", H2))
    if not integrity_ran:
        story.append(Paragraph("The integrity check was not run for this report.", SMALL))
    else:
        story.append(Paragraph(_esc(caution), SMALL))
        story.append(Spacer(1, 3))
        ist = integ["students"]
        story.append(Paragraph("<b>Students with concerns</b>", TINY))
        story.append(_table(ist[["Student ID", "Name", "Integrity", "Cases flagged", "Linked with"]]
                            if not ist.empty else ist, [14, 26, 16, 18, 40], W, status_col="Integrity"))
        story.append(Spacer(1, 4))
        pr = integ["pairs"]
        story.append(Paragraph("<b>Flagged pairs</b> (a pair is listed in both students' batches)", TINY))
        story.append(_table(pr[["Level", "Case", "Student A", "Name A", "Batch A", "Student B", "Name B", "Batch B",
                                "Evidence"]] if not pr.empty else pr,
                            [12, 7, 10, 14, 8, 10, 14, 8, 40], W, status_col="Level", max_rows=40))
        tr = integ["trainer"]
        if not tr.empty:
            story.append(Spacer(1, 4))
            story.append(Paragraph("<b>Returns built on the trainer's correct-return file</b>", TINY))
            story.append(_table(tr[["Level", "Case", "Student", "Name", "Evidence"]], [12, 8, 12, 22, 60], W,
                                status_col="Level"))

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont(FONT, 7)
        canvas.setFillColor(GREY)
        canvas.drawString(14 * mm, 8 * mm, f"{institute}  •  Batch report  •  {batch}  •  Confidential - for the "
                                            "batch trainer")
        canvas.drawRightString(A4[0] - 14 * mm, 8 * mm, f"Page {d.page}")
        canvas.restoreState()
    flat = []
    for x in story:
        flat.extend(x if isinstance(x, list) else [x])
    doc.build(flat, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def batch_report_excel(batch, trainer, summary, students, mocks, missing, mistakes, integ, integrity_ran=True,
                       improper=None):
    buf = io.BytesIO()
    head = pd.DataFrame([{"Item": k, "Value": v} for k, v in summary.items()])
    head = pd.concat([pd.DataFrame([{"Item": "Report", "Value": f"Batch report - {batch}"},
                                    {"Item": "Trainer", "Value": trainer or ""},
                                    {"Item": "Prepared on", "Value": datetime.now().strftime("%d %b %Y %H:%M")}]),
                      head[~head["Item"].isin(["Batch", "Trainer"])]])
    sheets = [("Summary", head), ("Students", students), ("Mock returns", mocks), ("Not received", missing)]
    if improper is not None:
        sheets.append(("Improper submissions", improper))
    sheets.append(("Common mistakes", mistakes))
    if integrity_ran:
        sheets += [("Integrity - students", integ["students"]), ("Integrity - pairs", integ["pairs"]),
                   ("Integrity - trainer file", integ["trainer"])]
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets:
            (df if df is not None and not df.empty else pd.DataFrame({"": ["None"]})).to_excel(
                xw, sheet_name=name[:31], index=False)
        for ws in xw.book.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(
                    max(len(str(c.value or "")) for c in col) + 2, 60)
    return buf.getvalue()
