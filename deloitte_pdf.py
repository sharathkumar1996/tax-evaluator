"""Programme progress report for Deloitte: a Simandhar-branded PDF that gives the big picture - where the
training stands, how each batch is doing, and above all which learners need attention so everyone can be deployed.

Built from the same learner tracker as the Deloitte Excel (attendance.tracker), so the numbers always agree.
Deliberately leaves out the integrity check (copying flags); it is not part of this report.

Every learner gets one readiness status:
  On track                 no concern raised so far
  Needs attention          one concern (e.g. missed a mock return, or attendance below the minimum)
  Needs urgent attention   two or more concerns, or absent for everything (no class, no mock, no MCQ test)
"""
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

from attendance import (ATTENTION, FLAG_ALL, FLAG_IMPROPER, FLAG_LOW_ATT, FLAG_LOW_PERF, FLAG_MCQ, FLAG_MOCK,
                        ON_TRACK, READINESS, URGENT, readiness)
from batch_pdf import TINY, _esc, _table, _tiles
from batchreport import ALL, batch_summary, mock_table
from brand import AMBER, FONT, FONT_BOLD, GREEN, GREY, LINE, MAROON, MAROON_LIGHT, RED
from scorecard import BODY, H1, H2, SMALL

READY_COLOR = {ON_TRACK: GREEN, ATTENTION: AMBER, URGENT: RED}

# plain-language wording for Deloitte
CONCERN = {FLAG_ALL: "Absent for everything", FLAG_LOW_ATT: "Low class attendance", FLAG_MOCK: "Mock return(s) missing",
           FLAG_IMPROPER: "Mock submission not accepted", FLAG_MCQ: "MCQ test(s) missed",
           FLAG_LOW_PERF: "Low scores"}
CONCERN_ORDER = [FLAG_ALL, FLAG_LOW_PERF, FLAG_LOW_ATT, FLAG_MOCK, FLAG_IMPROPER, FLAG_MCQ]


def _concerns(flags):
    fl = [f.strip() for f in str(flags or "").split(",") if f.strip()]
    if FLAG_ALL in fl:          # the other concerns follow from it
        return CONCERN[FLAG_ALL]
    return "; ".join(CONCERN.get(f, f) for f in CONCERN_ORDER if f in fl)


def _num(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else float(v)


def _pct(v, nd=0):
    v = _num(v)
    return "-" if v is None else f"{v:.{nd}f}%"


def programme_data(track, studs, evs, mcq, final_cases, case_info, cfg, classes=None, trainers=None):
    """Everything the report shows, as plain tables (also handy for tests and the screen)."""
    t = track.copy()
    if t.empty:
        t["Readiness"] = []
    else:
        t["Readiness"] = t["Flags"].apply(readiness)
        t["Concerns"] = t["Flags"].apply(_concerns)
    bs = batch_summary(studs, evs, mcq, final_cases, cfg, trainers=trainers)
    bs = bs.drop(columns=[c for c in ("Integrity flags", "Status not set") if c in bs.columns])

    rows = []
    for b in list(bs["Batch"]):
        tb = t if b == ALL else t[t["Batch"] == b]
        att = tb["Attendance %"].dropna() if not tb.empty else pd.Series(dtype=float)
        r = {"Batch": b, "Learners": len(tb), "Attendance %": round(float(att.mean()), 1) if len(att) else None}
        for k in READINESS:
            r[k] = int((tb["Readiness"] == k).sum()) if not tb.empty else 0
        rows.append(r)
    ready = pd.DataFrame(rows)
    batches = bs.merge(ready, on="Batch", how="left")

    mocks = mock_table(studs, evs, final_cases, case_info)
    concerns = []
    for f in CONCERN_ORDER:
        n = int(t["Flags"].str.contains(f, regex=False).sum()) if not t.empty else 0
        concerns.append({"Concern": CONCERN[f], "Learners": n, "flag": f})
    concerns = pd.DataFrame(concerns)

    att_trend = pd.DataFrame(columns=["Date", "Attendance %"])
    if classes is not None and not classes.empty:
        g = classes.groupby("Date")[["Present", "Learners"]].sum().reset_index()
        g["Attendance %"] = (100 * g["Present"] / g["Learners"]).round(1)
        att_trend = g[["Date", "Attendance %"]]

    held = {"classes": int(t["Classes held"].max()) if not t.empty else 0,
            "mocks": len(mocks), "mcq": int(t["MCQ tests held"].max()) if not t.empty else 0}
    return {"track": t, "batches": batches, "mocks": mocks, "concerns": concerns, "att_trend": att_trend,
            "held": held}


def _bars(labels, vals, title, W, ref=None, ref_label="", color=MAROON, h=48 * mm):
    if not vals:
        return None
    d = Drawing(W, h)
    bc = VerticalBarChart()
    bc.x, bc.y, bc.width, bc.height = 24, 18, W - 40, h - 34
    bc.data = [[float(v or 0) for v in vals]]
    bc.categoryAxis.categoryNames = [str(x) for x in labels]
    bc.categoryAxis.labels.fontName, bc.categoryAxis.labels.fontSize = FONT, 6.5 if len(labels) > 12 else 7
    bc.categoryAxis.strokeColor = LINE
    bc.valueAxis.valueMin, bc.valueAxis.valueMax, bc.valueAxis.valueStep = 0, 100, 25
    bc.valueAxis.labels.fontName, bc.valueAxis.labels.fontSize = FONT, 7
    bc.valueAxis.strokeColor = colors.white
    bc.valueAxis.visibleGrid, bc.valueAxis.gridStrokeColor, bc.valueAxis.gridStrokeWidth = True, LINE, 0.4
    bc.bars[0].fillColor, bc.bars[0].strokeColor = color, None
    bc.barWidth = 10
    bc.groupSpacing = max(4, min(14, (W - 40) / max(len(vals), 1) - 10))
    bc.barLabelFormat, bc.barLabels.fontName, bc.barLabels.fontSize = "%.0f", FONT, 6.5
    bc.barLabels.nudge = 6
    d.add(bc)
    d.add(String(bc.x, h - 9, title, fontName=FONT_BOLD, fontSize=8, fillColor=GREY))
    if ref is not None:
        y = bc.y + bc.height * ref / 100
        d.add(Line(bc.x, y, bc.x + bc.width, y, strokeColor=RED, strokeWidth=0.8, strokeDashArray=[3, 2]))
        d.add(String(bc.x + bc.width, h - 9, f"- - -  {ref_label}", fontName=FONT, fontSize=7, fillColor=RED,
                     textAnchor="end"))
    return d


def _readiness_bar(counts, total, W):
    """One horizontal stacked bar: on track / needs attention / urgent, with counts."""
    if not total:
        return None
    lab = ParagraphStyle("rb", parent=SMALL, fontName=FONT_BOLD, textColor=colors.white, alignment=1, fontSize=8)
    cells, widths, styles = [], [], []
    for i, k in enumerate(READINESS):
        n = counts.get(k, 0)
        if not n:
            continue
        w = max(W * n / total, 22 * mm)
        cells.append(Paragraph(f"{n} ({100 * n / total:.0f}%)", lab))
        widths.append(w)
        styles.append(("BACKGROUND", (len(cells) - 1, 0), (len(cells) - 1, 0), READY_COLOR[k]))
    scale = W / sum(widths)
    t = Table([cells], colWidths=[w * scale for w in widths], rowHeights=[9 * mm])
    t.setStyle(TableStyle(styles + [("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    key = Paragraph(" &nbsp;·&nbsp; ".join(f"<font color='#{READY_COLOR[k].hexval()[2:]}'><b>{k}</b></font>"
                                          for k in READINESS), SMALL)
    return [t, Spacer(1, 2), key]


def key_messages(data, cfg, min_att, low_perf):
    """Short, data-driven sentences for the first page."""
    t, b, m, held = data["track"], data["batches"], data["mocks"], data["held"]
    out = []
    if t.empty:
        return ["No learners on the roster yet."]
    n = len(t)
    allb = b.set_index("Batch").loc[ALL]
    on, att_n, urg = (int((t["Readiness"] == k).sum()) for k in READINESS)
    out.append(f"<b>{on} of {n} learners ({100 * on / n:.0f}%) are on track</b>; {att_n} need attention and "
               f"<b>{urg} need urgent attention</b> (named at the end of this report).")
    a = _num(allb.get("Attendance %"))
    if a is not None:
        low = int(t["Flags"].str.contains(FLAG_LOW_ATT, regex=False).sum())
        out.append(f"Class attendance (80% of a session or more) averages <b>{a:.0f}%</b> over {held['classes']} "
                   f"class(es); {low} learner(s) are below the {min_att:g}% minimum.")
    sr = _num(allb.get("Submission rate %"))
    if held["mocks"]:
        out.append(f"{held['mocks']} of {int(cfg['n_mocks'])} mock returns done: <b>{_pct(sr)}</b> of the returns "
                   f"due were received and evaluated, on-time rate {_pct(allb.get('On-time rate %'))}, average "
                   f"score <b>{_pct(allb.get('Average score %'))}</b>.")
        if len(m) >= 2:
            first, last = m.iloc[0], m.iloc[-1]
            diff = (_num(last["Average %"]) or 0) - (_num(first["Average %"]) or 0)
            word = "up" if diff > 0.5 else "down" if diff < -0.5 else "about level"
            out.append(f"Average mock score moved from {_pct(first['Average %'])} ({first['Case']}) to "
                       f"{_pct(last['Average %'])} ({last['Case']}) - {word}"
                       + (f" {abs(diff):.0f} points." if word != "about level" else "."))
    if held["mcq"]:
        out.append(f"{held['mcq']} of {int(cfg['n_mcq'])} MCQ tests held; average {_pct(allb.get('MCQ average %'))}.")
    lp = int(t["Flags"].str.contains(FLAG_LOW_PERF, regex=False).sum())
    if lp:
        out.append(f"{lp} learner(s) are scoring below {low_perf:g}% of the marks possible so far.")
    nb = b[b["Batch"] != ALL]
    if len(nb) > 1 and nb["Learners"].sum():
        nb = nb.assign(share=nb[URGENT] / nb["Learners"].where(nb["Learners"] > 0))
        w = nb.sort_values("share", ascending=False).iloc[0]
        if w[URGENT]:
            out.append(f"{_esc(w['Batch'])} has the highest share needing urgent attention "
                       f"({int(w[URGENT])} of {int(w['Learners'])}).")
    return out


def asks_for_deloitte(data, cfg):
    """What Simandhar asks Deloitte to help with (only the ones that apply)."""
    c = data["concerns"].set_index("flag")["Learners"]
    asks = []
    if c.get(FLAG_ALL, 0):
        asks.append(f"<b>{c[FLAG_ALL]} learner(s) have not joined any class, mock or MCQ test.</b> Please confirm "
                    "whether they are still in the programme, and if so ask their managers to free up time.")
    if c.get(FLAG_LOW_ATT, 0):
        asks.append(f"{c[FLAG_LOW_ATT]} learner(s) have low class attendance - a reminder from their managers "
                    "that the sessions count towards deployment would help.")
    if c.get(FLAG_MOCK, 0) or c.get(FLAG_IMPROPER, 0):
        asks.append(f"{c.get(FLAG_MOCK, 0)} learner(s) have mock returns missing and {c.get(FLAG_IMPROPER, 0)} "
                    "submitted a return that could not be accepted (wrong folder/file name or unreadable file). "
                    f"Mock returns carry {cfg['w_accuracy'] + cfg['w_on_time']:g} of the 100 final marks, so every "
                    "missed return costs marks.")
    if c.get(FLAG_LOW_PERF, 0):
        asks.append(f"{c[FLAG_LOW_PERF]} learner(s) are scoring low. Simandhar's walkthrough videos and query "
                    "sessions cover the common mistakes; we suggest these learners make use of them.")
    return asks


def deloitte_report_pdf(data, cfg, institute="Simandhar Education", min_att=80.0, low_perf=70.0, as_of=None):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=13 * mm,
                            bottomMargin=15 * mm, title="Programme progress report - Deloitte Form 1040 training",
                            author=institute)
    W = A4[0] - 28 * mm
    as_of = as_of or datetime.now().strftime("%d %b %Y")
    t, b, m, held = data["track"], data["batches"], data["mocks"], data["held"]
    n = len(t)
    allb = b.set_index("Batch").loc[ALL] if not b.empty else {}

    story = [Paragraph(_esc(institute.upper()), ParagraphStyle("i", parent=SMALL, fontName=FONT_BOLD,
                                                                 textColor=MAROON)),
             Paragraph("Deloitte Form 1040 Training - Programme Progress Report", H1),
             Paragraph(f"As of {as_of} &nbsp;·&nbsp; {n} learners in {max(len(b) - 1, 0)} batches &nbsp;·&nbsp; "
                       f"{held['classes']} classes held &nbsp;·&nbsp; {held['mocks']} of {int(cfg['n_mocks'])} mock "
                       f"returns &nbsp;·&nbsp; {held['mcq']} of {int(cfg['n_mcq'])} MCQ tests", SMALL)]
    rule = Table([[""]], colWidths=[W], rowHeights=[2])
    rule.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.8, MAROON)]))
    story += [rule, Spacer(1, 6)]

    # ---- headline numbers
    on = int((t["Readiness"] == ON_TRACK).sum()) if n else 0
    story.append(_tiles([
        ("Learners on track", f"{on} / {n}", f"{100 * on / n:.0f}% of the cohort" if n else ""),
        ("Class attendance", _pct(allb.get("Attendance %") if n else None), "attended 80%+ of a session"),
        ("Mock returns received", _pct(allb.get("Submission rate %") if n else None),
         f"{allb.get('Returns received', 0)} of {allb.get('Returns expected', 0)} due" if n else ""),
        ("Average mock score", _pct(allb.get("Average score %") if n else None), ""),
        ("MCQ average", _pct(allb.get("MCQ average %") if n else None), ""),
    ], W))
    story.append(Spacer(1, 8))

    # ---- readiness
    story.append(Paragraph("Deployment readiness", H2))
    story.append(Paragraph("Every learner is placed in one group from what they have done so far: classes, mock "
                           "returns, MCQ tests and scores.", SMALL))
    story.append(Spacer(1, 3))
    rb = _readiness_bar({k: int((t["Readiness"] == k).sum()) for k in READINESS}, n, W) if n else None
    if rb:
        story += rb
    story.append(Spacer(1, 6))

    # ---- key messages
    story.append(Paragraph("Key messages", H2))
    bul = ParagraphStyle("bul", parent=BODY, leftIndent=10, bulletIndent=2, spaceAfter=2)
    for s in key_messages(data, cfg, min_att, low_perf):
        story.append(Paragraph(s, bul, bulletText="•"))

    asks = asks_for_deloitte(data, cfg) if n else []
    if asks:
        story.append(Paragraph("Where we need Deloitte's help", H2))
        box = [Paragraph(a, bul, bulletText="•") for a in asks]
        bt = Table([[box]], colWidths=[W])
        bt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), MAROON_LIGHT),
                                ("LINEBEFORE", (0, 0), (0, -1), 3, MAROON),
                                ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        story.append(bt)

    # ---- progress over time
    story.append(CondPageBreak(110 * mm))
    story.append(Paragraph("Progress over time", H2))
    if not m.empty:
        ch = _bars(list(m["Case"]), list(m["Average %"]), "Average score per mock return (%)", W,
                   ref=cfg["pass_mark"], ref_label=f"pass mark {cfg['pass_mark']:g}")
        if ch:
            story.append(ch)
    at = data["att_trend"]
    if not at.empty:
        labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%d %b") if len(str(d)) == 10 else d
                  for d in at["Date"]]
        ch = _bars(labels, list(at["Attendance %"]), "Class attendance per class date (% attending 80%+)", W,
                   ref=min_att, ref_label=f"minimum {min_att:g}%", color=colors.HexColor("#5B6B8C"))
        if ch:
            story.append(ch)
    if m.empty and at.empty:
        story.append(Paragraph("No mock returns or class attendance recorded yet.", SMALL))

    # ---- batches
    story.append(CondPageBreak(70 * mm))
    story.append(Paragraph("Batch comparison", H2))
    bt = b.rename(columns={"Submission rate %": "Returns received %", "On-time rate %": "On time %",
                           "Average score %": "Avg mock %", "MCQ average %": "MCQ avg %"})
    cols = ["Batch", "Trainer", "Learners", "Attendance %", "Returns received %", "On time %", "Avg mock %",
            "MCQ avg %", ON_TRACK, ATTENTION, URGENT]
    bt = bt[[c for c in cols if c in bt.columns]]
    if "Trainer" in bt and not bt["Trainer"].astype(str).str.strip().any():
        bt = bt.drop(columns=["Trainer"])
    story.append(_table(bt, [14, 14, 8, 10, 11, 9, 9, 9, 9, 10, 11][-len(bt.columns):], W))

    if not m.empty:
        story.append(Paragraph("Mock returns (all batches)", H2))
        story.append(_table(m[["Case", "Title", "Received", "Submission %", "On time", "Late", "Average %",
                               "Median %", "Below 50%"]], [8, 34, 9, 11, 8, 7, 10, 10, 10], W))

    story.append(KeepTogether([Paragraph("Concerns at a glance", H2),
                               Paragraph(f"Low class attendance = below {min_att:g}% of classes attended at 80%+ of "
                                         f"the session. Low scores = below {low_perf:g}% of the marks possible so "
                                         "far. A learner can have more than one concern.", SMALL),
                               _table(data["concerns"][["Concern", "Learners"]], [40, 12], W * 0.55)]))

    # ---- learners needing attention
    for level, intro in ((URGENT, "Two or more concerns, or no participation at all. These learners are the "
                                  "priority for follow-up with their managers."),
                         (ATTENTION, "One concern so far - usually recoverable with a nudge.")):
        sub = t[t["Readiness"] == level] if n else t
        story.append(CondPageBreak(50 * mm))
        story.append(Paragraph(f"{level} ({len(sub)})", H2))
        story.append(Paragraph(intro + " Sorted by batch, lowest score first.", SMALL))
        story.append(Spacer(1, 2))
        if sub.empty:
            story.append(Paragraph("None.", SMALL))
            continue
        lst = sub.assign(
            Mocks=sub["Mocks evaluated"].astype(int).astype(str) + " / " + sub["Mocks due"].astype(int).astype(str),
            MCQ=sub["MCQ tests taken"].astype(int).astype(str) + " / " + sub["MCQ tests held"].astype(int).astype(str),
            _s=sub["% of possible so far"].fillna(-1),
            **{"Attendance %": sub["Attendance %"].round(0), "% of possible so far": sub["% of possible so far"].round(0)})
        lst = lst.sort_values(["Batch", "_s", "Student ID"])
        lst = lst[["Batch", "Student ID", "Name", "Attendance %", "Mocks", "MCQ", "% of possible so far",
                   "Concerns"]].rename(columns={"% of possible so far": "Score so far %", "Attendance %": "Classes %",
                                                "Mocks": "Mocks done / due", "MCQ": "MCQ done / held"})
        story.append(_table(lst, [10, 11, 21, 9, 9, 9, 9, 34], W))

    story.append(Spacer(1, 8))
    story.append(Paragraph("Score so far = share of the marks possible from the mock returns and MCQ tests held so far "
                           f"(final score: mock accuracy {cfg['w_accuracy']:g} + on-time submission "
                           f"{cfg['w_on_time']:g} + MCQ tests {cfg['w_mcq']:g} = 100; pass mark {cfg['pass_mark']:g}). "
                           "Full learner-level detail is in the Excel learner report.", SMALL))

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont(FONT, 7)
        canvas.setFillColor(GREY)
        canvas.drawString(14 * mm, 8 * mm, f"{institute}  •  Deloitte Form 1040 training  •  Programme progress "
                                            f"report  •  {as_of}  •  Confidential")
        canvas.drawRightString(A4[0] - 14 * mm, 8 * mm, f"Page {d.page}")
        canvas.restoreState()
    flat = []
    for x in story:
        flat.extend(x if isinstance(x, list) else [x])
    doc.build(flat, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
