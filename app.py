"""
Tax Return Evaluator - Streamlit app.

Marks students' practice returns (IRS fillable forms) against the trainer's answer key, and keeps the course
picture Simandhar reports to trainers and Deloitte: final scores, submissions, attendance, MCQs and integrity.

Pages (left navigation):
  Work      Evaluate · Learners · Submissions · Insights · Reports
  Manage    Setup · Admin · Help
The marking logic and the database are in the other modules; this file only signs in, connects and routes.
"""
import hashlib
import hmac
import logging
from pathlib import Path

import streamlit as st

from ui.core import CSS, ctx, secret, ss

st.set_page_config(page_title="Tax Return Evaluator", page_icon=":material/fact_check:", layout="wide")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tax_evaluator.app")
HERE = Path(__file__).parent
st.markdown(CSS, unsafe_allow_html=True)

# ------------------------------------------------------------------ sign in
PASSWORD = secret("app_password")
if PASSWORD and not ss.get("authed"):
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.title("Tax Return Evaluator")
        st.caption("Simandhar Education · staff sign-in")
        with st.form("signin"):
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", type="primary", use_container_width=True):
                if hmac.compare_digest(pw.encode(), str(PASSWORD).encode()):
                    ss.authed = True
                    st.rerun()
                st.error("Wrong password.")
    st.stop()


# ------------------------------------------------------------------ database
@st.cache_resource(show_spinner="Connecting to the database...")
def get_db(url, code_version):
    # code_version opens a fresh connection whenever db.py changes after an update
    import importlib
    import db as dbmod
    importlib.reload(dbmod)
    return dbmod.DB(url)


ctx.cache = {}                                   # data is read at most once per run
ctx.db, ctx.db_error = None, ""
DB_URL = secret("database_url")
if DB_URL:
    try:
        ctx.db = get_db(DB_URL, hashlib.sha256((HERE / "db.py").read_bytes()).hexdigest()[:12])
    except Exception as e:                       # keep the app usable without the database
        ctx.db_error = str(e)
ctx.institute = ((ctx.db.settings().get("institute") if ctx.db else None)
                 or secret("institute", "Simandhar Education"))

# ------------------------------------------------------------------ navigation
from ui import admin, evaluate, help as helppage, insights, learners, reports, setup, submissions  # noqa: E402

pages = {
    "Work": [
        st.Page(evaluate.page, title="Evaluate", icon=":material/task_alt:", url_path="evaluate", default=True),
        st.Page(learners.page, title="Learners", icon=":material/group:", url_path="learners"),
        st.Page(submissions.page, title="Submissions", icon=":material/inbox:", url_path="submissions"),
        st.Page(insights.page, title="Insights", icon=":material/insights:", url_path="insights"),
        st.Page(reports.page, title="Reports", icon=":material/description:", url_path="reports"),
    ],
    "Manage": [
        st.Page(setup.page, title="Setup", icon=":material/tune:", url_path="setup"),
        st.Page(admin.page, title="Admin", icon=":material/shield_person:", url_path="admin"),
        st.Page(helppage.page, title="Help", icon=":material/help:", url_path="help"),
    ],
}
nav = st.navigation(pages)

ctx.side = st.sidebar.container()            # page filters go here, above the name box
with st.sidebar:
    st.divider()
    ss.setdefault("staff_keep", "")
    ss.staff_in = ss.staff_keep
    st.text_input("Your name", key="staff_in", placeholder="Recorded with your changes",
                  on_change=lambda: ss.update(staff_keep=ss.staff_in))
    ctx.staff = ss.staff_keep
    if ctx.db:
        st.caption("Database connected")
    elif ctx.db_error:
        st.error("Database not reachable – nothing will be saved.", icon=":material/cloud_off:")
    else:
        st.caption("No database – results last only while this tab is open")

nav.run()
