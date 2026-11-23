"""
batchreport.py - batch-wise analysis for the trainers.

Everything is worked out from what the database holds (roster, evaluations of the course's mock returns,
their mistakes, MCQ marks and the integrity analysis). A batch is measured only on the mock returns it has
done so far: a mock counts as "done" by a batch once any of its students has been evaluated on it.

  batch_summary   one row per batch (+ all batches): submission and on-time rates, scores, on track / at risk,
                  MCQ average, integrity flags
  mock_table      per mock: returns received, on time, average / median / lowest / highest score
  top_mistakes    the lines most often wrong, with how many students got them wrong
  missing_returns students with mock returns not received (an improper submission is listed separately)
  improper_submissions  returns that came in but could not be evaluated (unreadable, no PDF in the folder ...),
                  or were evaluated but named wrongly, taken from the evaluation-run history
  case_submissions  for one mock return: who has not submitted and who submitted improperly
  integrity_for   the integrity findings that concern one batch
"""
from collections import Counter, defaultdict
from statistics import median

import pandas as pd

from finalscore import SO_FAR, final_scores

ALL = "All batches"
FLAGGED = ("Very likely copied", "Likely copied")


def _batch(x):
    return (x.get("batch") or "").strip() or "Unassigned"


def _pct(n, d):
    return round(100 * n / d, 1) if d else None


def prepare(students, evaluations, final_cases):
    """Roster with a batch name for everyone, and only the evaluations of the course's mock returns."""
    studs = [dict(s, batch=_batch(s)) for s in students]
    on_roster = {s["student_id"]: s["batch"] for s in studs}
    fc = set(final_cases)
    evs = [dict(e, batch=on_roster[e["student_id"]]) for e in evaluations
           if e["case_id"] in fc and e["student_id"] in on_roster]
    return studs, evs


def done_by_batch(evs):
    out = defaultdict(set)
    for e in evs:
        out[e["batch"]].add(e["case_id"])
    return out


def batch_summary(studs, evs, mcq, final_cases, cfg, integrity_students=None, trainers=None):
    trainers = trainers or {}
    flagged = Counter()
    if integrity_students is not None and not integrity_students.empty:
        for _, r in integrity_students.iterrows():
            if r["Integrity"] in FLAGGED:
                flagged[_batch({"batch": r["Batch"]})] += 1
    scores = final_scores(studs, evs, mcq, final_cases, cfg, view=SO_FAR)
    done = done_by_batch(evs)
    rows = []
    groups = sorted({s["batch"] for s in studs})
    for b in groups + [ALL]:
        bs = [s for s in studs if b == ALL or s["batch"] == b]
        ids = {s["student_id"] for s in bs}
        be = [e for e in evs if e["student_id"] in ids]
        expected = (sum(len(done[s["batch"]]) for s in bs))
        pcts = [100 * float(e["pct"] or 0) for e in be]
        sc = scores[scores["Student ID"].isin(ids)]
        tests = [m for m in mcq if m["student_id"] in ids and 1 <= int(m["test_no"]) <= int(cfg["n_mcq"])]
        set_status = [e for e in be if e.get("on_time") is not None]
        rows.append({
            "Batch": b, "Trainer": trainers.get(b, {}).get("trainer", "") if b != ALL else "",
            "Students": len(bs),
            "Mocks done": len(done[b]) if b != ALL else len(set().union(*done.values())) if done else 0,
            "Returns received": len(be), "Returns expected": expected,
            "Submission rate %": _pct(len(be), expected),
            "On-time rate %": _pct(sum(1 for e in set_status if e["on_time"] == 1), len(set_status)),
            "Average score %": round(sum(pcts) / len(pcts), 1) if pcts else None,
            "Median score %": round(median(pcts), 1) if pcts else None,
            "MCQ average %": round(100 * sum(float(m["marks"] or 0) for m in tests) /
                                   (len(tests) * cfg["mcq_out_of"]), 1) if tests else None,
            "On track": int((sc["Result"] == "On track").sum()),
            "At risk": int((sc["Result"] == "At risk").sum()),
            "Integrity flags": flagged[b] if b != ALL else sum(flagged.values()),
            "Status not set": sum(1 for e in be if e.get("on_time") is None),
        })
    return pd.DataFrame(rows)


def mock_table(studs, evs, final_cases, case_info, batch=None):
    bs = [s for s in studs if batch in (None, ALL) or s["batch"] == batch]
    ids = {s["student_id"] for s in bs}
    be = [e for e in evs if e["student_id"] in ids]
    done = {e["case_id"] for e in be}
    rows = []
    for cid in final_cases:
        ce = [e for e in be if e["case_id"] == cid]
        if cid not in done:
            continue
        p = [100 * float(e["pct"] or 0) for e in ce]
        rows.append({"Case": cid, "Title": (case_info.get(cid) or {}).get("title") or "",
                     "Received": len(ce), "Students": len(bs), "Submission %": _pct(len(ce), len(bs)),
                     "On time": sum(1 for e in ce if e.get("on_time") == 1),
                     "Late": sum(1 for e in ce if e.get("on_time") == 0),
                     "Average %": round(sum(p) / len(p), 1) if p else None,
                     "Median %": round(median(p), 1) if p else None,
                     "Lowest %": round(min(p), 1) if p else None, "Highest %": round(max(p), 1) if p else None,
                     "Below 50%": sum(1 for x in p if x < 50)})
    return pd.DataFrame(rows, columns=["Case", "Title", "Received", "Students", "Submission %", "On time", "Late",
                                       "Average %", "Median %", "Lowest %", "Highest %", "Below 50%"])


def top_mistakes(mistakes, evs, batch=None, limit=15):
    """The form lines most often not correct, across the batch's received returns."""
    be = [e for e in evs if batch in (None, ALL) or e["batch"] == batch]
    keys = {(e["student_id"], e["case_id"]) for e in be}
    received = Counter(e["case_id"] for e in be)
    agg = {}
    for m in mistakes:
        if (m["student_id"], m["case_id"]) not in keys or (m.get("result") or "Correct") == "Correct":
            continue
        k = (m["case_id"], m.get("form") or "", m.get("line") or "", m.get("description") or "")
        a = agg.setdefault(k, {"students": set(), "lost": 0.0, "kind": Counter()})
        a["students"].add(m["student_id"])
        a["lost"] += max(float(m.get("marks") or 0) - float(m.get("awarded") or 0), 0)
        a["kind"][m.get("result")] += 1
    rows = [{"Case": c, "Form": f, "Line": ln, "Item": d, "Students wrong": len(a["students"]),
             "% of returns": _pct(len(a["students"]), received[c]),
             "Marks lost (total)": round(a["lost"], 1), "Mostly": a["kind"].most_common(1)[0][0]}
            for (c, f, ln, d), a in agg.items()]
    df = pd.DataFrame(rows, columns=["Case", "Form", "Line", "Item", "Students wrong", "% of returns",
                                     "Marks lost (total)", "Mostly"])
    return df.sort_values(["% of returns", "Students wrong"], ascending=False).head(limit).reset_index(drop=True)


IMPROPER_COLS = ["Student ID", "Name", "Batch", "Case", "Problem", "Details", "File", "When"]
NOT_RECEIVED_COLS = ["Student ID", "Name", "Batch", "Case"]


def _problem(status):
    """The kind of improper submission a run row's Status describes, or None if it is not one."""
    st = str(status or "")
    low = st.lower()
    if st.startswith("Failed"):
        return "Could not be evaluated"
    if st.startswith("Skipped"):
        if "already evaluated" in low or "file is for case" in low:
            return None
        if "no pdf" in low or "not a pdf" in low:
            return "No PDF / wrong file type"
        return "Skipped"
    if "CHECK NAME" in st:
        return "Named incorrectly (evaluated)"
    return None


def improper_submissions(studs, evs, run_rows, batch=None, case_ids=None):
    """run_rows: db.run_rows(...). One row per student and case with the latest improper submission.
    A failed or skipped upload that was later evaluated is left out (it was put right); a wrongly named
    return that was evaluated stays in, so the student can be told. Files the system could not link to any
    student are listed (Student ID blank) only when all batches are shown."""
    roster = {s["student_id"]: s for s in studs}
    got = {(e["student_id"], e["case_id"]) for e in evs}
    wanted = set(case_ids) if case_ids is not None else None
    seen, rows = set(), []
    for r in run_rows:                                   # newest run first: the first row per student wins
        cid = r.get("case_id") or ""
        if wanted is not None and cid not in wanted:
            continue
        prob = _problem(r.get("Status"))
        sid = str(r.get("Student ID") or "").strip().upper()
        key = (sid, cid) if sid else (r.get("File"), cid)
        if key in seen:
            continue
        seen.add(key)
        if not prob:
            continue
        if sid:
            if sid not in roster:
                continue
            if batch not in (None, ALL) and roster[sid]["batch"] != batch:
                continue
            if prob != "Named incorrectly (evaluated)" and (sid, cid) in got:
                continue                                 # evaluated since - put right
            name, b = roster[sid].get("name") or "", roster[sid]["batch"]
        else:
            if batch not in (None, ALL):
                continue
            name, b, prob = "", "", "Student not identified"
        rows.append({"Student ID": sid, "Name": name, "Batch": b, "Case": cid, "Problem": prob,
                     "Details": str(r.get("Status") or "")[:300], "File": str(r.get("File") or "")[:200],
                     "When": str(r.get("run_at") or "")[:16]})
    df = pd.DataFrame(rows, columns=IMPROPER_COLS)
    return df.sort_values(["Case", "Student ID"]).reset_index(drop=True)


def missing_returns(studs, evs, batch=None, improper=None):
    """Students with mock returns their batch has done but that were not received. Returns that came in
    improperly (improper: an improper_submissions table) are not counted here - they are listed there."""
    done = done_by_batch(evs)
    got = {(e["student_id"], e["case_id"]) for e in evs}
    if improper is not None and not improper.empty:
        got |= {(r["Student ID"], r["Case"]) for _, r in improper.iterrows() if r["Student ID"]}
    rows = []
    for s in studs:
        if batch not in (None, ALL) and s["batch"] != batch:
            continue
        miss = sorted(c for c in done[s["batch"]] if (s["student_id"], c) not in got)
        if miss:
            rows.append({"Student ID": s["student_id"], "Name": s.get("name") or "", "Batch": s["batch"],
                         "Missing": len(miss), "Mock returns not received": ", ".join(miss)})
    df = pd.DataFrame(rows, columns=["Student ID", "Name", "Batch", "Missing", "Mock returns not received"])
    return df.sort_values(["Missing", "Student ID"], ascending=[False, True]).reset_index(drop=True)


def case_submissions(studs, evs, run_rows, case_id, batch=None):
    """For one mock return: (not_received, improper). not_received = roster students (of the batch) with no
    evaluation and no improper submission for it, whether or not their batch has started it."""
    improper = improper_submissions(studs, evs, run_rows, batch, case_ids=[case_id])
    got = {e["student_id"] for e in evs if e["case_id"] == case_id}
    got |= set(improper["Student ID"])
    nr = [{"Student ID": s["student_id"], "Name": s.get("name") or "", "Batch": s["batch"], "Case": case_id}
          for s in studs if (batch in (None, ALL) or s["batch"] == batch) and s["student_id"] not in got]
    return (pd.DataFrame(nr, columns=NOT_RECEIVED_COLS).sort_values(["Batch", "Student ID"]).reset_index(drop=True),
            improper)


def score_bands(scores_df):
    """How many students fall in each band of their share of the marks possible so far (%)."""
    bands = [("Below 50", 0, 50), ("50-59", 50, 60), ("60-69", 60, 70), ("70-79", 70, 80), ("80-89", 80, 90),
             ("90-100", 90, 100.01)]
    started = scores_df[scores_df["Result"] != "Not started"]
    t = started["% of possible so far"] if "% of possible so far" in started else started["Total / 100"]
    return pd.DataFrame([{"Score band": n, "Students": int(((t >= lo) & (t < hi)).sum())} for n, lo, hi in bands])


def mock_trend(studs, evs, final_cases, batches):
    """Average score per mock for each batch (long form, for a chart)."""
    rows = []
    for b in batches:
        for cid in final_cases:
            p = [100 * float(e["pct"] or 0) for e in evs if e["batch"] == b and e["case_id"] == cid]
            if p:
                rows.append({"Batch": b, "Case": cid, "Average %": round(sum(p) / len(p), 1), "Returns": len(p)})
    return pd.DataFrame(rows, columns=["Batch", "Case", "Average %", "Returns"])


def integrity_for(res, batch):
    """The parts of an integrity analysis (integrity.analyse) that concern one batch's students."""
    empty = {"students": pd.DataFrame(), "pairs": pd.DataFrame(), "trainer": pd.DataFrame()}
    if not res:
        return empty
    stu = res["students"]
    if stu.empty:
        return empty
    stu = stu.assign(Batch=stu["Batch"].map(lambda x: _batch({"batch": x})))
    mine = stu[stu["Batch"] == batch] if batch != ALL else stu
    ids = set(mine["Student ID"])
    pairs, trainer = res["pairs"], res["trainer"]
    if not pairs.empty:                                   # a pair counts for the batch if either student is in it
        pairs = pairs[pairs["Student A"].isin(ids) | pairs["Student B"].isin(ids)]
    if not trainer.empty:
        trainer = trainer[trainer["Student"].isin(ids)]
    return {"students": mine[mine["Integrity"] != "No concerns"].reset_index(drop=True),
            "pairs": pairs.reset_index(drop=True), "trainer": trainer.reset_index(drop=True)}
