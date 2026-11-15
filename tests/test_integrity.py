"""Copying scenarios, end to end: mark returns, save them with fingerprints, run the integrity check."""
import pytest

from db import DB
from grader_core import grade, load_key
from helpers import BLANK_1040, SAMPLE_TEXT, fill
from integrity import TRAINER, analyse, fingerprints
from make_keys import draft_from_return
from metrics import flag, return_metrics
from readers import read_return

BLANK = BLANK_1040.read_bytes()
CHECKS = {"c1_8": 1, "c1_28": 0, "c1_10": 1}


def own(**changes):
    """An independent student's return: the correct answers plus their own changes."""
    return fill(BLANK, dict(SAMPLE_TEXT, **changes), CHECKS)


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    db = DB(f"sqlite:///{tmp_path_factory.mktemp('i') / 'i.db'}")
    trainer = fill(BLANK, SAMPLE_TEXT, CHECKS)
    data, _, _ = draft_from_return([("trainer.pdf", trainer)], "C03", "Case", 2025, 1, 1)
    key = load_key(data)
    db.save_fingerprints(TRAINER, "C03", fingerprints([("trainer.pdf", trainer)], read_return([("trainer.pdf", trainer)])))
    db.upsert_students([(f"S{i}", f"Student {i}", "B1" if i <= 6 else "B2") for i in range(1, 15)])

    s1 = own(f2_06="68,900", f2_08="7,700", f1_59="1,250")                    # S1's own rare mistakes
    files = {
        "S1": [("S1_C03.pdf", s1)],
        "S2": [("S2_C03.pdf", s1)],                                           # same file, different ID
        "S3": [("S3_C03.pdf", fill(s1, {"f1_14": "Jim"}, incremental=True))],   # opened S1's file and re-saved
        "S4": [("S4_C03.pdf", own(f2_10="7,999", f2_14="5,799", f1_73="99,750"))],   # S4 and S5 share 3 rare
        "S5": [("S5_C03.pdf", own(f2_10="7999", f2_14="5,799", f1_73="99,750"))],    # wrong answers (own files)
        "S6": [("S6_C03.pdf", fill(trainer, {"f1_14": "James A"}, incremental=True))],  # built on trainer's file
    }
    for i in range(7, 15):                                                      # honest students; S7-S14 all
        files[f"S{i}"] = [(f"S{i}_C03.pdf", own(f2_02="29,200", **{"f1_47": f"98,5{i:02d}"}))]  # share 1 common slip
    for sid, fl in files.items():
        ret = read_return(fl)
        rows = flag(grade(ret, key))
        db.save_evaluation(sid, "C03", fl[0][0], return_metrics(rows), rows, "h", "test",
                           source_format=ret.format_name, fingerprints=fingerprints(fl, ret))
    res = analyse(db.evaluations(), db.fingerprints(["C03"]), db.mistakes(), db.students())
    db.close()
    return res


def pair(res, a, b):
    p = res["pairs"]
    m = p[((p["Student A"] == a) & (p["Student B"] == b)) | ((p["Student A"] == b) & (p["Student B"] == a))]
    return None if m.empty else m.iloc[0]


def test_same_file_under_another_id(result):
    p = pair(result, "S1", "S2")
    assert p is not None and p["Level"] == "Very likely copied" and "Exactly the same PDF file" in p["Evidence"]


def test_file_saved_over_another_students_work(result):
    p = pair(result, "S1", "S3")
    assert p is not None and p["Level"] == "Very likely copied" and "saved over" in p["Evidence"]


def test_shared_rare_wrong_answers(result):
    p = pair(result, "S4", "S5")
    assert p is not None and p["Score"] >= 60 and "3 identical wrong entries" in p["Evidence"]


def test_trainer_file(result):
    t = result["trainer"]
    assert list(t["Student"]) == ["S6"] and "trainer" in t.iloc[0]["Evidence"]


def test_common_mistake_and_template_values_are_not_evidence(result):
    honest = {f"S{i}" for i in range(7, 15)}
    p = result["pairs"]
    assert not any(a in honest and b in honest for a, b in zip(p["Student A"], p["Student B"]))
    st = result["students"].set_index("Student ID")
    assert (st.loc[sorted(honest), "Integrity"] == "No concerns").all()


def test_groups_and_batch_summary(result):
    g = result["groups"]
    assert any(set(x.split(", ")) == {"S1", "S2", "S3"} for x in g["Student IDs"])
    b = result["batches"].set_index("Batch")
    assert b.loc["B1", "Very likely copied"] >= 3 and b.loc["B2", "Very likely copied"] == 0
    assert b.loc["ALL BATCHES", "Students"] == 14


def test_id_checks():
    evals = [{"student_id": "S1", "case_id": "C1"}, {"student_id": "X9", "case_id": "C1"}]
    roster = [{"student_id": "S1", "name": "Asha Rao"}, {"student_id": "S2", "name": "asha  rao"}]
    out = analyse(evals, [], [], roster)["id_checks"]
    assert set(out["Check"]) == {"Evaluated ID not on the roster", "Same name under several IDs"}


def test_reports_build(result):
    from integrity_report import integrity_excel, integrity_pdf
    assert integrity_pdf("Simandhar Education", "all", result)[:4] == b"%PDF"
    assert integrity_excel("all", result)[:2] == b"PK"
