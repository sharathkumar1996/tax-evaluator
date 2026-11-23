"""Class attendance from the daily 80% report, and the learner tracker / Deloitte report built on it.
All names and emails here are made up; the layout copies the real daily report."""
import io
from datetime import date

import openpyxl
import pandas as pd

from attendance import (ABSENT, BELOW, FLAG_ALL, FLAG_IMPROPER, FLAG_LOW_ATT, FLAG_LOW_PERF, FLAG_MCQ, FLAG_MOCK,
                        PRESENT, alias_key, batch_key, batch_overview, class_summary, deloitte_workbook, find_date,
                        flag_list, match_rows, read_report, session_records, tracker)
from finalscore import SO_FAR, final_scores, load_settings

HEAD = ["S.No", "Batch", "Participant Name", "Deloitte Email ID", "Time in Session", "% of Session", "First Join",
        "Last Leave", "Times Joined", "Total Time in Meeting", "Remarks"]


def daily_report(day="18 Nov 2026", present=(), below=()):
    """A workbook laid out like Simandhar's daily 'Attendance-80pct' report."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Deloitte | Form 1040 Training: Attendance Report (80% Rule)"])
    ws.append([f"Session date: {day}. Teams attendance exports matched to the attendance form."])
    for title, people, pct in (("80% Attendance", present, 0.97), ("Below 80%", below, 0.31)):
        s = wb.create_sheet(title)
        s.append(HEAD)
        for i, (b, n, e) in enumerate(people, 1):
            s.append([i, b, n, e, 0.16, pct, 46344.37, 46344.54, 1, 0.16, "Matched to form by name"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ROSTER = [{"student_id": "164001", "name": "Ravi Kumar M", "batch": "Batch-1", "email": "ravim.ext@deloitte.com"},
          {"student_id": "164002", "name": "Asha K S", "batch": "Batch-1", "email": ""},
          {"student_id": "164003", "name": "Meena Rao", "batch": "Batch-1", "email": ""},
          {"student_id": "164004", "name": "Kiran P", "batch": "Batch-2", "email": "kiranp.ext@deloitte.com"},
          {"student_id": "164005", "name": "Zoya Ali", "batch": "Batch-2", "email": ""}]

REPORT = daily_report(present=[("Batch 1", "Ravi Kumar", "ravim.ext@deloitte.com"),       # by email
                               ("Batch 1", "k s asha", "asha.ks.ext@deloitte.com"),        # by name, any order
                               ("Batch 2", "Kiran P", "kiranp.ext@deloitte.com")],
                      below=[("Batch 2", "ZOYA ALI", ""),                                  # by name, no email
                             ("Batch 1", "Guest Trainer", "guest@simandhar.com")])         # not a learner


def test_reads_the_daily_report_and_its_date():
    d, rows = read_report(REPORT, "Deloitte-Form1040-Attendance-80pct-18Nov.xlsx")
    assert d == date(2026, 11, 18) and len(rows) == 5
    assert [r["status"] for r in rows].count(PRESENT) == 3 and rows[0]["pct"] == 97
    assert find_date("Deloitte-Form1040-Attendance-80pct-16Nov.xlsx", year=2026) == date(2026, 11, 16)
    assert find_date("Form 1040 Training conducted on 12th November 2026") == date(2026, 11, 12)
    assert batch_key("Batch 1") == batch_key("Batch-1") == batch_key("batch1")


def test_matches_by_email_then_name_and_marks_the_rest_absent():
    d, rows = read_report(REPORT, "x.xlsx")
    m = match_rows(rows, ROSTER)
    how = {r["name"]: (r["student_id"], r["matched_by"]) for r in m}
    assert how["Ravi Kumar"] == ("164001", "email") and how["k s asha"] == ("164002", "name")
    assert how["ZOYA ALI"] == ("164005", "name") and how["Guest Trainer"] == ("", "")
    recs, unmatched = session_records(d, m, ROSTER)
    st = {r["student_id"]: r["status"] for r in recs}
    assert st == {"164001": PRESENT, "164002": PRESENT, "164004": PRESENT, "164005": BELOW, "164003": ABSENT}
    assert [u["name"] for u in unmatched] == ["Guest Trainer"]


def test_a_manual_link_is_remembered():
    d, rows = read_report(REPORT, "x.xlsx")
    guest = next(r for r in rows if r["name"] == "Guest Trainer")
    m = match_rows(rows, ROSTER, aliases={alias_key(guest): "164003"})
    assert next(r for r in m if r["name"] == "Guest Trainer")["matched_by"] == "linked by hand"


def test_only_batches_in_the_report_get_absent_marks():
    one_batch = daily_report(present=[("Batch 2", "Kiran P", "kiranp.ext@deloitte.com")])
    d, rows = read_report(one_batch, "x.xlsx")
    recs, _ = session_records(d, match_rows(rows, ROSTER), ROSTER)
    assert {r["student_id"] for r in recs} == {"164004", "164005"}         # batch 1 had no class in this file


def _track():
    cfg = load_settings({})
    studs = [dict(s) for s in ROSTER]
    att = []
    for day, present, below in (("18 Nov", REPORT, None), ("19 Nov", daily_report(
            "19 Nov 2026", present=[("Batch 1", "Ravi Kumar", "ravim.ext@deloitte.com"),
                                    ("Batch 2", "Kiran P", "kiranp.ext@deloitte.com")]), None)):
        d, rows = read_report(present, "x.xlsx")
        att += session_records(d, match_rows(rows, studs), studs)[0]
    evs = [{"student_id": "164001", "case_id": "M01", "pct": 0.95, "on_time": 1},
           {"student_id": "164004", "case_id": "M01", "pct": 0.30, "on_time": 1}]
    mcq = [{"student_id": "164001", "test_no": 1, "marks": 45}, {"student_id": "164004", "test_no": 1, "marks": 40}]
    improper = pd.DataFrame([{"Student ID": "164002", "Name": "Asha K S", "Batch": "Batch-1", "Case": "M01",
                              "Problem": "Could not be evaluated", "Details": "Failed - unreadable", "File": "a.pdf",
                              "When": ""}])
    scores = final_scores(studs, evs, mcq, ["M01"], cfg, view=SO_FAR)
    return tracker(studs, att, evs, mcq, ["M01"], improper, scores, cfg), att, studs


def test_tracker_brings_classes_mocks_and_mcqs_together():
    t, att, studs = _track()
    r = t.set_index("Student ID")
    assert (r.loc["164001", "Classes held"], r.loc["164001", "Present (80%+)"], r.loc["164001", "Flags"]) == (2, 2, "")
    assert r.loc["164002", "Improper submissions"] == 1 and FLAG_IMPROPER in r.loc["164002", "Flags"]
    assert FLAG_MOCK not in r.loc["164002", "Flags"]                      # submitted (improperly), so not 'missing'
    assert FLAG_LOW_ATT in r.loc["164002", "Flags"] and r.loc["164002", "Attendance %"] == 50
    assert FLAG_ALL in r.loc["164003", "Flags"]                           # no class, no mock, no MCQ
    assert r.loc["164005", "Below 80%"] == 1 and r.loc["164005", "Attendance %"] == 0
    assert FLAG_MCQ in r.loc["164005", "Flags"] and FLAG_MOCK in r.loc["164005", "Flags"]
    assert FLAG_LOW_PERF in r.loc["164004", "Flags"]                      # 30% on the mock
    assert r.loc["164001", "Class 2026-11-18"] == PRESENT
    ov = batch_overview(t).set_index("Batch")
    assert ov.loc["All batches", "Learners"] == 5 and ov.loc["All batches", FLAG_ALL] == 1
    cs = class_summary(att, studs)
    assert cs[(cs["Date"] == "2026-11-18") & (cs["Batch"] == "Batch-1")]["Attendance %"].iloc[0] == 66.7
    assert list(flag_list(t, FLAG_IMPROPER)["Student ID"]) == ["164002"]


def test_deloitte_workbook_has_a_sheet_per_concern_and_shades_improper_submissions():
    t, att, studs = _track()
    wb = openpyxl.load_workbook(io.BytesIO(deloitte_workbook(t, class_summary(att, studs))))
    assert wb.sheetnames == ["Summary", "All learners", FLAG_LOW_ATT, FLAG_MOCK, FLAG_IMPROPER, FLAG_MCQ,
                             FLAG_LOW_PERF, FLAG_ALL, "Class-wise"]
    ws = wb["All learners"]
    assert "Simandhar" in ws["A1"].value and ws["A4"].value == "Student ID"
    shaded = {row[0].value for row in ws.iter_rows(min_row=5) if row[0].fill.fgColor.rgb.endswith("F7ECEF")}
    assert shaded == {"164002"}


def test_a_compiled_sheet_with_a_date_column_gives_one_class_per_date():
    from attendance import read_reports
    notes = pd.DataFrame({"Notes": ["Upload the Attendance sheet (Date, Batch, Student ID, Name, Email, Status)"]})
    att = pd.DataFrame({"Date": ["2026-11-12", "2026-11-12", "2026-11-16"], "Batch": ["Batch 1"] * 3,
                        "Student ID": ["", "", ""], "Name": ["Ravi Kumar", "Asha K S", "Ravi Kumar"],
                        "Email": ["ravim.ext@deloitte.com", "", "ravim.ext@deloitte.com"],
                        "Status": [PRESENT, ABSENT, BELOW], "% of Session": [97.0, None, 40.0]})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        notes.to_excel(xw, "Notes", index=False)                      # a note mentioning the headers is skipped
        att.to_excel(xw, "Attendance", index=False)
    res = read_reports(buf.getvalue(), "compiled.xlsx")
    assert [(d, [r["status"] for r in rows]) for d, rows in res] == [
        (date(2026, 11, 12), [PRESENT, ABSENT]), (date(2026, 11, 16), [BELOW])]
    recs, _ = session_records(res[0][0], match_rows(res[0][1], ROSTER), ROSTER)
    assert {r["student_id"]: r["status"] for r in recs if r["batch"] == "Batch-1"}["164001"] == PRESENT
    assert len(read_reports(REPORT, "daily.xlsx")) == 1                # the daily report still works
