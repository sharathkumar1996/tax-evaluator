"""
roster_flag.py - one consistent, Simandhar-branded way to point out students who are not on our roster.

Look (same palette as the PDFs in brand.py):
  * the row is shaded light maroon (#F7ECEF) and its text is bold maroon (#8E1B3A),
  * a 'Roster' column reads NOT ON ROSTER,
  * a call-out panel above the table - light maroon, maroon left rule - says how many and what to do.
Used on screen (pandas Styler for st.dataframe, HTML call-out) and in Excel downloads (openpyxl fills).
"""
import html
import io

import pandas as pd

MAROON_HEX = "#8E1B3A"
MAROON_LIGHT_HEX = "#F7ECEF"
NOT_ON_ROSTER = "NOT ON ROSTER"
ON_ROSTER = "On roster"
ROW_CSS = f"background-color: {MAROON_LIGHT_HEX}; color: {MAROON_HEX}; font-weight: 700"


def off_roster_ids(ids, roster_ids):
    """The IDs in ids (blank ones ignored) that are not in roster_ids, upper-cased, sorted."""
    known = {str(x).strip().upper() for x in roster_ids}
    return sorted({str(i).strip().upper() for i in ids if str(i or "").strip()} - known)


def add_roster_column(df, roster_ids, col="Student ID"):
    """A copy of df with a 'Roster' column right after the ID column: NOT ON ROSTER / On roster ('' when the
    row has no student ID)."""
    out = df.copy()
    if out.empty or col not in out:
        return out
    known = {str(x).strip().upper() for x in roster_ids}
    flag = out[col].map(lambda v: "" if not str(v or "").strip() or str(v).lower() == "nan" else
                        (ON_ROSTER if str(v).strip().upper() in known else NOT_ON_ROSTER))
    if "Roster" in out:
        out = out.drop(columns=["Roster"])
    out.insert(list(out.columns).index(col) + 1, "Roster", flag)
    return out


def style_off_roster(df, flag_col="Roster"):
    """pandas Styler that shades the NOT ON ROSTER rows in Simandhar maroon (for st.dataframe)."""
    def row_style(r):
        return [ROW_CSS if r.get(flag_col) == NOT_ON_ROSTER else ""] * len(r)
    if df.empty or flag_col not in df:
        return df
    return df.style.apply(row_style, axis=1)


def panel_html(label, body):
    """Simandhar call-out panel: light maroon, maroon left rule, bold maroon label, then body (HTML)."""
    return (f"<div style='background:{MAROON_LIGHT_HEX};border-left:4px solid {MAROON_HEX};padding:10px 14px;"
            f"border-radius:4px;margin:6px 0 10px 0;color:#2b2b2b'>"
            f"<span style='color:{MAROON_HEX};font-weight:700;letter-spacing:.3px'>{html.escape(label)}</span>"
            f" &nbsp;·&nbsp; {body}</div>")


def callout_html(ids, what="student ID(s)", action="Add them under <b>Students &amp; batches</b> so they count in "
                                                    "results and batch reports.",
                 label=NOT_ON_ROSTER, tail="not on our roster"):
    """Branded call-out panel naming the IDs (shown with st.markdown(..., unsafe_allow_html=True))."""
    shown = ", ".join(html.escape(str(i)) for i in ids[:30]) + (" ..." if len(ids) > 30 else "")
    return panel_html(label, f"<b>{len(ids)}</b> {what} {tail}: "
                             f"<span style='color:{MAROON_HEX};font-weight:700'>{shown}</span><br/>"
                             f"<span style='font-size:0.9em'>{action}</span>")


def highlight_excel_sheet(ws, flag_header="Roster"):
    """Shade the NOT ON ROSTER rows of an openpyxl worksheet (header in row 1) in Simandhar maroon."""
    from openpyxl.styles import Font, PatternFill
    heads = [c.value for c in ws[1]]
    if flag_header not in heads:
        return
    k = heads.index(flag_header)
    fill = PatternFill("solid", fgColor=MAROON_LIGHT_HEX[1:])
    font = Font(bold=True, color=MAROON_HEX[1:])
    for row in ws.iter_rows(min_row=2):
        if row[k].value == NOT_ON_ROSTER:
            for c in row:
                c.fill, c.font = fill, font


def excel_bytes(sheets):
    """sheets: [(name, DataFrame)] -> .xlsx bytes, with off-roster rows highlighted on every sheet."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets:
            (df if df is not None and not df.empty else pd.DataFrame({"": ["None"]})).to_excel(
                xw, sheet_name=name[:31], index=False)
        for ws in xw.book.worksheets:
            highlight_excel_sheet(ws)
            ws.freeze_panes = "A2"
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(
                    max(len(str(c.value or "")) for c in col) + 2, 60)
    return buf.getvalue()


def not_on_roster(evaluations, roster_ids, case_ids=None):
    """Evaluated returns of students who are not on the roster, one row per student:
    Student ID, Roster, Returns evaluated, Cases, Last file."""
    known = {str(x).strip().upper() for x in roster_ids}
    want = set(case_ids) if case_ids is not None else None
    by = {}
    for e in evaluations:
        sid = str(e.get("student_id") or "").strip().upper()
        if not sid or sid in known or (want is not None and e.get("case_id") not in want):
            continue
        d = by.setdefault(sid, {"file": "", "cases": set()})
        d["cases"].add(e.get("case_id") or "")
        d["file"] = e.get("file_name") or d["file"]
    rows = [{"Student ID": sid, "Roster": NOT_ON_ROSTER, "Returns evaluated": len(d["cases"]),
             "Cases": ", ".join(sorted(d["cases"])), "Last file": str(d["file"])[:120]}
            for sid, d in sorted(by.items())]
    return pd.DataFrame(rows, columns=["Student ID", "Roster", "Returns evaluated", "Cases", "Last file"])
