"""Schedules 1, 2, 3, A, B, C, D, E, SE and Form 8949 on the IRS fillable forms: every line, table column, list row and
Yes/No question is read from the right box (regressions for mapping bugs found in Nov 2026 testing)."""
import collections

import pytest

from helpers import FIX, fill
from irs_reader import IRSReturn

FORMS = {"SCH 1": "f1040s1_2025_blank.pdf", "SCH 2": "f1040s2_2025_blank.pdf", "SCH 3": "f1040s3_2025_blank.pdf",
         "SCH A": "f1040sa_2025_blank.pdf", "SCH B": "f1040sb_2025_blank.pdf", "SCH C": "f1040sc_2025_blank.pdf",
         "SCH D": "f1040sd_2025_blank.pdf", "SCH E": "f1040se_2025_blank.pdf",
         "8949": "f8949_2025_blank.pdf", "SCH SE": "f1040sse_2026_blank.pdf"}
BLANK = {f: (FIX / p).read_bytes() for f, p in FORMS.items()}


def layout(form):
    c = IRSReturn([("x.pdf", BLANK[form])]).copies[(form, 1)]
    lines, choices, kid = {}, collections.defaultdict(dict), collections.Counter()
    for t, w in zip(c.templates, c.widgets):
        for k, wd in w.items():
            if wd.kind == "check":
                i = kid[wd.short]
                kid[wd.short] += 1
                if k in t.choice:
                    choices[t.choice[k][0]][t.choice[k][1]] = (wd.short, i)
            elif k in t.lines:
                lines[t.lines[k]] = wd.short
    return lines, dict(choices)


LAYOUT = {f: layout(f) for f in FORMS}


@pytest.mark.parametrize("form", FORMS)
def test_no_two_boxes_share_a_line(form):
    c = IRSReturn([("x.pdf", BLANK[form])]).copies[(form, 1)]
    names = [t.lines[k] for t, w in zip(c.templates, c.widgets) for k in w if k in t.lines]
    assert [n for n, m in collections.Counter(names).items() if m > 1] == []


@pytest.mark.parametrize("form,lines", [
    ("SCH 2", ["4", "1z", "7", "17a", "21"]),                                       # 4 was missed (sub-box digits)
    ("SCH B", ["1 (payer 1)", "1 (amount 7)", "1 (payer 14)", "2", "5 (payer 1)", "5 (amount 15)", "6"]),
    ("SCH C", ["A", "B", "C", "D", "E", "E city", "1", "16a", "16b", "31", "44a", "44b", "44c",
               "48 (description 1)", "48 (amount 9)", "48"]),
    ("SCH 1", ["1", "2a", "2b", "8z", "19a", "19b", "19c", "24z", "26"]),
    ("SCH D", ["1a (d)", "1a (h)", "1b (g)", "3 (e)", "8a (h)", "10 (d)", "7", "15", "16", "21"]),
    ("SCH E", ["1a A", "1b C", "2 A (fair rental days)", "3 (A)", "3 (B)", "3 (C)", "19 (description)",
               "20 (C)", "22 (B)", "23a", "28 A (a)", "28 D (k)", "29a (g)", "29b (k)", "33 B (b)", "34a (c)",
               "34b (f)", "38 (e)", "41"]),
    ("8949", ["I 1 row 1 (a)", "I 1 row 1 (h)", "I 1 row 11 (d)", "I 2 (d)", "I 2 (e)", "I 2 (g)", "I 2 (h)",
              "II 1 row 1 (a)", "II 1 row 11 (h)", "II 2 (h)", "name", "ssn"]),
    ("SCH SE", ["name", "ssn", "1a", "1b", "2", "3", "4a", "4b", "4c", "5a", "5b", "6", "8a", "8b", "8c", "8d",
                "9", "10", "11", "12", "13", "15", "16", "17"]),
])
def test_expected_lines_exist(form, lines):
    have = LAYOUT[form][0]
    assert [ln for ln in lines if ln not in have] == []


def test_schedule_b_seventh_payer_is_not_line_1():
    lines = LAYOUT["SCH B"][0]
    assert "1" not in lines and "5" not in lines                 # the bold '1' sits beside payer row 7
    r = IRSReturn([("b.pdf", fill(BLANK["SCH B"], {lines["1 (amount 7)"]: "250", lines["2"]: "250"}))])
    assert r.value("SCH B", "1 (amount 7)")[0] == 250 and r.value("SCH B", "2")[0] == 250


@pytest.mark.parametrize("form", FORMS)
def test_every_line_reads_back(form):
    lines, _ = LAYOUT[form]
    vals = {short: str(1000 + i) for i, short in enumerate(sorted(lines.values()))}
    r = IRSReturn([("x.pdf", fill(BLANK[form], vals))])
    wrong = [(ln, r.value(form, ln)[0]) for ln, short in lines.items() if r.value(form, ln)[0] != float(vals[short])]
    assert wrong == []
    assert r.unlinked() == []


@pytest.mark.parametrize("form,line,options", [
    ("SCH B", "7a", ["Yes", "No"]), ("SCH B", "8", ["Yes", "No"]), ("SCH C", "F", ["Cash", "Accrual", "Other"]),
    ("SCH C", "G", ["Yes", "No"]), ("SCH C", "47b", ["Yes", "No"]), ("SCH C", "34", ["Yes", "No"]), ("SCH D", "22", ["Yes", "No"]),
    ("SCH E", "A", ["Yes", "No"]), ("SCH E", "27", ["Yes", "No"]),
])
def test_questions_give_the_option_ticked(form, line, options):
    choices = LAYOUT[form][1]
    assert sorted(choices[line]) == sorted(options)
    for opt in options:
        short, i = choices[line][opt]
        r = IRSReturn([("x.pdf", fill(BLANK[form], {}, {short: i}))])
        assert r.value(form, line)[0] == opt
    assert IRSReturn([("x.pdf", BLANK[form])]).value(form, line)[0] == ""         # not answered


@pytest.mark.parametrize("form,line", [("SCH C", "H"), ("SCH C", "33a"), ("SCH C", "33b"), ("SCH C", "33c"), ("SCH E", "28 B (f)"), ("SCH E", "2 C (qjv)"),
                                       ("SCH A", "18"), ("SCH 1", "4 (4797)")])
def test_single_boxes(form, line):
    short, i = LAYOUT[form][1][line]["Checked"]
    assert IRSReturn([("x.pdf", fill(BLANK[form], {}, {short: i}))]).value(form, line)[0] == "Checked"
    assert IRSReturn([("x.pdf", BLANK[form])]).value(form, line)[0] == "Unchecked"


def test_2026_schedule_c_layout_also_reads():
    """Next year's Schedule C moved boxes around (address split, 44a-c printed as one bold label)."""
    blank = (FIX / "f1040sc_2026_blank.pdf").read_bytes()
    c = IRSReturn([("x.pdf", blank)]).copies[("SCH C", 1)]
    lines = {t.lines[k] for t, w in zip(c.templates, c.widgets) for k in w if k in t.lines}
    assert {"A", "E", "E apt", "E city", "E state", "E zip", "16c", "44a", "44b", "44c", "48 (amount 9)"} <= lines


def test_8949_is_one_form_of_two_pages():
    r = IRSReturn([("x.pdf", BLANK["8949"])])
    assert r.forms_found() == ["8949"]                            # not 'Form 2025' (the year beside the number)
    assert [p for _, p in r.page_map] == ["8949", "8949"]


def test_8949_boxes_and_rows():
    lines, choices = LAYOUT["8949"]
    assert sorted(o for o in choices["I box"]) == ["A", "B", "C", "G", "H", "I"]
    assert sorted(o for o in choices["II box"]) == ["D", "E", "F", "J", "K", "L"]
    d = fill(BLANK["8949"], {lines["I 1 row 1 (a)"]: "100 sh XYZ", lines["I 1 row 1 (b)"]: "01/15/2025",
                             lines["I 1 row 1 (d)"]: "5,000", lines["I 2 (h)"]: "1000",
                             lines["II 1 row 2 (h)"]: "(1,000)"},
             {choices["I box"]["C"][0]: choices["I box"]["C"][1], choices["II box"]["E"][0]: choices["II box"]["E"][1]})
    r = IRSReturn([("x.pdf", d)])
    assert r.value("8949", "I box")[0] == "C" and r.value("8949", "II box")[0] == "E"
    assert r.value("8949", "I 1 row 1 (a)")[0] == "100 sh XYZ"
    assert r.value("8949", "I 1 row 1 (b)")[0] == "01/15/2025"
    assert r.value("8949", "I 1 row 1 (d)")[0] == 5000
    assert r.value("8949", "I 2 (h)")[0] == 1000
    assert r.value("8949", "II 1 row 2 (h)")[0] == -1000
    assert r.unlinked() == []


def test_schedule_se_reads_a_filled_return():
    lines, choices = LAYOUT["SCH SE"]
    vals = {"name": "Jane Doe", "2": "52,000", "4a": "48022", "6": "48022", "8a": "30000", "10": "5955",
            "11": "1393", "12": "7348", "13": "3674"}
    d = fill(BLANK["SCH SE"], {lines[k]: v for k, v in vals.items()}, {choices["A"]["Checked"][0]: 0})
    r = IRSReturn([("se.pdf", d)])
    assert r.forms_found() == ["SCH SE"]
    assert r.value("SCH SE", "name")[0] == "Jane Doe"
    assert r.value("SCH SE", "A")[0] == "Checked"
    for k in ("2", "4a", "6", "8a", "10", "11", "12", "13"):
        assert r.value("SCH SE", k)[0] == float(vals[k].replace(",", ""))
    assert r.unlinked() == []


def test_8949_keys_made_before_calibration_still_work():
    """Old keys: Part II rows were '8949' line '1 (d)'; Part I was a form named after the year ('2025')."""
    lines, _ = LAYOUT["8949"]
    d = fill(BLANK["8949"], {lines["II 1 row 1 (d)"]: "2600", lines["II 1 row 1 (b)"]: "02/10/2024",
                             lines["I 1 row 3 (h)"]: "100"})
    r = IRSReturn([("x.pdf", d)])
    assert r.value("8949", "1 (d)")[0] == 2600
    assert r.value("8949", "1 (b)")[0] == "02/10/2024"
    assert r.value("2025", "3 (h)")[0] == 100
