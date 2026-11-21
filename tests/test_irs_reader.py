"""IRS fillable-form reader: every kind of box on the 2025 Form 1040."""
import pytest

from helpers import BLANK_1040, SAMPLE_TEXT, fill
from irs_reader import IRSReturn, ReaderError
from readers import IRS_NAME, read_return

BLANK = BLANK_1040.read_bytes()
CHECKS = {"c1_8": 1, "c1_28": 0, "c1_10": 1, "c2_6": 0}      # MFJ, dep 1 CTC, digital assets No, you blind


@pytest.fixture(scope="module")
def ret():
    return IRSReturn([("S0001_C03_1040.pdf", fill(BLANK, SAMPLE_TEXT, CHECKS))])


def test_blank_form_reads_nothing():
    r = IRSReturn([("blank.pdf", BLANK)])
    assert r.forms_found() == ["1040"]
    assert r.all_fields() == []
    assert r.value("1040", "11a") == (0.0, "")


def test_every_amount_line_is_linked():
    r = IRSReturn([("blank.pdf", BLANK)])
    c = r.copies[("1040", 1)]
    lines = {t.lines[k] for t, w in zip(c.templates, c.widgets) for k in w if k in t.lines}
    expected = {"1a", "1b", "1c", "1d", "1e", "1f", "1g", "1h", "1i", "1z", "2a", "2b", "3a", "3b", "4a", "4b",
                "5a", "5b", "6a", "6b", "7a", "8", "9", "10", "11a", "11b", "12e", "13a", "13b", "14", "15", "16",
                "17", "18", "19", "20", "21", "22", "23", "24", "25a", "25b", "25c", "25d", "26", "27a", "28", "29",
                "30", "31", "32", "33", "34", "35a", "36", "37", "38"}
    assert expected <= lines
    assert "3" not in lines            # numbered sub-boxes ("1 Rollover 2 QCD 3") are not lines


@pytest.mark.parametrize("line,value", [
    ("1a", 98500), ("1z", 98500), ("2b", 1200), ("9", 99700), ("11a", 99700), ("11b", 99700), ("12e", 31500),
    ("15", 68200), ("16", 7612), ("22", 5412), ("24", 5412), ("25a", 8000), ("25d", 8000), ("33", 8000),
    ("34", 2588), ("35a", 2588), ("37", 0), ("4a", 0),
])
def test_amounts(ret, line, value):
    assert ret.value("1040", line) == (float(value), "")


@pytest.mark.parametrize("line,value", [
    ("FS", "MFJ"), ("first_name", "James"), ("last_name", "Bennett"), ("ssn", "400000301"),
    ("spouse_first_name", "Olivia"), ("spouse_last_name", "Bennett"), ("spouse_ssn", "400000302"),
    ("address", "2208 Oak Ridge Drive"), ("city", "Denver"), ("state", "CO"), ("zip", "80205"),
    ("occupation", "Mechanical Engineer"), ("spouse_occupation", "Dental Hygienist"),
    ("virtual_currency", "No"), ("you_blind", "Checked"), ("spouse_blind", "Unchecked"),
    ("dep1_name", "Ava Bennett"), ("dep1_ssn", "400000203"), ("dep1_relationship", "Daughter"),
    ("dep1_ctc", "Checked"), ("dep1_odc", "Unchecked"), ("dep2_name", ""), ("dep2_ctc", "Unchecked"),
    ("text: Your first name", "James"), ("check: Head of household", "Unchecked"), ("field: f1_47", 98500.0),
])
def test_named_boxes(ret, line, value):
    assert ret.value("1040", line)[0] == value


def test_unknown_items(ret):
    assert ret.value("1040", "99")[0] is None
    assert ret.value("SCH C", "1") == (None, "SCH C not found in the return")


def test_number_formats():
    for typed, want in [("1,234", 1234), ("$1,234.00", 1234), ("(500)", -500), ("-500", -500), (" 75 ", 75)]:
        r = IRSReturn([("x.pdf", fill(BLANK, {"f1_47": typed}))])
        assert r.value("1040", "1a")[0] == want, typed


def test_single_filing_status():
    r = IRSReturn([("x.pdf", fill(BLANK, {}, {"c1_8": 0}))])
    assert r.value("1040", "FS")[0] == "S"


def test_flattened_pdf_is_rejected_with_advice():
    flat = fill(BLANK, SAMPLE_TEXT, CHECKS, flatten=True)
    for reader in (IRSReturn, read_return):                   # no boxes left -> refused, with advice
        with pytest.raises(ReaderError, match="fillable"):
            reader([("x.pdf", flat)])


def test_duplicate_upload_is_ignored():
    d = fill(BLANK, SAMPLE_TEXT, CHECKS)
    r = read_return([("a.pdf", d), ("b.pdf", d)])
    assert r.format_name == IRS_NAME
    assert r.forms_found() == ["1040"]
    assert any("identical" in n for n in r.notes)


def test_second_copy_of_a_form_is_numbered():
    a = fill(BLANK, {"f1_47": "100"})
    b = fill(BLANK, {"f1_47": "200"})
    r = read_return([("a.pdf", a), ("b.pdf", b)])
    assert r.forms_found() == ["1040", "1040 #2"]
    assert r.value("1040 #2", "1a")[0] == 200


def test_not_a_pdf():
    with pytest.raises(ReaderError, match="not a PDF"):
        read_return([("x.pdf", b"hello")])


def test_template_cache_gives_same_result():
    d = fill(BLANK, SAMPLE_TEXT, CHECKS)
    a = IRSReturn([("a.pdf", d)]).all_fields()
    b = IRSReturn([("a.pdf", d)]).all_fields()          # second read uses the cached form layout
    assert a == b


def test_printed_copy_does_not_replace_the_fillable_copy():
    good = fill(BLANK, SAMPLE_TEXT, CHECKS)
    printed = fill(BLANK, SAMPLE_TEXT, CHECKS, flatten=True)
    r = read_return([("a_1040_printed.pdf", printed), ("z_1040.pdf", good)])     # printed one sorts first
    assert r.forms_found() == ["1040"]
    assert r.value("1040", "1a")[0] == 98500
    assert any("not marked" in n for n in r.notes)


def test_printed_pdfs_are_rejected():
    printed = fill(BLANK, SAMPLE_TEXT, CHECKS, flatten=True)
    with pytest.raises(ReaderError, match="fillable"):
        read_return([("x.pdf", printed)])


def test_unsaved_blank_form_is_flagged():
    r = read_return([("x.pdf", BLANK)])
    assert any("not saved" in n for n in r.notes)


def test_damaged_pdf_message():
    with pytest.raises(ReaderError, match="damaged"):
        read_return([("folder/1040.pdf", b"%PDF-1.7 broken")])
