"""End to end: trainer's correct fillable return -> answer key -> mark a student's return -> report."""
import pytest

from grader_core import grade, load_key, summarise
from helpers import BLANK_1040, SAMPLE_TEXT, fill
from make_keys import draft_from_return
from metrics import flag, return_metrics
from readers import read_return
from report_pdf import build_report

BLANK = BLANK_1040.read_bytes()
CHECKS = {"c1_8": 1, "c1_28": 0, "c1_10": 1}


@pytest.fixture(scope="module")
def key():
    correct = fill(BLANK, SAMPLE_TEXT, CHECKS)
    data, n, forms = draft_from_return([("correct_1040.pdf", correct)], "C03", "Bennett family", 2025, 1, 1)
    assert forms == ["1040"] and n > 30
    return load_key(data)


def mark(files, key):
    ret = read_return(files)
    rows = flag(grade(ret, key))
    return rows, return_metrics(rows)


def test_correct_return_scores_full_marks(key):
    rows, m = mark([("S1_C03.pdf", fill(BLANK, SAMPLE_TEXT, CHECKS))], key)
    assert m["score"] == m["out_of"] == key.total
    assert m["file_ready"] and m["bottom_line"] and m["errors"] == 0


def test_mistakes_are_found(key):
    wrong = dict(SAMPLE_TEXT, f2_31="2,600", f2_30="2,600", f1_16="400000310")   # refund + SSN typo
    wrong["f1_61"] = "350"                                                         # line 3b should be blank
    rows, m = mark([("S2_C03.pdf", fill(BLANK, wrong, {"c1_8": 0, "c1_28": 0, "c1_10": 1}))], key)  # Single
    bad = {(r["Line"], r["Result"]) for r in rows if r["Result"] != "Correct"}
    assert ("FS", "Wrong") in bad
    assert ("35a", "Wrong") in bad and ("34", "Wrong") in bad
    assert ("ssn", "Wrong") in bad
    assert ("3b", "Unexpected entry") in bad
    assert not m["file_ready"] and m["bottom_line"] is False
    assert m["score"] < key.total


def test_missing_form_and_report(key):
    rows, m = mark([("S3_C03.pdf", fill(BLANK, {}))], key)
    assert m["score"] == 0 or m["score"] < key.total * 0.2
    cats, total, out_of = summarise(rows)
    pdf = build_report("Test Student", "S3", key, rows, cats, total, out_of, "S3_C03.pdf", "Simandhar Education")
    assert pdf[:4] == b"%PDF" and len(pdf) > 2000
