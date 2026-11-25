"""Learners: one row per learner (classes, mock returns, MCQs, marks so far, flags) and a profile for each.

Replaces the old Final results, Batch detail → Students, Learner tracker and Find a report screens."""
import hashlib

import pandas as pd
import streamlit as st

from finalscore import FINAL, final_scores
from grader_core import summarise
from metrics import rebuild_rows
from report_pdf import build_report
from ui.core import (admin_ok, course, ctx, data, excel_download, global_filters, header, metrics, needs_db,
                     outsiders, parse_key, pct, report_name, ss, stamp)

BASE_COLS = ["Student ID", "Name", "Batch", "Readiness", "Flags", "Attendance %", "Mocks evaluated", "Mocks due",
             "MCQ tests taken", "Marks so far", "% of possible so far"]


def _table(track, result):
    from attendance import FLAGS
    f1, f2 = st.columns([1, 2])
    q = f1.text_input("Search", placeholder="Name or ID", key="lr_q", label_visibility="collapsed")
    flags = f2.pills("Flags", FLAGS, selection_mode="multi", key="lr_flags", label_visibility="collapsed")
    v = track
    if flags:
        v = v[v["Flags"].apply(lambda x: any(f_ in x for f_ in flags))]
    if q.strip():
        v = v[v["Name"].str.contains(q, case=False, na=False) | v["Student ID"].str.contains(q, case=False, na=False)]
    v = v.sort_values(["Flag count", "Attendance %"], ascending=[False, True])
    cols = [c for c in BASE_COLS if c in v.columns]
    if result is not None:
        v = v.merge(result, on="Student ID", how="left")
        cols.append("Result")
    all_cols = st.toggle("All columns", key="lr_all")
    if all_cols:
        cols = cols + [c for c in v.columns if c not in cols and c not in ("Flag count",)]
    ev = st.dataframe(v[cols], hide_index=True, use_container_width=True, on_select="rerun",
                      selection_mode="single-row", key="lr_table",
                      column_config={"Attendance %": st.column_config.ProgressColumn(format="%.0f%%", min_value=0,
                                                                                     max_value=100),
                                     "Flags": st.column_config.TextColumn(width="medium")})
    st.caption(f"{len(v)} learner(s). Click a row to open the learner.")
    rows = ev.selection.rows if ev and ev.selection else []
    if rows:
        return v.iloc[rows[0]]["Student ID"]
    if len(v) == 1 and q.strip():
        return v.iloc[0]["Student ID"]
    return None


def _mocks_and_report(sid, improper):
    """Mock-by-mock table, report download and (admin) delete - shared by both kinds of learner."""
    mine = {e["case_id"]: e for e in data.evaluations if e["student_id"] == sid}
    imp = improper[improper["Student ID"] == sid] if not improper.empty else improper
    lab = {1: "On time", 0: "Late", None: "Not set"}
    rows = []
    for cid in data.final_cases:
        e = mine.get(cid)
        problem = "; ".join(imp.loc[imp["Case"] == cid, "Details"].astype(str)) if not imp.empty else ""
        rows.append({"Mock": cid, "Title": (data.case_info.get(cid) or {}).get("title") or "",
                     "Score /100": round(100 * (e["pct"] or 0), 1) if e else None,
                     "Errors": e["errors"] if e else None,
                     "Submission": lab.get(e.get("on_time")) if e else ("Improper" if problem else "Not received"),
                     "Evaluated (UTC)": (e or {}).get("evaluated_at") or "", "Problem": problem})
    st.markdown("##### Mock returns")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                 column_config={"Score /100": st.column_config.ProgressColumn(format="%.1f", min_value=0,
                                                                              max_value=100),
                                "Problem": st.column_config.TextColumn(width="large")})
    if not mine:
        return
    _report(sid, mine)


def _profile(sid, c):
    t = c["track"].set_index("Student ID").loc[sid]
    st.divider()
    st.subheader(f"{t['Name']} · {sid}")
    st.markdown(" ".join(f"<span class='pill'>{x}</span>" for x in
                         [t.get("Batch") or "", t.get("Readiness") or ""] + [f for f in str(t.get("Flags") or "")
                                                                              .split(", ") if f]),
                unsafe_allow_html=True)
    metrics([("Attendance", pct(t.get("Attendance %"))),
             ("Mocks evaluated", f"{int(t.get('Mocks evaluated') or 0)} / {int(t.get('Mocks due') or 0)}"),
             ("MCQ tests", f"{int(t.get('MCQ tests taken') or 0)} / {int(t.get('MCQ tests held') or 0)}"),
             ("Marks so far", "–" if pd.isna(t.get("Marks so far")) else f"{t['Marks so far']:g} / 100"),
             ("% of possible", pct(t.get("% of possible so far")))])

    _mocks_and_report(sid, c["improper"])


def _report(sid, mine):
    r1, r2, r3 = st.columns([2, 1, 1])
    case_q = r1.selectbox("Evaluation report", list(mine), key=f"lr_case_{sid}", label_visibility="collapsed",
                          format_func=lambda x: f"Report for {x}")
    e = mine[case_q]
    if r2.button("Prepare report", key=f"lr_prep_{sid}", use_container_width=True):
        kb = ctx.db.case_key_bytes(case_q)
        key_q = parse_key(kb)
        rws = rebuild_rows(key_q, ctx.db.mistakes(sid=sid, case_id=case_q))
        cats, total, out_of = summarise(rws)
        ss.lr_found = (sid, case_q, build_report(e.get("name") or "", sid, key_q, rws, cats, total, out_of,
                                                 e["file_name"], ctx.institute,
                                                 evaluated_on=f"{e['evaluated_at']} UTC"))
        if hashlib.sha256(kb).hexdigest()[:16] != e["key_hash"]:
            st.warning("The answer key changed after this return was marked; the report uses the current key with "
                       "the mistakes recorded at the time. Re-evaluate for an exact report.")
    if ss.get("lr_found") and ss.lr_found[:2] == (sid, case_q):
        r3.download_button("Download", ss.lr_found[2], file_name=report_name(sid, case_q), type="primary",
                           mime="application/pdf", icon=":material/download:", use_container_width=True)
    if admin_ok():
        with st.popover("Delete this evaluation", icon=":material/delete:"):
            st.write(f"Permanently delete **{sid} / {case_q}**? Its score and mistakes are removed.")
            if st.button("Delete", type="primary", key=f"lr_del_{sid}_{case_q}"):
                ctx.db.delete_evaluation(sid, case_q)
                st.toast("Deleted")
                st.rerun()


# ------------------------------------------------------------------ learners not on the roster
def _add_to_roster(sid, name):
    with st.form(f"add_{sid}", border=True):
        st.markdown("**Add to the roster**")
        st.caption("Their saved returns, on-time status and MCQ marks then count in results and batch reports.")
        c1, c2 = st.columns(2)
        nm = c1.text_input("Name", value=name or "")
        em = c2.text_input("Deloitte email", placeholder="Used to match class attendance")
        c3, c4 = st.columns(2)
        b = c3.selectbox("Batch", data.batches + ["New batch…"])
        nb = c4.text_input("New batch name", placeholder="Only if 'New batch…'")
        if st.form_submit_button("Add to roster", type="primary"):
            batch = (nb.strip() if b == "New batch…" else b) or "Unassigned"
            if not nm.strip():
                st.error("Enter the learner's name.")
            else:
                ctx.db.upsert_students([(sid, nm.strip(), batch, em.strip().lower() if "@" in em else "")])
                ss.lr_scope_next = "roster"          # applied before the switch is drawn on the next run
                st.toast(f"{sid} added to {batch}")
                st.rerun()


def _outsiders():
    from offroster import CONSISTENT, IRREGULAR, LEVELS, LOW_SCORES
    df = outsiders()
    st.caption(f"Learners whose returns were marked but who are not on the roster. Deloitte may take on those who "
               f"are consistent: **{CONSISTENT}** = submitted at least {data.off_min_rate:g}% of the mock returns held "
               f"so far and averaged {data.off_min_avg:g}% or more (change under Setup → Settings).")
    if df.empty:
        st.success("Everyone whose return was marked is on the roster.")
        return
    cnt = df["Consistency"].value_counts()
    metrics([("Tracked", len(df)), (CONSISTENT, int(cnt.get(CONSISTENT, 0))),
             (LOW_SCORES, int(cnt.get(LOW_SCORES, 0))), (IRREGULAR, int(cnt.get(IRREGULAR, 0)))])
    f1, f2 = st.columns([1, 2])
    q = f1.text_input("Search", placeholder="Name or ID", key="or_q", label_visibility="collapsed")
    lv = f2.pills("Consistency", LEVELS, selection_mode="multi", key="or_lv", label_visibility="collapsed")
    v = df[df["Consistency"].isin(lv)] if lv else df
    if q.strip():
        v = v[v["Name"].str.contains(q, case=False, na=False) | v["Student ID"].str.contains(q, case=False, na=False)]
    ev = st.dataframe(v, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row",
                      key="or_table",
                      column_config={"Submission rate %": st.column_config.ProgressColumn(
                          format="%.0f%%", min_value=0, max_value=100),
                          "Average score %": st.column_config.NumberColumn(format="%.1f"),
                          "Latest score %": st.column_config.NumberColumn(format="%.1f")})
    c1, c2 = st.columns([3, 1])
    c1.caption(f"{len(v)} learner(s). Click a row to see their returns or add them to the roster.")
    with c2:
        excel_download("List (Excel)", [("Not on roster", v)], f"Learners_not_on_roster_{stamp()}.xlsx",
                       key="or_xls")
    rows = ev.selection.rows if ev and ev.selection else []
    sid = v.iloc[rows[0]]["Student ID"] if rows else (v.iloc[0]["Student ID"] if len(v) == 1 and q.strip()
                                                       else None)
    if not sid:
        return
    r = v.set_index("Student ID").loc[sid]
    st.divider()
    st.subheader(f"{r['Name'] or 'Name not known'} · {sid}")
    st.markdown(f"<span class='pill'>Not on roster</span><span class='pill'>{r['Consistency']}</span>"
                f"<span class='pill'>{r['Trend']}</span>", unsafe_allow_html=True)
    metrics([("Mocks submitted", f"{r['Mocks submitted']} / {r['Mocks held']}"),
             ("On time", int(r["On time"])), ("Average score", pct(r["Average score %"])),
             ("Latest score", pct(r["Latest score %"])), ("MCQ tests", int(r["MCQ tests taken"]))])
    _mocks_and_report(sid, ctx.cache.get("outsiders_improper", pd.DataFrame(columns=["Student ID", "Case"])))
    _add_to_roster(sid, r["Name"])


def page():
    header("Learners", "Every learner in one place – attendance, mock returns, MCQs and marks so far.")
    if needs_db():
        return
    global_filters()
    n_out = len(outsiders())
    if ss.get("lr_scope_next"):
        ss.lr_scope = ss.pop("lr_scope_next")
    scope = st.segmented_control("Learners", ["roster", "outside"], default="roster", key="lr_scope",
                                 label_visibility="collapsed",
                                 format_func=lambda x: "On the roster" if x == "roster" else f"Not on roster ({n_out})")
    if scope == "outside":
        _outsiders()
        return
    _roster_view()


def _roster_view():
    if not data.students:
        st.info("Add the roster under **Setup → Roster** first.")
        return
    c = course()
    track = c["track"]
    if track.empty:
        st.info("No learners in these batches.")
        return
    from attendance import FLAG_IMPROPER, FLAG_LOW_ATT, FLAG_LOW_PERF, FLAG_MCQ, FLAG_MOCK, batch_overview
    tot = batch_overview(track).set_index("Batch").loc["All batches"]
    metrics([("Learners", int(tot.get("Learners", 0))),
             ("Avg attendance", pct(tot.get("Average attendance %"))),
             (FLAG_LOW_ATT, int(tot.get(FLAG_LOW_ATT, 0))),
             (FLAG_MOCK, int(tot.get(FLAG_MOCK, 0))),
             (FLAG_IMPROPER, int(tot.get(FLAG_IMPROPER, 0))),
             (FLAG_MCQ, int(tot.get(FLAG_MCQ, 0))),
             (FLAG_LOW_PERF, int(tot.get(FLAG_LOW_PERF, 0)))])
    view = st.segmented_control("Marks", ["Progress so far", "Final result"], default="Progress so far",
                                key="lr_view", label_visibility="collapsed") or "Progress so far"
    result = None
    if view == "Final result":
        fin = final_scores(c["studs"], c["evs"], data.mcq, c["fc"], data.cfg, view=FINAL)
        result = fin[["Student ID", "Result"]]
        st.caption(f"Final result: Pass, or At risk below the pass mark of {data.cfg['pass_mark']:g}.")
    sid = _table(track, result)
    if sid:
        _profile(sid, c)
