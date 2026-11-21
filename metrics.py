"""
metrics.py - per-return figures saved with each evaluation (score, errors, items needing review),
and rebuilding a return's marked rows from what the database stores.

The database still has file_ready / bottom_line / critical_errors columns from earlier versions;
they are filled in so older copies of the app keep working, but nothing in the app shows them.
"""
import re

from grader_core import norm_form, norm_line, summarise

CRITICAL_1040_LINES = {"11", "15", "24", "33", "35a", "37"}
BOTTOM_LINE = {"35a", "37"}


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
