"""Schedules 1, 2, 3, A, B, C, D, E on the IRS fillable forms: every line, table column, list row and
Yes/No question is read from the right box (regressions for mapping bugs found in Nov 2026 testing)."""
import collections

import pytest

from helpers import FIX, fill
from irs_reader import IRSReturn

FORMS = {"SCH 1": "f1040s1_2025_blank.pdf", "SCH 2": "f1040s2_2025_blank.pdf", "SCH 3": "f1040s3_2025_blank.pdf",
         "SCH A": "f1040sa_2025_blank.pdf", "SCH B": "f1040sb_2025_blank.pdf", "SCH C": "f1040sc_2025_blank.pdf",
         "SCH D": "f1040sd_2025_blank.pdf", "SCH E": "f1040se_2025_blank.pdf"}
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
