"""Reports: every file that leaves the app, in one place, built from the sidebar filters and only when asked.
Replaces the download buttons spread over Management, Batch reports, Attendance and Integrity."""
import io
import zipfile

import pandas as pd
import streamlit as st

from batchreport import ALL, batch_summary, improper_submissions, integrity_for, missing_returns, mock_table, \
    top_mistakes
from finalscore import FINAL, SO_FAR, final_scores
from ui.core import (clean, course, ctx, data, global_filters, header, integrity, needs_db, outsiders, prepared,
                     ss, stamp)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _trainers(c):
    from batch_pdf import batch_report_excel, batch_report_pdf
    from integrity_report import CAUTION
    sel_b = sorted({x["batch"] for x in c["studs"]})
    tr = data.trainers

    def build(b):
        integ = integrity(tuple(c["fc"]))
        summary = batch_summary(c["studs"], c["evs"], data.mcq, c["fc"], data.cfg,
                                integ["students"] if integ else None, tr)
        all_row = summary.set_index("Batch").loc[ALL]
        bs = [x for x in c["studs"] if x["batch"] == b]
        sc = final_scores(bs, c["evs"], data.mcq, c["fc"], data.cfg, view=SO_FAR)
        imp = improper_submissions(c["studs"], c["evs"], c["runs"], b, case_ids=c["fc"])
        args = (b, tr.get(b, {}).get("trainer", ""), summary.set_index("Batch").loc[b].to_dict(), sc,
                mock_table(c["studs"], c["evs"], c["fc"], data.case_info, b),
                missing_returns(c["studs"], c["evs"], b, improper=imp), top_mistakes(data.mistakes, c["evs"], b),
                integrity_for(integ, b))
        return (batch_report_pdf(*args, data.cfg, ctx.institute, cohort_avg=all_row["Average score %"],
                                 integrity_ran=integ is not None, caution=CAUTION, improper=imp),
                batch_report_excel(*args, integrity_ran=integ is not None, improper=imp))

    fname = lambda b, ext: f"Batch_report_{clean(b)}_{clean(tr.get(b, {}).get('trainer') or 'trainer')}_{stamp()}.{ext}"

    def all_zip():
        zb = io.BytesIO()
        with zipfile.ZipFile(zb, "w", zipfile.ZIP_DEFLATED) as z:
            for b in sel_b:
                p_, x_ = build(b)
                z.writestr(fname(b, "pdf"), p_)
                z.writestr(fname(b, "xlsx"), x_)
        return zb.getvalue()

    prepared("trainers_all", f"Prepare all {len(sel_b)} batch reports", all_zip, f"Batch_reports_{stamp()}.zip")
    for b in sel_b:
        t = tr.get(b, {})
        c1, c2, c3, c4 = st.columns([2.2, 1, 0.7, 0.7])
        c1.markdown(f"**{b}** <span style='color:#888'>· {t.get('trainer') or 'trainer not set'}</span>",
                    unsafe_allow_html=True)
        if c2.button("Prepare", key=f"tr_prep_{b}", use_container_width=True):
            with st.spinner(f"Preparing {b}..."):
                ss[f"tr_rep_{b}"] = build(b)
        if ss.get(f"tr_rep_{b}"):
            p_, x_ = ss[f"tr_rep_{b}"]
            c3.download_button("PDF", p_, file_name=fname(b, "pdf"), key=f"tr_pdf_{b}", use_container_width=True)
            c4.download_button("Excel", x_, file_name=fname(b, "xlsx"), key=f"tr_xls_{b}", use_container_width=True)
    st.caption("Trainer names are set under Setup → Settings.")


def _deloitte(c):
    from attendance import class_summary, deloitte_workbook
    from deloitte_pdf import deloitte_report_pdf, programme_data
    track = c["track"]
    if track.empty:
        st.info("No learners for these filters.")
        return
    classes = lambda: class_summary(data.attendance, c["studs"])

    def pdf():
        pdata = programme_data(track, c["studs"], c["evs"], data.mcq, c["fc"], data.case_info, data.cfg, classes(),
                               data.trainers)
        return deloitte_report_pdf(pdata, data.cfg, ctx.institute, min_att=data.min_att, low_perf=data.low_perf)

    st.markdown("**Programme progress report (PDF)** – the big picture for Deloitte management: headline numbers, "
                "readiness, progress per mock and class, batch comparison. No learner names, no integrity data.")
    prepared("dl_pdf", "Prepare progress report", pdf, f"Deloitte_Form1040_Progress_Report_{stamp()}.pdf",
             "application/pdf")
    st.markdown("**Learner report (Excel)** – every learner, one sheet per concern, class-wise attendance.")
    prepared("dl_xlsx", "Prepare learner report",
             lambda: deloitte_workbook(track, classes(), ctx.institute, min_att=data.min_att, low_perf=data.low_perf),
             f"Deloitte_Form1040_Learner_Report_{stamp()}.xlsx", XLSX)
    _not_on_roster()


def _not_on_roster():
    from offroster import CONSISTENT, LEVELS
    from roster_flag import excel_bytes
    df = outsiders()
    st.markdown(f"**Learners not on the roster (Excel)** – everyone whose returns were marked without being on the "
                f"roster, rated for consistency; {int((df['Consistency'] == CONSISTENT).sum())} of {len(df)} are "
                f"consistent. One sheet per rating.")
    if df.empty:
        st.caption("Nobody to list.")
        return
    prepared("dl_off", "Prepare list", lambda: excel_bytes(
        [("All", df)] + [(lv[:31].replace(",", ""), df[df["Consistency"] == lv]) for lv in LEVELS]),
        f"Deloitte_Learners_not_on_roster_{stamp()}.xlsx", XLSX)


def _management(c):
    from scorecard import results_workbook, scorecard_pdf
    view = FINAL if (st.segmented_control("View", ["Progress so far", "Final result"], default="Progress so far",
                                          key="rp_view", label_visibility="collapsed") == "Final result") else SO_FAR
    df = final_scores(c["studs"], c["evs"], data.mcq, c["fc"], data.cfg, view=view)

    def returns_of(sid):
        mine = {e["case_id"]: e for e in c["evs"] if e["student_id"] == sid}
        return [{"case_id": cid, "title": (data.case_info.get(cid) or {}).get("title") or "",
                 "pct": (mine.get(cid) or {}).get("pct"), "on_time": (mine.get(cid) or {}).get("on_time")}
                for cid in c["fc"]]

    def cards():
        zb = io.BytesIO()
        with zipfile.ZipFile(zb, "w", zipfile.ZIP_DEFLATED) as z:
            for rec in df.to_dict("records"):
                z.writestr(f"Scorecard_{clean(rec['Student ID'])}.pdf",
                           scorecard_pdf(rec, returns_of(rec["Student ID"]), data.cfg, ctx.institute, view))
        return zb.getvalue()

    tag = "Final_results" if view == FINAL else "Progress"
    st.markdown("**Results workbook (Excel)** – every learner's marks, with the on-time change log.")
    prepared(f"res_{view}", "Prepare results", lambda: results_workbook(df, data.cfg, ctx.db.status_history(), view),
             f"{tag}_{stamp()}.xlsx", XLSX)
    st.markdown(f"**Scorecards (PDF)** – one per learner ({len(df)}).")
    prepared(f"cards_{view}", "Prepare scorecards", cards, f"Scorecards_{stamp()}.zip")
    one = st.selectbox("One learner's scorecard", [""] + df["Student ID"].tolist(), key="rp_one",
                       format_func=lambda x: "Choose a learner" if not x else
                       f"{x} · {df.set_index('Student ID').loc[x, 'Name']}")
    if one:
        rec = dict(df.set_index("Student ID").loc[one].to_dict(), **{"Student ID": one})
        st.download_button(f"Scorecard – {one}", scorecard_pdf(rec, returns_of(one), data.cfg, ctx.institute, view),
                           file_name=f"Scorecard_{clean(one)}.pdf", icon=":material/download:")


def _integrity(c):
    from integrity_report import integrity_excel, integrity_pdf
    scope = f"Batches: {', '.join(sorted({x['batch'] for x in c['studs']}))} · Mock returns: {', '.join(c['fc'])}"
    st.markdown("**Integrity report** – copying and shared files across the chosen mock returns.")
    res = lambda: integrity(tuple(c["fc"]))
    prepared("int_pdf", "Prepare PDF", lambda: integrity_pdf(ctx.institute, scope, res()),
             f"Integrity_report_{stamp()}.pdf", "application/pdf")
    prepared("int_xlsx", "Prepare Excel", lambda: integrity_excel(scope, res()), f"Integrity_report_{stamp()}.xlsx",
             XLSX)


def page():
    header("Reports", "Every report in one place, for the batches and mock returns chosen in the sidebar.")
    if needs_db():
        return
    global_filters()
    c = course()
    if not c["studs"]:
        st.info("Add the roster under **Setup → Roster** first.")
        return
    who = st.segmented_control("For", ["Trainers", "Deloitte", "Management", "Integrity"], default="Trainers",
                               key="rp_for", label_visibility="collapsed") or "Trainers"
    st.write("")
    {"Trainers": _trainers, "Deloitte": _deloitte, "Management": _management, "Integrity": _integrity}[who](c)
