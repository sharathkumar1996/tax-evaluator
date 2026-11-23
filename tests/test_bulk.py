"""Bulk upload grouping and zip safety limits."""
import pytest
import io
import zipfile

import bulk
from bulk import group_uploads


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in entries.items():
            z.writestr(n, d)
    return buf.getvalue()


def test_layouts_are_grouped_per_student():
    z = _zip({
        "batch/S1_C03/1040.pdf": b"%PDF a", "batch/S1_C03/SchC.pdf": b"%PDF b",      # folder per student
        "S2_C03_1040.pdf": b"%PDF c", "S2_C03_SchC.pdf": b"%PDF d",                    # prefix per file
        "S3_C03.pdf": b"%PDF e",                                                        # one file
        "__MACOSX/S1_C03/._1040.pdf": b"junk", "notes.txt": b"x", "random.pdf": b"%PDF z",
    })
    groups, skipped = group_uploads([("all.zip", z), ("s4_c03.pdf", b"%PDF f")])
    got = {(g.student_id, g.case_id): [n for n, _ in g.files] for g in groups}
    assert got == {
        ("S1", "C03"): ["batch/S1_C03/1040.pdf", "batch/S1_C03/SchC.pdf"],
        ("S2", "C03"): ["S2_C03_1040.pdf", "S2_C03_SchC.pdf"],
        ("S3", "C03"): ["S3_C03.pdf"],
        ("S4", "C03"): ["s4_c03.pdf"],
    }
    assert sorted(n for n, _ in skipped) == ["notes.txt", "random.pdf"]           # non-PDFs are reported too
    assert groups[0].load()[0][1] == b"%PDF a"          # read lazily, on demand


def test_bad_zip_is_skipped_not_fatal():
    groups, skipped = group_uploads([("bad.zip", b"not a zip"), ("S1_C03.pdf", b"%PDF")])
    assert len(groups) == 1 and skipped[0][0] == "bad.zip"


def test_zip_limits(monkeypatch):
    monkeypatch.setattr(bulk, "MAX_MEMBERS", 2)
    groups, skipped = group_uploads([("big.zip", _zip({"S1_C1.pdf": b"%PDF", "S2_C1.pdf": b"%PDF",
                                                          "S3_C1.pdf": b"%PDF"}))])
    assert not groups and "limit" in skipped[0][1]


def test_zip_bomb_is_refused():
    z = _zip({"S1_C1.pdf": b"0" * (5 * 1024 * 1024)})          # 5 MB of zeros compresses ~1000x
    groups, skipped = group_uploads([("bomb.zip", z)])
    assert not groups and "unsafe" in skipped[0][1]


# ---------------------------------------------------------------- OneDrive batch folders (mock returns)
R = "OneDrive_1_20-11-2026/Batch-1/"
ROSTER = ["164108", "164231", "164233", "164234", "164235", "164236", "164237", "164240", "164242", "164244",
          "164248", "164252", "164253", "164285"]


def _batch(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in entries.items():
            if n.endswith("/"):
                z.writestr(zipfile.ZipInfo(n), b"")
            else:
                z.writestr(n, d)
    return buf.getvalue()


def test_near_miss_folder_names_are_matched_and_flagged():
    inner = _zip({"164252_M01/1040.pdf": b"%PDF z"})
    z = _batch({
        R + "164285_M01/1040.pdf": b"%PDF a",            # correct
        R + "164108-M01/1040.pdf": b"%PDF b",            # hyphen
        R + "164231 _M01/1040.pdf": b"%PDF c",           # space
        R + "164233_M01 1/1040.pdf": b"%PDF d",          # OneDrive copy suffix
        R + "M01_164234/1040.pdf": b"%PDF e",            # reversed
        R + "164235/1040.pdf": b"%PDF f",                # no mock code
        R + "Ravi_164236_M01/1040.pdf": b"%PDF g",       # name prefix
        R + "164237_M1/1040.pdf": b"%PDF h",             # M1 = M01
        R + "Emp id164244.pdf": b"%PDF i",               # loose, badly named file
        R + "E164248_M01/1040.pdf": b"%PDF j",           # stray letter in the ID
        R + "164252_M01.zip": inner,                     # student zipped the folder
        R + "164240_M01/": b"",                          # empty folder
        R + "164242_M01/1040.jpg": b"\xff\xd8",          # picture instead of PDF
        R + "164253_M01/forms.zip": b"PK\x03\x04junk",   # broken zip
        R + "164247_M02/1040.pdf": b"%PDF k",            # another mock
    })
    groups, skipped = group_uploads([("batch.zip", z)], case_id="M01", roster_ids=ROSTER)
    got = {g.student_id: (g.case_id, bool(g.warnings)) for g in groups}
    assert got == {
        "164285": ("M01", False), "164108": ("M01", True), "164231": ("M01", True), "164233": ("M01", True),
        "164234": ("M01", True), "164235": ("M01", True), "164236": ("M01", True), "164237": ("M01", True),
        "164244": ("M01", True), "164248": ("M01", True), "164252": ("M01", False), "164247": ("M02", False),
    }
    why = {n.split("/")[-2]: w for n, w in skipped}
    assert set(why) == {"164240_M01", "164242_M01", "164253_M01"}
    assert "empty" in why["164240_M01"] and "1040.jpg" in why["164242_M01"] and "forms.zip" in why["164253_M01"]


def test_without_roster_long_numbers_are_ids_but_form_numbers_are_not():
    z = _batch({R + "164108-M01/1040.pdf": b"%PDF a", R + "1040.pdf": b"%PDF b", R + "Batch_1/Sch A.pdf": b"%PDF"})
    groups, skipped = group_uploads([("batch.zip", z)], case_id="M01")
    assert [g.student_id for g in groups] == ["164108"]
    assert len(skipped) == 2


def test_old_behaviour_without_case_is_unchanged():
    groups, _ = group_uploads([("x.zip", _zip({"164108-M01/1040.pdf": b"%PDF"}))])
    assert groups == []


def test_mock_codes_compare_by_number():
    assert bulk.same_case("M1", "M01") and bulk.same_case("m-01", "M01") and not bulk.same_case("M10", "M01")


ROSTER_NAMES = {"164746": "Ravi Kumar", "163316": "Emma Wilson", "165000": "Priya Sharma", "165001": "Sam Lee",
                "165002": "Ravi Teja"}


def _grp(names):
    return group_uploads([(n, b"%PDF-1.4") for n in names], case_id="M01", roster_ids=ROSTER_NAMES)


@pytest.mark.parametrize("path,sid", [
    ("B1/Ravi Kumar/f1040.pdf", "164746"),
    ("B1/Ravi_Kumar_M01/f1040.pdf", "164746"),
    ("B1/kumar ravi - mock 1/f1040.pdf", "164746"),
    ("B1/Emma Wilson.pdf", "163316"),
    ("B1/EmmaWilson_M01.pdf", "163316"),
    ("B1/Mock1 submissions/Priya/f1040.pdf", "165000"),
    ("B1/Sam/f1040.pdf", "165001"),
])
def test_folder_or_file_named_after_the_student_is_accepted_with_a_warning(path, sid):
    groups, skipped = _grp([path])
    assert skipped == [] and [(g.student_id, g.case_id) for g in groups] == [(sid, "M01")]
    assert any("has no student ID" in w and f"{sid}_M01" in w for w in groups[0].warnings)


def test_name_matching_more_than_one_student_is_skipped_with_the_reason():
    groups, skipped = _grp(["B1/RAVI/f1040.pdf"])
    assert groups == [] and "more than one student (164746, 165002)" in skipped[0][1]


def test_unknown_name_is_skipped_and_another_mock_is_kept_apart():
    groups, skipped = _grp(["B1/Unknown Person/f.pdf", "B1/Emma Wilson M02.pdf", "B1/Emma Wilson - mock 2.pdf"])
    assert "no student ID or roster name found" in skipped[0][1]
    assert {(g.student_id, g.case_id) for g in groups} == {("163316", "M02")}


def test_proper_names_still_win_and_carry_no_warning():
    groups, _ = _grp(["B1/164746_M01/f1040.pdf"])
    assert groups[0].student_id == "164746" and groups[0].warnings == []


@pytest.mark.parametrize("code,want", [("M01", "M01"), ("m01", "M01"), ("M1", "M01"), ("M-01", "M01"), ("MO1", "M01"),
                                       ("mO1", "M01"), ("M0l", "M01"), ("MI1", "M11"), ("Mock1", "M01"),
                                       ("MOCK 01", "M01"), ("MO2", "M02"), ("M12", "M12"), ("B1", None),
                                       ("Batch-1", None), ("MOL", None), ("164746", None)])
def test_mock_code_typing_slips_do_not_matter(code, want):
    from bulk import read_code
    assert read_code(code, "M01") == want


@pytest.mark.parametrize("path", ["164746_mO1/f.pdf", "164746_MO1/f.pdf", "164746_MO1 Batch-1/f.pdf",
                                  "164746 MO1 Batch-2/f.pdf", "164746-m01-batch 3/f.pdf", "164746_Mock 01 Batch-4/f.pdf",
                                  "163316_m0l/f.pdf".replace("163316", "164746"), "MO1_164746/f.pdf",
                                  "Batch-3/MO1/164746/f.pdf", "164746_B2/f.pdf", "Ravi Kumar MO1 Batch-2/f.pdf"])
def test_folder_with_identifiable_student_is_accepted_whatever_the_mock_code_spelling(path):
    groups, skipped = _grp([path])
    assert skipped == [] and [(g.student_id, g.case_id) for g in groups] == [("164746", "M01")]


def test_a_different_mock_is_still_kept_apart():
    groups, _ = _grp(["164746_MO2/f.pdf", "164746_Mock 2/f.pdf"])
    assert {g.case_id for g in groups} == {"M02"}
