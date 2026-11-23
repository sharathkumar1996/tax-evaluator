"""
finalscore.py - the student's final course score out of 100.

Every mock return and every MCQ test is worth a fixed share of the 100 marks, and a student keeps adding marks
as the course goes on (nothing is averaged over "what's done so far" or scaled up):

  Return accuracy  60   60 / 12 = 5 marks per mock return, earned in proportion to its score
                        (100/100 -> 5 marks, 80/100 -> 4 marks). 12 returns at 100 -> 60.
                        A return not submitted (or not evaluated) earns 0.
  On-time          20   20 / 12 = 1.67 marks for each mock return submitted on time. All 12 on time -> 20.
                        A late return earns 0 on-time marks - there is no part credit.
  MCQ tests        20   20 / 4 = 5 marks per test, in proportion to its marks (50/50 -> 5). A test not taken: 0.
  Pass mark        70

Two views; the marks are the same in both:
  * "final"   - the whole course; Pass / Fail against the pass mark.
  * "so far"  - also shows the marks that were possible from the returns and tests the student's batch has
                done so far, and the student's share of them. "On track" = that share is at least the pass mark
                (e.g. 70% of the marks possible so far), "At risk" = below it.
The numbers (12 returns, 60/20/20, 4 tests of 50, pass 70) are settings, changeable in the app.
"""
import re

import pandas as pd

DEFAULTS = {"n_mocks": 12, "w_accuracy": 60, "w_on_time": 20, "w_mcq": 20, "n_mcq": 4, "mcq_out_of": 50,
            "pass_mark": 70}
LABELS = {"n_mocks": "Mock returns in the course", "w_accuracy": "Marks for return accuracy",
          "w_on_time": "Marks for on-time submission", "w_mcq": "Marks for MCQ tests", "n_mcq": "Number of MCQ tests",
          "mcq_out_of": "Marks per MCQ test", "pass_mark": "Pass mark (out of 100)"}
FINAL, SO_FAR = "final", "so far"


def load_settings(stored):
    """stored: {name: text} from the database. Missing or unreadable values fall back to the defaults."""
    out = dict(DEFAULTS)
    for k, v in (stored or {}).items():
        if k in out:
            try:
                out[k] = float(v) if "." in str(v) else int(v)
            except (TypeError, ValueError):
                pass
    return out


def check_settings(s):
    """Plain-language problems with a set of settings ([] = fine)."""
    errs = []
    if s["n_mocks"] < 1 or s["n_mcq"] < 0 or s["mcq_out_of"] <= 0:
        errs.append("The number of mock returns must be at least 1 and marks per MCQ test above 0.")
    if abs(s["w_accuracy"] + s["w_on_time"] + s["w_mcq"] - 100) > 1e-9:
        errs.append(f"The three weightages add up to {s['w_accuracy'] + s['w_on_time'] + s['w_mcq']:g}, not 100.")
    if not 0 < s["pass_mark"] <= 100:
        errs.append("The pass mark must be between 1 and 100.")
    return errs


def _r(x):
    return round(float(x) + 1e-9, 2)


def per_item(settings):
    """Marks one mock return (accuracy at 100%, on time) and one MCQ test (full marks) are worth."""
    s = settings
    return (s["w_accuracy"] / s["n_mocks"] if s["n_mocks"] else 0,
            s["w_on_time"] / s["n_mocks"] if s["n_mocks"] else 0,
            s["w_mcq"] / s["n_mcq"] if s["n_mcq"] else 0)


def final_scores(students, evaluations, mcq, final_cases, settings, view=FINAL):
    """students: [{student_id, name, batch}]; evaluations: [{student_id, case_id, pct, on_time}] (pct 0-1);
    mcq: [{student_id, test_no, marks}]; final_cases: case IDs that are the course's mock returns.
    Returns a DataFrame, one row per student on the roster."""
    s = settings
    per_acc, per_ont, per_test = per_item(s)
    final = set(final_cases)
    batch_of = {x["student_id"]: (x.get("batch") or "") for x in students}
    ev = [e for e in evaluations if e["case_id"] in final and e["student_id"] in batch_of]
    tests = [m for m in mcq if 1 <= int(m["test_no"]) <= int(s["n_mcq"]) and m["student_id"] in batch_of]

    # what each batch has done so far (for the "so far" view)
    done_cases, done_tests = {}, {}
    for e in ev:
        done_cases.setdefault(batch_of[e["student_id"]], set()).add(e["case_id"])
    for m in tests:
        done_tests.setdefault(batch_of[m["student_id"]], set()).add(int(m["test_no"]))

    by_student = {}
    for e in ev:
        by_student.setdefault(e["student_id"], []).append(e)
    marks = {}
    for m in tests:
        marks.setdefault(m["student_id"], {})[int(m["test_no"])] = min(max(float(m["marks"] or 0), 0), s["mcq_out_of"])

    rows = []
    for st in students:
        sid, b = st["student_id"], batch_of[st["student_id"]]
        mine = by_student.get(sid, [])
        if view == SO_FAR:
            n_ret = len(done_cases.get(b, ()))
            n_tests = len(done_tests.get(b, ()))
        else:
            n_ret, n_tests = int(s["n_mocks"]), int(s["n_mcq"])
        pct_sum = sum(min(max(float(e["pct"] or 0), 0), 1) for e in mine)
        on_time = sum(1 for e in mine if e.get("on_time") == 1)
        late = sum(1 for e in mine if e.get("on_time") == 0)
        not_set = sum(1 for e in mine if e.get("on_time") is None)
        tm = marks.get(sid, {})
        mcq_total = sum(tm.values())

        # fixed marks per item, added up - the same in both views
        acc = per_acc * pct_sum
        ont = per_ont * on_time
        mcq_m = per_test * mcq_total / s["mcq_out_of"] if s["mcq_out_of"] else 0
        total = acc + ont + mcq_m
        row = {"Student ID": sid, "Name": st.get("name") or "", "Batch": b,
               "Returns evaluated": f"{len(mine)} / {n_ret}",
               "Not submitted": max(n_ret - len(mine), 0),
               "Average return score": _r(100 * pct_sum / n_ret) if n_ret else None,
               f"Accuracy / {s['w_accuracy']:g}": _r(acc),
               "On time": on_time, "Late": late,
               f"On-time / {s['w_on_time']:g}": _r(ont),
               "MCQ marks": f"{mcq_total:g} / {n_tests * s['mcq_out_of']:g}",
               f"MCQ / {s['w_mcq']:g}": _r(mcq_m),
               "Total / 100": _r(total)}
        if view == FINAL:
            row["Result"] = "Pass" if total >= s["pass_mark"] - 1e-9 else "Fail"
        else:
            possible = (per_acc + per_ont) * n_ret + per_test * n_tests
            share = 100 * total / possible if possible else None
            row["Marks possible so far"] = _r(possible)
            row["% of possible so far"] = _r(share) if share is not None else None
            row["Result"] = ("Not started" if not possible else
                             "On track" if share >= s["pass_mark"] - 1e-9 else "At risk")
        row["Status not set"] = not_set
        for t in range(1, int(s["n_mcq"]) + 1):
            row[f"MCQ {t}"] = tm.get(t)
        rows.append(row)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ MCQ upload
class MCQError(Exception):
    pass


def parse_mcq(df, settings, test_no=None):
    """A roster-style sheet of MCQ marks -> [(student_id, test_no, marks)].
    Accepted: a 'Student ID' column plus 'Test 1' ... 'Test 4' columns (or 'MCQ 1', 'T1' ...), or a
    'Student ID' column and one marks column with test_no given. Blank cells are skipped (not 0), so a
    sheet can carry only the tests already written. Marks must be between 0 and the marks per test."""
    cols = {c: re.sub(r"[^a-z0-9]", "", str(c).lower()) for c in df.columns}
    sid_col = next((c for c, n in cols.items() if n in ("studentid", "id", "empid", "employeeid", "rollno",
                                                       "studentno", "sid")), None)
    if sid_col is None:
        raise MCQError("No 'Student ID' column found.")
    test_cols = {}
    for c, n in cols.items():
        m = re.fullmatch(r"(?:mcq|test|mcqtest|t)(\d{1,2})(?:marks|score)?", n)
        if m:
            test_cols[c] = int(m.group(1))
    if not test_cols:
        other = [c for c, n in cols.items() if c != sid_col and n in ("marks", "score", "mark", "total", "result")]
        if not other:
            raise MCQError("No marks columns found: name them 'Test 1', 'Test 2' ... or use one 'Marks' column.")
        if not test_no:
            raise MCQError("This sheet has one 'Marks' column - choose which test it is.")
        test_cols = {other[0]: int(test_no)}
    bad_tests = sorted({t for t in test_cols.values() if not 1 <= t <= int(settings["n_mcq"])})
    if bad_tests:
        raise MCQError(f"Test number {', '.join(map(str, bad_tests))} is outside 1-{int(settings['n_mcq'])}.")
    out, problems = [], []
    for i, r in df.iterrows():
        sid = str(r[sid_col] or "").strip().upper()
        if sid.endswith(".0"):
            sid = sid[:-2]
        if not sid or sid == "NAN":
            continue
        for c, t in test_cols.items():
            v = r[c]
            if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "-"):
                continue
            try:
                x = float(str(v).replace(",", "").strip())
            except ValueError:
                problems.append(f"row {i + 2}, {c}: '{v}' is not a number")
                continue
            if not 0 <= x <= settings["mcq_out_of"]:
                problems.append(f"row {i + 2}, {c}: {x:g} is outside 0-{settings['mcq_out_of']:g}")
                continue
            out.append((sid, t, x))
    if problems:
        raise MCQError("Fix these cells and upload again: " + "; ".join(problems[:10]) +
                       (f" (and {len(problems) - 10} more)" if len(problems) > 10 else ""))
    return out
