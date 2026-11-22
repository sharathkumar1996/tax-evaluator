"""Students fill the IRS forms in different PDF programs. Some do not store what was typed in the form's
boxes: they keep it only in the box's look (appearance) or only in the form's XFA data copy. Those entries
must still be read, and a blank copy of a form uploaded next to the filled one must not be marked."""
import io
import re

from pypdf import PdfReader, PdfWriter

from helpers import fill
from irs_reader import IRSReturn
from test_schedules import BLANK, LAYOUT

LC, _ = LAYOUT["SCH C"]
LS, _ = LAYOUT["SCH SE"]
C_VALS = {LC["1"]: "80,000", LC["7"]: "80000", LC["28"]: "28000", LC["31"]: "52000"}
SE_VALS = {LS["2"]: "52,000", LS["6"]: "48022", LS["12"]: "7348", LS["13"]: "3674"}


def _appearance_only(blank, vals):
    w = PdfWriter(clone_from=io.BytesIO(blank))
    for p in w.pages:
        full = {a.get_object()["/T"]: vals[re.sub(r"\[\d+\]$", "", str(a.get_object().get("/T", "")))]
                for a in p.get("/Annots", [])
                if re.sub(r"\[\d+\]$", "", str(a.get_object().get("/T", ""))) in vals}
        if full:
            w.update_page_form_field_values(p, full, auto_regenerate=False)
    for p in w.pages:                       # keep the drawn text, drop the stored values
        for a in p.get("/Annots", []):
            n = a.get_object()
            while n is not None:
                if "/V" in n and str(n.get("/FT", "/Tx")) == "/Tx":
                    del n["/V"]
                n = n.get("/Parent")
                n = n.get_object() if n is not None else None
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def _xfa_only(blank, vals):
    w = PdfWriter(clone_from=io.BytesIO(blank))
    xfa = w._root_object["/AcroForm"]["/XFA"]
    for i in range(0, len(xfa) - 1, 2):
        if str(xfa[i]) == "datasets":
            st = xfa[i + 1].get_object()
            x = st.get_data().decode()
            for s, v in vals.items():
                x = re.sub(rf"<{s}\s*/>|<{s}>\s*</{s}>", f"<{s}>{v}</{s}>", x, count=1)
            st.set_data(x.encode())
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def _no_box_values(data):
    f = PdfReader(io.BytesIO(data)).get_fields()
    return all(not str(x.get("/V") or "").strip() for x in f.values() if x.get("/FT") == "/Tx")


def _check(make):
    c, se = make(BLANK["SCH C"], C_VALS), make(BLANK["SCH SE"], SE_VALS)
    assert _no_box_values(c) and _no_box_values(se)
    r = IRSReturn([("S1_C1_SchC.pdf", c), ("S1_C1_SE.pdf", se)])
    assert r.value("SCH C", "1")[0] == 80000 and r.value("SCH C", "31")[0] == 52000
    assert r.value("SCH SE", "2")[0] == 52000 and r.value("SCH SE", "12")[0] == 7348
    assert r.value("SCH SE", "13")[0] == 3674
    assert any("not saved in the form's boxes" in n for n in r.notes)


def test_entries_kept_only_as_the_box_appearance_are_read():
    _check(_appearance_only)


def test_entries_kept_only_in_the_xfa_copy_are_read():
    _check(_xfa_only)


def test_xfa_copy_ignored_when_the_boxes_hold_values():
    d = fill(_xfa_only(BLANK["SCH C"], {LC["31"]: "99999"}), {LC["1"]: "80000"})   # stale XFA, real box values
    r = IRSReturn([("c.pdf", d)])
    assert r.value("SCH C", "31")[0] == 0 and r.value("SCH C", "1")[0] == 80000


def test_blank_copy_next_to_filled_copy_is_not_marked():
    filled = fill(BLANK["SCH C"], {LC["31"]: "52000"})
    r = IRSReturn([("S1_C1_A_SchC.pdf", BLANK["SCH C"]), ("S1_C1_B_SchC.pdf", filled)])
    assert r.forms_found() == ["SCH C"] and r.value("SCH C", "31")[0] == 52000
    assert any("blank copy of SCH C" in n for n in r.notes)


def test_two_filled_copies_are_both_kept():
    a, b = fill(BLANK["SCH C"], {LC["31"]: "52000"}), fill(BLANK["SCH C"], {LC["31"]: "9000"})
    r = IRSReturn([("S1_C1_A.pdf", a), ("S1_C1_B.pdf", b)])
    assert r.value("SCH C", "31")[0] == 52000 and r.value("SCH C #2", "31")[0] == 9000
