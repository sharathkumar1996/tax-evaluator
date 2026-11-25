"""Setup: everything you prepare before or during the course - roster, mock returns (answer keys), class
attendance, MCQ marks and settings. Collects what used to be spread over six tabs and the sidebar."""
import io
import json

import pandas as pd
import streamlit as st

from finalscore import LABELS, MCQError, check_settings, parse_mcq, per_item
from grader_core import KeyError_
from integrity import TRAINER, fingerprints
from irs_reader import ReaderError
from make_keys import draft_from_return
from readers import read_return
from roster_flag import callout_html
from ui.core import (HERE_TEMPLATE, admin_ok, clean, ctx, data, header, key_hash, log, metrics, parse_key, read_table,
                     ss, staff_name)


# ------------------------------------------------------------------ roster
def _roster():
    st.caption("Columns: **Student ID**, **Student Name**, **Batch** and **Email** (the learner's Deloitte email, used "
               "to match class attendance). Uploading again adds new learners and updates existing ones.")
    tmpl = io.BytesIO()
    pd.DataFrame({"Student ID": ["S0001"], "Student Name": ["Example Name"], "Batch": ["Batch-1"],
                  "Email": ["example.ext@deloitte.com"]}).to_excel(tmpl, index=False)
    c1, c2 = st.columns([3, 1])
    up = c1.file_uploader("Roster file", type=["xlsx", "csv"], key="roster", label_visibility="collapsed")
    c2.download_button("Template", tmpl.getvalue(), file_name="student_roster_template.xlsx",
                       icon=":material/download:", use_container_width=True)
    if up is not None:
        try:
            rdf = read_table(up)
            cols = {c.lower().strip(): c for c in rdf.columns}
            cid_ = cols.get("student id") or cols.get("studentid") or cols.get("id")
            cnm = cols.get("student name") or cols.get("name")
            cbt = cols.get("batch")
            cem = next((cols[k_] for k_ in ("email", "email id", "deloitte email id", "deloitte email", "mail id",
                                            "e-mail") if k_ in cols), None)
            if not cid_:
                st.error("No 'Student ID' column found.")
                return
            recs = []
            for _, r in rdf.iterrows():
                sid_ = str(r[cid_]).strip().upper()
                if sid_ and sid_ != "NAN":
                    em_ = str(r[cem]).strip().lower() if cem and pd.notna(r[cem]) else ""
                    recs.append((sid_, str(r[cnm]).strip() if cnm and pd.notna(r[cnm]) else "",
                                 str(r[cbt]).strip() if cbt and pd.notna(r[cbt]) else "Unassigned",
                                 em_ if "@" in em_ else ""))
            st.write(f"**{len(recs)}** learners in the file" + (f", {sum(1 for x in recs if x[3])} with an email."
                                                               if cem else " – no Email column."))
            st.dataframe(pd.DataFrame(recs[:20], columns=["Student ID", "Name", "Batch", "Email"]), hide_index=True)
            saved = st.button("Save roster", type="primary")
            if saved:
                ctx.db.upsert_students(recs)
        except Exception as e:
            st.error(f"Could not read the roster: {e}")
            saved = False
        if saved:
            st.toast(f"Saved {len(recs)} learners")
            st.rerun()
    studs = pd.DataFrame(data.students)
    if not studs.empty:
        st.markdown("##### On the roster")
        per_b = studs.fillna({"batch": "Unassigned", "email": ""}).groupby("batch").agg(
            Learners=("student_id", "size"), **{"No email": ("email", lambda s: int((s == "").sum()))}).reset_index()
        st.dataframe(per_b.rename(columns={"batch": "Batch"}), hide_index=True)
        with st.expander(f"All learners ({len(studs)})"):
            st.dataframe(studs.rename(columns={"student_id": "Student ID", "name": "Name", "batch": "Batch",
                                               "email": "Email"}), hide_index=True, use_container_width=True)


# ------------------------------------------------------------------ mock returns
def _use_key(data_, name):
    """Without a database the key is kept for this session only (Evaluate reads it)."""
    ss.key, ss.key_bytes, ss.eval_case_loaded = parse_key(data_), data_, None


def _cases():
    if ctx.db and data.cases:
        counts = {c: 0 for c in data.case_info}
        for e in data.evaluations:
            counts[e["case_id"]] = counts.get(e["case_id"], 0) + 1
        st.dataframe(pd.DataFrame([{"Mock return": c["case_id"], "Title": c["title"] or "",
                                    "Counts in final score": c["case_id"] in data.final_cases,
                                    "Evaluated": counts.get(c["case_id"], 0)} for c in data.cases]),
                     hide_index=True, use_container_width=True)
    st.markdown("##### Add or update an answer key")
    c1, c2 = st.columns([3, 1])
    key_file = c1.file_uploader("Answer key (.xlsx)", type=["xlsx"], key="keyfile", label_visibility="collapsed")
    if HERE_TEMPLATE.exists():
        c2.download_button("Blank key", HERE_TEMPLATE.read_bytes(), file_name="answer_key_template.xlsx",
                           icon=":material/download:", use_container_width=True)
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
            st.write(f"**{k_new.case_id}** · {k_new.title} · {len(k_new.steps)} items · {k_new.total:g} marks")
            if not ctx.db:
                _use_key(key_file.getvalue(), key_file.name)
                st.success("Ready – go to Evaluate. (No database: the key lasts for this session.)")
                ss.prev_keyfile = (key_file.name, key_file.size)
            else:
                n_done = ctx.db.case_eval_count(k_new.case_id) if k_new.case_id in data.case_info else 0
                changed = n_done and data.case_info[k_new.case_id]["key_hash"] != key_hash(key_file.getvalue())
                if changed:
                    st.warning(f"{k_new.case_id} already has {n_done} evaluated returns. They keep their scores; "
                               "re-evaluate them if the key's answers changed.")
                if st.button(f"Replace the key for {k_new.case_id}" if changed else f"Save {k_new.case_id}",
                             type="primary"):
                    ctx.db.save_case(k_new, key_file.getvalue(), key_file.name)
                    ss.prev_keyfile = (key_file.name, key_file.size)
                    ss.eval_case_loaded = None
                    st.toast(f"{k_new.case_id} saved")
                    st.rerun()

    with st.expander("No key yet? Build one from the trainer's correct return"):
        st.caption("Every filled-in item becomes a line in a draft key. Set marks, categories and feedback in Excel, "
                   "then upload it above. The trainer's file is also remembered for the integrity checks.")
        good = st.file_uploader("Correct return (PDF files)", type=["pdf"], accept_multiple_files=True, key="goodret")
        b1, b2, b3 = st.columns([1, 2, 1])
        cid = b1.text_input("Case ID", "C01")
        ctitle = b2.text_input("Case title", "")
        cyear = b3.number_input("Tax year", 2015, 2035, 2025)
        b4, b5 = st.columns(2)
        dmarks = b4.number_input("Default marks per item", 0.0, 100.0, 1.0, 0.5)
        dunx = b5.number_input("Marks deducted per unexpected entry", 0.0, 100.0, 1.0, 0.5)
        if st.button("Build draft key", disabled=not good):
            try:
                gfiles = [(f.name, f.getvalue()) for f in good]
                out, n, forms = draft_from_return(gfiles, cid, ctitle, int(cyear), dmarks, dunx)
                if ctx.db and n:
                    try:
                        ctx.db.save_fingerprints(TRAINER, str(cid).strip(), fingerprints(gfiles, read_return(gfiles)))
                    except Exception:
                        log.exception("saving trainer fingerprints failed")
            except ReaderError as e:
                st.error(str(e))
            except Exception:
                log.exception("building a key failed")
                st.error("Could not read this return. The details were logged.")
            else:
                if n == 0:
                    st.error("No filled-in items found. Upload the saved fillable forms, not a printed PDF.")
                else:
                    st.success(f"{n} items found on: {', '.join(forms)}")
                    st.download_button("Download draft key (.xlsx)", out, file_name=f"answer_key_{clean(cid)}.xlsx",
                                       icon=":material/download:")


# ------------------------------------------------------------------ attendance
def _attendance():
    from attendance import (ABSENT, BELOW, PRESENT, AttendanceError, alias_key, match_rows, read_reports,
                            session_records)
    roster_all = data.students
    if not roster_all:
        st.info("Add the roster first (with the Email column, so attendance can be matched).")
        return
    st.caption("Upload Rajashekhar's daily report (*Deloitte-Form1040-Attendance-80pct-<date>.xlsx*), one or more days "
               "at once. 80%+ of the session = Present; left early = Below 80%; on the roster but not in the report = "
               "Absent. Uploading a date again replaces it.")
    aliases = data.setting_json("attendance_aliases")
    files = st.file_uploader("Attendance reports", type=["xlsx", "csv"], accept_multiple_files=True,
                             key=f"att_files{ss.get('att_n', 0)}", label_visibility="collapsed")
    done_dates = {r["session_date"] for r in ctx.db.attendance_dates()}
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
    if len(parsed) > 1 and st.button(f"Save all {len(parsed)} dates", type="primary", key="att_save_all"):
        n_all = 0
        for _, d0, rows in parsed:
            recs_, _ = session_records(d0.isoformat(), match_rows(rows, roster_all, aliases), roster_all)
            n_all += ctx.db.save_attendance(d0.isoformat(), recs_, staff_name())
        st.toast(f"Saved {n_all} records for {len(parsed)} dates")
    for label, d0, rows in parsed:
        with st.expander(label, expanded=len(parsed) == 1):
            d = st.date_input("Class date", value=d0, key=f"att_d_{label}")
            ds = d.isoformat()
            recs, unmatched = session_records(ds, match_rows(rows, roster_all, aliases), roster_all)
            cnt = pd.Series([r["status"] for r in recs]).value_counts()
            metrics([("In the report", len(rows)), (PRESENT, int(cnt.get(PRESENT, 0))),
                     (BELOW, int(cnt.get(BELOW, 0))), (ABSENT, int(cnt.get(ABSENT, 0))),
                     ("Not matched", len(unmatched))])
            if ds in done_dates:
                st.caption(f"{d:%d %b %Y} is already saved – saving replaces it for the batches in this file.")
            links = {}
            if unmatched:
                st.caption("Link anyone who is a learner (the link is remembered); leave the rest blank.")
                um = pd.DataFrame([{"Batch": r["batch"], "Name in report": r["name"], "Email": r["email"],
                                    "Status": r["status"], "Link to roster learner": ""} for r in unmatched])
                ed = st.data_editor(um, hide_index=True, use_container_width=True, key=f"att_um_{label}",
                                    disabled=["Batch", "Name in report", "Email", "Status"],
                                    column_config={"Link to roster learner": st.column_config.SelectboxColumn(
                                        options=id_opts, width="large")})
                for (_, e_), r in zip(ed.iterrows(), unmatched):
                    if e_["Link to roster learner"]:
                        links[alias_key(r)] = e_["Link to roster learner"].split(" - ")[0]
            if st.button(f"Save {d:%d %b %Y}", type="primary", key=f"att_save_{label}"):
                if links:
                    aliases.update(links)
                    ctx.db.save_settings({"attendance_aliases": json.dumps(aliases)})
                    recs, _ = session_records(ds, match_rows(rows, roster_all, aliases), roster_all)
                n = ctx.db.save_attendance(ds, recs, staff_name())
                st.toast(f"Saved {n} records for {d:%d %b %Y}")
    saved = ctx.db.attendance_dates()
    if saved:
        st.markdown("##### Class dates saved")
        st.dataframe(pd.DataFrame(saved).rename(columns={"session_date": "Date", "n": "Records",
                                                         "uploaded_at": "Uploaded", "uploaded_by": "By"}),
                     hide_index=True, use_container_width=True)
        with st.popover("Remove a class date", icon=":material/delete:"):
            rm = st.selectbox("Date", [r["session_date"] for r in saved], key="att_rm")
            if st.button(f"Remove {rm}", type="primary", key="att_rm_btn"):
                ctx.db.erase_attendance(rm)
                st.toast(f"{rm} removed")
                st.rerun()


# ------------------------------------------------------------------ MCQ marks
def _mcq():
    cfg = data.cfg
    st.caption(f"A **Student ID** column and one column per test, **Test 1** … **Test {cfg['n_mcq']:g}** (out of "
               f"{cfg['mcq_out_of']:g}). Blank cells are left as they are; uploading a mark again replaces it.")
    c1, c2 = st.columns([3, 1])
    up = c1.file_uploader("MCQ marks", type=["xlsx", "csv"], key="mcq_file", label_visibility="collapsed")
    tno = c2.selectbox("Single 'Marks' column is test", list(range(1, int(cfg["n_mcq"]) + 1)), key="mcq_tno")
    if up is not None:
        try:
            recs = parse_mcq(read_table(up) if up.name.lower().endswith(".csv") else
                             pd.read_excel(io.BytesIO(up.getvalue())), cfg, test_no=tno)
        except MCQError as e:
            st.error(str(e))
            recs = []
        except Exception:
            log.exception("reading MCQ file failed")
            st.error("This file could not be read. Save it as .xlsx or .csv and try again.")
            recs = []
        if recs:
            unknown = sorted({sid for sid, _, _ in recs} - set(data.roster))
            st.write(f"**{len(recs)}** marks for **{len({r[0] for r in recs})}** learners, test(s) "
                     f"{', '.join(map(str, sorted({t for _, t, _ in recs})))}.")
            if unknown:
                st.markdown(callout_html(unknown, "student ID(s) in this sheet are",
                                         "Their marks are saved and count once they are on the roster."),
                            unsafe_allow_html=True)
            if st.button("Save MCQ marks", type="primary"):
                ctx.db.save_mcq(recs, staff_name())
                st.toast(f"{len(recs)} marks saved")
                st.rerun()
    if data.mcq:
        wide = (pd.DataFrame(data.mcq).pivot_table(index="student_id", columns="test_no", values="marks")
                .rename(columns=lambda t: f"Test {t}").reset_index().rename(columns={"student_id": "Student ID"}))
        names = {x["student_id"]: (x["name"] or "", x["batch"] or "") for x in data.students}
        wide.insert(1, "Name", wide["Student ID"].map(lambda x: names.get(x, ("", ""))[0]))
        wide.insert(2, "Batch", wide["Student ID"].map(lambda x: names.get(x, ("", ""))[1]))
        st.dataframe(wide, hide_index=True, use_container_width=True)


# ------------------------------------------------------------------ settings
def _settings():
    cfg = data.cfg
    pa, po, pt = per_item(cfg)
    st.markdown("##### Final course score")
    st.caption(f"Accuracy {cfg['w_accuracy']:g} + on time {cfg['w_on_time']:g} + MCQ {cfg['w_mcq']:g} = 100. Each "
               f"mock return is worth {pa:.2f} accuracy and {po:.2f} on-time marks; each MCQ test {pt:.2f}. "
               f"Pass mark {cfg['pass_mark']:g}.")
    if not admin_ok():
        st.caption(":material/lock: Changing the scoring rules needs the admin password (Admin page).")
    with st.form("cfg_form"):
        vals, cols = {}, st.columns(3)
        for i, (k_, lab_) in enumerate(LABELS.items()):
            vals[k_] = cols[i % 3].number_input(lab_, value=float(cfg[k_]), min_value=0.0, step=1.0,
                                                disabled=not admin_ok(), key=f"cfg_{k_}")
        pick = st.multiselect("Mock returns that count in the final score", list(data.case_info),
                              default=[c for c in data.final_cases if c in data.case_info], disabled=not admin_ok(),
                              format_func=lambda c: f"{c} · {data.case_info[c]['title'] or ''}")
        if st.form_submit_button("Save scoring", disabled=not admin_ok()):
            newcfg = {k_: (int(v) if float(v).is_integer() else v) for k_, v in vals.items()}
            errs = check_settings(newcfg)
            if errs:
                st.error(" ".join(errs))
            else:
                ctx.db.save_settings(newcfg)
                ctx.db.set_final_cases(pick)
                st.toast("Scoring saved")
                st.rerun()

    st.markdown("##### Flags")
    with st.form("flags_form"):
        r1, r2 = st.columns(2)
        new_min = r1.number_input("Low class attendance below (% of classes at 80%+)", 0.0, 100.0, data.min_att, 5.0)
        new_low = r2.number_input("Not performing well below (% of marks possible so far)", 0.0, 100.0,
                                  data.low_perf, 5.0)
        if st.form_submit_button("Save flags"):
            ctx.db.save_settings({"att_min_pct": str(new_min), "low_perf_pct": str(new_low)})
            st.toast("Saved")
            st.rerun()

    st.markdown("##### Trainers")
    tr = data.trainers
    tdf = pd.DataFrame([{"Batch": b, "Trainer": tr.get(b, {}).get("trainer", ""),
                         "Email": tr.get(b, {}).get("email", "")} for b in data.batches])
    ed = st.data_editor(tdf, hide_index=True, use_container_width=True, disabled=["Batch"], key="set_trainers")
    if st.button("Save trainers"):
        new = {r["Batch"]: {"trainer": str(r["Trainer"] or "").strip(), "email": str(r["Email"] or "").strip()}
               for _, r in ed.iterrows()}
        ctx.db.save_settings({"batch_trainers": json.dumps(new)})
        st.toast("Trainers saved")
        st.rerun()

    st.markdown("##### Reports")
    with st.form("inst_form"):
        inst = st.text_input("Institute name on reports", ctx.institute)
        if st.form_submit_button("Save"):
            ctx.db.save_settings({"institute": inst.strip()})
            st.toast("Saved")
            st.rerun()


def page():
    header("Setup", "Roster, mock returns, attendance, MCQ marks and settings.")
    if not ctx.db:
        st.info("Without a database only an answer key can be loaded, for this session.")
        _cases()
        return
    sec = st.segmented_control("Section", ["Roster", "Mock returns", "Attendance", "MCQ marks", "Settings"],
                               default="Mock returns", key="set_sec", label_visibility="collapsed") or "Mock returns"
    st.write("")
    {"Roster": _roster, "Mock returns": _cases, "Attendance": _attendance, "MCQ marks": _mcq,
     "Settings": _settings}[sec]()
