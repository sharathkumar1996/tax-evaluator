"""Bulk upload grouping and zip safety limits."""
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
    assert [n for n, _ in skipped] == ["random.pdf"]
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
