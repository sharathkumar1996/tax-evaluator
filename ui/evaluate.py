"""Evaluate: pick the mock return, drop in one student's forms or a whole batch, get scores and reports."""
import io
import os
import re
import zipfile
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from bulk import group_uploads
from grader_core import fmt, grade, summarise
from integrity import fingerprints
from irs_reader import ReaderError
from metrics import flag, return_metrics
from readers import read_return
from report_pdf import build_report
from roster_flag import add_roster_column, callout_html, excel_bytes, off_roster_ids, style_off_roster
from ui.core import ctx, data, header, key_hash, log, parse_key, report_name, ss, stamp

SUBMITTED = ["On time", "Late"]


# ------------------------------------------------------------------ the case being marked
def _choose_case():
    """Returns (key, key_bytes) for the chosen saved case, or (None, None)."""
    cases = data.cases if ctx.db else []
    if not cases:
        if ss.get("key"):                        # no database: a key uploaded under Setup for this session
            return ss.key, ss.key_bytes
        st.info("No mock return is set up yet. Add its answer key under **Setup → Mock returns**.")
        return None, None
    ids = [c["case_id"] for c in cases]
    title = {c["case_id"]: c["title"] or "" for c in cases}
    default = ids.index(ss.eval_case) if ss.get("eval_case") in ids else len(ids) - 1
    c1, c2 = st.columns([3, 1])
    cid = c1.selectbox("Mock return", ids, index=default, key="eval_case",
                       format_func=lambda c: f"{c} · {title[c]}")
    c2.toggle("Test run", key="test_run",
              help="Try a key or sample returns without saving anything: no scores, mistakes, integrity data or "
                   "run history. Reports are stamped TEST RUN.")
    if ss.get("eval_case_loaded") != cid:
        kb = ctx.db.case_key_bytes(cid)
        ss.key, ss.key_bytes, ss.eval_case_loaded = parse_key(kb), kb, cid
        ss.last, ss.bulk_out = None, None
    return ss.key, ss.key_bytes


def _case_summary(k):
    wt = {}
    for s_ in k.steps:
        c_ = wt.setdefault(s_["Category"], [0, 0.0])
        c_[0] += 1
        c_[1] += s_["Marks"]
    with st.expander(f"{k.title} · tax year {k.tax_year} · {len(k.steps)} items · scored out of 100"):
        st.dataframe(pd.DataFrame([{"Category": c_, "Items": n_, "Marks": m_,
                                    "Weight /100": round(m_ / k.total * 100, 1) if k.total else 0}
                                   for c_, (n_, m_) in wt.items()]),
                     hide_index=True, use_container_width=True,
                     column_config={"Weight /100": st.column_config.ProgressColumn(
                         "Weight /100", format="%.1f", min_value=0, max_value=100)})


# ------------------------------------------------------------------ marking
def evaluate_files(files, sid, sname, key):
    """Read and grade one student's return; returns rows, metrics and the PDF report.
    Raises ReaderError with a message the user can act on."""
    ret = read_return(files)
    rows = flag(grade(ret, key))
    m = return_metrics(rows)
    cats, total, out_of = summarise(rows)
    label = files[0][0] if len(files) == 1 else f"{len(files)} files"
    report = build_report(sname, sid, key, rows, cats, total, out_of, label, ctx.institute, test_run=ctx.test)
    try:
        fp = fingerprints(files, ret)
    except Exception:              # integrity data must never stop a return from being marked
        log.exception("fingerprinting failed for %s", sid)
        fp = None
    return {"fp": fp, "rows": rows, "cats": cats, "m": m, "report": report, "pages": ret.page_map,
            "forms": ret.forms_found(), "format": ret.format_name, "notes": list(ret.notes),
            "unlinked": ret.unlinked(), "file_label": ", ".join(n for n, _ in files)[:500]}


def save(sid, case_id, res, on_time=None):
    if ctx.save:
        ctx.db.save_evaluation(sid, case_id, res["file_label"], res["m"], res["rows"], key_hash(ss.key_bytes),
                               ctx.staff or "", source_format=res["format"], notes=" | ".join(res["notes"]),
                               fingerprints=res.get("fp"), on_time=on_time)


def record_run(kind, case_id, started, uploads, rows, options=None):
    """Keep a record of an evaluation run: what was uploaded and, for every file or student, whether it was
    evaluated, skipped or failed and why. Not kept in a test run."""
    if not ctx.save:
        return
    try:
        st_ = [str(r.get("Status") or "") for r in rows]
        clean_rows = [{k_: (None if isinstance(v, float) and pd.isna(v) else v) for k_, v in r.items()} for r in rows]
        ctx.db.save_run({"run_id": started.strftime("%Y%m%d%H%M%S") + "-" + os.urandom(3).hex(),
                         "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
                         "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                         "kind": kind, "case_id": case_id, "run_by": ctx.staff or "", "files": len(uploads),
                         "evaluated": sum(x.startswith("Evaluated") for x in st_),
                         "skipped": sum(x.startswith("Skipped") for x in st_),
                         "failed": sum(x.startswith("Failed") for x in st_),
                         "warnings": sum(("CHECK" in x) or ("check" in x) for x in st_),
                         "detail": {"uploads": uploads, "rows": clean_rows, "options": options or {}}})
    except Exception:
        log.exception("could not record the evaluation run")


def _needs_check(res):
    return bool(res["m"]["needs_review"] or res["notes"] or res["unlinked"])


# ------------------------------------------------------------------ one student
def _guess_id(pdfs):
    segs = pdfs[0].name.replace("\\", "/").split("/")
    for part in list(reversed(segs[:-1])) + segs[-1:]:      # the student's folder first, then the file
        mt = re.match(r"^\s*([A-Za-z0-9-]*\d[A-Za-z0-9-]*)\s*_", part)
        if mt and not re.fullmatch(r"F\d{4}[A-Z]*", mt.group(1).upper()):   # not 'f1040_2025.pdf'
            return mt.group(1).upper()
    return ""


def show_result(r):
    rows, cats, m = r["rows"], r["cats"], r["m"]
    st.subheader(f"{r['Student Name']} · {r['Student ID']}")
    c = st.columns([1, 1, 1, 1.4])
    c[0].metric("Score / 100", f"{round(m['pct'] * 100, 1):g}")
    c[1].metric("Marks", f"{m['score']:g} / {m['out_of']:g}")
    c[2].metric("Errors", m["errors"])
    c[3].download_button("Download report", r["report"], type="primary", icon=":material/download:",
                         file_name=report_name(r["Student ID"], r["Case"]), mime="application/pdf",
                         use_container_width=True)
    if m["needs_review"]:
        st.warning(f"{m['needs_review']} item(s) could not be read and were marked 0. Check *What the system read* "
                   "before sharing the report.")
    for n in r.get("notes", []):
        st.caption(f":material/info: {n}")
    t1, t2, t3 = st.tabs(["Where marks were lost", "By category", "What the system read"])
    with t1:
        wrong = [x for x in rows if x["Result"] != "Correct"]
        if not wrong:
            st.success("No mistakes found.")
        else:
            blank = lambda v: "(blank)" if str(v).strip() == "" else fmt(v)
            st.dataframe(pd.DataFrame([{
                "Form": x["Form"], "Line": str(x["Line"]), "What was checked": x["Description"],
                "Student": blank(x["Student Value"]), "Correct": blank(x["Correct Value"]),
                "Marks lost": x["Marks"] - x["Awarded"],
                "Feedback": (x["Feedback"] + (f" [{x['Note']}]" if x["Note"] else "")).strip()} for x in wrong]),
                hide_index=True, use_container_width=True,
                column_config={"Feedback": st.column_config.TextColumn(width="large")})
    with t2:
        st.dataframe(pd.DataFrame([{"Category": x["Category"], "Marks": x["Awarded"], "Out of": x["Marks"],
                                    "Score": round(x["Pct"] * 100) if x["Marks"] else None,
                                    "Mistakes": x["Mistakes"]} for x in cats]),
                     hide_index=True, use_container_width=True,
                     column_config={"Score": st.column_config.ProgressColumn("Score", format="%d%%", min_value=0,
                                                                             max_value=100)})
    with t3:
        st.write("**Forms recognised:** " + ", ".join(r["forms"]))
        st.dataframe(pd.DataFrame(r["pages"], columns=["PDF page", "Recognised as"]), hide_index=True)
        if r.get("unlinked"):
            st.write("**Filled-in boxes not linked to a line** (not marked; use `field: <name>` in the key):")
            st.dataframe(pd.DataFrame([{"Form": x["form"], "Key line": x["line"], "Value": fmt(x["value"]),
                                        "Box": x["desc"]} for x in r["unlinked"]]), hide_index=True)


def one_student(k):
    n = ss.uploader_n
    pick = st.segmented_control("Upload", ["PDF files", "A folder"], default="PDF files", key="one_pick",
                                label_visibility="collapsed") or "PDF files"
    pdfs = st.file_uploader("The student's filled-in IRS forms (one PDF per form is fine)" if pick == "PDF files"
                            else "The student's folder", type=["pdf"],
                            accept_multiple_files=True if pick == "PDF files" else "directory",
                            key=f"ret{n}_{pick[0]}")
    guess = _guess_id(pdfs) if pdfs else ""
    c1, c2, c3 = st.columns([1, 2, 1.3])
    sid = c1.text_input("Student ID", value=guess, key=f"sid{n}").strip().upper()
    known = ctx.db.student(sid) if (ctx.db and sid) else None
    sname = c2.text_input("Name", value=(known or {}).get("name") or "", key=f"sn{n}_{sid}")
    sub_one = c3.segmented_control("Submitted", SUBMITTED, default="On time", key=f"subm{n}") or "On time"
    if sid and not re.fullmatch(r"[A-Z0-9-]{1,40}", sid):
        st.error("Student ID may only contain letters, digits and '-' (up to 40 characters).")
        sid = ""
    if ctx.save and sid and not known:
        st.caption(f":material/info: {sid} is not on the roster. The return is saved and tracked under "
                   "Learners → Not on roster; it counts in batch results once the learner is added.")
    if st.button("Evaluate", type="primary", disabled=not pdfs or not sid, icon=":material/play_arrow:"):
        with st.spinner("Reading the return and marking it..."):
            files = [(f.name, f.getvalue()) for f in pdfs]
            started = datetime.now(timezone.utc)
            one_row = {"File": ", ".join(n_ for n_, _ in files)[:300], "Student ID": sid, "Name": sname or "",
                       "Batch": (known or {}).get("batch") or "", "Submission": sub_one}
            one_up = [(n_, len(d)) for n_, d in files]
            try:
                res = evaluate_files(files, sid, sname, k)
            except ReaderError as e:
                record_run("Single", k.case_id, started, one_up, [dict(one_row, Status=f"Failed - {e}")])
                st.error(str(e))
                return
            except Exception as e:
                log.exception("evaluation failed for %s", sid)
                record_run("Single", k.case_id, started, one_up, [dict(
                    one_row, Status=f"Failed - unexpected error ({type(e).__name__}); details logged")])
                st.error("Something went wrong while reading this return. The details were logged.")
                return
            saved = False
            try:
                save(sid, k.case_id, res, on_time=sub_one == "On time")
                saved = True
            except Exception:
                log.exception("saving failed for %s", sid)
                st.error("Marked but NOT saved to the database. Download the report, then try again later.")
            m1 = res["m"]
            record_run("Single", k.case_id, started, one_up, [dict(
                one_row, **{"Score": m1["score"], "Out of": m1["out_of"], "Score /100": round(m1["pct"] * 100, 1),
                            "Errors": m1["errors"],
                            "Status": ("Evaluated" if saved else "Failed - marked but NOT saved")
                            + (" - check 'What the system read'" if _needs_check(res) else ""),
                            "Notes": " | ".join(res["notes"])[:300]})])
            ss.last = {"Student ID": sid, "Student Name": sname or "–", "Case": k.case_id, **res}
        st.toast("Test run – not saved" if ctx.test else "Saved" if (ctx.db and saved) else "Marked",
                 icon=":material/check:")
    r = ss.get("last")
    if r and r["Case"] == k.case_id:
        st.divider()
        show_result(r)
        if st.button("Next student", icon=":material/arrow_forward:"):
            ss.uploader_n += 1
            ss.last = None
            st.rerun()


# ------------------------------------------------------------------ a batch
def a_batch(k):
    n = ss.uploader_n
    st.caption(f"Upload the batch folder, a zip of it, or many PDFs. Each student's folder or file should be named "
               f"`StudentID_{k.case_id}`; near-misses are still matched and marked **CHECK NAME**.")
    pick = st.segmented_control("Upload", ["Files or zips", "A folder"], default="A folder", key="bulk_pick",
                                label_visibility="collapsed") or "A folder"
    files = st.file_uploader("Returns (PDFs or zips)" if pick != "A folder" else "The batch folder",
                             type=["pdf", "zip"], accept_multiple_files=True if pick != "A folder" else "directory",
                             key=f"bulk{n}_{pick[0]}")
    c1, c2 = st.columns([1, 2])
    bulk_sub = c1.segmented_control("Batch submitted", SUBMITTED, default="On time", key="bulk_sub") or "On time"
    other = "Late" if bulk_sub == "On time" else "On time"
    exc_txt = c2.text_input(f"Except these IDs ({other.lower()})", key=f"bulk_exc{n}", placeholder="164001, 164002")
    exceptions = {x.strip().upper() for x in re.split(r"[,;\s]+", exc_txt or "") if x.strip()}
    with st.expander("Options"):
        skip_done = st.checkbox("Skip students already evaluated for this case", value=ctx.save,
                                disabled=not ctx.save, help="Restart an interrupted upload without marking twice.")
        make_reports = st.checkbox("Create a PDF report for every student", value=True)
    if st.button("Evaluate all", type="primary", disabled=not files, icon=":material/play_arrow:"):
        started = datetime.now(timezone.utc)
        roster = data.roster if ctx.db else {}
        groups, skipped = group_uploads([(f.name, f.getvalue()) for f in files], case_id=k.case_id,
                                        roster_ids={i: (r_.get("name") or "") for i, r_ in roster.items()} or None)
        done = ctx.db.evaluated(k.case_id) if (ctx.db and skip_done) else set()
        out, zbuf = [], io.BytesIO()
        for name, why in skipped:
            out.append({"File": name, "Student ID": "", "Name": "", "Batch": "", "Score": None, "Out of": None,
                        "Score /100": None, "Errors": None,
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
                        save(sid, k.case_id, res, on_time=sub_status == "On time")
                        m = res["m"]
                        row.update({"Score": m["score"], "Out of": m["out_of"], "Score /100": round(m["pct"] * 100, 1),
                                    "Errors": m["errors"],
                                    "Status": "Evaluated" + (" - check 'What the system read'" if _needs_check(res)
                                                             else "") + (f" - CHECK NAME: {naming}" if naming else ""),
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
        bar.empty()
        record_run("Bulk", k.case_id, started, [(f.name, f.size) for f in files], out,
                   {"Batch submitted": bulk_sub, "Exceptions": ", ".join(sorted(exceptions)),
                    "Skip already evaluated": skip_done, "PDF reports made": make_reports})
        ss.bulk_out = {"case": k.case_id, "table": pd.DataFrame(out),
                       "zip": zbuf.getvalue() if make_reports else None, "when": stamp("%Y%m%d_%H%M"),
                       "test": ctx.test}
    _bulk_result(k)


def _bulk_result(k):
    bo = ss.get("bulk_out")
    if not bo or bo["case"] != k.case_id:
        return
    st.divider()
    df = bo["table"]
    sv = df["Status"].astype(str)
    n_ok, n_skip, n_fail = (int(sv.str.startswith(x).sum()) for x in ("Evaluated", "Skipped", "Failed"))
    n_check = int(sv.str.contains("CHECK|check", regex=True).sum())
    c = st.columns(4)
    c[0].metric("Evaluated", n_ok)
    c[1].metric("Need a check", n_check, help="CHECK NAME or 'check What the system read'")
    c[2].metric("Failed", n_fail)
    c[3].metric("Skipped", n_skip)
    if bo.get("test"):
        st.caption(":material/science: Test run – nothing was saved. Download what you need now.")
    if ctx.db:
        roster_now = set(data.roster)
        df = add_roster_column(df, roster_now)
        off = off_roster_ids(df["Student ID"], roster_now)
        if off:
            st.markdown(callout_html(off, "student(s) in this upload are"), unsafe_allow_html=True)
    show = st.segmented_control("Show", ["Everything", "Need a check", "Failed", "Skipped"], default="Everything",
                                key="bulk_show") or "Everything"
    view = df if show == "Everything" else df[
        sv.str.contains("CHECK|check", regex=True) if show == "Need a check" else
        sv.str.startswith("Failed" if show == "Failed" else "Skipped")]
    st.dataframe(style_off_roster(view), hide_index=True, use_container_width=True,
                 column_config={"Status": st.column_config.TextColumn(width="large")})
    d1, d2, _ = st.columns([1, 1, 2])
    pre = "TEST_" if bo.get("test") else ""
    if bo["zip"] and n_ok:
        d1.download_button("All reports (zip)", bo["zip"], icon=":material/download:", use_container_width=True,
                           file_name=f"{pre}Evaluation_reports_{k.case_id}_{bo['when']}.zip")
    d2.download_button("Results (Excel)", excel_bytes([("Results", df)]), icon=":material/download:",
                       use_container_width=True, file_name=f"{pre}Bulk_results_{k.case_id}_{bo['when']}.xlsx")


# ------------------------------------------------------------------ page
def page():
    for k_, v_ in [("uploader_n", 0), ("last", None), ("bulk_out", None)]:
        ss.setdefault(k_, v_)
    header("Evaluate", "Mark one student's return, or a whole batch at once.")
    k, kb = _choose_case()
    if not k:
        return
    if ctx.test:
        st.info("Test run – returns are marked and reports made, but nothing is saved.", icon=":material/science:")
    _case_summary(k)
    mode = st.segmented_control("How many", ["One student", "A batch"], default="One student", key="eval_mode",
                                label_visibility="collapsed") or "One student"
    if mode == "One student":
        one_student(k)
    else:
        a_batch(k)
