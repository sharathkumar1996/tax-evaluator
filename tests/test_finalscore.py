"""Final course score: 60 accuracy (12 mock returns) + 20 on-time + 20 MCQ (4 tests x 50); pass 70."""
import pandas as pd
import pytest

from finalscore import (DEFAULTS, FINAL, SO_FAR, MCQError, check_settings, final_scores, load_settings,
                        parse_mcq)

S = load_settings({})
CASES = [f"M{i:02d}" for i in range(1, 13)]
STUDENTS = [{"student_id": "A", "name": "Asha", "batch": "B1"}, {"student_id": "B", "name": "Ben", "batch": "B1"},
            {"student_id": "C", "name": "Chen", "batch": "B2"}]


def ev(sid, case, pct, on_time=1):
    return {"student_id": sid, "case_id": case, "pct": pct, "on_time": on_time}


def row(df, sid):
    return df.set_index("Student ID").loc[sid]


def test_defaults_match_the_scheme():
    assert DEFAULTS == {"n_mocks": 12, "w_accuracy": 60, "w_on_time": 20, "w_mcq": 20, "n_mcq": 4,
                        "mcq_out_of": 50, "pass_mark": 70}
    assert check_settings(S) == []


def test_perfect_student_scores_100():
    evals = [ev("A", c, 1.0) for c in CASES]
    mcq = [{"student_id": "A", "test_no": t, "marks": 50} for t in range(1, 5)]
    r = row(final_scores(STUDENTS, evals, mcq, CASES, S), "A")
    assert (r["Accuracy / 60"], r["On-time / 20"], r["MCQ / 20"], r["Total / 100"], r["Result"]) == \
        (60, 20, 20, 100, "Pass")


def test_worked_example():
    # average 78% on all 12 returns, 10 on time and 2 late, MCQs 170/200 (85%)  ->  46.8 + 16.67 + 17 = 80.47
    evals = [ev("A", c, 0.78, on_time=int(i < 10)) for i, c in enumerate(CASES)]
    mcq = [{"student_id": "A", "test_no": t, "marks": m} for t, m in zip(range(1, 5), (40, 45, 42, 43))]
    r = row(final_scores(STUDENTS, evals, mcq, CASES, S), "A")
    assert r["Accuracy / 60"] == 46.8 and r["On-time / 20"] == 16.67 and r["MCQ / 20"] == 17
    assert r["Total / 100"] == 80.47 and r["Result"] == "Pass" and (r["On time"], r["Late"]) == (10, 2)


def test_late_gets_no_on_time_marks_and_missing_returns_count_zero():
    evals = [ev("A", c, 1.0, on_time=0) for c in CASES[:6]]           # 6 returns, all late; 6 never submitted
    r = row(final_scores(STUDENTS, evals, [], CASES, S), "A")
    assert r["Accuracy / 60"] == 30 and r["On-time / 20"] == 0 and r["Returns evaluated"] == "6 / 12"
    assert r["Total / 100"] == 30 and r["Result"] == "At risk"


def test_status_not_set_is_counted_and_reported():
    r = row(final_scores(STUDENTS, [ev("A", "M01", 1.0, on_time=None)], [], CASES, S), "A")
    assert r["On-time / 20"] == 0 and r["Status not set"] == 1


def test_only_course_cases_count_and_marks_are_capped():
    evals = [ev("A", "PRACTICE", 1.0), ev("A", "M01", 0.5)]
    mcq = [{"student_id": "A", "test_no": 1, "marks": 80}, {"student_id": "A", "test_no": 9, "marks": 50}]
    r = row(final_scores(STUDENTS, evals, mcq, CASES, S), "A")
    assert r["Returns evaluated"] == "1 / 12" and r["Average return score"] == 4.17
    assert r["MCQ marks"] == "50 / 200"                                    # 80 capped at 50; test 9 ignored


def test_pass_mark_boundary():
    evals = [ev("A", c, 0.5) for c in CASES]                              # 30 + 20
    mcq = [{"student_id": "A", "test_no": t, "marks": 50} for t in range(1, 5)]   # + 20 = 70 exactly
    assert row(final_scores(STUDENTS, evals, mcq, CASES, S), "A")["Result"] == "Pass"


def test_so_far_view_adds_fixed_marks_and_compares_with_what_was_possible():
    # batch B1 has done 2 returns and test 1. Asha: both on time, 90%; test 1 = 45/50
    evals = [ev("A", "M01", 0.9), ev("A", "M02", 0.9), ev("B", "M01", 0.4, on_time=0)]
    mcq = [{"student_id": "A", "test_no": 1, "marks": 45}]
    df = final_scores(STUDENTS, evals, mcq, CASES, S, view=SO_FAR)
    a, b, c = row(df, "A"), row(df, "B"), row(df, "C")
    # 5 marks a return x 0.9 x 2 = 9; 1.67 x 2 on time = 3.33; 5 marks a test x 45/50 = 4.5 -> 16.83, not scaled
    assert a["Returns evaluated"] == "2 / 2" and a["Accuracy / 60"] == 9 and a["On-time / 20"] == 3.33
    assert a["MCQ / 20"] == 4.5 and a["Total / 100"] == 16.83
    assert a["Marks possible so far"] == 18.33 and a["% of possible so far"] == 91.82 and "Result" not in a
    assert b["Returns evaluated"] == "1 / 2" and b["Not submitted"] == 1 and b["Total / 100"] == 2
    assert c["Returns evaluated"] == "0 / 0" and c["Marks possible so far"] == 0   # batch B2 has not started
    fin = row(final_scores(STUDENTS, evals, mcq, CASES, S, view=FINAL), "A")
    assert fin["Total / 100"] == 16.83 and fin["Result"] == "At risk"          # same marks in both views


def test_each_mock_and_on_time_submission_is_worth_a_fixed_share():
    # one mock at 100, on time -> 5 accuracy + 1.67 on-time; all 12 at 100 on time -> 60 + 20
    one = row(final_scores(STUDENTS, [ev("A", "M01", 1.0)], [], CASES, S), "A")
    assert one["Accuracy / 60"] == 5 and one["On-time / 20"] == 1.67 and one["Total / 100"] == 6.67
    all12 = row(final_scores(STUDENTS, [ev("A", c, 1.0) for c in CASES], [], CASES, S), "A")
    assert all12["Accuracy / 60"] == 60 and all12["On-time / 20"] == 20 and all12["Total / 100"] == 80
    half = row(final_scores(STUDENTS, [ev("A", "M01", 0.5, on_time=0)], [], CASES, S), "A")
    assert half["Accuracy / 60"] == 2.5 and half["On-time / 20"] == 0


def test_settings_are_read_and_checked():
    s = load_settings({"pass_mark": "65", "w_mcq": "x", "unknown": "1"})
    assert s["pass_mark"] == 65 and s["w_mcq"] == 20
    assert "add up to 110" in check_settings(dict(S, w_accuracy=70))[0]


def test_mcq_sheet_with_test_columns():
    df = pd.DataFrame({"Student ID": ["163316", 164746.0, None], "Name": ["a", "b", "c"],
                       "Test 1": [45, 38, 10], "Test 2": [None, "40", 5], "MCQ 3": ["", "-", 1]})
    assert parse_mcq(df, S) == [("163316", 1, 45.0), ("164746", 1, 38.0), ("164746", 2, 40.0)]


def test_mcq_single_marks_column_needs_a_test_number():
    df = pd.DataFrame({"Emp ID": ["163316"], "Marks": [44]})
    with pytest.raises(MCQError, match="choose which test"):
        parse_mcq(df, S)
    assert parse_mcq(df, S, test_no=3) == [("163316", 3, 44.0)]


def test_mcq_bad_cells_are_reported():
    df = pd.DataFrame({"Student ID": ["1", "2"], "Test 1": [55, "abc"]})
    with pytest.raises(MCQError, match="outside 0-50.*not a number"):
        parse_mcq(df, S)
    with pytest.raises(MCQError, match="outside 1-4"):
        parse_mcq(pd.DataFrame({"Student ID": ["1"], "Test 5": [1]}), S)
