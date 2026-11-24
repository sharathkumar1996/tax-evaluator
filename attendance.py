"""
attendance.py - class attendance, and one place that brings together everything Deloitte asks about each learner:
classes, mock returns, MCQ tests and performance.

Class attendance comes from the daily reports Simandhar sends Deloitte ("Deloitte-Form1040-Attendance-80pct-
<date>.xlsx"): Teams attendance per batch, with the 80% rule already applied. Its sheets "80% Attendance" and
"Below 80%" list everyone who joined (Batch, Participant Name, Deloitte Email ID, % of Session). A learner on the
roster who is in neither sheet did not join that day.

Status of a learner for one class:
  Present     joined and stayed at least 80% of the session (Deloitte's rule)
  Below 80%   joined but left early - does NOT count as attended
  Absent      not in the report at all, while their batch had a class that day

Learners are matched to the roster by Deloitte email (add an 'Email' column to the roster), then by employee ID
if the sheet has one, then by name (same words in any order, inside the batch first). Anyone still not matched is
listed so the roster can be fixed - nobody is dropped silently.

A simpler sheet also works for other sources: one row per learner with a Student ID or Email column and a
Status / Present / Attended column (Present, P, Yes, 1 ... / Absent, A, No, 0), and a date.
"""
import re
from datetime import date, datetime

import pandas as pd

PRESENT, BELOW, ABSENT = "Present", "Below 80%", "Absent"


class AttendanceError(Exception):
    pass


# ------------------------------------------------------------------ helpers
def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def name_key(name):
    """Order-free key for a person's name: 'A Rohith V Pai' == 'Pai Rohith V A'."""
    return " ".join(sorted(t for t in re.split(r"[^a-z]+", str(name or "").lower()) if t))


def batch_key(b):
    """'Batch 1', 'Batch-1', 'batch1', '1' -> 'batch1' so the report's batch names match the roster's."""
    s = _norm(b)
    return s if s.startswith("batch") else ("batch" + s if s.isdigit() else s)


def _email(v):
    s = str(v or "").strip().lower()
    return s if "@" in s and s != "nan" else ""


def _pct(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x * 100 if x <= 1.5 else x                 # the report stores 0.98 for 98%


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], 1)}


def find_date(*texts, year=None):
    """First date found in the texts: '18 Nov 2026', '18th November 2026', '18Nov', '2026-11-18', '18-11-2026'."""
    year = year or date.today().year
    for t in texts:
        t = str(t or "")
        m = re.search(r"(20\d\d)-(\d{1,2})-(\d{1,2})", t)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        for m in re.finditer(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s\-_]*([A-Za-z]{3})[A-Za-z]*[\s\-_,]*(20\d\d)?", t):
            if m.group(2).lower() in MONTHS:
                return date(int(m.group(3) or year), MONTHS[m.group(2).lower()], int(m.group(1)))
        m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](20\d\d)", t)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


# ------------------------------------------------------------------ reading a report
def _find_header(df, must=("email", "name")):
    """Row index whose cells contain all the words in must (for sheets with a title above the header)."""
    for i in range(min(len(df), 15)):
        cells = [_norm(c) for c in df.iloc[i].tolist() if _norm(c) and _norm(c) != "nan"]
        if len(cells) >= 3 and all(any(w in c for c in cells) for w in must):    # a real header row, not a note
            return i
    return None


def _table(raw, must):
    h = _find_header(raw, must)
    if h is None:
        return None
    t = raw.iloc[h + 1:].copy()
    t.columns = [str(c).strip() for c in raw.iloc[h].tolist()]
    return t.dropna(how="all")


def _col(df, *names):
    cols = {_norm(c): c for c in df.columns}
    for n in names:
        if _norm(n) in cols:
            return cols[_norm(n)]
    for n in names:                                    # 'Deloitte Email ID' for 'email'
        for k, c in cols.items():
            if _norm(n) in k:
                return c
    return None


def read_report(file_bytes, file_name="", session_date=None):
    """One attendance file -> (session_date, rows). rows: [{batch, name, email, student_id, pct, status, remark}].
    Raises AttendanceError with a plain message when the file can't be used."""
    import io
    try:
        if str(file_name).lower().endswith(".csv"):
            sheets = {"Sheet1": pd.read_csv(io.BytesIO(file_bytes), header=None, dtype=str)}
        else:
            sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, header=None, dtype=object)
    except Exception as e:
        raise AttendanceError(f"The file could not be opened ({type(e).__name__}). Save it as .xlsx and try again.")

    title_text = " ".join(str(v) for df in list(sheets.values())[:1] for v in df.head(4).fillna("").values.ravel())
    d = session_date or find_date(title_text, file_name)
    if d is None:
        raise AttendanceError("No class date found in the file or its name - choose the date before saving.")

    rows = []
    names = {_norm(n): n for n in sheets}
    # Simandhar's daily report: the two combined sheets
    pairs = [(names.get("80attendance"), PRESENT), (names.get("below80"), BELOW)]
    if any(p for p, _ in pairs):
        for sh, status in pairs:
            if not sh:
                continue
            t = _table(sheets[sh], ("name", "email"))
            if t is None:
                continue
            cb, cn, ce = _col(t, "Batch"), _col(t, "Participant Name", "Name"), _col(t, "Deloitte Email ID", "Email")
            cp, cr = _col(t, "% of Session", "Percent"), _col(t, "Remarks")
            cid = _col(t, "Employee ID", "Student ID", "Emp ID")
            for _, r in t.iterrows():
                nm, em = str(r[cn] if cn else "").strip(), _email(r[ce] if ce else "")
                if (not nm or nm.lower() == "nan") and not em:
                    continue
                rows.append({"batch": str(r[cb] if cb else "").strip(), "name": "" if nm.lower() == "nan" else nm,
                             "email": em, "student_id": _sid(r[cid]) if cid else "",
                             "pct": _pct(r[cp]) if cp else None, "status": status,
                             "remark": "" if not cr or pd.isna(r[cr]) else str(r[cr])[:200]})
    else:
        # a simple one-row-per-learner sheet with a status column
        for sh, raw in sheets.items():
            t = _table(raw, ("status",)) if _find_header(raw, ("status",)) is not None else \
                _table(raw, ("present",)) if _find_header(raw, ("present",)) is not None else \
                _table(raw, ("attend",))
            if t is None:
                continue
            cs = _col(t, "Status", "Present", "Attended", "Attendance")
            cb, cn, ce = _col(t, "Batch"), _col(t, "Name", "Student Name", "Participant Name"), _col(t, "Email")
            cid, cp = _col(t, "Student ID", "Employee ID", "Emp ID", "ID"), _col(t, "% of Session", "Percent")
            for _, r in t.iterrows():
                v = str(r[cs] if cs else "").strip().lower()
                if v in ("", "nan"):
                    continue
                st = PRESENT if v in ("present", "p", "yes", "y", "1", "attended", "true") else \
                    BELOW if "below" in v or "partial" in v else ABSENT
                rows.append({"batch": str(r[cb] if cb else "").strip(), "name": str(r[cn] if cn else "").strip(),
                             "email": _email(r[ce] if ce else ""), "student_id": _sid(r[cid]) if cid else "",
                             "pct": _pct(r[cp]) if cp else None, "status": st, "remark": ""})
            break
    if not rows:
        raise AttendanceError("No attendance rows found. Expected the '80% Attendance' and 'Below 80%' sheets of "
                              "the daily report, or a sheet with a Status / Present column.")
    return d, rows


def read_reports(file_bytes, file_name=""):
    """Like read_report, for a file that may hold several class dates: a compiled sheet with a 'Date' column
    (one row per learner per date) gives one (date, rows) per date. Any other file gives [read_report(...)]."""
    import io
    if not str(file_name).lower().endswith(".csv"):
        try:
            sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, header=None, dtype=object)
        except Exception:
            sheets = {}
        for raw in sheets.values():
            h = _find_header(raw, ("date", "status"))
            if h is None:
                continue
            t = _table(raw, ("date", "status"))
            cd = _col(t, "Date", "Session Date", "Class Date")
            if cd is None:
                continue
            out = {}
            for _, r in t.iterrows():
                v = r[cd]
                d = v.date() if hasattr(v, "date") and not isinstance(v, str) else find_date(str(v))
                if d is None:
                    continue
                out.setdefault(d, []).append(r)
            if not out:
                continue
            res = []
            for d, rs in sorted(out.items()):
                sub = pd.DataFrame(rs, columns=t.columns)
                buf = io.BytesIO()
                sub.drop(columns=[cd]).to_excel(buf, index=False)
                res.append(read_report(buf.getvalue(), "x.xlsx", session_date=d))
            return res
    return [read_report(file_bytes, file_name)]


def _sid(v):
    s = str(v or "").strip().upper()
    s = s[:-2] if s.endswith(".0") else s
    return "" if s in ("", "NAN") else s


# ------------------------------------------------------------------ matching to the roster
def alias_key(row):
    """Key under which a manual link for a report row is remembered: its email, else its name."""
    return row.get("email") or ("name:" + name_key(row.get("name")))


def match_rows(rows, roster, aliases=None):
    """roster: [{student_id, name, batch, email}]; aliases: {alias_key: student_id} saved from manual links.
    Adds student_id (roster's) and matched_by to every row ('' when no single roster learner fits)."""
    aliases = aliases or {}
    by_email = {_email(s.get("email")): s for s in roster if _email(s.get("email"))}
    by_id = {s["student_id"].upper(): s for s in roster}
    by_name = {}
    for s in roster:
        by_name.setdefault(name_key(s.get("name")), []).append(s)
    taken = set()
    out = []
    for r in rows:
        r = dict(r)
        hit, how = None, ""
        if aliases.get(alias_key(r)) in by_id:
            hit, how = by_id[aliases[alias_key(r)]], "linked by hand"
        elif r.get("email") and r["email"] in by_email:
            hit, how = by_email[r["email"]], "email"
        elif r.get("student_id") and r["student_id"] in by_id:
            hit, how = by_id[r["student_id"]], "employee ID"
        else:
            cands = by_name.get(name_key(r.get("name")), []) if name_key(r.get("name")) else []
            same = [s for s in cands if batch_key(s.get("batch")) == batch_key(r.get("batch"))]
            pick = same if len(same) == 1 else cands if len(cands) == 1 else []
            if pick:
                hit, how = pick[0], "name" + ("" if same else " (other batch)")
        if hit and hit["student_id"] not in taken:
            r["student_id"], r["matched_by"] = hit["student_id"], how
            taken.add(hit["student_id"])
        else:
            r["student_id"], r["matched_by"] = "", ("already matched to another row" if hit else "")
        out.append(r)
    return out


def session_records(session_date, rows, roster):
    """Matched rows -> records to save: present / below-80% learners, plus Absent for every roster learner of a
    batch that had this class and is not in the report. Returns (records, unmatched_rows).
    record: {session_date, student_id, batch, status, pct, matched_by, source_name, source_email}."""
    d = session_date.isoformat() if isinstance(session_date, (date, datetime)) else str(session_date)
    batches = {batch_key(r.get("batch")) for r in rows if r.get("batch")}
    roster_batch = {s["student_id"]: s.get("batch") or "" for s in roster}
    recs, seen, unmatched = [], set(), []
    rank = {PRESENT: 2, BELOW: 1, ABSENT: 0}
    for r in rows:
        if not r.get("student_id"):
            unmatched.append(r)
            continue
        sid = r["student_id"]
        rec = {"session_date": d, "student_id": sid, "batch": roster_batch.get(sid, ""), "status": r["status"],
               "pct": r.get("pct"), "matched_by": r.get("matched_by") or "", "source_name": r.get("name") or "",
               "source_email": r.get("email") or ""}
        prev = next((x for x in recs if x["student_id"] == sid), None)
        if prev is None:
            recs.append(rec)
        elif rank[rec["status"]] > rank[prev["status"]]:
            recs[recs.index(prev)] = rec
        seen.add(sid)
    for s in roster:
        if s["student_id"] not in seen and (not batches or batch_key(s.get("batch")) in batches):
            recs.append({"session_date": d, "student_id": s["student_id"], "batch": s.get("batch") or "",
                         "status": ABSENT, "pct": None, "matched_by": "not in report", "source_name": "",
                         "source_email": ""})
    return recs, unmatched


# ------------------------------------------------------------------ the learner tracker
FLAG_LOW_ATT = "Low class attendance"
FLAG_MOCK = "Missed mock returns"
FLAG_IMPROPER = "Improper submission"
FLAG_MCQ = "Missed MCQ tests"
FLAG_LOW_PERF = "Not performing well"
FLAG_ALL = "Absent for everything"          # no class joined, no mock return, no MCQ test
FLAGS = [FLAG_ALL, FLAG_IMPROPER, FLAG_LOW_ATT, FLAG_MOCK, FLAG_MCQ, FLAG_LOW_PERF]

# one readiness status per learner (Deloitte wants everyone deployable, so this is what they look at first)
ON_TRACK, ATTENTION, URGENT = "On track", "Needs attention", "Needs urgent attention"
READINESS = [ON_TRACK, ATTENTION, URGENT]


def readiness(flags):
    """On track = no concern; Needs attention = one; Needs urgent attention = two or more, or absent for
    everything."""
    fl = [f.strip() for f in str(flags or "").split(",") if f.strip()]
    if not fl:
        return ON_TRACK
    if FLAG_ALL in fl or len(fl) >= 2:
        return URGENT
    return ATTENTION


def tracker(students, attendance, evaluations, mcq, final_cases, improper, scores, cfg, min_att=80.0,
            low_perf=None):
    """One row per roster learner with classes, mock returns, MCQ tests, marks and flags.
    students: roster; attendance: saved attendance records; evaluations / mcq: as in the database;
    improper: batchreport.improper_submissions table; scores: finalscore.final_scores(..., SO_FAR) table.
    min_att: % of classes a learner must attend; low_perf: % of possible marks below which a learner is 'not
    performing well' (default: the pass mark)."""
    low_perf = cfg["pass_mark"] if low_perf is None else low_perf
    fc = set(final_cases)
    batch_of = {s["student_id"]: (s.get("batch") or "").strip() or "Unassigned" for s in students}

    # what each batch has had so far
    classes_b, mocks_b, tests_b = {}, {}, {}
    for a in attendance:
        if a["student_id"] in batch_of:
            classes_b.setdefault(batch_of[a["student_id"]], set()).add(a["session_date"])
    for e in evaluations:
        if e["case_id"] in fc and e["student_id"] in batch_of:
            mocks_b.setdefault(batch_of[e["student_id"]], set()).add(e["case_id"])
    for m in mcq:
        if m["student_id"] in batch_of and 1 <= int(m["test_no"]) <= int(cfg["n_mcq"]):
            tests_b.setdefault(batch_of[m["student_id"]], set()).add(int(m["test_no"]))
    imp_rows = improper[improper["Student ID"] != ""] if improper is not None and not improper.empty else None
    imp_of = {}
    if imp_rows is not None:
        for _, r in imp_rows.iterrows():
            imp_of.setdefault(r["Student ID"], []).append(f"{r['Case']}: {r['Problem']}")
    sc = scores.set_index("Student ID") if scores is not None and not scores.empty else None

    att_of, ev_of, mcq_of = {}, {}, {}
    for a in attendance:
        att_of.setdefault(a["student_id"], {})[a["session_date"]] = a["status"]
    for e in evaluations:
        if e["case_id"] in fc:
            ev_of.setdefault(e["student_id"], set()).add(e["case_id"])
    for m in mcq:
        mcq_of.setdefault(m["student_id"], set()).add(int(m["test_no"]))

    rows = []
    for s in students:
        sid, b = s["student_id"], batch_of[s["student_id"]]
        held = sorted(classes_b.get(b, ()))
        mine = att_of.get(sid, {})
        present = sum(1 for d in held if mine.get(d) == PRESENT)
        below = sum(1 for d in held if mine.get(d) == BELOW)
        absent = len(held) - present - below
        att_pct = round(100 * present / len(held), 1) if held else None
        due = sorted(mocks_b.get(b, ()))
        imp = imp_of.get(sid, [])
        imp_cases = {x.split(":")[0] for x in imp}
        got = ev_of.get(sid, set())
        not_recv = [c for c in due if c not in got and c not in imp_cases]
        tests = sorted(tests_b.get(b, ()))
        missed_t = [t for t in tests if t not in mcq_of.get(sid, set())]
        r = sc.loc[sid] if sc is not None and sid in sc.index else None
        share = None if r is None or pd.isna(r.get("% of possible so far")) else float(r["% of possible so far"])
        flags = []
        # never joined a class (not even briefly), no mock return in (or even attempted), no MCQ test taken
        # (counting only what the batch has had so far)
        if held and absent == len(held) and not (got & set(due)) and not imp and len(missed_t) == len(tests):
            flags.append(FLAG_ALL)
        if imp:
            flags.append(FLAG_IMPROPER)
        if att_pct is not None and att_pct < min_att - 1e-9:
            flags.append(FLAG_LOW_ATT)
        if not_recv:
            flags.append(FLAG_MOCK)
        if missed_t:
            flags.append(FLAG_MCQ)
        if share is not None and share < low_perf - 1e-9:
            flags.append(FLAG_LOW_PERF)
        row = {"Student ID": sid, "Name": s.get("name") or "", "Batch": b, "Email": s.get("email") or "",
               "Classes held": len(held), "Present (80%+)": present, "Below 80%": below, "Absent": absent,
               "Attendance %": att_pct,
               "Mocks due": len(due), "Mocks evaluated": len(got & set(due)), "Improper submissions": len(imp),
               "Mocks not received": len(not_recv), "Not received": ", ".join(not_recv),
               "Improper details": "; ".join(imp),
               "MCQ tests held": len(tests), "MCQ tests taken": len(tests) - len(missed_t),
               "MCQ missed": ", ".join(f"Test {t}" for t in missed_t),
               "Marks so far": None if r is None else r.get("Total / 100"),
               "% of possible so far": share,
               "Readiness": readiness(", ".join(flags)), "Flags": ", ".join(flags), "Flag count": len(flags)}
        for d in held:
            row[f"Class {d}"] = mine.get(d, ABSENT)
        rows.append(row)
    return pd.DataFrame(rows)


def class_summary(attendance, students):
    """Per class date and batch: how many present / below 80% / absent."""
    batch_of = {s["student_id"]: (s.get("batch") or "").strip() or "Unassigned" for s in students}
    rows = {}
    for a in attendance:
        b = batch_of.get(a["student_id"])
        if b is None:
            continue
        k = (a["session_date"], b)
        r = rows.setdefault(k, {"Date": a["session_date"], "Batch": b, PRESENT: 0, BELOW: 0, ABSENT: 0})
        r[a["status"]] = r.get(a["status"], 0) + 1
    df = pd.DataFrame(rows.values(), columns=["Date", "Batch", PRESENT, BELOW, ABSENT])
    if df.empty:
        return df
    df["Learners"] = df[PRESENT] + df[BELOW] + df[ABSENT]
    df["Attendance %"] = (100 * df[PRESENT] / df["Learners"]).round(1)
    return df.sort_values(["Date", "Batch"]).reset_index(drop=True)


# ------------------------------------------------------------------ summary and the report for Deloitte
def batch_overview(track):
    """Per batch (+ all): learners and how many carry each flag."""
    if track.empty:
        return pd.DataFrame()
    rows = []
    for b in sorted(track["Batch"].unique()) + ["All batches"]:
        t = track if b == "All batches" else track[track["Batch"] == b]
        att = t["Attendance %"].dropna()
        row = {"Batch": b, "Learners": len(t),
               "Classes held": int(t["Classes held"].max()),
               "Average attendance %": round(float(att.mean()), 1) if len(att) else None}
        for k in READINESS:
            row[k] = int((t["Readiness"] == k).sum()) if "Readiness" in t else 0
        for f in FLAGS:
            row[f] = int(t["Flags"].str.contains(f, regex=False).sum())
        rows.append(row)
    return pd.DataFrame(rows)


LIST_COLS = {
    FLAG_LOW_ATT: ["Student ID", "Name", "Batch", "Email", "Classes held", "Present (80%+)", "Below 80%", "Absent",
                   "Attendance %"],
    FLAG_MOCK: ["Student ID", "Name", "Batch", "Email", "Mocks due", "Mocks evaluated", "Mocks not received",
                "Not received"],
    FLAG_IMPROPER: ["Student ID", "Name", "Batch", "Email", "Improper submissions", "Improper details",
                    "Mocks evaluated", "Mocks due"],
    FLAG_MCQ: ["Student ID", "Name", "Batch", "Email", "MCQ tests held", "MCQ tests taken", "MCQ missed"],
    FLAG_LOW_PERF: ["Student ID", "Name", "Batch", "Email", "Marks so far", "% of possible so far", "Mocks evaluated",
                    "Attendance %"],
    FLAG_ALL: ["Student ID", "Name", "Batch", "Email", "Classes held", "Mocks due", "MCQ tests held"],
}


def flag_list(track, flag):
    t = track[track["Flags"].str.contains(flag, regex=False)] if not track.empty else track
    cols = LIST_COLS[flag] + (["Flags"] if flag != FLAG_ALL else [])
    return t[[c for c in cols if c in t.columns]].sort_values(["Batch", "Student ID"]).reset_index(drop=True)


def deloitte_workbook(track, classes, institute="Simandhar Education", as_of=None, min_att=80.0, low_perf=70.0):
    """Branded Excel for Deloitte: summary, every learner, one sheet per concern and class-wise attendance.
    Learners with an improper mock submission are highlighted in Simandhar maroon on every sheet."""
    import io

    from openpyxl.styles import Alignment, Font, PatternFill
    maroon, light = "8E1B3A", "F7ECEF"
    as_of = as_of or datetime.now().strftime("%d %b %Y")
    sheets = [("Summary", batch_overview(track)), ("All learners", track.drop(columns=["Flag count"],
                                                                               errors="ignore"))]
    titles = {FLAG_LOW_ATT: f"Not attending classes (below {min_att:g}% of classes at 80%+ of the session)",
              FLAG_MOCK: "Mock returns not submitted",
              FLAG_IMPROPER: "Mock returns submitted but not accepted (improper submission)",
              FLAG_MCQ: "MCQ tests not taken",
              FLAG_LOW_PERF: f"Not performing well (below {low_perf:g}% of the marks possible so far)",
              FLAG_ALL: "Absent for everything: no class attended, no mock return and no MCQ test"}
    for f in [FLAG_LOW_ATT, FLAG_MOCK, FLAG_IMPROPER, FLAG_MCQ, FLAG_LOW_PERF, FLAG_ALL]:
        sheets.append((f, flag_list(track, f)))
    sheets.append(("Class-wise", classes))
    improper_ids = set(track.loc[track["Flags"].str.contains(FLAG_IMPROPER, regex=False), "Student ID"]) \
        if not track.empty else set()

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets:
            df = df if df is not None and not df.empty else pd.DataFrame({"": ["None"]})
            df.to_excel(xw, sheet_name=name[:31], index=False, startrow=3)
            ws = xw.book[name[:31]]
            ws["A1"] = f"{institute} | Deloitte Form 1040 Training - {titles.get(name, name)}"
            ws["A1"].font = Font(bold=True, size=13, color=maroon)
            ws["A2"] = (f"As of {as_of}. Class attendance follows the 80% rule (Below 80% is not counted as "
                        "attended). Rows shaded maroon: the learner has a mock return that was submitted but not "
                        "accepted (improper submission).")
            ws["A2"].font = Font(italic=True, size=9, color="5F5F5F")
            for c in ws[4]:
                c.fill = PatternFill("solid", fgColor=maroon)
                c.font = Font(bold=True, color="FFFFFF")
                c.alignment = Alignment(wrap_text=True, vertical="center")
            heads = [c.value for c in ws[4]]
            if "Student ID" in heads:
                k = heads.index("Student ID")
                for row in ws.iter_rows(min_row=5):
                    if row[k].value in improper_ids:
                        for c in row:
                            c.fill = PatternFill("solid", fgColor=light)
                            c.font = Font(bold=True, color=maroon)
            ws.freeze_panes = "C5" if "Student ID" in heads else "A5"
            for col in ws.iter_cols(min_row=4):
                w = max(len(str(c.value or "")) for c in col) + 2
                ws.column_dimensions[col[0].column_letter].width = min(max(w, 8), 45)
    return buf.getvalue()
