"""Learners not on the roster: consistency rating for Deloitte."""
import pandas as pd

from offroster import CONSISTENT, IRREGULAR, LOW_SCORES, candidates, names_from_runs


def ev(sid, case, pct, on_time=1):
    return {"student_id": sid, "case_id": case, "pct": pct, "on_time": on_time, "evaluated_at": f"2026-11-2{case[-1]}"}


EVS = [ev("R1", "M01", .9), ev("R1", "M02", .9),                       # roster learner - ignored
       ev("X1", "M01", .8), ev("X1", "M02", .9), ev("X1", "M03", .95),  # every mock, good scores
       ev("X2", "M01", .5), ev("X2", "M02", .6, 0), ev("X2", "M03", .4),  # every mock, low scores
       ev("x3", "M01", .99),                                            # one of three
       ev("X4", "P01", .9)]                                             # practice case only - not tracked


def test_ratings_and_order():
    df = candidates(EVS, ["R1"], ["M01", "M02", "M03"], min_rate=80, min_avg=70)
    assert list(df["Student ID"]) == ["X1", "X2", "X3"]
    lv = df.set_index("Student ID")["Consistency"]
    assert lv["X1"] == CONSISTENT and lv["X2"] == LOW_SCORES and lv["X3"] == IRREGULAR
    x1 = df.set_index("Student ID").loc["X1"]
    assert x1["Mocks held"] == 3 and x1["Average score %"] == 88.3 and x1["Trend"] == "Improving"
    x2 = df.set_index("Student ID").loc["X2"]
    assert x2["On time"] == 2 and x2["Trend"] == "Falling"
    assert df.set_index("Student ID").loc["X3", "Mocks missed"] == "M02, M03"


def test_mock_held_only_once_evaluated_and_names_improper_mcq():
    imp = pd.DataFrame([{"Student ID": "X1", "Case": "M04"}, {"Student ID": "R1", "Case": "M04"}])
    names = names_from_runs([{"Student ID": "x1", "Name": "Priya"}, {"Student ID": "X1", "Name": "Old"}])
    mcq = [{"student_id": "X1", "test_no": 1, "marks": 40}, {"student_id": "R1", "test_no": 2, "marks": 30}]
    df = candidates(EVS, ["R1"], ["M01", "M02", "M03", "M04"], mcq=mcq, improper=imp, names=names).set_index(
        "Student ID")
    assert df.loc["X1", "Mocks held"] == 3                  # M04 has no evaluation yet
    assert df.loc["X1", "Name"] == "Priya" and df.loc["X1", "Improper"] == 1 and df.loc["X1", "MCQ tests taken"] == 1


def test_empty():
    assert candidates([], [], ["M01"]).empty
