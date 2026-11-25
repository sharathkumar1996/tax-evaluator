"""Learners who are not on the roster.

Deloitte tracks them too: those who keep submitting good returns may be taken on. Their returns are saved like
everyone else's but don't count in batch results until they are added to the roster. This module rates how
consistent each one has been, so staff can see who is worth recommending.

Consistency, over the mock returns held so far (a mock is 'held' once anyone's return for it is evaluated):
  Consistent          - submitted at least `min_rate` % of them and averaged at least `min_avg` %
  Regular, low scores - submitted at least `min_rate` % but averaged below `min_avg` %
  Irregular           - submitted fewer than `min_rate` %
"""
import pandas as pd

CONSISTENT, LOW_SCORES, IRREGULAR = "Consistent", "Regular, low scores", "Irregular"
LEVELS = [CONSISTENT, LOW_SCORES, IRREGULAR]
COLS = ["Student ID", "Name", "Consistency", "Mocks submitted", "Mocks held", "Submission rate %", "On time",
        "Average score %", "Latest score %", "Trend", "MCQ tests taken", "Improper", "Mocks missed", "Last evaluated"]


def _sid(x):
    return str(x or "").strip().upper()


def held_mocks(evaluations, final_cases):
    """Mock returns that have at least one evaluated return, in course order."""
    done = {e["case_id"] for e in evaluations}
    return [c for c in final_cases if c in done]


def names_from_runs(run_rows):
    """Names typed in when an off-roster learner was evaluated on their own (newest first wins)."""
    out = {}
    for r in run_rows or []:
        sid, nm = _sid(r.get("Student ID")), str(r.get("Name") or "").strip()
        if sid and nm and sid not in out:
            out[sid] = nm
    return out


def candidates(evaluations, roster_ids, final_cases, mcq=(), improper=None, names=None, min_rate=80.0,
               min_avg=70.0):
    """One row per student ID that has evaluated mock returns but is not on the roster."""
    known = {_sid(x) for x in roster_ids}
    held = held_mocks(evaluations, final_cases)
    order = {c: i for i, c in enumerate(final_cases)}
    tests_held = {int(m["test_no"]) for m in mcq}
    by = {}
    for e in evaluations:
        sid = _sid(e.get("student_id"))
        if sid and sid not in known and e.get("case_id") in held:
            by.setdefault(sid, []).append(e)
    imp = {}
    if improper is not None and not improper.empty:
        for _, r in improper.iterrows():
            if _sid(r["Student ID"]) and _sid(r["Student ID"]) not in known:
                imp[_sid(r["Student ID"])] = imp.get(_sid(r["Student ID"]), 0) + 1
    rows = []
    for sid, evs in by.items():
        evs = sorted(evs, key=lambda e: order.get(e["case_id"], 0))
        scores = [100 * (e.get("pct") or 0) for e in evs]
        n, rate = len(evs), 100 * len(evs) / len(held) if held else 0
        avg = sum(scores) / n
        level = IRREGULAR if rate < min_rate - 1e-9 else (CONSISTENT if avg >= min_avg - 1e-9 else LOW_SCORES)
        trend = "–"
        if n >= 2:
            d = scores[-1] - scores[0]
            trend = "Improving" if d >= 5 else "Falling" if d <= -5 else "Steady"
        got = {e["case_id"] for e in evs}
        rows.append({"Student ID": sid, "Name": (names or {}).get(sid, ""), "Consistency": level,
                     "Mocks submitted": n, "Mocks held": len(held), "Submission rate %": round(rate, 1),
                     "On time": sum(1 for e in evs if e.get("on_time") == 1),
                     "Average score %": round(avg, 1), "Latest score %": round(scores[-1], 1), "Trend": trend,
                     "MCQ tests taken": len({int(m["test_no"]) for m in mcq if _sid(m["student_id"]) == sid}
                                            & tests_held),
                     "Improper": imp.get(sid, 0), "Mocks missed": ", ".join(c for c in held if c not in got),
                     "Last evaluated": max(str(e.get("evaluated_at") or "") for e in evs)[:16]})
    df = pd.DataFrame(rows, columns=COLS)
    rank = {lv: i for i, lv in enumerate(LEVELS)}
    return (df.assign(_r=df["Consistency"].map(rank))
            .sort_values(["_r", "Average score %"], ascending=[True, False]).drop(columns="_r").reset_index(drop=True))
