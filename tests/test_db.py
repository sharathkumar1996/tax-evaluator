"""Database layer (SQLite stands in for Neon Postgres; the SQL is shared)."""
import sqlite3
import threading

import pytest

import db as dbmod
from db import DB


class _Key:
    case_id, title, tax_year, total, steps = "C03", "Case", 2025, 10.0, [1, 2]


ROWS = [
    {"Step": 1, "Category": "Income", "Form": "1040", "Line": "1a", "Description": "Wages", "Correct Value": 100,
     "Student Value": 90, "Marks": 5, "Awarded": 0, "Result": "Wrong", "Feedback": "check W-2", "Note": ""},
    {"Step": 2, "Category": "Income", "Form": "1040", "Line": "9", "Description": "Total", "Correct Value": 100,
     "Student Value": 100, "Marks": 5, "Awarded": 5, "Result": "Correct", "Feedback": "", "Note": ""},
]
M = {"score": 5, "out_of": 10, "pct": 0.5, "file_ready": False, "bottom_line": None, "errors": 1,
     "critical_errors": 0, "unexpected": 0, "needs_review": 0}


@pytest.fixture
def db(tmp_path):
    d = DB(f"sqlite:///{tmp_path / 't.db'}")
    yield d
    d.close()


def test_save_and_read_back(db):
    db.upsert_students([("S1", "Asha", "B1")])
    db.save_case(_Key(), b"key-bytes", "key.xlsx")
    db.save_evaluation("S1", "C03", "S1_C03.pdf", M, ROWS, "h", "staff", source_format="IRS fillable forms",
                       notes="n1")
    ev = db.evaluations(student_id="S1")
    assert len(ev) == 1 and ev[0]["source_format"] == "IRS fillable forms" and ev[0]["name"] == "Asha"
    assert [m["line"] for m in db.mistakes(sid="S1")] == ["1a"]          # correct items are not stored
    db.save_evaluation("S1", "C03", "again.pdf", M, ROWS[1:], "h", "staff")  # re-evaluation replaces
    assert db.mistakes(sid="S1") == [] and db.counts()["evaluations"] == 1


def test_save_is_all_or_nothing(db):
    db.save_evaluation("S1", "C03", "a.pdf", M, ROWS, "h", "staff")
    broken = [ROWS[0], dict(ROWS[0])]          # same step twice: the 2nd insert fails INSIDE the transaction,
    with pytest.raises(sqlite3.IntegrityError):  # after the evaluation row was already updated
        db.save_evaluation("S1", "C03", "b.pdf", dict(M, score=0), broken, "h", "staff")
    ev = db.evaluations()[0]
    assert ev["file_name"] == "a.pdf" and ev["score"] == 5                 # old result intact
    assert len(db.mistakes(sid="S1")) == 1


def test_upgrade_of_an_older_database(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    for s in dbmod.BASE_SCHEMA:                                            # what the 'database' branch created
        con.execute(s)
    con.execute("INSERT INTO evaluations (student_id, case_id, score) VALUES ('S9', 'C01', 7)")
    con.commit()
    con.close()
    d = DB(f"sqlite:///{path}")
    assert d.schema_version() == dbmod.SCHEMA_VERSION
    assert d.evaluations()[0]["score"] == 7 and "source_format" in d.evaluations()[0]
    DB(f"sqlite:///{path}").close()                                        # running the upgrade again is harmless
    d.close()


def test_parallel_saves(db):
    errors = []

    def work(i):
        try:
            for j in range(5):
                db.save_evaluation(f"S{i}", "C03", "f.pdf", M, ROWS, "h", "staff")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and db.counts()["evaluations"] == 8 and db.counts()["mistakes"] == 8


def test_erasing(db):
    db.upsert_students([("S1", "A", "B1"), ("S2", "B", "B2")])
    for s in ("S1", "S2", "X9"):
        db.save_evaluation(s, "C03", "f.pdf", M, ROWS, "h", "staff")
    db.erase_unlisted()
    assert {e["student_id"] for e in db.evaluations()} == {"S1", "S2"}
    db.erase_batch("B1")
    assert {e["student_id"] for e in db.evaluations()} == {"S2"} and db.student("S1") is None
    db.erase_all()
    assert db.counts() == {"students": 0, "cases": 0, "evaluations": 0, "mistakes": 0}
    assert db.health()
