"""Submissions: for one mock return - who is in, who sent something we could not mark, who is missing,
and on-time / late status (with a change log). Replaces Management → Submission status and
Batch detail → Not received / improper."""
import pandas as pd
import streamlit as st

from batchreport import case_submissions
from roster_flag import NOT_ON_ROSTER, add_roster_column, style_off_roster
from ui.core import (batches_selected, clean, ctx, data, excel_download, global_filters, header, metrics,
                     needs_db, ss, staff_name, stamp)

LAB = {1: "On time", 0: "Late", None: "Not set"}


def _status_editor(cs, evs_c):
    roster = data.roster
    base = pd.DataFrame([{"Student ID": e["student_id"], "Name": e.get("name") or "",
                          "Batch": (e.get("batch") or "") if e["student_id"] in roster else NOT_ON_ROSTER,
                          "Score /100": round(100 * (e["pct"] or 0), 1),
                          "Submission": LAB.get(e.get("on_time"), "Not set")} for e in evs_c])
    edited = st.data_editor(base, hide_index=True, use_container_width=True, key=f"sub_ed_{cs}",
                            disabled=["Student ID", "Name", "Batch", "Score /100"],
                            column_config={"Submission": st.column_config.SelectboxColumn(
                                "Submission", options=["On time", "Late", "Not set"], required=True),
                                "Score /100": st.column_config.ProgressColumn(format="%.1f", min_value=0,
                                                                              max_value=100)})
    changed = [(r["Student ID"], r["Submission"]) for (_, r), (_, o) in zip(edited.iterrows(), base.iterrows())
               if r["Submission"] != o["Submission"]]
    changes = [(sid, cs, v == "On time") for sid, v in changed if v != "Not set"]
    unset = [r["Student ID"] for _, r in base.iterrows() if r["Submission"] == "Not set"]
    if any(v == "Not set" for _, v in changed):
        st.caption("A status can be changed to On time or Late, not back to Not set.")
    if not changes and not unset:
        return
    who = staff_name()
    why = st.text_input("Reason for the change", key="sub_why",
                        placeholder="e.g. uploaded after the deadline; OneDrive time stamp checked")
    if not who:
        st.caption("Enter your name in the sidebar to save changes.")
    b1, b2, _ = st.columns([1, 1.4, 1])
    if changes and b1.button(f"Save {len(changes)} change(s)", type="primary", disabled=not who or not why.strip()):
        n = ctx.db.set_on_time(changes, who, why.strip())
        st.toast(f"{n} status(es) changed")
        st.rerun()
    if unset and b2.button(f"Mark all {len(unset)} 'Not set' as On time", disabled=not who or not why.strip()):
        n = ctx.db.set_on_time([(sid, cs, True) for sid in unset], who, why.strip())
        st.toast(f"{n} status(es) set to On time")
        st.rerun()


def page():
    header("Submissions", "For one mock return: who is in, what could not be marked, and who is missing.")
    if needs_db():
        return
    global_filters()
    info = data.case_info
    if not info:
        st.info("No mock returns yet. Add one under **Setup → Mock returns**.")
        return
    ids = list(info)
    default = ids.index(ss.sub_case) if ss.get("sub_case") in ids else len(ids) - 1
    cs = st.selectbox("Mock return", ids, index=default, key="sub_case",
                      format_func=lambda c: f"{c} · {info[c]['title'] or ''}")
    sel_b = set(batches_selected())
    studs = [dict(x, batch=x["batch"] or "Unassigned") for x in data.students]
    in_b = [x for x in studs if x["batch"] in sel_b]
    ids_b = {x["student_id"] for x in in_b}
    all_b = len(sel_b) == len(data.batches)
    evs_c = [e for e in data.evaluations if e["case_id"] == cs and
             (e["student_id"] in ids_b or (all_b and e["student_id"] not in data.roster))]
    nr_df, imp_df = case_submissions(studs, data.evaluations, data.runs([cs]), cs)
    nr_df = nr_df[nr_df["Batch"].isin(sel_b)]
    if not all_b:
        imp_df = imp_df[imp_df["Batch"].isin(sel_b)]
    imp_df = add_roster_column(imp_df, list(data.roster))
    n_unset = sum(1 for e in evs_c if e.get("on_time") is None)
    metrics([("On roster", len(in_b)), ("Evaluated", len(evs_c)),
             ("Improper", int((imp_df["Student ID"] != "").sum()) if not imp_df.empty else 0,
              "Came in but could not be marked, could not be matched, or named wrongly"),
             ("Not received", len(nr_df)), ("Status not set", n_unset)])
    t1, t2, t3, t4 = st.tabs([f"Evaluated ({len(evs_c)})", f"Improper ({len(imp_df)})",
                              f"Not received ({len(nr_df)})", "Change log"])
    with t1:
        if evs_c:
            st.caption("Change On time / Late in the table, give a reason, then save. Every change is logged.")
            _status_editor(cs, evs_c)
        else:
            st.info("Nothing evaluated for this mock return and these batches yet.")
    with t2:
        if imp_df.empty:
            st.success("None – every return that came in was marked and named properly.")
        else:
            st.caption("These learners earn no marks for this mock until their return is evaluated. A failed upload "
                       "that is evaluated later drops off this list.")
            st.dataframe(style_off_roster(imp_df), hide_index=True, use_container_width=True,
                         column_config={"Details": st.column_config.TextColumn(width="large")})
    with t3:
        if nr_df.empty:
            st.success("Everyone has submitted.")
        else:
            st.dataframe(nr_df, hide_index=True, use_container_width=True)
    with t4:
        hist = ctx.db.status_history(case_id=cs)
        if hist:
            h = pd.DataFrame(hist)
            h["changed_at"] = h["changed_at"].str[:16] + " UTC"
            st.dataframe(h.rename(columns={"student_id": "Student ID", "old_status": "From", "new_status": "To",
                                           "changed_by": "Changed by", "reason": "Reason", "changed_at": "When"})
                         [["When", "Student ID", "From", "To", "Changed by", "Reason"]], hide_index=True,
                         use_container_width=True)
        else:
            st.caption("No changes yet.")
    if not imp_df.empty or not nr_df.empty:
        excel_download("Improper and not received (Excel)", [("Improper submissions", imp_df),
                                                              ("Not received", nr_df)],
                       f"Submissions_{clean(cs)}_{stamp()}.xlsx")
