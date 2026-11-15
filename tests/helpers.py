"""Test helpers: fill an IRS fillable PDF the way Adobe Reader does (value in /V, check box state in /AS)."""
import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, TextStringObject

FIX = Path(__file__).parent / "fixtures"
BLANK_1040 = FIX / "f1040_2025_blank.pdf"


def _field_of(a):
    node = a
    while node is not None and "/T" not in node:
        p = node.get("/Parent")
        node = p.get_object() if p is not None else None
    return node


def _short(a):
    import re
    names, node = [], a
    while node is not None:
        if "/T" in node:
            names.append(str(node["/T"]))
        p = node.get("/Parent")
        node = p.get_object() if p is not None else None
    return re.sub(r"\[\d+\]$", "", names[0]) if names else ""


def fill(blank: bytes, text=None, checks=None, flatten=False) -> bytes:
    """text: {short field name: value}; checks: {short field name: kid index to tick (0-based)}."""
    text, checks = text or {}, checks or {}
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(blank)))
    kid_no = {}
    for page in w.pages:
        for ref in page.get("/Annots", []):
            a = ref.get_object()
            if a.get("/Subtype") != "/Widget":
                continue
            s = _short(a)
            fld = _field_of(a)
            if s in text:
                fld[NameObject("/V")] = TextStringObject(str(text[s]))
            if s in checks:
                i = kid_no.get(s, 0)
                kid_no[s] = i + 1
                on = [k for k in a["/AP"]["/N"].keys() if k != "/Off"][0]
                if i == checks[s]:
                    a[NameObject("/AS")] = NameObject(on)
                    fld[NameObject("/V")] = NameObject(on)
                else:
                    a[NameObject("/AS")] = NameObject("/Off")
        if flatten and "/Annots" in page:
            del page["/Annots"]
    if flatten and "/AcroForm" in w._root_object:
        del w._root_object["/AcroForm"]
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# A complete sample: the trainer's correct 1040 for a married couple with one child.
SAMPLE_TEXT = {
    "f1_14": "James", "f1_15": "Bennett", "f1_16": "400000301", "f1_17": "Olivia", "f1_18": "Bennett",
    "f1_19": "400000302", "f1_20": "2208 Oak Ridge Drive", "f1_22": "Denver", "f1_23": "CO", "f1_24": "80205",
    "f1_31": "Ava", "f1_35": "Bennett", "f1_39": "400000203", "f1_43": "Daughter",
    "f1_47": "98,500", "f1_57": "98,500", "f1_59": "1,200", "f1_73": "99,700", "f1_75": "99,700",
    "f2_01": "99,700", "f2_02": "31,500", "f2_05": "31,500", "f2_06": "68,200", "f2_08": "7,612",
    "f2_10": "7,612", "f2_11": "2,200", "f2_13": "2,200", "f2_14": "5,412", "f2_16": "5,412",
    "f2_17": "8,000", "f2_20": "8,000", "f2_28": "0", "f2_29": "8,000", "f2_30": "2,588", "f2_31": "2,588",
    "f2_40": "Mechanical Engineer", "f2_42": "Dental Hygienist",
}
