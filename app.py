"""
Tax Return Evaluator - Streamlit app
Staff upload a case answer key (Excel) once, then upload one student return (ATX PDF) at a time
and get a category-wise evaluation report with feedback.

Nothing is stored on the server: the key, returns and results live only in the open browser session.
"""
import io
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from grader_core import KeyError_, ReturnPDF, fmt, grade, load_key, summarise
from report_pdf import build_report
from make_keys import draft_from_return

st.set_page_config(page_title="Tax Return Evaluator", page_icon="📝", layout="wide")
HERE = Path(__file__).parent


def report_name(student_id, case_id):
    clean = lambda x: re.sub(r"[^A-Za-z0-9-]+", "_", str(x or "NA")).strip("_") or "NA"
    return f"Evaluation_report_{clean(student_id)}_{clean(case_id)}.pdf"


def secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:          # no secrets file (e.g. running locally)
        return default


# ------------------------------------------------------------------ access
PASSWORD = secret("app_password")
if PASSWORD and not st.session_state.get("authed"):
    st.title("Tax Return Evaluator")
    pw = st.text_input("Staff password", type="password")
    if st.button("Sign in"):
        if pw == PASSWORD:
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()

ss = st.session_state
ss.setdefault("key", None)
ss.setdefault("key_name", None)
ss.setdefault("results", [])        # list of dicts, one per evaluated return
ss.setdefault("last", None)
ss.setdefault("uploader_n", 0)

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("1. Answer key")
    institute = st.text_input("Institute name on reports", secret("institute", "Simandhar Education"))
    key_file = st.file_uploader("Upload the case answer key (.xlsx)", type=["xlsx"], key="keyfile")
    if key_file is not None and key_file.name != ss.key_name:
        try:
            ss.key = load_key(key_file.getvalue())
            ss.key_name = key_file.name
            ss.last = None
        except KeyError_ as e:
            ss.key, ss.key_name = None, None
            st.error(f"The answer key has problems:\n\n{e}")
        except Exception as e:
            ss.key, ss.key_name = None, None
            st.error(f"Could not read this workbook: {e}")
    if ss.key:
        k = ss.key
        st.success(f"Case {k.case_id} loaded")
        st.caption(f"{k.title}  \nTax year {k.tax_year} · {len(k.steps)} steps · {k.total:g} marks")
        st.dataframe(pd.DataFrame([{"Category": c, "Marks": sum(s["Marks"] for s in k.steps if s["Category"] == c)}
                                   for c in k.categories]), hide_index=True)
    tpl = HERE / "answer_key_template.xlsx"
    if tpl.exists():
        st.download_button("Download blank answer-key template", tpl.read_bytes(),
                           file_name="answer_key_template.xlsx")

st.title("Tax Return Evaluator")
tab_eval, tab_session, tab_build, tab_help = st.tabs(["Evaluate a return", "Results this session",
                                                     "Build a key from a correct return", "How it works"])

# ------------------------------------------------------------------ evaluate
with tab_eval:
    if not ss.key:
        st.info("Start by uploading the case answer key in the left panel.")
    else:
        k = ss.key
        st.subheader(f"2. Upload a student return — Case {k.case_id}")
        pdf = st.file_uploader("Student return printed from ATX (complete return, PDF)", type=["pdf"],
                               key=f"ret{ss.uploader_n}")
        guess = ""
        if pdf is not None:
            m = re.match(r"^([A-Za-z0-9-]+)_", pdf.name)
            guess = m.group(1).upper() if m else ""
        c1, c2 = st.columns(2)
        sname = c1.text_input("Student name", key=f"sn{ss.uploader_n}")
        sid = c2.text_input("Student ID", value=guess, key=f"sid{ss.uploader_n}")

        if st.button("Evaluate", type="primary", disabled=pdf is None):
            with st.spinner("Reading the return and marking it..."):
                try:
                    ret = ReturnPDF(pdf.getvalue())
                except Exception as e:
                    st.error(f"This PDF could not be opened: {e}")
                    st.stop()
                if not ret.copies:
                    st.error("No IRS form pages were recognised in this PDF. Make sure it is the complete return "
                             "printed to PDF from ATX (not a scan or a photo).")
                    st.stop()
                rows = grade(ret, k)
                cats, total, out_of = summarise(rows)
                report = build_report(sname, sid, k, rows, cats, total, out_of, pdf.name, institute)
                rec = {"Student ID": sid or "-", "Student Name": sname or "-", "Case": k.case_id,
                       "File": pdf.name, "Score": total, "Out of": out_of,
                       "Percent": total / out_of if out_of else 0,
                       "Evaluated": datetime.now().strftime("%d-%b %H:%M"),
                       "rows": rows, "cats": cats, "report": report,
                       "pages": ret.page_map, "forms": ret.forms_found()}
                ss.results.append(rec)
                ss.last = rec

        r = ss.last
        if r and r["Case"] == k.case_id:
            st.divider()
            rows, cats = r["rows"], r["cats"]
            n_bad = sum(x["Result"] == "Wrong" for x in rows)
            n_unx = sum(x["Result"] == "Unexpected entry" for x in rows)
            review = [x for x in rows if x["Needs Review"]]
            st.subheader(f"Evaluation — {r['Student Name']} ({r['Student ID']})")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Score", f"{r['Score']:g} / {r['Out of']:g}")
            m2.metric("Percentage", f"{r['Percent']:.0%}")
            m3.metric("Wrong items", n_bad)
            m4.metric("Unexpected entries", n_unx)
            if review:
                st.warning(f"{len(review)} line(s) could not be read from the PDF and were marked 0. "
                           "Check them under 'What the system read' before sharing the report.")

            st.download_button("Download evaluation report (PDF)", r["report"], type="primary",
                               file_name=report_name(r["Student ID"], r["Case"]), mime="application/pdf")

            st.markdown("#### Marks by category")
            st.dataframe(
                pd.DataFrame([{"Category": c["Category"], "Marks": c["Awarded"], "Out of": c["Marks"],
                               "Score": round(c["Pct"] * 100) if c["Marks"] else None, "Mistakes": c["Mistakes"]} for c in cats]),
                hide_index=True,
                column_config={"Score": st.column_config.ProgressColumn("Score", format="%d%%",
                                                                        min_value=0, max_value=100)})

            st.markdown("#### Where marks were lost")
            wrong = [x for x in rows if x["Result"] != "Correct"]
            if not wrong:
                st.success("No mistakes found.")
            else:
                blank = lambda v: "(blank)" if str(v).strip() == "" else fmt(v)
                st.dataframe(pd.DataFrame([{
                    "Step": x["Step"], "Form": x["Form"], "Line": str(x["Line"]), "What was checked": x["Description"],
                    "Student": blank(x["Student Value"]), "Correct": blank(x["Correct Value"]),
                    "Marks lost": x["Marks"] - x["Awarded"],
                    "Feedback": (x["Feedback"] + (f" [{x['Note']}]" if x["Note"] else "")).strip()} for x in wrong]),
                    hide_index=True,
                    column_config={"Feedback": st.column_config.TextColumn(width="large")})

            with st.expander("All steps"):
                st.dataframe(pd.DataFrame([{
                    "Step": x["Step"], "Category": x["Category"], "Form": x["Form"], "Line": str(x["Line"]),
                    "What was checked": x["Description"], "Student": fmt(x["Student Value"]),
                    "Correct": fmt(x["Correct Value"]), "Marks": f"{x['Awarded']:g}/{x['Marks']:g}",
                    "Result": x["Result"]} for x in rows]), hide_index=True)

            with st.expander("What the system read (for staff checks)"):
                st.write("**Forms recognised:** " + ", ".join(r["forms"]))
                st.dataframe(pd.DataFrame(r["pages"], columns=["PDF page", "Recognised as"]),
                             hide_index=True)
                notes = [x for x in rows if x["Note"]]
                if notes:
                    st.write("**Notes:**")
                    for x in notes:
                        st.write(f"- Step {x['Step']} ({x['Form']} line {x['Line']}): {x['Note']}")

            if st.button("Next student"):
                ss.uploader_n += 1
                ss.last = None
                st.rerun()

# ------------------------------------------------------------------ session
with tab_session:
    if not ss.results:
        st.info("Returns you evaluate in this browser session will be listed here.")
    else:
        summary = pd.DataFrame([{k_: v for k_, v in x.items()
                                 if k_ in ("Student ID", "Student Name", "Case", "File", "Score", "Out of",
                                           "Percent", "Evaluated")} for x in ss.results])
        summary["Percent"] = (summary["Percent"] * 100).round(1)
        st.dataframe(summary, hide_index=True,
                     column_config={"Percent": st.column_config.NumberColumn(format="%.0f%%")})
        detail = pd.DataFrame([{"Student ID": x["Student ID"], "Student Name": x["Student Name"], "Case": x["Case"],
                                "Step": y["Step"], "Category": y["Category"], "Form": y["Form"],
                                "Line": str(y["Line"]), "Description": y["Description"],
                                "Student Value": fmt(y["Student Value"]), "Correct Value": fmt(y["Correct Value"]),
                                "Marks": y["Marks"], "Awarded": y["Awarded"], "Result": y["Result"],
                                "Feedback": y["Feedback"]} for x in ss.results for y in x["rows"]])
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            summary.to_excel(xw, sheet_name="Summary", index=False)
            detail.to_excel(xw, sheet_name="Detail", index=False)
            for ws in xw.book.worksheets:
                for col in ws.columns:
                    ws.column_dimensions[col[0].column_letter].width = min(
                        60, max(10, max(len(str(c.value or "")) for c in col) + 2))
        st.download_button("Download all results this session (Excel)", buf.getvalue(),
                           file_name=f"evaluation_results_{datetime.now():%Y%m%d_%H%M}.xlsx")
        st.caption("Results are kept only while this browser tab is open. Download them before closing.")
        pick = st.selectbox("Download a report again", range(len(ss.results)),
                            format_func=lambda i: f"{ss.results[i]['Student ID']} · {ss.results[i]['Case']} · "
                                                  f"{ss.results[i]['Evaluated']}")
        st.download_button("Download that report (PDF)", ss.results[pick]["report"],
                           file_name=report_name(ss.results[pick]["Student ID"], ss.results[pick]["Case"]),
                           mime="application/pdf")

# ------------------------------------------------------------------ build key
with tab_build:
    st.subheader("Build an answer key from the trainer's correct return")
    st.write("Upload the trainer's **correct** return printed from ATX. Every filled-in item on every form is listed "
             "in a new answer key (amounts, names, SSN, address, checkboxes, dependents). Open it in Excel, set the "
             "marks, categories and feedback, check anything that should be blank, then upload it in the left panel.")
    good = st.file_uploader("Correct return (PDF from ATX)", type=["pdf"], key="goodret")
    b1, b2, b3 = st.columns([1, 2, 1])
    cid = b1.text_input("Case ID", "C01")
    ctitle = b2.text_input("Case title", "")
    cyear = b3.number_input("Tax year", 2015, 2035, 2020)
    b4, b5 = st.columns(2)
    dmarks = b4.number_input("Default marks per item", 0.0, 100.0, 1.0, 0.5)
    dunx = b5.number_input("Marks deducted per unexpected entry", 0.0, 100.0, 1.0, 0.5)
    if st.button("Build answer key", disabled=good is None):
        try:
            data, n, forms = draft_from_return(good.getvalue(), cid, ctitle, int(cyear), dmarks, dunx)
        except Exception as e:
            st.error(f"Could not read this return: {e}")
        else:
            if n == 0:
                st.error("No filled-in items were found. Make sure this is the complete return printed from ATX.")
            else:
                st.success(f"{n} items found on: {', '.join(forms)}")
                st.download_button("Download draft answer key (.xlsx)", data,
                                   file_name=f"answer_key_{re.sub(r'[^A-Za-z0-9-]+', '_', cid)}.xlsx")
                st.caption("Check the draft against the case before using it: the key is only as correct as the "
                           "return it was built from.")

# ------------------------------------------------------------------ help
with tab_help:
    st.markdown("""
**Workflow**
1. Upload the case answer key (Excel) in the left panel — once per case. To create one quickly, use
   **Build a key from a correct return**.
2. Upload one student's return (complete return printed to PDF from ATX), type the student's name and ID, click **Evaluate**.
3. Download the PDF evaluation report. Click **Next student** and repeat.
4. At the end, download all results from **Results this session**.

**How marks are given**
- Every item in the key is compared with the student's return: amounts on every line of every form and
  schedule, filing status, names, SSNs, address, checkboxes, dependents and occupation.
- **Correct**: matches the key (within the rounding tolerance) → full marks for the item.
- **Wrong**: any difference → the item's marks are lost, with the trainer's feedback. No partial credit.
- **Unexpected entry**: an amount on a line the key says nothing about (it should be blank) → deducts the marks
  set in the key. The score never goes below 0.
- A form missing from the return makes all its items wrong. An item the system could not read is flagged for staff review.

**Student PDF rules**: print the *complete* return from ATX to PDF (not scanned), without masking SSNs.

**Privacy**: nothing is saved on the server. The key, returns and results exist only in this browser session.
""")
