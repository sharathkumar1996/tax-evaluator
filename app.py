"""
Tax Return Evaluator - Streamlit app.

* Evaluate one return or many at once (several PDFs or a zip), each against the case answer key.
* Returns are IRS fillable forms, saved after filling in (one PDF per form is fine; see readers.py).
* With a database (Neon Postgres) every evaluation is saved: students and batches, case keys,
  scores and mistakes. Management reports and any student's report can be produced at any time.
* Without a database the app still works; results then last only while the browser tab is open.
"""
import hashlib
import hmac
import io
import logging
import re
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from bulk import group_uploads
from grader_core import KeyError_, fmt, grade, load_key, summarise
from integrity import TRAINER, analyse, fingerprints
from irs_reader import ReaderError
from make_keys import draft_from_return
from finalscore import (FINAL, LABELS, SO_FAR, MCQError, check_settings, final_scores, load_settings,
                        parse_mcq)
from metrics import flag, rebuild_rows, return_metrics
from readers import read_return
from report_pdf import build_report
from scorecard import results_workbook, scorecard_pdf

st.set_page_config(page_title="Tax Return Evaluator", page_icon="📝", layout="wide")
HERE = Path(__file__).parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tax_evaluator.app")


def secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def clean(x):
    return re.sub(r"[^A-Za-z0-9-]+", "_", str(x or "NA")).strip("_") or "NA"


def report_name(student_id, case_id):
    return f"Evaluation_report_{clean(student_id)}_{clean(case_id)}.pdf"


# ------------------------------------------------------------------ access
PASSWORD = secret("app_password")
if PASSWORD and not st.session_state.get("authed"):
    st.title("Tax Return Evaluator")
    pw = st.text_input("Staff password", type="password")
    if st.button("Sign in"):
        if hmac.compare_digest(pw.encode(), str(PASSWORD).encode()):
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()


# ------------------------------------------------------------------ database
@st.cache_resource(show_spinner="Connecting to the database...")
def get_db(url, code_version):
    # code_version makes Streamlit open a fresh connection whenever db.py changes after an update,
    # instead of reusing a connection object built from the old code
    import importlib
    import db as dbmod
    importlib.reload(dbmod)
    return dbmod.DB(url)


DB_URL = secret("database_url")
DB_CODE_VERSION = hashlib.sha256((HERE / "db.py").read_bytes()).hexdigest()[:12]
db, db_error = None, None
if DB_URL:
    try:
        db = get_db(DB_URL, DB_CODE_VERSION)
    except Exception as e:  # keep the app usable without the database
        db_error = str(e)


@st.cache_data(show_spinner=False)
def parse_key(data: bytes):
    return load_key(data)


ss = st.session_state
for k_, v_ in [("key", None), ("key_bytes", None), ("key_name", None), ("key_hash", None), ("results", []),
               ("last", None), ("uploader_n", 0), ("bulk_out", None)]:
    ss.setdefault(k_, v_)


def use_key(data, name):
    ss.key = parse_key(data)
    ss.key_bytes, ss.key_name = data, name
    ss.key_hash = hashlib.sha256(data).hexdigest()[:16]
    ss.last, ss.bulk_out = None, None


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    if db:
        try:
            st.success(f"Database connected · {db.size_mb():.1f} MB used", icon="🗄️")
        except Exception as e:
            st.warning(f"Database connected but not responding: {e}")
    elif db_error:
        st.error(f"Database not reachable - results will NOT be saved.\n\n{db_error[:300]}")
    else:
        st.info("No database set up: results last only while this tab is open.", icon="ℹ️")
    institute = st.text_input("Institute name on reports", secret("institute", "Simandhar Education"))
    staff = st.text_input("Your name (recorded with each evaluation)", key="staff")

    st.header("1. Case")
    saved = db.cases() if db else []
    if saved:
        labels = ["(choose a saved case)"] + [f"{c['case_id']} - {c['title']}" for c in saved]
        pick = st.selectbox("Saved cases", labels, key="saved_pick")
        if pick != ss.get("prev_pick"):            # act only when the choice changes
            ss.prev_pick = pick
            if pick != labels[0]:
                cid = pick.split(" - ")[0]
                use_key(db.case_key_bytes(cid), f"saved:{cid}")
    key_file = st.file_uploader("Upload a new or updated answer key (.xlsx)", type=["xlsx"], key="keyfile")
    if key_file is not None and (key_file.name, key_file.size) != ss.get("prev_keyfile"):
        try:
            k_new = parse_key(key_file.getvalue())
        except KeyError_ as e:
            st.error(f"The answer key has problems:\n\n{e}")
            k_new = None
        except Exception as e:
            st.error(f"Could not read this workbook: {e}")
            k_new = None
        if k_new:
            ok = True
            if db:
                existing = {c["case_id"]: c for c in saved}
                n_done = db.case_eval_count(k_new.case_id) if k_new.case_id in existing else 0
                new_hash = hashlib.sha256(key_file.getvalue()).hexdigest()[:16]
                if n_done and existing[k_new.case_id]["key_hash"] != new_hash:
                    st.warning(f"Case {k_new.case_id} already has {n_done} evaluated returns. Saving this key changes "
                               "how new returns are marked; returns already marked keep their scores. Re-evaluate them "
                               "if the key has changed.")
                    ok = st.checkbox(f"Yes, replace the saved key for {k_new.case_id}")
                if ok:
                    db.save_case(k_new, key_file.getvalue(), key_file.name)
            if ok:
                ss.prev_keyfile = (key_file.name, key_file.size)
                use_key(key_file.getvalue(), key_file.name)
                if db:
                    st.rerun()
    if ss.key:
        k = ss.key
        st.success(f"Case {k.case_id} ready")
        st.caption(f"{k.title}  \nTax year {k.tax_year} · {len(k.steps)} items · {k.total:g} marks, "
                   "scored out of 100")
        wt = {}
        for s_ in k.steps:
            c_ = wt.setdefault(s_["Category"], [0, 0.0])
            c_[0] += 1
            c_[1] += s_["Marks"]
        st.dataframe(pd.DataFrame([{"Category": c_, "Items": n_, "Marks": m_,
                                    "Weight /100": round(m_ / k.total * 100, 1) if k.total else 0}
                                   for c_, (n_, m_) in wt.items()]),
                     hide_index=True, use_container_width=True,
                     column_config={"Weight /100": st.column_config.ProgressColumn(
                         "Weight /100", format="%.1f", min_value=0, max_value=100)})
    tpl = HERE / "answer_key_template.xlsx"
    if tpl.exists():
        st.download_button("Download blank answer-key template", tpl.read_bytes(), file_name="answer_key_template.xlsx")

st.title("Tax Return Evaluator")
t_eval, t_students, t_mgmt, t_integ, t_find, t_build, t_data, t_help = st.tabs(
    ["Evaluate returns", "Students & batches", "Management dashboard", "Integrity checks", "Find a report",
     "Build a key from a correct return", "Data management", "How it works"])


def evaluate_files(files, sid, sname, key):
    """Read and grade one student's return (one or more PDFs); returns rows, metrics and the PDF report.
    Raises ReaderError with a message the user can act on."""
    ret = read_return(files)
    rows = flag(grade(ret, key))
    m = return_metrics(rows)
    cats, total, out_of = summarise(rows)
    label = files[0][0] if len(files) == 1 else f"{len(files)} files"
    report = build_report(sname, sid, key, rows, cats, total, out_of, label, institute)
    try:
        fp = fingerprints(files, ret)
    except Exception:              # integrity data must never stop a return from being marked
        log.exception("fingerprinting failed for %s", sid)
        fp = None
    return {"fp": fp, "rows": rows, "cats": cats, "m": m, "report": report, "pages": ret.page_map,
            "forms": ret.forms_found(), "format": ret.format_name, "notes": list(ret.notes),
            "unlinked": ret.unlinked(), "file_label": ", ".join(n for n, _ in files)[:500]}


def save(sid, res, on_time=None):
    if db:
        db.save_evaluation(sid, ss.key.case_id, res["file_label"], res["m"], res["rows"], ss.key_hash, staff or "",
                           source_format=res["format"], notes=" | ".join(res["notes"]), fingerprints=res.get("fp"),
                           on_time=on_time)


SUBMITTED = ["On time", "Late"]


def show_result(r):
    rows, cats, m = r["rows"], r["cats"], r["m"]
    st.subheader(f"Evaluation - {r['Student Name']} ({r['Student ID']})")
    c = st.columns(3)
    c[0].metric("Score / 100", f"{round(m['pct'] * 100, 1):g}")
    c[1].metric("Marks", f"{m['score']:g} / {m['out_of']:g}")
    c[2].metric("Errors", m["errors"])
    if m["needs_review"]:
        st.warning(f"{m['needs_review']} item(s) could not be read from the PDF and were marked 0. Check them under "
                   "'What the system read' before sharing the report.")
    st.download_button("Download evaluation report (PDF)", r["report"], type="primary",
                       file_name=report_name(r["Student ID"], r["Case"]), mime="application/pdf")
    st.dataframe(pd.DataFrame([{"Category": x["Category"], "Marks": x["Awarded"], "Out of": x["Marks"],
                                "Score": round(x["Pct"] * 100) if x["Marks"] else None, "Mistakes": x["Mistakes"]}
                               for x in cats]), hide_index=True,
                 column_config={"Score": st.column_config.ProgressColumn("Score", format="%d%%", min_value=0,
                                                                         max_value=100)})
    wrong = [x for x in rows if x["Result"] != "Correct"]
    st.markdown("#### Where marks were lost")
    if not wrong:
        st.success("No mistakes found.")
    else:
        blank = lambda v: "(blank)" if str(v).strip() == "" else fmt(v)
        st.dataframe(pd.DataFrame([{
            "Step": x["Step"], "Form": x["Form"], "Line": str(x["Line"]), "What was checked": x["Description"],
            "Student": blank(x["Student Value"]),
            "Correct": blank(x["Correct Value"]), "Marks lost": x["Marks"] - x["Awarded"],
            "Feedback": (x["Feedback"] + (f" [{x['Note']}]" if x["Note"] else "")).strip()} for x in wrong]),
            hide_index=True, column_config={"Feedback": st.column_config.TextColumn(width="large")})
    for n in r.get("notes", []):
        st.warning(n)
    with st.expander("What the system read (for staff checks)"):
        st.write("**Forms recognised:** " + ", ".join(r["forms"]))
        st.dataframe(pd.DataFrame(r["pages"], columns=["PDF page", "Recognised as"]), hide_index=True)
        if r.get("unlinked"):
            st.write("**Filled-in boxes not linked to a line** (not marked; use `field: <name>` in the key "
                     "to mark one):")
            st.dataframe(pd.DataFrame([{"Form": x["form"], "Key line": x["line"], "Value": fmt(x["value"]),
                                        "Box": x["desc"]} for x in r["unlinked"]]), hide_index=True)


# ------------------------------------------------------------------ evaluate
with t_eval:
    if not ss.key:
        st.info("Start by choosing a saved case or uploading the case answer key in the left panel.")
    else:
        k = ss.key
        st.subheader(f"2. Evaluate returns - Case {k.case_id}")
        mode = st.radio("How many returns?", ["One return", "Many returns at once (bulk)"], horizontal=True,
                        key="eval_mode")

        if mode == "One return":
            pick = st.radio("Upload", ["PDF files", "A whole folder"], horizontal=True, key="one_pick",
                            help="'A whole folder': choose the student's folder (e.g. 164746_M01); every PDF in it "
                                 "and its sub-folders is uploaded.")
            pdfs = st.file_uploader("Student return: the filled-in IRS fillable forms (select all of them - "
                                    "one PDF per form is fine)" if pick == "PDF files" else
                                    "Student return: choose the folder holding the student's PDF forms",
                                    type=["pdf"], accept_multiple_files=True if pick == "PDF files" else "directory",
                                    key=f"ret{ss.uploader_n}_{pick[0]}")
            guess = ""
            if pdfs:
                segs = pdfs[0].name.replace("\\", "/").split("/")
                for part in list(reversed(segs[:-1])) + segs[-1:]:      # the student's folder first, then the file
                    mt = re.match(r"^\s*([A-Za-z0-9-]*\d[A-Za-z0-9-]*)\s*_", part)
                    if mt and not re.fullmatch(r"F\d{4}[A-Z]*", mt.group(1).upper()):   # not 'f1040_2025.pdf'
                        guess = mt.group(1).upper()
                        break
            c1, c2 = st.columns(2)
            sid = c2.text_input("Student ID", value=guess, key=f"sid{ss.uploader_n}").strip().upper()
            known = db.student(sid) if (db and sid) else None
            sname = c1.text_input("Student name", value=(known or {}).get("name") or "", key=f"sn{ss.uploader_n}_{sid}")
            if db and sid and not known:
                st.caption("This student ID is not on the roster yet - add it under 'Students & batches' so it "
                           "appears in batch reports.")
            if sid and not re.fullmatch(r"[A-Z0-9-]{1,40}", sid):
                st.error("Student ID may only contain letters, digits and '-' (up to 40 characters).")
                sid = ""
            sub_one = st.radio("Return submitted", SUBMITTED, horizontal=True, key=f"subm{ss.uploader_n}",
                               help="On time earns this return's on-time marks in the final score; late earns none. "
                                    "It can be changed later under Management dashboard > Submission status.")
            if st.button("Evaluate", type="primary", disabled=not pdfs or not sid):
                with st.spinner("Reading the return and marking it..."):
                    files = [(f.name, f.getvalue()) for f in pdfs]
                    try:
                        res = evaluate_files(files, sid, sname, k)
                    except ReaderError as e:
                        st.error(str(e))
                        st.stop()
                    except Exception:
                        log.exception("evaluation failed for %s", sid)
                        st.error("Something went wrong while reading this return. The details were logged; "
                                 "please check the files and try again.")
                        st.stop()
                    saved = False
                    try:
                        save(sid, res, on_time=sub_one == "On time")
                        saved = True
                    except Exception:
                        log.exception("saving failed for %s", sid)
                        st.error("The return was marked but could NOT be saved to the database. Download the "
                                 "report below, then try again later.")
                    rec = {"Student ID": sid, "Student Name": sname or "-", "Case": k.case_id, "File": res["file_label"],
                           "Score": res["m"]["score"], "Out of": res["m"]["out_of"], "Percent": res["m"]["pct"] * 100,
                           "Errors": res["m"]["errors"], "Submission": sub_one,
                           "Evaluated": datetime.now().strftime("%d-%b %H:%M"), **res}
                    ss.results.append(rec)
                    ss.results = ss.results[-200:]          # keep the session's memory bounded
                    ss.last = rec
                if db and saved:
                    st.toast("Saved to the database", icon="✅")
            r = ss.last
            if r and r["Case"] == k.case_id:
                st.divider()
                show_result(r)
                if st.button("Next student"):
                    ss.uploader_n += 1
                    ss.last = None
                    st.rerun()

        else:
            st.write("**OneDrive batch folder:** download the batch folder (it arrives as a zip) and upload the zip "
                     "here - each student's folder `EmployeeID_" + k.case_id + "` is one return. Near-miss folder names "
                     "(spaces, hyphens, missing mock code, ...) are still matched and marked **CHECK NAME**; folders "
                     "with no PDF in them are listed as skipped.")
            st.write("Upload many PDFs at once, or zip files containing them. Name each student's file(s) "
                     "`StudentID_CaseID.pdf` (e.g. `S0123_" + k.case_id + ".pdf`). A student "
                     "can have several PDFs, one per form: `S0123_" + k.case_id + "_1040.pdf`, `S0123_" + k.case_id + "_SchC.pdf`, "
                     "or a folder `S0123_" + k.case_id + "` holding them. You can also upload the whole batch "
                     "folder in one go. Files for another case are skipped. A folder or file named with the student's "
                     "ID or roster name in some other way is still accepted, with a warning to check it.")
            bpick = st.radio("Upload", ["PDF / zip files", "A whole folder"], horizontal=True, key="bulk_pick",
                             help="'A whole folder': choose the batch folder (e.g. the OneDrive folder holding one "
                                  "sub-folder per student). Every PDF and zip inside it is uploaded.")
            files = st.file_uploader("Returns (PDFs or zips)" if bpick != "A whole folder" else
                                     "Choose the folder holding the students' returns",
                                     type=["pdf", "zip"],
                                     accept_multiple_files=True if bpick != "A whole folder" else "directory",
                                     key=f"bulk{ss.uploader_n}_{bpick[0]}")
            c1, c2 = st.columns(2)
            skip_done = c1.checkbox("Skip students already evaluated for this case", value=bool(db), disabled=not db,
                                    help="Lets you restart an interrupted upload without marking anything twice.")
            make_reports = c2.checkbox("Also create the PDF report for every student", value=True)
            s1, s2 = st.columns([1, 2])
            bulk_sub = s1.radio("This batch submitted", SUBMITTED, horizontal=True, key="bulk_sub",
                                help="Applied to every student in this upload, except the IDs listed beside it.")
            other = "Late" if bulk_sub == "On time" else "On time"
            exc_txt = s2.text_input(f"Except these student IDs - they were {other.lower()} (comma separated)",
                                    key=f"bulk_exc{ss.uploader_n}")
            exceptions = {x.strip().upper() for x in re.split(r"[,;\s]+", exc_txt or "") if x.strip()}
            if st.button("Evaluate all", type="primary", disabled=not files):
                roster = {s_["student_id"]: s_ for s_ in db.students()} if db else {}
                groups, skipped = group_uploads([(f.name, f.getvalue()) for f in files], case_id=k.case_id,
                                                roster_ids={i: (r_.get("name") or "") for i, r_ in roster.items()}
                                                or None)
                done = db.evaluated(k.case_id) if (db and skip_done) else set()
                out, zbuf = [], io.BytesIO()
                for name, why in skipped:
                    out.append({"File": name, "Student ID": "", "Name": "", "Batch": "", "Score": None,
                                "Out of": None, "Score /100": None, "Errors": None,
                                "Status": why if why.startswith("Skipped") else f"Skipped - {why}"})
                bar = st.progress(0.0, text="Starting...")
                with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zout:
                    for i, g in enumerate(groups, 1):
                        bar.progress(i / max(len(groups), 1), text=f"{i} of {len(groups)}: {g.student_id}")
                        sid = g.student_id
                        stud = roster.get(sid, {})
                        row = {"File": g.label[:200], "Student ID": sid, "Name": stud.get("name") or "",
                               "Batch": stud.get("batch") or "", "Score": None, "Out of": None, "Score /100": None,
                               "Errors": None, "Submission": "", "Status": "", "Notes": ""}
                        naming = ("; ".join(g.warnings) + ". ") if g.warnings else ""
                        if roster and sid not in roster:
                            naming += f"Student ID {sid} is not on the roster. "
                        if g.case_id != k.case_id.upper():
                            row["Status"] = f"Skipped - file is for case {g.case_id}"
                        elif sid in done:
                            row["Status"] = "Skipped - already evaluated"
                        else:
                            try:
                                res = evaluate_files(g.load(), sid, row["Name"], k)
                                sub_status = other if sid in exceptions else bulk_sub
                                row["Submission"] = sub_status
                                save(sid, res, on_time=sub_status == "On time")
                                m = res["m"]
                                warn = m["needs_review"] or res["notes"] or res["unlinked"]
                                row.update({"Score": m["score"], "Out of": m["out_of"], "Score /100": round(m["pct"] * 100, 1),
                                            "Errors": m["errors"],
                                            "Status": "Evaluated" + (" - check 'What the system read'" if warn else "")
                                            + (f" - CHECK NAME: {naming}" if naming else ""),
                                            "Notes": " | ".join(res["notes"])[:300]})
                                if make_reports:
                                    zout.writestr(report_name(sid, k.case_id), res["report"])
                                done.add(sid)
                            except ReaderError as e:
                                row["Status"] = f"Failed - {str(e)[:200]}" + (f" - CHECK NAME: {naming}" if naming else "")
                            except Exception as e:
                                log.exception("bulk evaluation failed for %s", sid)
                                row["Status"] = f"Failed - unexpected error ({type(e).__name__}); details logged"
                        out.append(row)
                bar.progress(1.0, text="Finished")
                ss.bulk_out = {"case": k.case_id, "table": pd.DataFrame(out),
                               "zip": zbuf.getvalue() if make_reports else None,
                               "when": datetime.now().strftime("%Y%m%d_%H%M")}
            bo = ss.bulk_out
            if bo and bo["case"] == k.case_id:
                df = bo["table"]
                n_ok = int(df["Status"].str.startswith("Evaluated").sum())
                st.success(f"{n_ok} evaluated · {int(df['Status'].str.startswith('Skipped').sum())} skipped · "
                           f"{int(df['Status'].str.startswith('Failed').sum())} failed"
                           + (" · all saved to the database" if db else ""))
                misnamed = df[df["Status"].str.contains("CHECK NAME", na=False)]
                if len(misnamed):
                    st.warning(f"{len(misnamed)} student(s) did not name their files `StudentID_{k.case_id}` and were "
                               "matched by the ID or name found in the folder or file name: "
                               + ", ".join(f"{r['Student ID']} {r['Name']}".strip() for _, r in misnamed.iterrows())[:600]
                               + ". Check these rows, and ask the students to name their folders properly next time.")
                st.dataframe(df, hide_index=True)
                d1, d2 = st.columns(2)
                if bo["zip"] and n_ok:
                    d1.download_button("Download all reports (zip)", bo["zip"],
                                       file_name=f"Evaluation_reports_{k.case_id}_{bo['when']}.zip")
                xb = io.BytesIO()
                df.to_excel(xb, index=False)
                d2.download_button("Download this upload's results (Excel)", xb.getvalue(),
                                   file_name=f"Bulk_results_{k.case_id}_{bo['when']}.xlsx")
                if not db:
                    st.caption("No database: download these now - they are not saved anywhere else.")

        if ss.results:
            with st.expander(f"Returns evaluated one-by-one in this session ({len(ss.results)})"):
                summ = pd.DataFrame([{c: x[c] for c in ("Student ID", "Student Name", "Case", "File", "Score", "Out of",
                                                        "Percent", "Errors", "Evaluated")}
                                     for x in ss.results])
                st.dataframe(summ, hide_index=True)

# ------------------------------------------------------------------ students
with t_students:
    if not db:
        st.info("Connect a database (see 'How it works') to keep a student roster with batches.")
    else:
        st.subheader("Student roster")
        st.write("Upload an Excel or CSV file with the columns **Student ID**, **Student Name** and **Batch**. "
                 "Uploading again adds new students and updates existing ones (e.g. to move a student to another batch).")
        tmpl = io.BytesIO()
        pd.DataFrame({"Student ID": ["S0001", "S0002"], "Student Name": ["Example Name", "Another Name"],
                      "Batch": ["Batch-1", "Batch-1"]}).to_excel(tmpl, index=False)
        st.download_button("Download roster template", tmpl.getvalue(), file_name="student_roster_template.xlsx")
        up = st.file_uploader("Roster file", type=["xlsx", "csv"], key="roster")
        if up is not None:
            try:
                raw = io.BytesIO(up.getvalue())
                rdf = pd.read_csv(raw, dtype=str) if up.name.lower().endswith(".csv") else pd.read_excel(raw, dtype=str)
                cols = {c.lower().strip(): c for c in rdf.columns}
                cid_ = cols.get("student id") or cols.get("studentid") or cols.get("id")
                cnm = cols.get("student name") or cols.get("name")
                cbt = cols.get("batch")
                if not cid_:
                    st.error("No 'Student ID' column found.")
                else:
                    recs = []
                    for _, r in rdf.iterrows():
                        sid_ = str(r[cid_]).strip().upper()
                        if sid_ and sid_ != "NAN":
                            recs.append((sid_, str(r[cnm]).strip() if cnm and pd.notna(r[cnm]) else "",
                                         str(r[cbt]).strip() if cbt and pd.notna(r[cbt]) else "Unassigned"))
                    st.write(f"{len(recs)} students found in the file.")
                    st.dataframe(pd.DataFrame(recs[:20], columns=["Student ID", "Name", "Batch"]), hide_index=True)
                    if st.button("Save roster", type="primary"):
                        db.upsert_students(recs)
                        st.success(f"Saved {len(recs)} students.")
            except Exception as e:
                st.error(f"Could not read the roster: {e}")
        studs = pd.DataFrame(db.students())
        if not studs.empty:
            st.markdown("#### Students on the roster")
            st.dataframe(studs.groupby("batch").size().rename("Students").reset_index().rename(columns={"batch": "Batch"}),
                         hide_index=True)
            with st.expander(f"All students ({len(studs)})"):
                st.dataframe(studs.rename(columns={"student_id": "Student ID", "name": "Name", "batch": "Batch"}),
                             hide_index=True)

# ------------------------------------------------------------------ management
with t_mgmt:
    st.subheader("Management dashboard")
    if not db:
        st.info("The management dashboard needs the database (it adds up every saved evaluation).")
    else:
        cfg = load_settings(db.settings())
        m_res, m_sub, m_mcq, m_cfg = st.tabs(["Final results", "Submission status", "MCQ test marks",
                                              "Scoring settings"])
        all_students = db.students()
        fcases = db.final_cases()
        case_info = {c["case_id"]: c for c in db.cases()}

        # ---------------------------------------------------------- final results
        with m_res:
            st.caption(f"Return accuracy {cfg['w_accuracy']:g} + on-time submission {cfg['w_on_time']:g} + MCQ tests "
                       f"{cfg['w_mcq']:g} = 100. Pass mark {cfg['pass_mark']:g}. {cfg['n_mocks']:g} mock returns "
                       f"({len(fcases)} set up so far), {cfg['n_mcq']:g} MCQ tests of {cfg['mcq_out_of']:g} marks.")
            if len(fcases) > cfg["n_mocks"]:
                st.warning(f"{len(fcases)} cases count toward the final score but the course has {cfg['n_mocks']:g} "
                           "mock returns. Untick the practice cases under **Scoring settings**.")
            r1, r2 = st.columns([1, 2])
            view = r1.radio("View", ["Progress so far", "Final (course complete)"], key="fs_view",
                            help="Progress so far: only the returns and MCQ tests the student's batch has done "
                                 "count, scaled to 100. Final: every mock return and test counts; missing ones "
                                 "count 0.")
            view = SO_FAR if view.startswith("Progress") else FINAL
            batches = sorted({x["batch"] or "" for x in all_students})
            pick_b = r2.multiselect("Batches", batches, default=batches, key="fs_batches")
            studs = [x for x in all_students if (x["batch"] or "") in pick_b]
            if not studs:
                st.info("No students on the roster for these batches - add them under 'Students & batches'.")
            else:
                evs = [e for e in db.evaluations() if e["case_id"] in fcases]
                df = final_scores(studs, evs, db.mcq_scores(), fcases, cfg, view=view)
                good = "Pass" if view == FINAL else "On track"
                n_good = int((df["Result"] == good).sum())
                c = st.columns(3)
                c[0].metric("Students", len(df))
                c[1].metric(good, n_good)
                c[2].metric("Fail" if view == FINAL else "At risk",
                            int((df["Result"] == ("Fail" if view == FINAL else "At risk")).sum()))
                if int(df["Status not set"].sum()):
                    st.warning(f"{int(df['Status not set'].sum())} evaluated return(s) have no on-time status yet "
                               "(evaluated before this feature) and earn no on-time marks. Set them under "
                               "**Submission status**.")
                st.dataframe(df, hide_index=True, use_container_width=True)
                stamp = datetime.now().strftime("%Y%m%d")
                d1, d2 = st.columns(2)
                d1.download_button("Download results (Excel)", results_workbook(df, cfg, db.status_history(), view),
                                   file_name=f"{'Final_results' if view == FINAL else 'Progress'}_{stamp}.xlsx")

                def returns_of(sid):
                    mine = {e["case_id"]: e for e in evs if e["student_id"] == sid}
                    return [{"case_id": cid, "title": (case_info.get(cid) or {}).get("title") or "",
                             "pct": (mine.get(cid) or {}).get("pct"), "on_time": (mine.get(cid) or {}).get("on_time")}
                            for cid in fcases]
                if d2.button("Prepare scorecards for these students (zip)"):
                    zb = io.BytesIO()
                    with zipfile.ZipFile(zb, "w", zipfile.ZIP_DEFLATED) as z:
                        for rec in df.to_dict("records"):
                            z.writestr(f"Scorecard_{clean(rec['Student ID'])}.pdf",
                                       scorecard_pdf(rec, returns_of(rec["Student ID"]), cfg, institute, view))
                    ss.scorecards = zb.getvalue()
                if ss.get("scorecards"):
                    d2.download_button("Download scorecards (zip)", ss.scorecards, file_name=f"Scorecards_{stamp}.zip")
                one = st.selectbox("Scorecard for one student", [""] + df["Student ID"].tolist(), key="fs_one",
                                   format_func=lambda x: "Choose a student" if not x else
                                   f"{x} - {df.set_index('Student ID').loc[x, 'Name']}")
                if one:
                    rec = df.set_index("Student ID").loc[one].to_dict()
                    rec["Student ID"] = one
                    st.download_button(f"Download scorecard - {one}",
                                       scorecard_pdf(rec, returns_of(one), cfg, institute, view),
                                       file_name=f"Scorecard_{clean(one)}.pdf")

        # ---------------------------------------------------------- submission status
        with m_sub:
            st.write("Mark each evaluated return **On time** or **Late**. Every change is recorded with who made it "
                     "and why.")
            if not case_info:
                st.info("No cases yet.")
            else:
                q1, q2 = st.columns(2)
                cs = q1.selectbox("Case", list(case_info), key="sub_case",
                                  format_func=lambda c: f"{c} - {case_info[c]['title'] or ''}")
                bsel = q2.selectbox("Batch", ["All batches"] + batches, key="sub_batch")
                roster = {x["student_id"]: x for x in all_students}
                evs_c = [e for e in db.evaluations() if e["case_id"] == cs and
                         (bsel == "All batches" or (roster.get(e["student_id"], {}).get("batch") or "") == bsel)]
                if not evs_c:
                    st.info("No evaluated returns for this case and batch yet.")
                else:
                    lab = {1: "On time", 0: "Late", None: "Not set"}
                    base = pd.DataFrame([{"Student ID": e["student_id"], "Name": e.get("name") or "",
                                          "Batch": e.get("batch") or "", "Score /100": round(100 * (e["pct"] or 0), 1),
                                          "Submission": lab.get(e.get("on_time"), "Not set")} for e in evs_c])
                    edited = st.data_editor(
                        base, hide_index=True, use_container_width=True, key=f"sub_ed_{cs}_{bsel}",
                        disabled=["Student ID", "Name", "Batch", "Score /100"],
                        column_config={"Submission": st.column_config.SelectboxColumn(
                            "Submission", options=["On time", "Late", "Not set"], required=True)})
                    changed = [(r["Student ID"], r["Submission"]) for (_, r), (_, o) in
                               zip(edited.iterrows(), base.iterrows()) if r["Submission"] != o["Submission"]]
                    bad = [sid for sid, v in changed if v == "Not set"]
                    changes = [(sid, cs, v == "On time") for sid, v in changed if v != "Not set"]
                    w1, w2 = st.columns([1, 2])
                    who = w1.text_input("Your name", value=staff or "", key="sub_who")
                    why = w2.text_input("Reason for the change", key="sub_why",
                                        placeholder="e.g. uploaded after the deadline; OneDrive time stamp checked")
                    if bad:
                        st.warning("A status can be changed to On time or Late, not back to 'Not set'.")
                    if st.button(f"Save {len(changes)} change(s)", type="primary",
                                 disabled=not changes or not who.strip() or not why.strip()):
                        n = db.set_on_time(changes, who.strip(), why.strip())
                        st.success(f"{n} status(es) changed.")
                        st.rerun()
                    if changes and (not who.strip() or not why.strip()):
                        st.caption("Enter your name and a reason to save.")
                    unset = [r["Student ID"] for _, r in base.iterrows() if r["Submission"] == "Not set"]
                    if unset and st.button(f"Mark all {len(unset)} 'Not set' returns in this list as On time",
                                           disabled=not who.strip() or not why.strip(),
                                           help="For returns evaluated before submission status existed. "
                                                "Uses the name and reason above."):
                        n = db.set_on_time([(sid, cs, True) for sid in unset], who.strip(), why.strip())
                        st.success(f"{n} status(es) set to On time.")
                        st.rerun()
                hist = db.status_history(case_id=cs)
                with st.expander(f"Change history for {cs} ({len(hist)})"):
                    if hist:
                        h = pd.DataFrame(hist)
                        h["changed_at"] = h["changed_at"].str[:16] + " UTC"
                        st.dataframe(h.rename(columns={"student_id": "Student ID", "old_status": "From",
                                                       "new_status": "To", "changed_by": "Changed by",
                                                       "reason": "Reason", "changed_at": "When"})
                                     [["When", "Student ID", "From", "To", "Changed by", "Reason"]], hide_index=True)
                    else:
                        st.write("No changes yet.")

        # ---------------------------------------------------------- MCQ marks
        with m_mcq:
            st.write(f"Upload the MCQ marks as Excel or CSV: a **Student ID** column, and one column per test named "
                     f"**Test 1** ... **Test {cfg['n_mcq']:g}** (marks out of {cfg['mcq_out_of']:g}). Blank cells are "
                     "left as they are, so a sheet can hold only the tests written so far. Uploading a mark again "
                     "replaces it.")
            up = st.file_uploader("MCQ marks file", type=["xlsx", "csv"], key="mcq_file")
            tno = st.selectbox("If the sheet has a single 'Marks' column, which test is it?",
                               list(range(1, int(cfg["n_mcq"]) + 1)), key="mcq_tno")
            if up is not None:
                try:
                    raw = io.BytesIO(up.getvalue())
                    mdf = pd.read_csv(raw, dtype=str) if up.name.lower().endswith(".csv") else pd.read_excel(raw)
                    recs = parse_mcq(mdf, cfg, test_no=tno)
                except MCQError as e:
                    st.error(str(e))
                    recs = []
                except Exception:
                    log.exception("reading MCQ file failed")
                    st.error("This file could not be read. Save it as .xlsx or .csv and try again.")
                    recs = []
                if recs:
                    known = {x["student_id"] for x in all_students}
                    unknown = sorted({sid for sid, _, _ in recs} - known)
                    tests = sorted({t for _, t, _ in recs})
                    st.write(f"**{len(recs)}** marks for **{len({r[0] for r in recs})}** students, "
                             f"test(s) {', '.join(map(str, tests))}.")
                    if unknown:
                        st.warning(f"{len(unknown)} student ID(s) are not on the roster and will be saved anyway "
                                   f"(they show in results once added to the roster): {', '.join(unknown[:15])}"
                                   + (" ..." if len(unknown) > 15 else ""))
                    if st.button("Save MCQ marks", type="primary"):
                        db.save_mcq(recs, staff or "")
                        st.success(f"{len(recs)} marks saved.")
            cur = db.mcq_scores()
            if cur:
                wide = (pd.DataFrame(cur).pivot_table(index="student_id", columns="test_no", values="marks")
                        .rename(columns=lambda t: f"Test {t}").reset_index().rename(columns={"student_id": "Student ID"}))
                names = {x["student_id"]: (x["name"] or "", x["batch"] or "") for x in all_students}
                wide.insert(1, "Name", wide["Student ID"].map(lambda x: names.get(x, ("", ""))[0]))
                wide.insert(2, "Batch", wide["Student ID"].map(lambda x: names.get(x, ("", ""))[1]))
                st.dataframe(wide, hide_index=True, use_container_width=True)

        # ---------------------------------------------------------- settings
        with m_cfg:
            can_edit = bool(ss.get("admin_ok"))
            if not can_edit:
                st.caption("Changing these needs the admin password - unlock it under **Data management**.")
            with st.form("cfg_form"):
                vals = {}
                cols = st.columns(2)
                for i, (k_, lab_) in enumerate(LABELS.items()):
                    vals[k_] = cols[i % 2].number_input(lab_, value=float(cfg[k_]), min_value=0.0, step=1.0,
                                                        disabled=not can_edit, key=f"cfg_{k_}")
                pick = st.multiselect("Cases that are the course's mock returns (counted in the final score)",
                                      list(case_info), default=fcases, disabled=not can_edit,
                                      format_func=lambda c: f"{c} - {case_info[c]['title'] or ''}")
                if st.form_submit_button("Save settings", disabled=not can_edit):
                    newcfg = {k_: (int(v) if float(v).is_integer() else v) for k_, v in vals.items()}
                    errs = check_settings(newcfg)
                    if errs:
                        st.error(" ".join(errs))
                    else:
                        db.save_settings(newcfg)
                        db.set_final_cases(pick)
                        st.success("Settings saved.")
                        st.rerun()

# ------------------------------------------------------------------ integrity checks
with t_integ:
    if not db:
        st.info("Integrity checks compare returns saved in the database, so they need the database.")
    else:
        from integrity_report import CAUTION, integrity_excel, integrity_pdf
        st.subheader("Integrity checks - copying and shared files")
        st.caption(CAUTION)
        ev_all = db.evaluations()
        if not ev_all:
            st.info("No evaluations saved yet.")
        else:
            for e in ev_all:
                e["batch"] = e.get("batch") or "Unassigned"
            c1, c2 = st.columns(2)
            ib = sorted({e["batch"] for e in ev_all})
            ic = sorted({e["case_id"] for e in ev_all})
            sel_ib = c1.multiselect("Batches", ib, default=ib, key="int_b")
            sel_ic = c2.multiselect("Cases", ic, default=ic, key="int_c")
            st.caption("Returns of the same case are compared across all the selected batches.")
            if st.button("Run integrity check", type="primary", disabled=not sel_ib or not sel_ic):
                with st.spinner("Comparing returns..."):
                    evs = [e for e in ev_all if e["batch"] in sel_ib and e["case_id"] in sel_ic]
                    ids = {e["student_id"] for e in evs}
                    fps_ = db.fingerprints(sel_ic)
                    mis = [m for m in db.mistakes() if m["case_id"] in sel_ic and m["student_id"] in ids]
                    ss.integ = {"scope": f"Batches: {', '.join(sel_ib) if len(sel_ib) < len(ib) else 'all'} · "
                                         f"Cases: {', '.join(sel_ic) if len(sel_ic) < len(ic) else 'all'}",
                                "res": analyse(evs, fps_, mis, db.students())}
            if ss.get("integ"):
                res, scope = ss.integ["res"], ss.integ["scope"]
                stu = res["students"]
                cnt = lambda lv: int((stu["Integrity"] == lv).sum()) if not stu.empty else 0
                m = st.columns(6)
                m[0].metric("Students checked", len(stu))
                m[1].metric("Very likely copied", cnt("Very likely copied"))
                m[2].metric("Likely copied", cnt("Likely copied"))
                m[3].metric("Review", cnt("Review"))
                m[4].metric("Groups", len(res["groups"]))
                m[5].metric("Built on trainer's file", len(res["trainer"]))
                st.markdown("#### By batch")
                st.dataframe(res["batches"], hide_index=True)
                if not res["trainer"].empty:
                    st.markdown("#### Returns built on the trainer's correct-return file")
                    st.dataframe(res["trainer"], hide_index=True)
                st.markdown("#### Groups of linked students")
                st.dataframe(res["groups"], hide_index=True)
                st.markdown("#### Flagged pairs")
                lv_pick = st.multiselect("Show levels", ["Very likely copied", "Likely copied", "Review"],
                                         default=["Very likely copied", "Likely copied", "Review"], key="int_lv")
                pr = res["pairs"]
                st.dataframe(pr[pr["Level"].isin(lv_pick)] if not pr.empty else pr, hide_index=True,
                             column_config={"Evidence": st.column_config.TextColumn(width="large")})
                st.markdown("#### Students")
                only = st.checkbox("Only students with concerns", value=True, key="int_only")
                st.dataframe(stu[stu["Integrity"] != "No concerns"] if only and not stu.empty else stu, hide_index=True)
                if not res["id_checks"].empty:
                    st.markdown("#### Student ID checks")
                    st.dataframe(res["id_checks"], hide_index=True)
                d1, d2 = st.columns(2)
                if d1.button("Prepare integrity report (PDF)"):
                    ss.integ_pdf = integrity_pdf(institute, scope, res)
                if ss.get("integ_pdf"):
                    d1.download_button("Download integrity report (PDF)", ss.integ_pdf, mime="application/pdf",
                                       file_name=f"Integrity_report_{datetime.now():%Y%m%d}.pdf")
                d2.download_button("Download integrity workbook (Excel)", integrity_excel(scope, res),
                                   file_name=f"Integrity_report_{datetime.now():%Y%m%d}.xlsx")
                with st.expander("What is checked"):
                    st.markdown("""
- **Same file**: exactly the same PDF, even renamed or uploaded under another student ID.
- **Saved over another student's file**: PDF viewers add each save to the end of the file, so a file saved over
  someone else's filled-in form still contains that student's version.
- **Same save ID / document ID / time stamps**: hidden properties written when a PDF is saved or printed.
- **Same unusual wrong answers**: the same wrong figure on the same line. Mistakes that many students make are
  ignored; the rarer the shared mistake, the more it counts.
- **Same typing habits**: 0 typed in the same boxes others leave blank; identical capitalisation and spacing;
  every box identical.
- **Built on the trainer's file**: the trainer's correct return (uploaded under *Build a key*) found inside a
  student's file.

Values every blank IRS form carries (its own author, dates and IDs) are ignored. Students re-evaluated after
this update are fully covered; returns marked earlier only have their wrong answers compared.
""")

# ------------------------------------------------------------------ find a report
with t_find:
    if not db:
        st.info("Saved reports need the database.")
    else:
        sid_q = st.text_input("Student ID").strip().upper()
        if sid_q:
            evs = db.evaluations(student_id=sid_q)
            if not evs:
                st.warning("No saved evaluations for this student ID.")
            else:
                st.dataframe(pd.DataFrame([{"Case": e["case_id"], "Score": f"{e['score']:g} / {e['out_of']:g}",
                                            "%": round(e["pct"] * 100, 1),
                                            "Errors": e["errors"], "File": e["file_name"],
                                            "Evaluated (UTC)": e["evaluated_at"], "By": e["evaluated_by"]} for e in evs]),
                             hide_index=True)
                case_q = st.selectbox("Case", [e["case_id"] for e in evs])
                e = next(x for x in evs if x["case_id"] == case_q)
                kb = db.case_key_bytes(case_q)
                if st.button("Prepare report"):
                    key_q = parse_key(kb)
                    rows = rebuild_rows(key_q, db.mistakes(sid=sid_q, case_id=case_q))
                    cats, total, out_of = summarise(rows)
                    ss.found = (sid_q, case_q, build_report(e.get("name") or "", sid_q, key_q, rows, cats, total, out_of,
                                                            e["file_name"], institute,
                                                            evaluated_on=f"{e['evaluated_at']} UTC"))
                    if hashlib.sha256(kb).hexdigest()[:16] != e["key_hash"]:
                        st.warning("The answer key for this case was changed after this return was marked. The report "
                                   "uses the current key with the mistakes recorded at the time; re-evaluate the return "
                                   "for an exact report.")
                if ss.get("found") and ss.found[:2] == (sid_q, case_q):
                    st.download_button("Download report (PDF)", ss.found[2], file_name=report_name(sid_q, case_q),
                                       mime="application/pdf")
                with st.expander("Delete this evaluation"):
                    if st.checkbox(f"Yes, delete {sid_q} / {case_q} permanently"):
                        if st.button("Delete"):
                            db.delete_evaluation(sid_q, case_q)
                            st.success("Deleted.")
                            st.rerun()

# ------------------------------------------------------------------ build key
with t_build:
    st.subheader("Build an answer key from the trainer's correct return")
    st.write("Upload the trainer's **correct** return: the filled-in IRS fillable forms "
             "(select all of them). Every filled-in item on every form is listed in a new answer key. Open it in "
             "Excel, set marks, categories and feedback, then upload it in the left panel.")
    good = st.file_uploader("Correct return (PDF files)", type=["pdf"], accept_multiple_files=True, key="goodret")
    b1, b2, b3 = st.columns([1, 2, 1])
    cid = b1.text_input("Case ID", "C01")
    ctitle = b2.text_input("Case title", "")
    cyear = b3.number_input("Tax year", 2015, 2035, 2025)
    b4, b5 = st.columns(2)
    dmarks = b4.number_input("Default marks per item", 0.0, 100.0, 1.0, 0.5)
    dunx = b5.number_input("Marks deducted per unexpected entry", 0.0, 100.0, 1.0, 0.5)
    if db:
        st.caption("The trainer's file is also remembered for the integrity checks (a student file built on it is "
                   "flagged). Use the same Case ID as the answer key.")
    if st.button("Build answer key", disabled=not good):
        try:
            gfiles = [(f.name, f.getvalue()) for f in good]
            data, n, forms = draft_from_return(gfiles, cid, ctitle, int(cyear), dmarks, dunx)
            if db and n:
                try:
                    db.save_fingerprints(TRAINER, str(cid).strip(), fingerprints(gfiles, read_return(gfiles)))
                except Exception:
                    log.exception("saving trainer fingerprints failed")
        except ReaderError as e:
            st.error(str(e))
        except Exception:
            log.exception("building a key failed")
            st.error("Could not read this return. The details were logged; check the files and try again.")
        else:
            if n == 0:
                st.error("No filled-in items were found. Upload the saved fillable forms (not a printed PDF).")
            else:
                st.success(f"{n} items found on: {', '.join(forms)}")
                st.download_button("Download draft answer key (.xlsx)", data, file_name=f"answer_key_{clean(cid)}.xlsx")

# ------------------------------------------------------------------ data management
with t_data:
    if not db:
        st.info("Nothing is stored without a database - results disappear when this tab closes.")
    else:
        st.subheader("Data management")
        ADMIN = secret("admin_password") or PASSWORD

        # Buttons here use on_click callbacks instead of "if st.button(...): ...; st.rerun()".
        # A callback runs BEFORE the page redraws, so there is only one rerun: the page stays on
        # this tab and the result message is still on screen afterwards (st.rerun() used to wipe it).
        def _unlock():
            typed = ss.get("adminpw", "") or ""
            if hmac.compare_digest(typed.encode(), str(ADMIN).encode()):
                ss.admin_ok = True
                ss.adminpw = ""
                ss.admin_msg = None
            else:
                ss.admin_msg = ("error", "Wrong admin password.")

        def _run(action, done_msg, reset_key=None):
            try:
                action()
                ss.admin_msg = ("success", done_msg)
                if reset_key:
                    ss[reset_key] = False if reset_key.startswith("ok_") else ""
            except Exception as e:
                log.exception("erasing data failed")
                ss.admin_msg = ("error", f"Nothing was erased - the database reported: {e}")

        msg = ss.pop("admin_msg", None)
        if msg:
            (st.success if msg[0] == "success" else st.error)(msg[1])

        if ADMIN and not ss.get("admin_ok"):
            st.write("Erasing data needs the admin password.")
            st.text_input("Admin password", type="password", key="adminpw")
            st.button("Unlock", on_click=_unlock)
        else:
            n = db.counts()
            c = st.columns(4)
            c[0].metric("Students on roster", n["students"])
            c[1].metric("Saved cases", n["cases"])
            c[2].metric("Evaluated returns", n["evaluations"])
            c[3].metric("Saved mistakes", n["mistakes"])
            st.warning("Erasing cannot be undone. Download the management workbook first if you may need the results.")

            def danger(label, key, action, done_msg):
                """Two-step confirmation: tick the box, then press the button."""
                if st.checkbox(f"I understand - {label}", key=f"ok_{key}"):
                    st.button(label, key=f"btn_{key}", type="primary",
                              on_click=_run, args=(action, done_msg, f"ok_{key}"))

            case_ids = [x["case_id"] for x in db.cases()]
            with st.expander("Erase the results of one case (keeps its answer key)"):
                cx = st.selectbox("Case", case_ids, key="er_case") if case_ids else None
                if cx:
                    st.caption(f"{db.case_eval_count(cx)} evaluated returns for {cx}.")
                    danger(f"Erase all results for {cx}", "case_res", lambda c=cx: db.erase_case_results(c),
                           f"Results for {cx} erased.")
            with st.expander("Delete a case completely (answer key and all its results)"):
                cy = st.selectbox("Case", case_ids, key="er_case2") if case_ids else None
                if cy:
                    danger(f"Delete case {cy}", "case_all", lambda c=cy: db.erase_case(c), f"Case {cy} deleted.")
            with st.expander("Erase a batch"):
                bl = db.batches()
                bx = st.selectbox("Batch", bl, key="er_batch") if bl else None
                if bx:
                    roster_too = st.checkbox("Also remove these students from the roster", value=True, key="er_roster")
                    danger(f"Erase batch {bx}", "batch", lambda b=bx, r=roster_too: db.erase_batch(b, r), f"Batch {bx} erased.")
            with st.expander("Erase one student (roster entry and all their results)"):
                sx = st.text_input("Student ID", key="er_sid").strip().upper()
                if sx:
                    danger(f"Erase student {sx}", "student", lambda s=sx: db.erase_student(s), f"Student {sx} erased.")
            with st.expander("Erase results of student IDs that are not on the roster"):
                st.caption("Useful after test runs with made-up IDs.")
                danger("Erase results of unlisted IDs", "unlisted", db.erase_unlisted, "Done.")
            with st.expander("Erase EVERYTHING (start fresh)"):
                st.caption("Removes the roster, all saved cases and all results.")
                typed = st.text_input("Type ERASE ALL to confirm", key="er_all_txt")
                if typed.strip() == "ERASE ALL":
                    st.button("Erase everything", type="primary", key="er_all",
                              on_click=_run, args=(db.erase_all, "All data erased.", "er_all_txt"))
            st.button("Lock this tab", on_click=lambda: ss.update(admin_ok=False))

# ------------------------------------------------------------------ help
with t_help:
    st.markdown("""
### What this app does
It marks students' practice returns, prepared on the **IRS fillable forms**, against the trainer's answer key for
each case, and gives every student a Simandhar-branded evaluation report with a score out of 100.

### Before you start: how students prepare and save their return
- Download the IRS's own fillable PDFs (Form 1040, schedules and other forms the case needs) from irs.gov.
- Fill them in using Adobe Acrobat Reader and **save** them (File > Save). Keep **one PDF per form** and don't merge them.
- **Don't use Print to PDF** or flatten the file. That removes the boxes, and the app will refuse the file with a
  message asking for the saved fillable form.
- Name the files `StudentID_CaseID.pdf`, or one file per form: `StudentID_CaseID_1040.pdf`,
  `StudentID_CaseID_SchC.pdf` ... A folder named `StudentID_CaseID` holding the forms also works.

### Step by step
1. **Student roster** *(once)*: under **Students & batches**, upload an Excel/CSV file with Student ID, Student Name
   and Batch.
2. **Answer key** *(once per case)*: upload the case's answer key (.xlsx) in the left panel. It is saved, so next time
   pick the case from **Saved cases**. The panel shows how the marks are weighted across categories.
   - No key yet? Under **Build a key from a correct return**, upload the trainer's correct fillable forms. You get
     a draft key listing every filled-in item; set the marks, categories and feedback in Excel, then upload it.
3. **Evaluate** under **Evaluate returns**:
   - **One return**: select all of the student's PDFs, or choose **A whole folder** and pick the student's folder.
     Check the Student ID (taken from the folder or file name) and name, then press *Evaluate*.
   - **Many returns at once**: upload the PDFs or zip files, or choose **A whole folder** and pick the batch folder
     (one sub-folder per student, named `StudentID_CaseID`). Each student's files are grouped by their name.
     A folder named differently is still accepted when it holds the student's roster ID or name (e.g. "Ravi Kumar"
     or "Ravi_Kumar_M01"); the row then says **CHECK NAME**. A name shared by two students is skipped.
     Download all the reports as one zip, and the results as Excel.
   - Evaluating the same student and case again replaces the earlier result.
4. **Check before sharing**: open *What the system read* when the app asks you to. Items it could not read are marked
   0 and flagged. Boxes it could not link to a line are listed there; to mark one, use `field: <box name>` in the key.
   - Form 8949 line names: `I box` (A/B/C/G/H/I), `I 1 row 3 (d)` = Part I row 3 column (d), `I 2 (h)` = Part I
     totals column (h); Part II uses `II`. Building the key from a correct return fills these in for you.
5. **Reports later**: use **Find a report** to download any student's report again, or to delete one evaluation.

### How marks are given
- Every item in the key carries its own marks; the total is scaled to a **score out of 100**. Each case keeps its
  own weightage, so one mistake only costs that item's marks.
- An answer must match the key, within the rounding tolerance set in the key, to earn its marks.
- Amounts on lines that should be blank ("unexpected entries") deduct the marks set in the key.
- A form that is missing from the upload makes all of its items wrong.

### Final course score (Management dashboard)
| Part | Marks | How |
|---|---|---|
| Return accuracy | 60 | average score of the 12 mock returns; a return not submitted counts 0 |
| On-time submission | 20 | 20 / 12 = 1.67 marks for each return submitted on time; a late return earns none |
| MCQ tests | 20 | total of the 4 tests (50 marks each) out of 200 |

**Pass mark: 70.** These numbers can be changed under *Scoring settings* (admin password).
- **On time or late** is chosen when you evaluate: for one return, or for the whole batch with a list of exceptions.
  Re-evaluating a return keeps its status. Staff and admin can change it later under *Submission status*;
  every change records who made it, why and when.
- **MCQ marks**: upload an Excel/CSV with *Student ID* and *Test 1* ... *Test 4* columns under *MCQ test marks*.
- **Final results**: *Progress so far* counts only the returns and tests a batch has done (scaled to 100) and shows
  who is at risk; *Final* counts everything, missing work as 0. Download the results in Excel and a
  Simandhar-branded scorecard PDF for each student.
- Under *Scoring settings*, tick only the cases that are the course's mock returns, so practice cases don't count.

### Other tabs
- **Integrity checks**: compares returns of the same case for copied or shared files, files saved over another
  student's work, the same unusual wrong answers and identical typing habits.
- **Data management** *(admin password)*: erase a case's results, a whole case, a batch, a student, or everything.

### Privacy
Student PDFs are not stored: only scores, mistakes and the reader's notes are saved. Without a database nothing is
kept after the browser tab closes.
""")
