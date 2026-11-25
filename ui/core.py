"""Shared state and helpers for every page.

* `ctx` holds what every page needs for one run of the script: the database, who is signed in, test mode.
* Data is read from the database at most once per run (`data()`), and only by the page being shown -
  Streamlit runs only the current page, so nothing is built for pages you are not looking at.
* One set of filters (batches, mock returns) lives in the sidebar and applies to every page that reads results.
"""
import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from finalscore import SO_FAR, final_scores, load_settings
from grader_core import load_key

log = logging.getLogger("tax_evaluator.ui")
ss = st.session_state

HERE_TEMPLATE = Path(__file__).resolve().parent.parent / "answer_key_template.xlsx"
BATCH_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]


# ------------------------------------------------------------------ run context
@dataclass
class Ctx:
    db: object = None
    db_error: str = ""
    institute: str = "Simandhar Education"
    staff: str = ""
    cache: dict = field(default_factory=dict)
    side: object = None                       # sidebar container for page filters

    @property
    def test(self):
        return bool(ss.get("test_run"))

    @property
    def save(self):
        """True when results should be written to the database."""
        return bool(self.db) and not self.test


ctx = Ctx()


def secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def clean(x):
    return re.sub(r"[^A-Za-z0-9-]+", "_", str(x or "NA")).strip("_") or "NA"


def stamp(fmt="%Y%m%d"):
    return datetime.now().strftime(fmt)


def report_name(student_id, case_id):
    return f"{'TEST_' if ctx.test else ''}Evaluation_report_{clean(student_id)}_{clean(case_id)}.pdf"


@st.cache_data(show_spinner=False)
def parse_key(data: bytes):
    return load_key(data)


def key_hash(data: bytes):
    return hashlib.sha256(data).hexdigest()[:16]


# ------------------------------------------------------------------ data (read once per run)
def _once(name, fn):
    if name not in ctx.cache:
        ctx.cache[name] = fn()
    return ctx.cache[name]


class Data:
    """Lazy, per-run view of the database. Each property hits the database at most once per run."""

    @property
    def students(self):
        return _once("students", ctx.db.students)

    @property
    def roster(self):
        return _once("roster", lambda: {x["student_id"]: x for x in self.students})

    @property
    def evaluations(self):
        return _once("evaluations", ctx.db.evaluations)

    @property
    def cases(self):
        return _once("cases", ctx.db.cases)

    @property
    def case_info(self):
        return _once("case_info", lambda: {c["case_id"]: c for c in self.cases})

    @property
    def final_cases(self):
        return _once("final_cases", ctx.db.final_cases)

    @property
    def settings(self):
        return _once("settings", ctx.db.settings)

    @property
    def cfg(self):
        return _once("cfg", lambda: load_settings(self.settings))

    @property
    def mcq(self):
        return _once("mcq", ctx.db.mcq_scores)

    @property
    def attendance(self):
        return _once("attendance", ctx.db.attendance)

    @property
    def mistakes(self):
        return _once("mistakes", ctx.db.mistakes)

    @property
    def batches(self):
        return sorted({(x["batch"] or "Unassigned") for x in self.students})

    def setting_json(self, name):
        try:
            return json.loads(self.settings.get(name) or "{}")
        except ValueError:
            return {}

    @property
    def trainers(self):
        return self.setting_json("batch_trainers")

    @property
    def min_att(self):
        return float(self.settings.get("att_min_pct") or 80)

    @property
    def low_perf(self):
        return float(self.settings.get("low_perf_pct") or self.cfg["pass_mark"])

    def runs(self, case_ids):
        key = "runs:" + ",".join(sorted(case_ids))
        def read():
            try:
                return ctx.db.run_rows(list(case_ids))
            except Exception:
                log.exception("reading the evaluation runs failed")
                return []
        return _once(key, read)


data = Data()


# ------------------------------------------------------------------ the course picture (shared by most pages)
def course():
    """Everything the learner-facing pages need, for the batches and mock returns chosen in the sidebar."""
    def build():
        from attendance import tracker
        from batchreport import improper_submissions, prepare
        fc = mocks_selected()
        studs_all, evs_all = prepare(data.students, data.evaluations, data.final_cases)
        sel_b = set(batches_selected())
        studs = [x for x in studs_all if x["batch"] in sel_b]
        ids = {x["student_id"] for x in studs}
        evs = [e for e in evs_all if e["case_id"] in fc and e["student_id"] in ids]
        runs = data.runs(fc)
        improper = improper_submissions(studs, evs, runs, case_ids=fc, all_evaluations=data.evaluations)
        scores = final_scores(studs, evs, data.mcq, fc, data.cfg, view=SO_FAR)
        track = tracker(studs, data.attendance, evs, data.mcq, fc, improper, scores, data.cfg,
                        min_att=data.min_att, low_perf=data.low_perf)
        return {"studs": studs, "evs": evs, "fc": fc, "runs": runs, "improper": improper, "scores": scores,
                "track": track}
    return _once("course", build)


def integrity(case_ids):
    """Integrity analysis of the chosen mock returns; re-run only when the evaluations change."""
    from integrity import analyse
    evs_in = [e for e in data.evaluations if e["case_id"] in case_ids]
    sig = (tuple(sorted(case_ids)), len(evs_in), max((str(e.get("evaluated_at") or "") for e in evs_in), default=""),
           len(data.students))
    if ss.get("integ_sig") != sig:
        ids = {e["student_id"] for e in evs_in}
        mis = [m for m in data.mistakes if m["case_id"] in case_ids and m["student_id"] in ids]
        ss.integ_res = analyse(evs_in, ctx.db.fingerprints(list(case_ids)), mis, data.students) if evs_in else None
        ss.integ_sig = sig
        ss.integ_at = datetime.now().strftime("%d %b %H:%M")
    return ss.integ_res


# ------------------------------------------------------------------ global filters (sidebar)
def _mock_options():
    done = {e["case_id"] for e in data.evaluations}
    return [c for c in data.final_cases if c in done] or list(data.final_cases)


def batches_selected():
    """Chosen batches (empty choice = all). Kept outside the widget so it survives moving between pages."""
    opts = data.batches
    return [b for b in ss.get("keep_batches", opts) if b in opts] or opts


def mocks_selected():
    opts = _mock_options()
    return [c for c in ss.get("keep_mocks", opts) if c in opts] or opts


def _keep(widget, keep):
    ss[keep] = list(ss[widget])


def global_filters():
    """Batches and mock returns, shown in the sidebar. Every results page reads them."""
    if not ctx.db:
        return
    b_opts, m_opts = data.batches, _mock_options()
    with (ctx.side or st.sidebar):
        st.caption("FILTERS")
        if b_opts:
            ss.w_batches = [b for b in ss.get("keep_batches", []) if b in b_opts]
            st.multiselect("Batches", b_opts, key="w_batches", placeholder="All batches",
                           on_change=_keep, args=("w_batches", "keep_batches"))
        if m_opts:
            ss.w_mocks = [c for c in ss.get("keep_mocks", []) if c in m_opts]
            st.multiselect("Mock returns", m_opts, key="w_mocks", placeholder="All mock returns",
                           on_change=_keep, args=("w_mocks", "keep_mocks"),
                           format_func=lambda c: f"{c} · {(data.case_info.get(c) or {}).get('title') or ''}"[:40])


# ------------------------------------------------------------------ small UI kit
CSS = """
<style>
#MainMenu, footer, [data-testid="stDecoration"] {visibility: hidden;}
.block-container {padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1280px;}
h1 {font-size: 1.6rem !important; font-weight: 600 !important; letter-spacing: -0.01em;}
h2, h3 {font-weight: 600 !important; letter-spacing: -0.01em;}
h3 {font-size: 1.1rem !important; margin-top: 1.2rem !important;}
[data-testid="stMetric"] {background: #FAFAFA; border: 1px solid #EEE; border-radius: 10px; padding: 12px 14px;}
[data-testid="stMetricLabel"] p {font-size: 0.78rem; color: #666;}
[data-testid="stMetricValue"] {font-size: 1.45rem;}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {letter-spacing: 0.06em; font-size: 0.7rem;}
.sub {color: #666; margin: -0.6rem 0 1.2rem 0; font-size: 0.95rem;}
.pill {display:inline-block; padding: 2px 10px; border-radius: 999px; font-size: 0.78rem; margin-right: 6px;
       background: #F7ECEF; color: #8E1B3A;}
</style>
"""


def header(title, sub=None):
    st.title(title)
    if sub:
        st.markdown(f"<div class='sub'>{sub}</div>", unsafe_allow_html=True)


def needs_db(what="This page"):
    if ctx.db:
        return False
    st.info(f"{what} needs the database. Add `database_url` to the app's secrets.")
    return True


def metrics(items):
    """items: list of (label, value) or (label, value, help)."""
    cols = st.columns(len(items))
    for c, it in zip(cols, items):
        c.metric(it[0], it[1], help=it[2] if len(it) > 2 else None)


def pct(v):
    return "–" if v is None or pd.isna(v) else f"{v:g}%"


def excel_download(label, sheets, file_name, key=None):
    from roster_flag import excel_bytes
    st.download_button(label, excel_bytes(sheets), file_name=file_name, key=key, icon=":material/download:")


def prepared(key, label, build, file_name, mime=None, help=None):
    """Build a file only when asked (reports are slow to make), then offer it for download.
    A prepared file is forgotten when the sidebar filters change, so it never shows stale data."""
    if ctx.db:
        key = f"{key}_{key_hash('|'.join(batches_selected() + ['#'] + mocks_selected()).encode())}"
    c1, c2 = st.columns([1, 1])
    if c1.button(label, key=f"prep_{key}", help=help, use_container_width=True):
        with st.spinner("Preparing..."):
            try:
                ss[f"file_{key}"] = build()
            except Exception:
                log.exception("preparing %s failed", key)
                st.error("Could not prepare this file. The details were logged.")
    if ss.get(f"file_{key}"):
        c2.download_button("Download", ss[f"file_{key}"], file_name=file_name, mime=mime, key=f"dl_{key}",
                           type="primary", icon=":material/download:", use_container_width=True)


def admin_ok():
    return bool(ss.get("admin_ok"))


def staff_name():
    return (ctx.staff or "").strip()


def read_table(upload):
    raw = io.BytesIO(upload.getvalue())
    return pd.read_csv(raw, dtype=str) if upload.name.lower().endswith(".csv") else pd.read_excel(raw, dtype=str)
