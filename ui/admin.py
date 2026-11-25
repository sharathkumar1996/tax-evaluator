"""Admin: the record of every evaluation run, and erasing data. One unlock for everything risky."""
import hmac
import io
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from roster_flag import add_roster_column, callout_html, highlight_excel_sheet, off_roster_ids, style_off_roster
from ui.core import admin_ok, ctx, data, header, log, metrics, needs_db, secret, ss, stamp

NO_STUDENT = "No student identified"
CHECK = "CHECK|check"


# ------------------------------------------------------------------ run history
def _history():
    roster_b = {x["student_id"]: (x["batch"] or "").strip() or "Unassigned" for x in data.students}
    h1, h2, h3 = st.columns([1.2, 1, 1])
    h_case = h1.selectbox("Mock return", ["All"] + list(data.case_info), key="rh_case")
    h_days = h2.selectbox("Period", ["Last 7 days", "Last 30 days", "Last 90 days", "Everything"], key="rh_days")
    h_kind = h3.multiselect("Type", ["Bulk", "Single"], default=["Bulk", "Single"], key="rh_kind")
    days = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}.get(h_days)
    since = (datetime.now(timezone.utc) - pd.Timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S") if days else None
    runs_ = [r for r in ctx.db.runs(case_id=None if h_case == "All" else h_case, since=since) if r["kind"] in h_kind]
    all_rows, all_up = [], []
    for meta in runs_:
        d = ctx.db.run_detail(meta["run_id"]) or {}
        tag = {"run_id": meta["run_id"], "Run started (UTC)": meta["started_at"], "Type": meta["kind"],
               "Case": meta["case_id"], "By": meta["run_by"]}
        for r in d.get("rows") or []:
            sid_ = str(r.get("Student ID") or "").upper()
            b_ = (str(r.get("Batch") or "").strip() or (roster_b.get(sid_) if sid_ else None)
                  or ("Not on roster" if sid_ else NO_STUDENT))
            all_rows.append(dict(tag, **dict(r, Batch=b_)))
        all_up += [dict(tag, File=u[0], Bytes=u[1]) for u in d.get("uploads") or []]
        if d.get("options"):
            all_up.append(dict(tag, File="(options) " + "; ".join(f"{k_}: {v}" for k_, v in d["options"].items()),
                               Bytes=None))
    rows_f = pd.DataFrame(all_rows)
    if rows_f.empty:
        st.info("No evaluation runs for these filters.")
        return
    sv_all = rows_f["Status"].astype(str)
    per_run = pd.DataFrame({"run_id": rows_f["run_id"], "Evaluated": sv_all.str.startswith("Evaluated"),
                            "Skipped": sv_all.str.startswith("Skipped"), "Failed": sv_all.str.startswith("Failed"),
                            "To check": sv_all.str.contains(CHECK, regex=True)}).groupby("run_id").sum().astype(int)
    shown = [r for r in runs_ if r["run_id"] in per_run.index]
    metrics([("Runs", len(shown)), ("Evaluated", int(per_run["Evaluated"].sum())),
             ("To check", int(per_run["To check"].sum())), ("Failed", int(per_run["Failed"].sum())),
             ("Skipped", int(per_run["Skipped"].sum()))])
    table = pd.DataFrame([{"Started (UTC)": r["started_at"], "Type": r["kind"], "Case": r["case_id"],
                           "By": r["run_by"] or "–", "Files": r["files"], **per_run.loc[r["run_id"]].to_dict()}
                          for r in shown])
    ev = st.dataframe(table, hide_index=True, use_container_width=True, on_select="rerun",
                      selection_mode="single-row", key="rh_table")
    st.caption("Click a run to see every file and learner in it.")

    def workbook(run_ids):
        rows_df = add_roster_column(rows_f[rows_f["run_id"].isin(run_ids)].drop(columns=["run_id"]), roster_b)
        up_df = pd.DataFrame([u for u in all_up if u["run_id"] in run_ids]).drop(columns=["run_id"], errors="ignore")
        sv_ = rows_df["Status"].astype(str)
        buf_ = io.BytesIO()
        with pd.ExcelWriter(buf_, engine="openpyxl") as xw:
            table[[r["run_id"] in run_ids for r in shown]].to_excel(xw, sheet_name="Runs", index=False)
            for nm, df_ in (("Results", rows_df), ("Skipped", rows_df[sv_.str.startswith("Skipped")]),
                            ("Failed", rows_df[sv_.str.startswith("Failed")]),
                            ("To check", rows_df[sv_.str.contains(CHECK, regex=True)]), ("Uploaded files", up_df)):
                (df_ if not df_.empty else pd.DataFrame({"": ["None"]})).to_excel(xw, sheet_name=nm, index=False)
            for ws in xw.book.worksheets:
                highlight_excel_sheet(ws)
                ws.freeze_panes = "A2"
                for col in ws.columns:
                    ws.column_dimensions[col[0].column_letter].width = min(
                        max(len(str(c_.value or "")) for c_ in col) + 2, 70)
        return buf_.getvalue()

    sel = ev.selection.rows if ev and ev.selection else []
    if not sel:
        st.download_button(f"All {len(shown)} runs (Excel)", workbook([r["run_id"] for r in shown]),
                           file_name=f"Run_history_{stamp('%Y%m%d_%H%M')}.xlsx", icon=":material/download:")
        return
    pick = shown[sel[0]]["run_id"]
    one = add_roster_column(rows_f[rows_f["run_id"] == pick], roster_b)
    st.divider()
    show = st.segmented_control("Show", ["Everything", "To check", "Failed", "Skipped"], default="Everything",
                                key="rh_show", label_visibility="collapsed") or "Everything"
    sv = one["Status"].astype(str)
    view = one if show == "Everything" else one[sv.str.contains(CHECK, regex=True) if show == "To check"
                                                else sv.str.startswith(show)]
    off = off_roster_ids(view["Student ID"], roster_b) if "Student ID" in view else []
    if off:
        st.markdown(callout_html(off, "student(s) in this run are"), unsafe_allow_html=True)
    cols_ = [c for c in ["File", "Student ID", "Roster", "Name", "Batch", "Score /100", "Submission", "Status", "Notes"]
             if c in view.columns]
    st.dataframe(style_off_roster(view[cols_]), hide_index=True, use_container_width=True,
                 column_config={"Status": st.column_config.TextColumn(width="large")})
    up_one = [u for u in all_up if u["run_id"] == pick]
    with st.expander(f"Files uploaded ({len(up_one)})"):
        st.dataframe(pd.DataFrame(up_one)[["File", "Bytes"]] if up_one else pd.DataFrame(), hide_index=True,
                     use_container_width=True)
    st.download_button("This run (Excel)", workbook([pick]), file_name=f"Run_{pick}.xlsx", key="rh_one",
                       icon=":material/download:")


# ------------------------------------------------------------------ erase data
def _run(action, done_msg):
    try:
        action()
        ss.admin_msg = ("success", done_msg)
    except Exception as e:
        log.exception("erasing data failed")
        ss.admin_msg = ("error", f"Nothing was erased – the database reported: {e}")


def _danger(title, key, action, done_msg, body=None):
    """One pattern for every irreversible action: open, read, confirm."""
    with st.popover(title, icon=":material/delete:", use_container_width=True):
        st.write(body or "This cannot be undone.")
        if st.checkbox("I understand", key=f"ok_{key}"):
            st.button("Erase", key=f"btn_{key}", type="primary", on_click=_run, args=(action, done_msg))


def _erase():
    n = ctx.db.counts()
    metrics([("Learners", n["students"]), ("Mock returns", n["cases"]), ("Evaluated returns", n["evaluations"]),
             ("Saved mistakes", n["mistakes"])])
    st.caption("Erasing cannot be undone. Download the results workbook first (Reports → Management).")
    case_ids = list(data.case_info)
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### A mock return")
        cx = st.selectbox("Mock return", case_ids, key="er_case") if case_ids else None
        if cx:
            _danger(f"Erase results of {cx}", "case_res", lambda c=cx: ctx.db.erase_case_results(c),
                    f"Results for {cx} erased.", f"{ctx.db.case_eval_count(cx)} evaluated returns; the answer key "
                                                  "is kept.")
            _danger(f"Delete {cx} completely", "case_all", lambda c=cx: ctx.db.erase_case(c), f"{cx} deleted.",
                    "The answer key and all its results.")
        st.markdown("##### A learner")
        sx = st.text_input("Student ID", key="er_sid").strip().upper()
        if sx:
            _danger(f"Erase {sx}", "student", lambda s=sx: ctx.db.erase_student(s), f"{sx} erased.",
                    "Roster entry and all results.")
    with c2:
        st.markdown("##### A batch")
        bl = ctx.db.batches()
        bx = st.selectbox("Batch", bl, key="er_batch") if bl else None
        if bx:
            roster_too = st.checkbox("Also remove these learners from the roster", value=True, key="er_roster")
            _danger(f"Erase {bx}", "batch", lambda b=bx, r=roster_too: ctx.db.erase_batch(b, r), f"{bx} erased.")
        st.markdown("##### Clean-up")
        _danger("Erase results of IDs not on the roster", "unlisted", ctx.db.erase_unlisted, "Done.",
                "Useful after test runs with made-up IDs.")
        _danger("Delete run history older than 90 days", "runs",
                lambda: ctx.db.erase_runs(before=(datetime.now(timezone.utc) - pd.Timedelta(days=90))
                                          .strftime("%Y-%m-%d %H:%M:%S")), "Old runs deleted.")
    st.divider()
    with st.popover("Erase everything", icon=":material/warning:"):
        st.write("Removes the roster, all mock returns and all results.")
        if st.text_input("Type ERASE ALL", key="er_all_txt").strip() == "ERASE ALL":
            st.button("Erase everything", type="primary", key="er_all", on_click=_run,
                      args=(ctx.db.erase_all, "All data erased."))


# ------------------------------------------------------------------ page
def _unlock():
    admin = secret("admin_password") or secret("app_password")
    if not admin or hmac.compare_digest((ss.get("adminpw") or "").encode(), str(admin).encode()):
        ss.admin_ok, ss.adminpw, ss.admin_msg = True, "", None
    else:
        ss.admin_msg = ("error", "Wrong admin password.")


def page():
    header("Admin", "Evaluation history and data clean-up.")
    if needs_db():
        return
    msg = ss.pop("admin_msg", None)
    if msg:
        (st.success if msg[0] == "success" else st.error)(msg[1])
    sec = st.segmented_control("Section", ["Run history", "Erase data"], default="Run history", key="ad_sec",
                               label_visibility="collapsed") or "Run history"
    if sec == "Run history":
        _history()
        return
    if not admin_ok():
        with st.form("unlock"):
            st.text_input("Admin password", type="password", key="adminpw")
            st.form_submit_button("Unlock", on_click=_unlock, type="primary")
        st.caption("Unlocking also allows changing the scoring rules (Setup → Settings) and deleting a single "
                   "evaluation (Learners).")
        return
    _erase()
    st.button("Lock", on_click=lambda: ss.update(admin_ok=False), icon=":material/lock:")
