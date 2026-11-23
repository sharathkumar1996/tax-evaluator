"""Batch-wise analysis for the trainers' consolidated reports."""
import pandas as pd

from batchreport import (ALL, batch_summary, integrity_for, missing_returns, mock_table, mock_trend, prepare,
                         score_bands, top_mistakes)
from finalscore import SO_FAR, final_scores, load_settings

S = load_settings({})
FC = ["M01", "M02", "M03"]
STUDENTS = [{"student_id": "A1", "name": "Asha", "batch": "B1"}, {"student_id": "A2", "name": "Ben", "batch": "B1"},
            {"student_id": "C1", "name": "Chen", "batch": "B2"}, {"student_id": "U1", "name": "Uma", "batch": ""}]
EVALS = [
    {"student_id": "A1", "case_id": "M01", "pct": 0.9, "on_time": 1},
    {"student_id": "A1", "case_id": "M02", "pct": 0.8, "on_time": 0},
    {"student_id": "A2", "case_id": "M01", "pct": 0.4, "on_time": 1},
    {"student_id": "C1", "case_id": "M01", "pct": 0.7, "on_time": None},
    {"student_id": "C1", "case_id": "PRAC", "pct": 1.0, "on_time": 1},           # not a mock return
    {"student_id": "ZZ", "case_id": "M01", "pct": 1.0, "on_time": 1},            # not on the roster
]
MISTAKES = [
    {"student_id": "A1", "case_id": "M01", "form": "1040", "line": "11", "description": "AGI", "marks": 2,
     "awarded": 0, "result": "Wrong"},
    {"student_id": "A2", "case_id": "M01", "form": "1040", "line": "11", "description": "AGI", "marks": 2,
     "awarded": 0, "result": "Wrong"},
    {"student_id": "A2", "case_id": "M01", "form": "SCH C", "line": "31", "description": "Net profit", "marks": 3,
     "awarded": 0, "result": "Wrong"},
    {"student_id": "C1", "case_id": "M01", "form": "1040", "line": "11", "description": "AGI", "marks": 2,
     "awarded": 2, "result": "Correct"},
]


def data():
    return prepare(STUDENTS, EVALS, FC)


def test_prepare_keeps_mock_returns_of_roster_students_and_names_unassigned():
    studs, evs = data()
    assert [s["batch"] for s in studs] == ["B1", "B1", "B2", "Unassigned"]
    assert sorted((e["student_id"], e["case_id"]) for e in evs) == [("A1", "M01"), ("A1", "M02"), ("A2", "M01"),
                                                                    ("C1", "M01")]


def test_batch_summary():
    studs, evs = data()
    df = batch_summary(studs, evs, [{"student_id": "A1", "test_no": 1, "marks": 40}], FC, S,
                       trainers={"B1": {"trainer": "Ravi"}}).set_index("Batch")
    b1 = df.loc["B1"]
    # B1 has done M01 and M02: 2 students x 2 mocks = 4 expected, 3 received
    assert (b1["Trainer"], b1["Students"], b1["Mocks done"], b1["Returns received"], b1["Returns expected"]) == \
        ("Ravi", 2, 2, 3, 4)
    assert b1["Submission rate %"] == 75.0 and b1["On-time rate %"] == 66.7 and b1["Average score %"] == 70.0
    assert b1["MCQ average %"] == 80.0
    b2 = df.loc["B2"]
    assert b2["On-time rate %"] is None or pd.isna(b2["On-time rate %"])     # only status not set
    assert b2["Status not set"] == 1
    assert df.loc["Unassigned"]["Mocks done"] == 0
    assert df.loc[ALL]["Students"] == 4 and df.loc[ALL]["Returns received"] == 4


def test_mock_table_and_trend():
    studs, evs = data()
    mt = mock_table(studs, evs, FC, {"M01": {"title": "Wages"}}, "B1").set_index("Case")
    assert list(mt.index) == ["M01", "M02"]                                   # M03 not done yet
    m1 = mt.loc["M01"]
    assert (m1["Title"], m1["Received"], m1["Students"], m1["Average %"], m1["Lowest %"], m1["Below 50%"]) == \
        ("Wages", 2, 2, 65.0, 40.0, 1)
    tr = mock_trend(studs, evs, FC, ["B1", "B2"])
    assert tr.to_dict("records")[0] == {"Batch": "B1", "Case": "M01", "Average %": 65.0, "Returns": 2}


def test_top_mistakes_counts_students_per_line():
    studs, evs = data()
    tm = top_mistakes(MISTAKES, evs, "B1")
    top = tm.iloc[0]
    assert (top["Form"], top["Line"], top["Students wrong"], top["% of returns"], top["Marks lost (total)"]) == \
        ("1040", "11", 2, 100.0, 4.0)
    assert len(tm) == 2
    assert top_mistakes(MISTAKES, evs, "B2").empty                            # C1's line was correct


def test_missing_returns_only_for_mocks_the_batch_has_done():
    studs, evs = data()
    mr = missing_returns(studs, evs)
    assert mr.to_dict("records") == [{"Student ID": "A2", "Name": "Ben", "Batch": "B1", "Missing": 1,
                                      "Mock returns not received": "M02"}]


def test_score_bands_skip_students_not_started():
    studs, evs = data()
    sc = final_scores(studs, evs, [], FC, S, view=SO_FAR)
    bands = score_bands(sc).set_index("Score band")["Students"]
    assert bands.sum() == 3                                                    # Uma's batch has not started


def test_integrity_for_one_batch():
    res = {"students": pd.DataFrame([
        {"Student ID": "A1", "Batch": "B1", "Integrity": "Likely copied"},
        {"Student ID": "A2", "Batch": "B1", "Integrity": "No concerns"},
        {"Student ID": "C1", "Batch": "B2", "Integrity": "Review"}]),
        "pairs": pd.DataFrame([{"Student A": "A1", "Student B": "C1", "Level": "Likely copied"}]),
        "trainer": pd.DataFrame([{"Student": "C1", "Level": "Review"}])}
    b1 = integrity_for(res, "B1")
    assert list(b1["students"]["Student ID"]) == ["A1"] and len(b1["pairs"]) == 1 and b1["trainer"].empty
    b2 = integrity_for(res, "B2")
    assert len(b2["pairs"]) == 1 and len(b2["trainer"]) == 1
    assert integrity_for(None, "B1")["students"].empty


# ------------------------------------------------------------------ not received / improper submissions
from batchreport import case_submissions, improper_submissions  # noqa: E402

RUN_ROWS = [   # newest run first, as db.run_rows returns them
    {"case_id": "M02", "run_at": "2026-11-20 10:00", "Student ID": "A2", "File": "A2_M02.pdf",
     "Status": "Failed - page 1 of Form 1040 could not be read"},
    {"case_id": "M02", "run_at": "2026-11-20 10:00", "Student ID": "C1", "File": "C1/",
     "Status": "Skipped - student C1's folder has no PDF files: scan.jpg"},
    {"case_id": "M02", "run_at": "2026-11-20 10:00", "Student ID": "", "File": "random.pdf",
     "Status": "Skipped - no student ID in the name"},
    {"case_id": "M01", "run_at": "2026-11-19 10:00", "Student ID": "A2", "File": "ben m1.pdf",
     "Status": "Evaluated - CHECK NAME: matched by name"},
    {"case_id": "M01", "run_at": "2026-11-18 10:00", "Student ID": "A1", "File": "A1_M01.pdf",
     "Status": "Failed - unreadable"},                                         # evaluated later: put right
    {"case_id": "M02", "run_at": "2026-11-18 10:00", "Student ID": "A2", "File": "old.pdf",
     "Status": "Evaluated"},                                                   # older than the failure above
    {"case_id": "M01", "run_at": "2026-11-18 10:00", "Student ID": "A2", "File": "x.pdf",
     "Status": "Skipped - already evaluated"},
]


def test_improper_submissions_lists_failed_no_pdf_misnamed_and_unidentified():
    studs, evs = data()
    imp = improper_submissions(studs, evs, RUN_ROWS)
    got = {(r["Student ID"], r["Case"], r["Problem"]) for _, r in imp.iterrows()}
    assert got == {("A2", "M02", "Could not be evaluated"), ("C1", "M02", "No PDF / wrong file type"),
                   ("", "M02", "Student not identified"), ("A2", "M01", "Named incorrectly (evaluated)")}
    b1 = improper_submissions(studs, evs, RUN_ROWS, batch="B1")
    assert set(b1["Student ID"]) == {"A2"}                                   # unidentified files only for all batches


def test_case_submissions_splits_not_received_from_improper():
    studs, evs = data()
    nr, imp = case_submissions(studs, evs, RUN_ROWS, "M02")
    assert list(nr["Student ID"]) == ["U1"]          # A1 evaluated; A2 and C1 submitted improperly
    assert set(imp["Student ID"]) == {"A2", "C1", ""}
    nr3, imp3 = case_submissions(studs, evs, RUN_ROWS, "M03", batch="B1")
    assert list(nr3["Student ID"]) == ["A1", "A2"] and imp3.empty


def test_missing_returns_leaves_out_improper_submissions():
    studs, evs = data()
    imp = improper_submissions(studs, evs, RUN_ROWS)
    mr = missing_returns(studs, evs, "B1", improper=imp)
    assert mr.empty                                   # A2's M02 is improper, not missing
    assert list(missing_returns(studs, evs, "B1")["Student ID"]) == ["A2"]
