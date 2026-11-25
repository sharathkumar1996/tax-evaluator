"""Insights: how the batches compare, how scores move mock by mock, the mistakes most worth a revision class,
and copying checks. Replaces Batch reports → Overview / Batch detail and the Integrity checks tab.
Only the section on screen is computed."""
import pandas as pd
import streamlit as st

from batchreport import ALL, batch_summary, mock_table, mock_trend, score_bands, top_mistakes
from finalscore import SO_FAR, final_scores
from ui.core import (BATCH_COLORS, course, data, global_filters, header, integrity, log, metrics, needs_db, pct, ss)


def _overview(c, summary, sel_b):
    st.dataframe(summary.drop(columns=["Returns expected"]), hide_index=True, use_container_width=True,
                 column_config={
                     "Submission rate %": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100),
                     "On-time rate %": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100),
                     "Average score %": st.column_config.NumberColumn(format="%.1f"),
                     "MCQ average %": st.column_config.NumberColumn(format="%.1f")})
    trend = mock_trend(c["studs"], c["evs"], c["fc"], sel_b)
    if trend.empty:
        return
    st.markdown("##### Average score per mock return")
    color_of = {b: BATCH_COLORS[i % len(BATCH_COLORS)] for i, b in enumerate(data.batches)}
    try:
        import altair as alt
        dom = [b for b in data.batches if b in set(trend["Batch"])]
        chart = (alt.Chart(trend).mark_line(point=alt.OverlayMarkDef(size=60, filled=True), strokeWidth=2)
                 .encode(x=alt.X("Case:N", sort=c["fc"], title=None),
                         y=alt.Y("Average %:Q", scale=alt.Scale(domain=[0, 100]), title="Average score (%)"),
                         color=alt.Color("Batch:N", scale=alt.Scale(domain=dom, range=[color_of[b] for b in dom]),
                                         legend=alt.Legend(orient="top", title=None)),
                         tooltip=["Batch", "Case", "Average %", "Returns"])
                 .properties(height=280))
        rule = alt.Chart(pd.DataFrame({"y": [data.cfg["pass_mark"]]})).mark_rule(
            strokeDash=[4, 3], color="#8a8a8a").encode(y="y:Q")
        st.altair_chart(chart + rule, use_container_width=True)
        st.caption(f"Dashed line: pass mark {data.cfg['pass_mark']:g}.")
    except Exception:
        log.exception("chart failed")


def _one_batch(c, sel_b):
    b = st.selectbox("Batch", sel_b, key="in_batch",
                     format_func=lambda x: x + (f" · {data.trainers[x]['trainer']}"
                                                if data.trainers.get(x, {}).get("trainer") else ""))
    scores = final_scores([x for x in c["studs"] if x["batch"] == b], c["evs"], data.mcq, c["fc"], data.cfg,
                          view=SO_FAR)
    st.markdown("##### Mock by mock")
    st.dataframe(mock_table(c["studs"], c["evs"], c["fc"], data.case_info, b), hide_index=True,
                 use_container_width=True)
    bands = score_bands(scores)
    if bands["Students"].sum():
        st.markdown("##### Spread (% of marks possible so far)")
        color = BATCH_COLORS[data.batches.index(b) % len(BATCH_COLORS)] if b in data.batches else None
        st.bar_chart(bands, x="Score band", y="Students", color=color, height=220)
    st.markdown("##### Most common mistakes")
    st.caption("The lines most often wrong in this batch – good topics for a revision class.")
    st.dataframe(top_mistakes(data.mistakes, c["evs"], b, limit=25), hide_index=True, use_container_width=True)


def _integrity(c, sel_b):
    from integrity_report import CAUTION
    with st.spinner("Comparing returns..."):
        res = integrity(tuple(c["fc"]))
    if res is None:
        st.info("Nothing to compare yet.")
        return
    st.caption(f"{CAUTION} Last refreshed {ss.get('integ_at', '')}; it re-runs by itself when returns are added.")
    keep = lambda df: df[df["Batch"].isin(sel_b)] if (not df.empty and "Batch" in df) else df
    stu = keep(res["students"])
    cnt = lambda lv: int((stu["Integrity"] == lv).sum()) if not stu.empty else 0
    metrics([("Very likely copied", cnt("Very likely copied")), ("Likely copied", cnt("Likely copied")),
             ("Review", cnt("Review")), ("Built on trainer's file", len(keep(res["trainer"])))])
    concerns = stu[stu["Integrity"] != "No concerns"] if not stu.empty else stu
    st.markdown("##### Learners with concerns")
    if concerns.empty:
        st.success("No integrity concerns.")
    else:
        st.dataframe(concerns, hide_index=True, use_container_width=True)
    pr = res["pairs"]
    if not pr.empty:
        st.markdown("##### Flagged pairs")
        lv = st.pills("Levels", ["Very likely copied", "Likely copied", "Review"], selection_mode="multi",
                      default=["Very likely copied", "Likely copied"], key="in_lv", label_visibility="collapsed")
        st.dataframe(pr[pr["Level"].isin(lv or [])], hide_index=True, use_container_width=True,
                     column_config={"Evidence": st.column_config.TextColumn(width="large")})
    if not res["groups"].empty:
        st.markdown("##### Groups of linked learners")
        st.dataframe(res["groups"], hide_index=True, use_container_width=True)
    if not res["trainer"].empty:
        st.markdown("##### Built on the trainer's correct-return file")
        st.dataframe(keep(res["trainer"]), hide_index=True, use_container_width=True)
    if not res["id_checks"].empty:
        st.markdown("##### Student ID checks")
        st.dataframe(res["id_checks"], hide_index=True, use_container_width=True)
    with st.expander("What is checked"):
        st.markdown("""
- **Same file** – exactly the same PDF, even renamed or uploaded under another ID.
- **Saved over another learner's file** – the earlier learner's version is still inside the PDF.
- **Same save ID / document ID / time stamps** – hidden properties written when a PDF is saved.
- **Same unusual wrong answers** – the rarer the shared mistake, the more it counts.
- **Same typing habits** – 0 in the same boxes others leave blank; identical capitalisation and spacing.
- **Built on the trainer's file** – the trainer's correct return found inside a learner's file.
""")


def page():
    header("Insights", "How batches compare, where learners lose marks, and copying checks.")
    if needs_db():
        return
    global_filters()
    c = course()
    if not c["studs"]:
        st.info("Add the roster under **Setup → Roster** first.")
        return
    if not c["evs"]:
        st.info("No mock returns evaluated yet for these filters.")
        return
    sel_b = sorted({x["batch"] for x in c["studs"]})
    integ_students = None
    summary = batch_summary(c["studs"], c["evs"], data.mcq, c["fc"], data.cfg, integ_students, data.trainers)
    a = summary.set_index("Batch").loc[ALL]
    metrics([("Learners", int(a["Students"])),
             ("Submission rate", pct(a["Submission rate %"]),
              f"{a['Returns received']} of {a['Returns expected']} returns due so far"),
             ("On-time rate", pct(a["On-time rate %"])), ("Average score", pct(a["Average score %"])),
             ("MCQ average", pct(a["MCQ average %"]))])
    sec = st.segmented_control("Section", ["Batches", "One batch", "Integrity"], default="Batches",
                               key="in_sec", label_visibility="collapsed") or "Batches"
    if sec == "Batches":
        _overview(c, summary, sel_b)
    elif sec == "One batch":
        _one_batch(c, sel_b)
    else:
        _integrity(c, sel_b)
