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
import os
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from bulk import group_uploads
from grader_core import KeyError_, fmt, grade, load_key, summarise
from integrity import TRAINER, analyse, fingerprints
from irs_reader import ReaderError
from make_keys import draft_from_return
from finalscore import (FINAL, LABELS, SO_FAR, MCQError, check_settings, final_scores, load_settings, per_item,
                        parse_mcq)
from metrics import flag, rebuild_rows, return_metrics
from roster_flag import (NOT_ON_ROSTER, add_roster_column, callout_html, excel_bytes, highlight_excel_sheet,
                         not_on_roster, off_roster_ids, style_off_roster)
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
    test = "TEST_" if st.session_state.get("test_run") else ""
    return f"{test}Evaluation_report_{clean(student_id)}_{clean(case_id)}.pdf"


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
    TEST = st.toggle("Test run - don't save anything", key="test_run",
                     help="For trying out a key or sample returns. Nothing is written to the database: not the "
                          "answer key, scores, mistakes, integrity data or the run history. Reports are stamped "
                          "TEST RUN. Saved cases and the roster can still be used.")
    if TEST:
        st.markdown("<div style='background:#8E1B3A;color:#fff;padding:8px 10px;border-radius:6px;"
                    "font-weight:600'>TEST RUN - nothing is being saved</div>", unsafe_allow_html=True)

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
    if key_file is not None and (key_file.name, key_file.size, TEST) != ss.get("prev_keyfile"):
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
            if db and not TEST:
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
                ss.prev_keyfile = (key_file.name, key_file.size, TEST)     # turning test run off saves the key
                use_key(key_file.getvalue(), key_file.name)
                if TEST:
                    st.caption("Test run: this key is used for now but not saved as a case.")
                elif db:
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
t_eval, t_students, t_att, t_mgmt, t_batch, t_integ, t_find, t_build, t_data, t_help = st.tabs(
    ["Evaluate returns", "Students & batches", "Attendance & Deloitte report", "Management dashboard",
     "Batch reports", "Integrity checks",
     "Find a report / history",
     "Build a key from a correct return", "Data management", "How it works"])


def evaluate_files(files, sid, sname, key):
    """Read and grade one student's return (one or more PDFs); returns rows, metrics and the PDF report.
    Raises ReaderError with a message the user can act on."""
    ret = read_return(files)
    rows = flag(grade(ret, key))
    m = return_metrics(rows)
    cats, total, out_of = summarise(rows)
    label = files[0][0] if len(files) == 1 else f"{len(files)} files"
    report = build_report(sname, sid, key, rows, cats, total, out_of, label, institute, test_run=TEST)
    try:
        fp = fingerprints(files, ret)
    except Exception:              # integrity data must never stop a return from being marked
        log.exception("fingerprinting failed for %s", sid)
        fp = None
    return {"fp": fp, "rows": rows, "cats": cats, "m": m, "report": report, "pages": ret.page_map,
            "forms": ret.forms_found(), "format": ret.format_name, "notes": list(ret.notes),
            "unlinked": ret.unlinked(), "file_label": ", ".join(n for n, _ in files)[:500]}


def save(sid, res, on_time=None):
    if db and not TEST:
        db.save_evaluation(sid, ss.key.case_id, res["file_label"], res["m"], res["rows"], ss.key_hash, staff or "",
                           source_format=res["format"], notes=" | ".join(res["notes"]), fingerprints=res.get("fp"),
                           on_time=on_time)


SUBMITTED = ["On time", "Late"]


def record_run(kind, case_id, started, uploads, rows, options=None):
    """Keep a record of an evaluation run (everything except the student PDF reports): what was uploaded and,
    for every file / student, whether it was evaluated, skipped or failed and why. Not kept in a test run."""
    if not db or TEST:
        return
    try:
        st_ = [str(r.get("Status") or "") for r in rows]
        clean_rows = [{k_: (None if isinstance(v, float) and pd.isna(v) else v) for k_, v in r.items()} for r in rows]
        db.save_run({"run_id": started.strftime("%Y%m%d%H%M%S") + "-" + os.urandom(3).hex(),
                     "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
                     "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                     "kind": kind, "case_id": case_id, "run_by": staff or "", "files": len(uploads),
                     "evaluated": sum(x.startswith("Evaluated") for x in st_),
                     "skipped": sum(x.startswith("Skipped") for x in st_),
                     "failed": sum(x.startswith("Failed") for x in st_),
                     "warnings": sum(("CHECK" in x) or ("check" in x) for x in st_),
                     "detail": {"uploads": uploads, "rows": clean_rows, "options": options or {}}})
    except Exception:
        log.exception("could not record the evaluation run")


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
        if TEST:
            st.warning("**Test run:** returns are marked and reports are made as usual, but nothing is saved - "
                       "not scores, mistakes, integrity data or run history. Download what you need before leaving. "
                       "Switch it off in the left panel for real evaluations.", icon="🧪")
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
            if db and sid and not known and not TEST:
                st.markdown(callout_html([sid], "student ID", "It will be evaluated and saved, but it won't appear "
                                         "in final results or batch reports until you add it under "
                                         "<b>Students &amp; batches</b>."), unsafe_allow_html=True)
            if sid and not re.fullmatch(r"[A-Z0-9-]{1,40}", sid):
                st.error("Student ID may only contain letters, digits and '-' (up to 40 characters).")
                sid = ""
            sub_one = st.radio("Return submitted", SUBMITTED, horizontal=True, key=f"subm{ss.uploader_n}",
                               help="On time earns this return's on-time marks in the final score; late earns none. "
                                    "It can be changed later under Management dashboard > Submission status.")
            if st.button("Evaluate", type="primary", disabled=not pdfs or not sid):
                with st.spinner("Reading the return and marking it..."):
                    files = [(f.name, f.getvalue()) for f in pdfs]
                    started = datetime.now(timezone.utc)
                    one_batch = ((db.student(sid) or {}).get("batch") or "") if db else ""
                    one_row = {"File": ", ".join(n for n, _ in files)[:300], "Student ID": sid, "Name": sname or "",
                               "Batch": one_batch, "Submission": sub_one}
                    one_up = [(n, len(d)) for n, d in files]
                    try:
                        res = evaluate_files(files, sid, sname, k)
                    except ReaderError as e:
                        record_run("Single", k.case_id, started, one_up, [dict(one_row, Status=f"Failed - {e}")])
                        st.error(str(e))
                        st.stop()
                    except Exception as e:
                        log.exception("evaluation failed for %s", sid)
                        record_run("Single", k.case_id, started, one_up, [dict(
                            one_row, Status=f"Failed - unexpected error ({type(e).__name__}); details logged")])
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
                    m1 = res["m"]
                    record_run("Single", k.case_id, started, one_up, [dict(
                        one_row, **{"Score": m1["score"], "Out of": m1["out_of"],
                                    "Score /100": round(m1["pct"] * 100, 1), "Errors": m1["errors"],
                                    "Status": ("Evaluated" if saved else "Failed - marked but NOT saved")
                                    + (" - check 'What the system read'" if m1["needs_review"] or res["notes"]
                                       or res["unlinked"] else ""),
                                    "Notes": " | ".join(res["notes"])[:300]})])
                    ss.last = rec
                if TEST:
                    st.toast("Test run - not saved", icon="🧪")
                elif db and saved:
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
            skip_done = c1.checkbox("Skip students already evaluated for this case", value=bool(db) and not TEST,
                                    disabled=not db or TEST,
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
                started = datetime.now(timezone.utc)
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
                record_run("Bulk", k.case_id, started, [(f.name, f.size) for f in files], out,
                           {"Batch submitted": bulk_sub, "Exceptions": ", ".join(sorted(exceptions)),
                            "Skip already evaluated": skip_done, "PDF reports made": make_reports})
                ss.bulk_out = {"case": k.case_id, "table": pd.DataFrame(out),
                               "zip": zbuf.getvalue() if make_reports else None,
                               "when": datetime.now().strftime("%Y%m%d_%H%M"), "test": TEST}
            bo = ss.bulk_out
            if bo and bo["case"] == k.case_id:
                df = bo["table"]
                n_ok = int(df["Status"].str.startswith("Evaluated").sum())
                st.success(f"{n_ok} evaluated · {int(df['Status'].str.startswith('Skipped').sum())} skipped · "
                           f"{int(df['Status'].str.startswith('Failed').sum())} failed"
                           + (" · TEST RUN - nothing saved" if bo.get("test") else
                              " · all saved to the database" if db else ""))
                misnamed = df[df["Status"].str.contains("CHECK NAME", na=False)]
                if len(misnamed):
                    st.warning(f"{len(misnamed)} student(s) did not name their files `StudentID_{k.case_id}` and were "
                               "matched by the ID or name found in the folder or file name: "
                               + ", ".join(f"{r['Student ID']} {r['Name']}".strip() for _, r in misnamed.iterrows())[:600]
                               + ". Check these rows, and ask the students to name their folders properly next time.")
                roster_now = {x["student_id"] for x in db.students()} if db else set()
                if db:
                    df = add_roster_column(df, roster_now)
                    off = off_roster_ids(df["Student ID"], roster_now)
                    if off:
                        st.markdown(callout_html(off, "student(s) in this upload are"), unsafe_allow_html=True)
                st.dataframe(style_off_roster(df), hide_index=True)
                d1, d2 = st.columns(2)
                if bo["zip"] and n_ok:
                    d1.download_button("Download all reports (zip)", bo["zip"],
                                       file_name=f"{'TEST_' if bo.get('test') else ''}Evaluation_reports_{k.case_id}_{bo['when']}.zip")
                d2.download_button("Download this upload's results (Excel)", excel_bytes([("Results", df)]),
                                   file_name=f"{'TEST_' if bo.get('test') else ''}Bulk_results_{k.case_id}_{bo['when']}.xlsx")
                if not db or bo.get("test"):
                    st.caption("Not saved: download these now - they are not kept anywhere else.")

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
        st.write("Upload an Excel or CSV file with the columns **Student ID**, **Student Name**, **Batch** and "
                 "(recommended) **Email** - the learner's Deloitte email, used to match class attendance. "
                 "Uploading again adds new students and updates existing ones (e.g. to move a student to another "
                 "batch); a blank email keeps the one already saved.")
        tmpl = io.BytesIO()
        pd.DataFrame({"Student ID": ["S0001", "S0002"], "Student Name": ["Example Name", "Another Name"],
                      "Batch": ["Batch-1", "Batch-1"],
                      "Email": ["example.ext@deloitte.com", "another.ext@deloitte.com"]}).to_excel(tmpl, index=False)
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
                cem = next((cols[k_] for k_ in ("email", "email id", "deloitte email id", "deloitte email",
                                                "mail id", "e-mail") if k_ in cols), None)
                if not cid_:
                    st.error("No 'Student ID' column found.")
                else:
                    recs = []
                    for _, r in rdf.iterrows():
                        sid_ = str(r[cid_]).strip().upper()
                        if sid_ and sid_ != "NAN":
                            em_ = str(r[cem]).strip().lower() if cem and pd.notna(r[cem]) else ""
                            recs.append((sid_, str(r[cnm]).strip() if cnm and pd.notna(r[cnm]) else "",
                                         str(r[cbt]).strip() if cbt and pd.notna(r[cbt]) else "Unassigned",
                                         em_ if "@" in em_ else ""))
                    st.write(f"{len(recs)} students found in the file"
                             + (f", {sum(1 for x in recs if x[3])} with an email." if cem else
                                ". No Email column - class attendance will be matched by name only."))
                    st.dataframe(pd.DataFrame(recs[:20], columns=["Student ID", "Name", "Batch", "Email"]),
                                 hide_index=True)
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
                st.dataframe(studs.rename(columns={"student_id": "Student ID", "name": "Name", "batch": "Batch",
                                                   "email": "Email"}), hide_index=True)

# ------------------------------------------------------------------ attendance & the Deloitte report
with t_att:
    if not db:
        st.info("Attendance needs the database (it keeps every class date).")
    else:
        import json

        from attendance import (ABSENT, BELOW, FLAG_ALL, FLAG_IMPROPER, FLAG_LOW_ATT, FLAG_LOW_PERF, FLAG_MCQ,
                                FLAG_MOCK, FLAGS, PRESENT, AttendanceError, alias_key, batch_overview,
                                class_summary, deloitte_workbook, flag_list, match_rows, read_reports,
                                session_records, tracker)
        from batchreport import improper_submissions, prepare
        from roster_flag import ROW_CSS, panel_html

        acfg = load_settings(db.settings())
        a_set = db.settings()
        try:
            aliases = json.loads(a_set.get("attendance_aliases") or "{}")
        except ValueError:
            aliases = {}
        min_att = float(a_set.get("att_min_pct") or 80)
        low_perf = float(a_set.get("low_perf_pct") or acfg["pass_mark"])
        roster_all = db.students()
        st.subheader("Attendance & the report for Deloitte")
        st.caption("Classes, mock returns and MCQ tests for every learner in one place - who is not attending, who "
                   "has not submitted, whose submission was not accepted, who missed MCQs and who is not "
                   "performing well.")
        if not roster_all:
            st.info("Add the student roster under **Students & batches** first (with the Email column, so class "
                    "attendance can be matched).")
        a_up, a_tr, a_cl, a_rep = st.tabs(["Upload class attendance", "Learner tracker", "Class-wise",
                                           "Report for Deloitte"])

        # ---------------------------------------------------------- upload
        with a_up:
            st.write("Upload the daily attendance report Rajashekhar sends Deloitte "
                     "(**Deloitte-Form1040-Attendance-80pct-&lt;date&gt;.xlsx**) - one or more days at once. The "
                     "class date is read from the file. Learners at 80% or more of the session are **Present**, "
                     "those who left early are **Below 80%** (not counted as attended), and roster learners who "
                     "are not in the report are **Absent**. Uploading a date again replaces it.")
            n_mail = sum(1 for x in roster_all if x.get("email"))
            if roster_all and n_mail < len(roster_all):
                st.markdown(panel_html("NO EMAIL ON ROSTER",
                                       f"<b>{len(roster_all) - n_mail} of {len(roster_all)}</b> roster learners have "
                                       "no Deloitte email, so they are matched by name only. Add the <b>Email</b> "
                                       "column to the roster under Students &amp; batches for exact matching."),
                            unsafe_allow_html=True)
            files = st.file_uploader("Attendance reports", type=["xlsx", "csv"], accept_multiple_files=True,
                                     key=f"att_files{ss.get('att_n', 0)}")
            done_dates = {r["session_date"]: r for r in db.attendance_dates()}
            id_opts = [""] + [f"{x['student_id']} - {x['name'] or ''} ({x['batch'] or ''})" for x in roster_all]
            parsed = []
            for f in files or []:
                try:
                    for d0, rows in read_reports(f.getvalue(), f.name):
                        parsed.append((f"{f.name} | {d0:%d %b %Y}", d0, rows))
                except AttendanceError as e:
                    st.error(f"{f.name}: {e}")
                except Exception:
                    log.exception("reading attendance failed")
                    st.error(f"{f.name} could not be read. Check it is an attendance report.")
            if len(parsed) > 1:
                st.info(f"{len(parsed)} class dates found. Check each below, then save them one by one or all at "
                        "once.")
                if st.button(f"Save all {len(parsed)} dates", type="primary", key="att_save_all"):
                    n_all = 0
                    for _, d0, rows in parsed:
                        recs_, _ = session_records(d0.isoformat(), match_rows(rows, roster_all, aliases), roster_all)
                        n_all += db.save_attendance(d0.isoformat(), recs_, staff or "")
                    st.success(f"Saved {n_all} attendance records for {len(parsed)} class dates.")
            for label, d0, rows in parsed:
                f = type("F", (), {"name": label})
                with st.expander(label, expanded=len(parsed) == 1):
                    d = st.date_input("Class date", value=d0, key=f"att_d_{f.name}")
                    ds = d.isoformat()
                    matched = match_rows(rows, roster_all, aliases)
                    recs, unmatched = session_records(ds, matched, roster_all)
                    cnt = pd.Series([r["status"] for r in recs]).value_counts()
                    k = st.columns(5)
                    k[0].metric("In the report", len(rows))
                    k[1].metric(PRESENT, int(cnt.get(PRESENT, 0)))
                    k[2].metric(BELOW, int(cnt.get(BELOW, 0)))
                    k[3].metric(ABSENT, int(cnt.get(ABSENT, 0)), help="Roster learners of the batches in this "
                                                                     "report who are not in it")
                    k[4].metric("Not matched", len(unmatched))
                    by_how = pd.Series([r["matched_by"] for r in recs if r["status"] != ABSENT]).value_counts()
                    st.caption("Matched by " + ", ".join(f"{h}: {n}" for h, n in by_how.items()) if len(by_how)
                               else "Nobody matched yet.")
                    if ds in done_dates:
                        st.warning(f"Attendance for {d:%d %b %Y} is already saved - saving replaces it for the "
                                   "batches in this file.")
                    links = {}
                    if unmatched:
                        st.markdown(f"**{len(unmatched)} people in the report could not be matched to the roster.** "
                                    "Link them below if they are learners (the link is remembered for later "
                                    "days); leave blank if not. If they are not learners at all, leave them.")
                        um = pd.DataFrame([{"Batch": r["batch"], "Name in report": r["name"], "Email": r["email"],
                                            "Status": r["status"], "Link to roster learner": ""} for r in unmatched])
                        ed = st.data_editor(um, hide_index=True, use_container_width=True, key=f"att_um_{f.name}",
                                            disabled=["Batch", "Name in report", "Email", "Status"],
                                            column_config={"Link to roster learner": st.column_config.SelectboxColumn(
                                                options=id_opts, width="large")})
                        for (_, e_), r in zip(ed.iterrows(), unmatched):
                            if e_["Link to roster learner"]:
                                links[alias_key(r)] = e_["Link to roster learner"].split(" - ")[0]
                    if st.button(f"Save attendance for {d:%d %b %Y}", type="primary", key=f"att_save_{f.name}"):
                        if links:
                            aliases.update(links)
                            db.save_settings({"attendance_aliases": json.dumps(aliases)})
                            matched = match_rows(rows, roster_all, aliases)
                            recs, unmatched = session_records(ds, matched, roster_all)
                        n = db.save_attendance(ds, recs, staff or "")
                        st.success(f"Saved {n} attendance records for {d:%d %b %Y}"
                                   + (f" ({len(links)} learner(s) linked by hand)." if links else "."))
            saved = db.attendance_dates()
            st.markdown("##### Class dates saved")
            if not saved:
                st.write("None yet.")
            else:
                st.dataframe(pd.DataFrame(saved).rename(columns={"session_date": "Date", "n": "Records",
                                                                 "uploaded_at": "Uploaded", "uploaded_by": "By"}),
                             hide_index=True)
                with st.expander("Remove a class date"):
                    rm = st.selectbox("Date", [r["session_date"] for r in saved], key="att_rm")
                    if st.checkbox(f"Yes, remove all attendance for {rm}", key="att_rm_ok") and \
                            st.button("Remove", key="att_rm_btn"):
                        db.erase_attendance(rm)
                        st.success(f"Attendance for {rm} removed.")
                        st.rerun()

        # ---------------------------------------------------------- shared data for the other tabs
        fc_a = db.final_cases()
        all_evs_a = db.evaluations()
        studs_a, evs_a = prepare(roster_all, all_evs_a, fc_a)
        try:
            runs_a = db.run_rows(fc_a)
        except Exception:
            log.exception("reading the evaluation runs failed")
            runs_a = []
        imp_a = improper_submissions(studs_a, evs_a, runs_a, case_ids=fc_a, all_evaluations=all_evs_a)
        mcq_a = db.mcq_scores()
        att_a = db.attendance()
        scores_a = final_scores(studs_a, evs_a, mcq_a, fc_a, acfg, view=SO_FAR)
        track = tracker(studs_a, att_a, evs_a, mcq_a, fc_a, imp_a, scores_a, acfg, min_att=min_att,
                        low_perf=low_perf)

        def style_track(df):
            if df.empty or "Flags" not in df:
                return df
            return df.style.apply(lambda r: [ROW_CSS if FLAG_IMPROPER in str(r.get("Flags")) else ""] * len(r),
                                  axis=1)

        # ---------------------------------------------------------- tracker
        with a_tr:
            if track.empty:
                st.info("No learners yet.")
            else:
                f1, f2, f3 = st.columns([1, 2, 1])
                bsel_a = f1.multiselect("Batches", sorted(track["Batch"].unique()),
                                        default=sorted(track["Batch"].unique()), key="att_b")
                fsel = f2.multiselect("Show learners flagged for", FLAGS, key="att_f",
                                      help="Empty = everyone. Several = learners with any of them.")
                q_a = f3.text_input("Search name or ID", key="att_q")
                v = track[track["Batch"].isin(bsel_a)]
                if fsel:
                    v = v[v["Flags"].apply(lambda x: any(f_ in x for f_ in fsel))]
                if q_a.strip():
                    v = v[v["Name"].str.contains(q_a, case=False, na=False) |
                          v["Student ID"].str.contains(q_a, case=False, na=False)]
                ov = batch_overview(track[track["Batch"].isin(bsel_a)])
                tot = ov.set_index("Batch").loc["All batches"] if not ov.empty else {}
                k = st.columns(7)
                k[0].metric("Learners", int(tot.get("Learners", 0)))
                k[1].metric("Avg attendance", "-" if pd.isna(tot.get("Average attendance %")) else
                            f"{tot.get('Average attendance %'):g}%")
                for i_, f_ in enumerate([FLAG_LOW_ATT, FLAG_MOCK, FLAG_IMPROPER, FLAG_MCQ, FLAG_LOW_PERF], 2):
                    k[i_].metric(f_, int(tot.get(f_, 0)))
                if int(tot.get(FLAG_ALL, 0)):
                    st.markdown(callout_html(flag_list(track[track["Batch"].isin(bsel_a)], FLAG_ALL)["Student ID"]
                                             .tolist(), "learner(s) attended no class, submitted no mock return "
                                             "and took no MCQ test", "Listed on the 'Absent for everything' sheet "
                                             "of the Deloitte report.", label="ABSENT FOR EVERYTHING", tail=""),
                                unsafe_allow_html=True)
                st.caption(f"Rows shaded maroon: a mock return was submitted but not accepted (improper "
                           f"submission). Low class attendance = below {min_att:g}% of classes at 80%+ of the "
                           f"session. Not performing well = below {low_perf:g}% of the marks possible so far. "
                           "Change these under 'Report for Deloitte'.")
                show_cols = ["Student ID", "Name", "Batch", "Flags", "Classes held", "Present (80%+)", "Below 80%",
                             "Absent", "Attendance %", "Mocks due", "Mocks evaluated", "Improper submissions",
                             "Mocks not received", "MCQ tests held", "MCQ tests taken", "Marks so far",
                             "% of possible so far", "Not received", "Improper details", "MCQ missed"] + \
                            [c for c in v.columns if c.startswith("Class ")]
                st.dataframe(style_track(v.sort_values(["Flag count", "Attendance %"], ascending=[False, True])
                                         [show_cols]),
                             hide_index=True, use_container_width=True,
                             column_config={"Attendance %": st.column_config.ProgressColumn(
                                 format="%.0f%%", min_value=0, max_value=100),
                                 "Flags": st.column_config.TextColumn(width="large")})
                st.caption(f"{len(v)} learner(s) shown.")

        # ---------------------------------------------------------- class-wise
        with a_cl:
            cs_df = class_summary(att_a, studs_a)
            if cs_df.empty:
                st.info("No class attendance saved yet.")
            else:
                st.dataframe(cs_df, hide_index=True, use_container_width=True,
                             column_config={"Attendance %": st.column_config.ProgressColumn(
                                 format="%.1f%%", min_value=0, max_value=100)})
                try:
                    st.line_chart(cs_df.pivot(index="Date", columns="Batch", values="Attendance %"), height=260)
                except Exception:
                    log.exception("attendance chart failed")

        # ---------------------------------------------------------- report for Deloitte
        with a_rep:
            st.write("One Simandhar-branded Excel workbook for Deloitte: a summary per batch, every learner, and "
                     "one sheet each for learners not attending classes, mock returns not submitted, **submissions "
                     "not accepted (improper)**, MCQ tests missed, learners not performing well, and learners "
                     "absent for everything - plus class-wise attendance. Learners with an improper submission are "
                     "shaded maroon on every sheet.")
            r1, r2 = st.columns(2)
            new_min = r1.number_input("Minimum class attendance (% of classes attended at 80%+)", 0.0, 100.0,
                                      min_att, 5.0, key="att_min")
            new_low = r2.number_input("Not performing well below (% of marks possible so far)", 0.0, 100.0,
                                      low_perf, 5.0, key="att_low")
            if (new_min, new_low) != (min_att, low_perf) and st.button("Save these limits"):
                db.save_settings({"att_min_pct": str(new_min), "low_perf_pct": str(new_low)})
                st.rerun()
            ov_all = batch_overview(track)
            if not ov_all.empty:
                st.dataframe(ov_all, hide_index=True, use_container_width=True)
            st.download_button("Download the report for Deloitte (Excel)",
                               deloitte_workbook(track, class_summary(att_a, studs_a), institute,
                                                 min_att=min_att, low_perf=low_perf),
                               file_name=f"Deloitte_Form1040_Learner_Report_{datetime.now():%Y%m%d}.xlsx",
                               type="primary", disabled=track.empty)

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
            pa, po, pt = per_item(cfg)
            st.caption(f"Return accuracy {cfg['w_accuracy']:g} + on-time submission {cfg['w_on_time']:g} + MCQ tests "
                       f"{cfg['w_mcq']:g} = 100. Each mock return is worth {pa:.2f} accuracy marks (in proportion "
                       f"to its score) and {po:.2f} on-time marks; each MCQ test {pt:.2f} marks. Marks add up as the "
                       f"course goes on. Pass mark {cfg['pass_mark']:g}. {cfg['n_mocks']:g} mock returns "
                       f"({len(fcases)} set up so far), {cfg['n_mcq']:g} MCQ tests of {cfg['mcq_out_of']:g} marks.")
            if len(fcases) > cfg["n_mocks"]:
                st.warning(f"{len(fcases)} cases count toward the final score but the course has {cfg['n_mocks']:g} "
                           "mock returns. Untick the practice cases under **Scoring settings**.")
            r1, r2 = st.columns([1, 2])
            view = r1.radio("View", ["Progress so far", "Final (course complete)"], key="fs_view",
                            help="Both views show the marks earned so far out of 100. Progress so far also "
                                 "shows the marks that were possible from the returns and tests the batch has done; "
                                 "no Pass / At risk result. Final: Pass, or At risk if the marks out of 100 are "
                                 "below the pass mark.")
            view = SO_FAR if view.startswith("Progress") else FINAL
            batches = sorted({x["batch"] or "" for x in all_students})
            pick_b = r2.multiselect("Batches", batches, default=batches, key="fs_batches")
            studs = [x for x in all_students if (x["batch"] or "") in pick_b]
            if not studs:
                st.info("No students on the roster for these batches - add them under 'Students & batches'.")
            else:
                evs = [e for e in db.evaluations() if e["case_id"] in fcases]
                df = final_scores(studs, evs, db.mcq_scores(), fcases, cfg, view=view)
                c = st.columns(3)
                c[0].metric("Students", len(df))
                if view == FINAL:
                    c[1].metric("Pass", int((df["Result"] == "Pass").sum()))
                    c[2].metric("At risk", int((df["Result"] == "At risk").sum()),
                                help=f"Marks out of 100 below the pass mark of {cfg['pass_mark']:g}")
                else:
                    c[1].metric("Average marks so far", f"{df['Total / 100'].mean():.2f}")
                    c[2].metric("Average % of possible so far",
                                "-" if df["% of possible so far"].isna().all() else
                                f"{df['% of possible so far'].mean():.1f}%")
                if int(df["Status not set"].sum()):
                    st.warning(f"{int(df['Status not set'].sum())} evaluated return(s) have no on-time status yet "
                               "(evaluated before this feature) and earn no on-time marks. Set them under "
                               "**Submission status**.")
                st.dataframe(df, hide_index=True, use_container_width=True)
                off_df = not_on_roster(db.evaluations(), [x["student_id"] for x in all_students], fcases)
                if not off_df.empty:
                    st.markdown(callout_html(off_df["Student ID"].tolist(), "student(s) with evaluated mock returns "
                                             "are", "Their returns are saved but are <b>not counted</b> in these "
                                             "results. Add them under <b>Students &amp; batches</b> (or erase "
                                             "them under Admin if they are test entries)."),
                                unsafe_allow_html=True)
                    st.dataframe(style_off_roster(off_df), hide_index=True, use_container_width=True)
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
                                          "Batch": (e.get("batch") or "") if e["student_id"] in roster
                                          else NOT_ON_ROSTER, "Score /100": round(100 * (e["pct"] or 0), 1),
                                          "Submission": lab.get(e.get("on_time"), "Not set")} for e in evs_c])
                    off_c = sorted(e["student_id"] for e in evs_c if e["student_id"] not in roster)
                    if off_c:
                        st.markdown(callout_html(off_c, "evaluated student(s) for this case are",
                                                 "Their Batch shows NOT ON ROSTER below. Add them under "
                                                 "<b>Students &amp; batches</b> so they count in results."),
                                    unsafe_allow_html=True)
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
                # students who did not submit, or whose submission could not be evaluated
                from batchreport import case_submissions
                c_studs = [dict(x, batch=x["batch"] or "") for x in all_students]
                try:
                    c_runs = db.run_rows([cs])
                except Exception:
                    log.exception("reading the evaluation runs failed")
                    c_runs = []
                nr_df, imp_df = case_submissions(c_studs, db.evaluations(), c_runs, cs,
                                                 None if bsel == "All batches" else bsel)
                imp_df = add_roster_column(imp_df, [x["student_id"] for x in all_students])
                n_roster = sum(1 for x in c_studs if bsel == "All batches" or x["batch"] == bsel)
                n_imp = int((imp_df["Student ID"] != "").sum())
                st.markdown(f"**{len(evs_c)}** evaluated · **{n_imp}** improper submission(s) · "
                            f"**{len(nr_df)}** not received - of {n_roster} student(s) on the roster")
                cs_stamp = datetime.now().strftime("%Y%m%d")
                st.markdown("##### Improper submissions")
                if imp_df.empty:
                    st.caption("None - every return that came in for this case was evaluated and named properly.")
                else:
                    st.caption("Returns that came in but could not be evaluated (unreadable PDF, no PDF in the "
                               "folder, wrong file type), files that could not be matched to a student, and returns "
                               "evaluated but not named StudentID_CaseID. A failed upload that was evaluated later "
                               "drops off this list. These students earn no marks for this case until their return is "
                               "evaluated.")
                    st.dataframe(style_off_roster(imp_df), hide_index=True, use_container_width=True,
                                 column_config={"Details": st.column_config.TextColumn(width="large")})
                st.markdown("##### Not received (did not attempt)")
                if nr_df.empty:
                    st.caption("Every student on the roster has submitted this case.")
                else:
                    st.caption("Students on the roster with no return for this case - not evaluated and no upload "
                               "on record. They earn 0 accuracy and 0 on-time marks for it.")
                    st.dataframe(nr_df, hide_index=True, use_container_width=True)
                if not imp_df.empty or not nr_df.empty:
                    st.download_button("Download both lists (Excel)",
                                       excel_bytes([("Improper submissions", imp_df), ("Not received", nr_df)]),
                                       file_name=f"Submissions_{clean(cs)}_{clean(bsel)}_{cs_stamp}.xlsx")
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
                        st.markdown(callout_html(unknown, "student ID(s) in this sheet are",
                                                 "Their marks will be saved anyway and show in results once they "
                                                 "are added under <b>Students &amp; batches</b>."),
                                    unsafe_allow_html=True)
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

# ------------------------------------------------------------------ batch reports
BATCH_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]   # validated categorical order


def _integrity_cached(evs_in, cases_in):
    """Integrity analysis of the course's mock returns, re-run only when the evaluations change."""
    sig = (tuple(sorted(cases_in)), len(evs_in), max((str(e.get("evaluated_at") or "") for e in evs_in), default=""),
           len(db.students()))
    if ss.get("bi_integ_sig") != sig:
        ids = {e["student_id"] for e in evs_in}
        mis = [m for m in db.mistakes() if m["case_id"] in cases_in and m["student_id"] in ids]
        ss.bi_integ = analyse(evs_in, db.fingerprints(list(cases_in)), mis, db.students()) if evs_in else None
        ss.bi_integ_sig = sig
        ss.bi_integ_at = datetime.now().strftime("%d %b %H:%M")
    return ss.bi_integ


with t_batch:
    if not db:
        st.info("Batch reports need the database.")
    else:
        import json

        from batch_pdf import batch_report_excel, batch_report_pdf
        from batchreport import (ALL, batch_summary, improper_submissions, integrity_for, missing_returns, mock_table,
                                 mock_trend, prepare, score_bands, top_mistakes)
        from integrity_report import CAUTION as INTEG_CAUTION

        bcfg = load_settings(db.settings())
        try:
            trainers = json.loads(db.settings().get("batch_trainers") or "{}")
        except ValueError:
            trainers = {}
        b_fc_all = db.final_cases()
        b_case_info = {c["case_id"]: c for c in db.cases()}
        b_studs_all, b_evs_all = prepare(db.students(), db.evaluations(), b_fc_all)
        all_batches = sorted({x["batch"] for x in b_studs_all})
        color_of = {b: BATCH_COLORS[i % len(BATCH_COLORS)] for i, b in enumerate(all_batches)}   # follows the batch

        st.subheader("Batch reports")
        if not b_studs_all:
            st.info("Add the student roster under **Students & batches** first.")
        else:
            f1, f2 = st.columns([1, 1])
            sel_b = f1.multiselect("Batches", all_batches, default=all_batches, key="bi_batches")
            done_cases = [c for c in b_fc_all if any(e["case_id"] == c for e in b_evs_all)]
            sel_c = f2.multiselect("Mock returns", done_cases, default=done_cases, key="bi_cases",
                                   help="Only these mock returns are counted everywhere on this page and in the "
                                        "reports.")
            if not sel_b or not sel_c:
                st.info("Choose at least one batch and one mock return." if done_cases else
                        "No mock returns evaluated yet.")
            else:
                studs = [x for x in b_studs_all if x["batch"] in sel_b]
                ids_sel = {x["student_id"] for x in studs}
                evs = [e for e in b_evs_all if e["case_id"] in sel_c and e["student_id"] in ids_sel]
                mcq_all = db.mcq_scores()
                try:
                    b_runs = db.run_rows(sel_c)
                except Exception:
                    log.exception("reading the evaluation runs failed")
                    b_runs = []
                improper_all = improper_submissions(studs, evs, b_runs, case_ids=sel_c)
                with st.spinner("Checking integrity..."):
                    integ = _integrity_cached([e for e in b_evs_all if e["case_id"] in sel_c], tuple(sel_c))
                summary = batch_summary(studs, evs, mcq_all, sel_c, bcfg,
                                        integ["students"] if integ else None, trainers)
                all_row = summary.set_index("Batch").loc[ALL]
                pc = lambda v: "-" if v is None or pd.isna(v) else f"{v:g}%"

                o_tab, d_tab, r_tab, t_tab = st.tabs(["Overview", "Batch detail", "Reports for trainers",
                                                      "Trainers"])
                # ------------------------------------------------ overview
                with o_tab:
                    k = st.columns(6)
                    k[0].metric("Students", int(all_row["Students"]))
                    k[1].metric("Submission rate", pc(all_row["Submission rate %"]),
                                help=f"{all_row['Returns received']} of {all_row['Returns expected']} returns due "
                                     "so far")
                    k[2].metric("On-time rate", pc(all_row["On-time rate %"]))
                    k[3].metric("Average score", pc(all_row["Average score %"]))
                    k[4].metric("MCQ average", pc(all_row["MCQ average %"]))
                    k[5].metric("Integrity flags", int(all_row["Integrity flags"]),
                                help="Students rated likely or very likely copied")
                    st.markdown("##### Batch comparison")
                    st.dataframe(
                        summary.drop(columns=["Returns expected"]), hide_index=True, use_container_width=True,
                        column_config={
                            "Submission rate %": st.column_config.ProgressColumn(format="%.1f%%", min_value=0,
                                                                                 max_value=100),
                            "On-time rate %": st.column_config.ProgressColumn(format="%.1f%%", min_value=0,
                                                                              max_value=100),
                            "Average score %": st.column_config.NumberColumn(format="%.1f"),
                            "MCQ average %": st.column_config.NumberColumn(format="%.1f")})
                    trend = mock_trend(studs, evs, sel_c, [b for b in all_batches if b in sel_b])
                    if not trend.empty:
                        st.markdown("##### Average score per mock return")
                        try:
                            import altair as alt
                            dom = [b for b in all_batches if b in set(trend["Batch"])]
                            chart = (alt.Chart(trend).mark_line(point=alt.OverlayMarkDef(size=60, filled=True),
                                                                strokeWidth=2)
                                     .encode(x=alt.X("Case:N", sort=sel_c, title=None),
                                             y=alt.Y("Average %:Q", scale=alt.Scale(domain=[0, 100]),
                                                     title="Average score (%)"),
                                             color=alt.Color("Batch:N", scale=alt.Scale(
                                                 domain=dom, range=[color_of[b] for b in dom]),
                                                 legend=alt.Legend(orient="top", title=None)),
                                             tooltip=["Batch", "Case", "Average %", "Returns"])
                                     .properties(height=280))
                            rule = alt.Chart(pd.DataFrame({"y": [bcfg["pass_mark"]]})).mark_rule(
                                strokeDash=[4, 3], color="#8a8a8a").encode(y="y:Q")
                            st.altair_chart(chart + rule, use_container_width=True)
                            st.caption(f"Dashed line: pass mark {bcfg['pass_mark']:g}. Hover a point for details.")
                        except Exception:
                            log.exception("chart failed")
                        with st.expander("Table view"):
                            st.dataframe(trend.pivot(index="Case", columns="Batch", values="Average %")
                                         .reindex(sel_c).dropna(how="all"), use_container_width=True)
                    if integ is None:
                        st.caption("Integrity: nothing to check yet.")
                    else:
                        st.caption(f"Integrity check last refreshed {ss.get('bi_integ_at', '')} - it re-runs by "
                                   "itself when new returns are evaluated.")

                # ------------------------------------------------ batch detail
                with d_tab:
                    pick = st.selectbox("Batch", sel_b, key="bi_one",
                                        format_func=lambda b: b + (f" - {trainers[b]['trainer']}"
                                                                   if trainers.get(b, {}).get("trainer") else ""))
                    row = summary.set_index("Batch").loc[pick]
                    bstuds = [x for x in studs if x["batch"] == pick]
                    scores = final_scores(bstuds, evs, mcq_all, sel_c, bcfg, view=SO_FAR)

                    def delta(col):
                        a, b_ = row[col], all_row[col]
                        return None if a is None or b_ is None or pd.isna(a) or pd.isna(b_) else \
                            f"{a - b_:+.1f} vs all"
                    k = st.columns(6)
                    k[0].metric("Students", int(row["Students"]), help=f"{int(row['Mocks done'])} mock returns done")
                    k[1].metric("Submission rate", pc(row["Submission rate %"]), delta("Submission rate %"))
                    k[2].metric("On-time rate", pc(row["On-time rate %"]), delta("On-time rate %"))
                    k[3].metric("Average score", pc(row["Average score %"]), delta("Average score %"))
                    k[4].metric("MCQ average", pc(row["MCQ average %"]), delta("MCQ average %"))
                    k[5].metric("Integrity flags", int(row["Integrity flags"]))
                    s1, s2, s3, s4, s5 = st.tabs(["Students", "Mock returns", "Not received / improper",
                                                  "Common mistakes", "Integrity"])
                    with s1:
                        q = st.text_input("Search name or ID", key="bi_q")
                        view_df = scores
                        if q.strip():
                            view_df = view_df[view_df["Name"].str.contains(q, case=False, na=False) |
                                              view_df["Student ID"].str.contains(q, case=False, na=False)]
                        st.caption(f"Total / 100 = marks earned so far (each mock return is worth "
                                   f"{per_item(bcfg)[0]:.2f} accuracy + {per_item(bcfg)[1]:.2f} on-time marks). "
                                   "Pass / At risk is shown under Management dashboard > Final results (Final view).")
                        st.dataframe(view_df.sort_values(["% of possible so far", "Total / 100"]), hide_index=True,
                                     use_container_width=True,
                                     column_config={"Total / 100": st.column_config.ProgressColumn(
                                         format="%.2f", min_value=0, max_value=100),
                                         "% of possible so far": st.column_config.NumberColumn(format="%.1f%%")})
                        bands = score_bands(scores)
                        if bands["Students"].sum():
                            st.markdown("##### How the batch is spread (% of marks possible so far)")
                            st.bar_chart(bands, x="Score band", y="Students", color=color_of[pick], height=220)
                    with s2:
                        st.dataframe(mock_table(studs, evs, sel_c, b_case_info, pick), hide_index=True,
                                     use_container_width=True)
                    with s3:
                        imp_b = improper_submissions(studs, evs, b_runs, pick, case_ids=sel_c)
                        mr = missing_returns(studs, evs, pick, improper=imp_b)
                        st.markdown("##### Not received (did not attempt)")
                        st.write("Every return is in." if mr.empty else
                                 f"**{len(mr)}** student(s) have mock returns not received.")
                        if not mr.empty:
                            st.dataframe(mr, hide_index=True, use_container_width=True)
                        st.markdown("##### Improper submissions")
                        if imp_b.empty:
                            st.write("None.")
                        else:
                            st.write(f"**{len(imp_b)}** return(s) came in but could not be evaluated, or were named "
                                     "wrongly.")
                            st.dataframe(imp_b, hide_index=True, use_container_width=True,
                                         column_config={"Details": st.column_config.TextColumn(width="large")})
                    with s4:
                        st.caption("The lines most often wrong in this batch - good topics for a revision class.")
                        st.dataframe(top_mistakes(db.mistakes(), evs, pick, limit=25), hide_index=True,
                                     use_container_width=True)
                    with s5:
                        st.caption(INTEG_CAUTION)
                        ib = integrity_for(integ, pick)
                        if ib["students"].empty and ib["pairs"].empty and ib["trainer"].empty:
                            st.success("No integrity concerns in this batch.")
                        else:
                            st.markdown("**Students with concerns**")
                            st.dataframe(ib["students"], hide_index=True, use_container_width=True)
                            if not ib["pairs"].empty:
                                st.markdown("**Flagged pairs**")
                                st.dataframe(ib["pairs"], hide_index=True, use_container_width=True,
                                             column_config={"Evidence": st.column_config.TextColumn(width="large")})
                            if not ib["trainer"].empty:
                                st.markdown("**Built on the trainer's correct-return file**")
                                st.dataframe(ib["trainer"], hide_index=True, use_container_width=True)

                # ------------------------------------------------ reports for trainers
                def build_reports(b):
                    bs = [x for x in studs if x["batch"] == b]
                    sc = final_scores(bs, evs, mcq_all, sel_c, bcfg, view=SO_FAR)
                    r_ = summary.set_index("Batch").loc[b].to_dict()
                    imp_ = improper_submissions(studs, evs, b_runs, b, case_ids=sel_c)
                    args = (b, trainers.get(b, {}).get("trainer", ""), r_, sc,
                            mock_table(studs, evs, sel_c, b_case_info, b), missing_returns(studs, evs, b, improper=imp_),
                            top_mistakes(db.mistakes(), evs, b), integrity_for(integ, b))
                    return (batch_report_pdf(*args, bcfg, institute, cohort_avg=all_row["Average score %"],
                                             integrity_ran=integ is not None, caution=INTEG_CAUTION, improper=imp_),
                            batch_report_excel(*args, integrity_ran=integ is not None, improper=imp_))

                with r_tab:
                    st.write("One consolidated report per batch for its trainer: a Simandhar-branded PDF and an "
                             "Excel workbook with progress, submissions, mock-by-mock results, common mistakes and "
                             "integrity findings. They use the batches and mock returns chosen above.")
                    stamp = datetime.now().strftime("%Y%m%d")
                    fname = lambda b, ext: (f"Batch_report_{clean(b)}_"
                                            f"{clean(trainers.get(b, {}).get('trainer', '') or 'trainer')}_{stamp}.{ext}")
                    if st.button(f"Prepare reports for all {len(sel_b)} batches", type="primary"):
                        zb = io.BytesIO()
                        with st.spinner("Preparing reports..."), zipfile.ZipFile(zb, "w", zipfile.ZIP_DEFLATED) as z:
                            for b in sel_b:
                                p_, x_ = build_reports(b)
                                z.writestr(fname(b, "pdf"), p_)
                                z.writestr(fname(b, "xlsx"), x_)
                        ss.bi_zip = zb.getvalue()
                    if ss.get("bi_zip"):
                        st.download_button("Download all batch reports (zip)", ss.bi_zip,
                                           file_name=f"Batch_reports_{stamp}.zip")
                    st.divider()
                    for b in sel_b:
                        tr = trainers.get(b, {})
                        c1, c2, c3 = st.columns([3, 1, 1])
                        c1.markdown(f"**{b}** · {tr.get('trainer') or 'trainer not set'}"
                                    + (f" · {tr.get('email')}" if tr.get("email") else ""))
                        if c2.button("Prepare", key=f"bi_prep_{b}"):
                            ss[f"bi_rep_{b}"] = build_reports(b)
                        if ss.get(f"bi_rep_{b}"):
                            p_, x_ = ss[f"bi_rep_{b}"]
                            c3.download_button("PDF", p_, file_name=fname(b, "pdf"), key=f"bi_pdf_{b}")
                            c3.download_button("Excel", x_, file_name=fname(b, "xlsx"), key=f"bi_xls_{b}")

                # ------------------------------------------------ trainers
                with t_tab:
                    st.write("Who trains each batch. Their names go on the reports and in the file names.")
                    tdf = pd.DataFrame([{"Batch": b, "Trainer": trainers.get(b, {}).get("trainer", ""),
                                         "Email": trainers.get(b, {}).get("email", "")} for b in all_batches])
                    ed = st.data_editor(tdf, hide_index=True, use_container_width=True, disabled=["Batch"],
                                        key="bi_trainers")
                    if st.button("Save trainers"):
                        new = {r["Batch"]: {"trainer": str(r["Trainer"] or "").strip(),
                                            "email": str(r["Email"] or "").strip()} for _, r in ed.iterrows()}
                        db.save_settings({"batch_trainers": json.dumps(new)})
                        for b in all_batches:
                            ss.pop(f"bi_rep_{b}", None)
                        ss.pop("bi_zip", None)
                        st.success("Trainers saved.")
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
        f_rep, f_hist = st.tabs(["Student reports", "Run history"])
        with f_rep:
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

        # ---------------------------------------------------------- run history
        with f_hist:
            st.write("A record of every evaluation run - what was uploaded, and for every file or student whether "
                     "it was evaluated, skipped or failed, and why. The students' PDF reports are not kept here "
                     "(find those under *Student reports*).")
            roster_b = {x["student_id"]: (x["batch"] or "").strip() or "Unassigned" for x in db.students()}
            NO_STUDENT = "No student identified"
            h1, h2, h3, h4 = st.columns(4)
            h_case = h1.selectbox("Case", ["All cases"] + [c["case_id"] for c in db.cases()], key="rh_case")
            h_days = h2.selectbox("Period", ["Last 7 days", "Last 30 days", "Last 90 days", "Everything"],
                                  key="rh_days")
            h_kind = h3.multiselect("Type", ["Bulk", "Single"], default=["Bulk", "Single"], key="rh_kind")
            days = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}.get(h_days)
            since = ((datetime.now(timezone.utc) - pd.Timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                     if days else None)
            runs_ = [r for r in db.runs(case_id=None if h_case == "All cases" else h_case, since=since)
                     if r["kind"] in h_kind]

            # every row of every run in the period, each with its batch (from the run, else today's roster)
            all_rows, all_up = [], []
            for meta in runs_:
                d = db.run_detail(meta["run_id"]) or {}
                tag = {"run_id": meta["run_id"], "Run started (UTC)": meta["started_at"], "Type": meta["kind"],
                       "Case": meta["case_id"], "By": meta["run_by"]}
                for r in d.get("rows") or []:
                    sid_ = str(r.get("Student ID") or "").upper()
                    b_ = (str(r.get("Batch") or "").strip() or (roster_b.get(sid_) if sid_ else None)
                          or ("Not on roster" if sid_ else NO_STUDENT))
                    all_rows.append(dict(tag, **dict(r, Batch=b_)))
                all_up += [dict(tag, File=u[0], Bytes=u[1]) for u in d.get("uploads") or []]
                if d.get("options"):
                    all_up.append(dict(tag, File="(options) " + "; ".join(f"{k_}: {v}" for k_, v in
                                                                          d["options"].items()), Bytes=None))
            rows_all = pd.DataFrame(all_rows)
            batch_opts = sorted(set(rows_all["Batch"])) if not rows_all.empty else []
            h_batch = h4.multiselect("Batch", batch_opts, default=batch_opts, key="rh_batch",
                                     help=f"'{NO_STUDENT}' holds the files that could not be matched to anyone.")
            rows_f = rows_all[rows_all["Batch"].isin(h_batch)] if not rows_all.empty else rows_all
            if rows_f.empty:
                st.info("No evaluation runs for these filters.")
            else:
                sv_all = rows_f["Status"].astype(str)
                flags = pd.DataFrame({"run_id": rows_f["run_id"],
                                      "Evaluated": sv_all.str.startswith("Evaluated"),
                                      "Skipped": sv_all.str.startswith("Skipped"),
                                      "Failed": sv_all.str.startswith("Failed"),
                                      "To check": sv_all.str.contains("CHECK|check", regex=True)})
                per_run = flags.groupby("run_id").sum().astype(int)
                shown_runs = [r for r in runs_ if r["run_id"] in per_run.index]
                c = st.columns(4)
                c[0].metric("Runs", len(shown_runs))
                c[1].metric("Evaluated", int(per_run["Evaluated"].sum()))
                c[2].metric("Skipped", int(per_run["Skipped"].sum()))
                c[3].metric("Failed", int(per_run["Failed"].sum()))
                table = pd.DataFrame([{"Started (UTC)": r["started_at"], "Type": r["kind"], "Case": r["case_id"],
                                       "By": r["run_by"] or "-",
                                       "Batches": ", ".join(sorted(set(rows_f.loc[rows_f["run_id"] == r["run_id"],
                                                                                  "Batch"])))[:120],
                                       "Files uploaded": r["files"], **per_run.loc[r["run_id"]].to_dict()}
                                      for r in shown_runs])
                st.dataframe(table, hide_index=True, use_container_width=True)
                if set(h_batch) != set(batch_opts):
                    st.caption("Counts cover only the chosen batches.")

                def run_workbook(run_ids):
                    rows_df = add_roster_column(rows_f[rows_f["run_id"].isin(run_ids)].drop(columns=["run_id"]),
                                                roster_b)
                    up_df = pd.DataFrame([u for u in all_up if u["run_id"] in run_ids]).drop(
                        columns=["run_id"], errors="ignore")
                    meta = table[[r["run_id"] in run_ids for r in shown_runs]]
                    sv_ = rows_df["Status"].astype(str)
                    buf_ = io.BytesIO()
                    with pd.ExcelWriter(buf_, engine="openpyxl") as xw:
                        meta.to_excel(xw, sheet_name="Runs", index=False)
                        for nm, df_ in (("Results", rows_df), ("Skipped", rows_df[sv_.str.startswith("Skipped")]),
                                        ("Failed", rows_df[sv_.str.startswith("Failed")]),
                                        ("To check", rows_df[sv_.str.contains("CHECK|check", regex=True)]),
                                        ("Uploaded files", up_df)):
                            (df_ if not df_.empty else pd.DataFrame({"": ["None"]})).to_excel(
                                xw, sheet_name=nm, index=False)
                        for ws in xw.book.worksheets:
                            highlight_excel_sheet(ws)
                            ws.freeze_panes = "A2"
                            for col in ws.columns:
                                ws.column_dimensions[col[0].column_letter].width = min(
                                    max(len(str(c_.value or "")) for c_ in col) + 2, 70)
                    return buf_.getvalue()

                stamp = datetime.now().strftime("%Y%m%d_%H%M")
                st.download_button(f"Download all {len(shown_runs)} runs shown (Excel)",
                                   run_workbook([r["run_id"] for r in shown_runs]),
                                   file_name=f"Run_history_{stamp}.xlsx")
                st.divider()
                pick_run = st.selectbox(
                    "Open one run", [r["run_id"] for r in shown_runs], key="rh_pick",
                    format_func=lambda rid: next(
                        f"{r['started_at']} UTC · {r['kind']} · {r['case_id']} · "
                        f"{per_run.loc[rid, 'Evaluated']} evaluated, {per_run.loc[rid, 'Skipped']} skipped, "
                        f"{per_run.loc[rid, 'Failed']} failed" for r in shown_runs if r["run_id"] == rid))
                one = rows_f[rows_f["run_id"] == pick_run]
                status_f = st.radio("Show", ["Everything", "Skipped", "Failed", "To check"], horizontal=True,
                                    key="rh_show")
                sv = one["Status"].astype(str)
                view_ = one if status_f == "Everything" else one[
                    sv.str.startswith(status_f) if status_f != "To check" else sv.str.contains("CHECK|check",
                                                                                               regex=True)]
                view_ = add_roster_column(view_, roster_b)
                cols_ = [c_ for c_ in ["File", "Student ID", "Roster", "Name", "Batch", "Score /100", "Submission",
                                       "Status", "Notes"] if c_ in view_.columns]
                off_run = off_roster_ids(view_["Student ID"], roster_b) if "Student ID" in view_ else []
                if off_run:
                    st.markdown(callout_html(off_run, "student(s) in this run are"), unsafe_allow_html=True)
                st.dataframe(style_off_roster(view_[cols_]), hide_index=True, use_container_width=True,
                             column_config={"Status": st.column_config.TextColumn(width="large")})
                up_one = [u for u in all_up if u["run_id"] == pick_run]
                with st.expander(f"Files uploaded in this run ({len(up_one)})"):
                    st.dataframe(pd.DataFrame(up_one)[["File", "Bytes"]] if up_one else pd.DataFrame(),
                                 hide_index=True, use_container_width=True)
                st.download_button("Download this run (Excel)", run_workbook([pick_run]),
                                   file_name=f"Run_{pick_run}.xlsx", key="rh_one")
            if ss.get("admin_ok"):
                with st.expander("Clear old run history (admin)"):
                    old = st.selectbox("Delete runs older than", ["90 days", "180 days", "1 year"], key="rh_old")
                    if st.button("Delete old runs", key="rh_del"):
                        dd = {"90 days": 90, "180 days": 180, "1 year": 365}[old]
                        db.erase_runs(before=(datetime.now(timezone.utc) - pd.Timedelta(days=dd))
                                      .strftime("%Y-%m-%d %H:%M:%S"))
                        st.success("Old runs deleted.")


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
            if db and n and not TEST:
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
     Typing slips in the mock code don't matter: `M01`, `m01`, `M1`, `MO1` (letter O), `M0l`, `Mock 01`, with or
     without `Batch-1` in the name, are all read as M01; a different mock (`MO2`) is kept apart.
     Download all the reports as one zip, and the results as Excel.
   - Evaluating the same student and case again replaces the earlier result.
4. **Check before sharing**: open *What the system read* when the app asks you to. Items it could not read are marked
   0 and flagged. Boxes it could not link to a line are listed there; to mark one, use `field: <box name>` in the key.
   - Form 8949 line names: `I box` (A/B/C/G/H/I), `I 1 row 3 (d)` = Part I row 3 column (d), `I 2 (h)` = Part I
     totals column (h); Part II uses `II`. Building the key from a correct return fills these in for you.
5. **Reports later**: use **Find a report / history → Student reports** to download any student's report again,
   or to delete one evaluation. **Run history** keeps a record of every evaluation run: what was uploaded and,
   for every file, whether it was evaluated, skipped or failed and why - download one run or a whole period as Excel.

### How marks are given
- Every item in the key carries its own marks; the total is scaled to a **score out of 100**. Each case keeps its
  own weightage, so one mistake only costs that item's marks.
- An answer must match the key, within the rounding tolerance set in the key, to earn its marks.
- Amounts on lines that should be blank ("unexpected entries") deduct the marks set in the key.
- A form that is missing from the upload makes all of its items wrong.

### Final course score (Management dashboard)
| Part | Marks | How |
|---|---|---|
| Return accuracy | 60 | 60 / 12 = 5 marks per mock return, in proportion to its score (100/100 = 5, 80/100 = 4); 12 returns at 100 = 60; a return not submitted earns 0 |
| On-time submission | 20 | 20 / 12 = 1.67 marks for each return submitted on time; all 12 on time = 20; a late return earns none |
| MCQ tests | 20 | 20 / 4 = 5 marks per test, in proportion to its marks out of 50 |

**Pass mark: 70.** These numbers can be changed under *Scoring settings* (admin password).
- **On time or late** is chosen when you evaluate: for one return, or for the whole batch with a list of exceptions.
  Re-evaluating a return keeps its status. Staff and admin can change it later under *Submission status*;
  every change records who made it, why and when.
- **MCQ marks**: upload an Excel/CSV with *Student ID* and *Test 1* ... *Test 4* columns under *MCQ test marks*.
- **Final results**: marks add up return by return, out of 100, in both views. *Progress so far* also shows the
  marks possible from what the batch has done so far (no Pass / At risk there). *Final* gives **Pass**, or
  **At risk** when the total is below the pass mark.
- Students **not on our roster** are shaded maroon with a **NOT ON ROSTER** tag wherever they appear (evaluation
  results, submission status, final results, MCQ upload, run history and the Excel downloads). Their returns are
  saved but don't count in results or batch reports until they are added under *Students & batches*.
- **Submission status** lists, for each case, the evaluated returns, the **improper submissions** (could not be
  evaluated, no PDF, not matched to a student, named wrongly) and the students **not received** (did not attempt). Download the results in Excel and a
  Simandhar-branded scorecard PDF for each student.
- Under *Scoring settings*, tick only the cases that are the course's mock returns, so practice cases don't count.

### Attendance & the report for Deloitte
- **Upload class attendance**: the daily report Rajashekhar sends Deloitte (*Deloitte-Form1040-Attendance-80pct-
  &lt;date&gt;.xlsx*), one or more days at once. 80%+ of the session = **Present**; joined but left early = **Below 80%**
  (not counted as attended); on the roster but not in the report = **Absent**. Learners are matched by Deloitte email
  (add an *Email* column to the roster), then by name; anyone not matched can be linked by hand once and is
  remembered.
- **Learner tracker**: one row per learner - classes, mock returns, MCQ tests, marks so far - with flags: *Low class
  attendance*, *Missed mock returns*, *Improper submission* (submitted but not accepted - shaded maroon),
  *Missed MCQ tests*, *Not performing well* and *Absent for everything*. Filter by batch and flag.
- **Report for Deloitte**: a Simandhar-branded Excel with a summary per batch, every learner, one sheet per concern
  and class-wise attendance. The attendance and performance limits can be changed there.

### Batch reports (for the trainers)
- **Filters** at the top (batches, mock returns) apply to everything on the page and to the reports.
- **Overview**: totals, a batch-by-batch comparison (submission rate, on-time rate, average and median score,
  MCQ average, on track / at risk, integrity flags) and the average score of every mock per batch.
- **Batch detail**: one batch's students (filter by result, search), mock-by-mock results, returns not received,
  the most common mistakes and its integrity findings.
- **Reports for trainers**: one Simandhar-branded PDF and Excel workbook per batch - download one, or all as a zip.
- **Trainers**: who trains each batch (names go on the reports and file names).
- A batch is measured only on the mock returns it has done so far. The integrity check runs by itself and
  refreshes when new returns are evaluated.

### Other tabs
- **Integrity checks**: compares returns of the same case for copied or shared files, files saved over another
  student's work, the same unusual wrong answers and identical typing habits.
- **Data management** *(admin password)*: erase a case's results, a whole case, a batch, a student, or everything.

### Privacy
Student PDFs are not stored: only scores, mistakes and the reader's notes are saved. Without a database nothing is
kept after the browser tab closes.
""")
