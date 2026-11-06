"""
metrics.py - management metrics.

Per return
  * File-ready      : no mistakes on any CRITICAL item (filing status, dependents' credit boxes,
                      AGI, taxable income, total tax, total payments, refund, amount owed).
  * Bottom-line     : the refund (1040 line 35a) and amount owed (line 37) both match the key.
  * Errors          : number of items marked wrong + unexpected entries.
Across returns these become the File-ready rate, Bottom-line accuracy and Average errors per return.
"""
import re

import pandas as pd

from grader_core import norm_form, norm_line, summarise

CRITICAL_1040_LINES = {"11", "15", "24", "33", "35a", "37"}
BOTTOM_LINE = {"35a", "37"}
CRITICAL_TEXT = ("Filing status, dependents' credit boxes, AGI (line 11), taxable income (15), total tax (24), "
                 "total payments (33), refund (35a) and amount owed (37)")


def is_critical(form, line):
    f, inst = norm_form(form)
    ln = norm_line(line)
    if f != "1040" or inst != 1:
        return False
    return ln == "fs" or bool(re.match(r"^dep\d_(ctc|odc)$", ln)) or ln in CRITICAL_1040_LINES


def flag(rows):
    for r in rows:
        r["Critical"] = r["Result"] != "Unexpected entry" and is_critical(r["Form"], r["Line"])
    return rows


def return_metrics(rows):
    _, score, out_of = summarise(rows)
    wrong = [r for r in rows if r["Result"] != "Correct"]
    crit = [r for r in wrong if r.get("Critical")]
    bl = [r for r in rows if r["Result"] != "Unexpected entry" and norm_form(r["Form"]) == ("1040", 1)
          and norm_line(r["Line"]) in BOTTOM_LINE]
    return {"score": score, "out_of": out_of, "pct": score / out_of if out_of else 0,
            "errors": len(wrong), "critical_errors": len(crit),
            "unexpected": sum(r["Result"] == "Unexpected entry" for r in rows),
            "needs_review": sum(bool(r.get("Needs Review")) for r in rows),
            "file_ready": len(crit) == 0,
            "bottom_line": None if not bl else all(r["Result"] == "Correct" for r in bl)}


def rebuild_rows(key, stored):
    """Full graded rows from the answer key plus the stored mistakes of one evaluation."""
    by_step = {int(m["step"]): m for m in stored}
    rows = []
    for s in key.steps:
        m = by_step.pop(int(s["Step"]), None)
        if m:
            rows.append(_from_stored(m))
        else:
            rows.append({"Step": s["Step"], "Category": s["Category"], "Form": s["Form"], "Line": s["Line"],
                         "Description": s["Description"], "Correct Value": s["Correct Value"],
                         "Student Value": s["Correct Value"], "Marks": s["Marks"], "Awarded": s["Marks"],
                         "Result": "Correct", "Feedback": "", "Note": "", "Needs Review": False})
    rows += [_from_stored(m) for m in sorted(by_step.values(), key=lambda m: m["step"])]
    return flag(rows)


def _from_stored(m):
    def val(x):
        if x is None:
            return None
        try:
            f = float(str(x).replace(",", ""))
            return f if not re.search(r"\d-\d", str(x)) and not str(x).startswith("0.") else x
        except ValueError:
            return x
    return {"Step": m["step"], "Category": m["category"], "Form": m["form"], "Line": m["line"],
            "Description": m["description"], "Correct Value": val(m["correct_value"]) if m["correct_value"] else "",
            "Student Value": val(m["student_value"]), "Marks": m["marks"], "Awarded": m["awarded"],
            "Result": m["result"], "Feedback": m["feedback"] or "", "Note": m["note"] or "",
            "Needs Review": bool(m["needs_review"])}


# ------------------------------------------------------------------ aggregates
def headline(ev: pd.DataFrame, n_students=None, n_cases=None):
    if ev.empty:
        return {"returns": 0, "students": 0, "file_ready_rate": None, "bottom_line_accuracy": None,
                "avg_errors": None, "avg_score": None, "expected": (n_students or 0) * (n_cases or 0)}
    bl = ev["bottom_line"].dropna()
    return {"returns": len(ev), "students": ev["student_id"].nunique(),
            "file_ready_rate": ev["file_ready"].mean(),
            "bottom_line_accuracy": bl.mean() if len(bl) else None,
            "avg_errors": ev["errors"].mean(), "avg_score": ev["pct"].mean(),
            "expected": (n_students or 0) * (n_cases or 0)}


def group_table(ev: pd.DataFrame, by):
    if ev.empty:
        return pd.DataFrame()
    g = ev.groupby(by, dropna=False)
    out = pd.DataFrame({
        "Students": g["student_id"].nunique(),
        "Returns": g.size(),
        "Avg score %": (g["pct"].mean() * 100).round(1),
        "File-ready rate %": (g["file_ready"].mean() * 100).round(1),
        "Bottom-line accuracy %": (g["bottom_line"].mean() * 100).round(1),
        "Avg errors per return": g["errors"].mean().round(1),
    }).reset_index()
    return out


def top_mistakes(ms: pd.DataFrame, ev: pd.DataFrame, n=15):
    if ms.empty or ev.empty:
        return pd.DataFrame()
    ms = ms.merge(ev[["student_id", "case_id"]], on=["student_id", "case_id"])
    ms = ms[ms["result"] != "Correct"]
    per_case = ev.groupby("case_id").size().rename("returns")
    t = (ms.groupby(["case_id", "form", "line", "description"]).size().rename("Students wrong").reset_index()
         .merge(per_case, left_on="case_id", right_index=True))
    t["% of returns"] = (t["Students wrong"] / t["returns"] * 100).round(1)
    t = t.sort_values(["Students wrong", "% of returns"], ascending=False).head(n)
    return t.rename(columns={"case_id": "Case", "form": "Form", "line": "Line", "description": "What was checked"}) \
            .drop(columns=["returns"])


def student_table(ev: pd.DataFrame):
    if ev.empty:
        return pd.DataFrame()
    piv = ev.pivot_table(index=["student_id", "name", "batch"], columns="case_id", values="pct", aggfunc="first")
    piv = (piv * 100).round(1)
    g = ev.groupby(["student_id", "name", "batch"], dropna=False)
    piv["Returns"] = g.size()
    piv["Avg score %"] = (g["pct"].mean() * 100).round(1)
    piv["File-ready rate %"] = (g["file_ready"].mean() * 100).round(1)
    piv["Avg errors"] = g["errors"].mean().round(1)
    return piv.reset_index().rename(columns={"student_id": "Student ID", "name": "Name", "batch": "Batch"})
