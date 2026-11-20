"""
irs_reader.py - reads a return made on the IRS's own fillable PDF forms (Form 1040, its
schedules and other forms filled in Adobe Reader or a browser and SAVED, not printed).

Why this is reliable
--------------------
A fillable form stores every entry in a named form field (e.g. ``f1_47``), so the value is
exactly what the student typed - no text recognition and no dependence on fonts or on how the
entry looks on screen. The reader only has to know which field is which line, and it works
that out from the form itself:

* a box that sits immediately right of a bold line number ("1a", "11a", "25d") on the same
  row is that line's entry; on multi-column rows the column letter printed above the row
  ("(d)", "(h)") is added, e.g. "1a (h)";
* a text box's label is the printed text just above it (or just left of it);
* a check box's label is the printed text just right of it.

This mapping depends only on the blank form, never on what the student typed, so it is the
same for every student. It is worked out once per form page and then cached, which also
makes bulk evaluation fast (later returns need no text extraction at all).

The class ``IRSReturn`` offers the same interface as ``grader_core.ReturnPDF`` (``value``,
``all_fields``, ``forms_found``, ``page_map``, ``copies``) so grading, reports, metrics and
the database are shared by both readers.
"""
import hashlib
import io
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

import pdfplumber
from pypdf import PdfReader

from grader_core import (DEP_FIELDS, F1040_FIELDS, FILING_STATUS, LABEL, _identify, _norm_txt, norm_form,
                         norm_line)

FORMAT_NAME = "IRS fillable forms"

LABEL_GAP = 42          # pt: max gap between a line number and its entry box
ROW_TOL = 5             # pt: centre-line tolerance for "same row"
PUSHBUTTON = 1 << 16    # /Ff bit for push buttons (not data)
RADIO = 1 << 15

# Some 1040 labels changed wording over the years; try each phrase in turn.
PHRASE_ALTERNATIVES = {
    "virtual_currency": ["digital asset", "virtual currency"],
    "you_as_dependent": ["You as a dependent"],
    "spouse_as_dependent": ["Your spouse as a dependent"],
}


class ReaderError(Exception):
    """A problem with the uploaded files that the user can fix (shown as-is)."""


def _bold(fontname):
    return bool(re.search(r"(Bold|Bd|Black|Heavy|Demi)", fontname or ""))


def _num(text):
    """'1,234' / '$1,234.00' / '(500)' / '-500' / '500-' -> float; None if not a number."""
    t = str(text).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not t:
        return None
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-") or t.endswith("-")
    t = t.strip("()-")
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", t):
        return None
    v = float(t)
    return -v if neg else v


def _short(full_name):
    """'topmostSubform[0].Page1[0].f1_47[0]' -> 'f1_47'."""
    last = full_name.split(".")[-1]
    return re.sub(r"\[\d+\]$", "", last)


# ------------------------------------------------------------------ widgets
@dataclass
class Widget:
    key: str            # unique on its page: full name (+ '#state' for check boxes)
    name: str           # full field name
    short: str          # last part of the name, e.g. f1_47
    kind: str           # 'text' | 'check' | 'choice'
    x0: float
    x1: float
    top: float
    bottom: float
    state: str = ""     # check box: its "on" appearance name
    value: object = None  # text: str; check: bool
    parent_id: int = 0

    @property
    def cy(self):
        return (self.top + self.bottom) / 2


def _widgets(page):
    """All data widgets on a pypdf page, with their current values."""
    out = []
    mb = page.mediabox
    left, top_edge = float(mb.left), float(mb.top)
    annots = page.get("/Annots")
    if annots is None:
        return out
    try:
        annots = annots.get_object()
    except Exception:
        return out
    seen = {}
    for ref in annots:
        try:
            a = ref.get_object()
        except Exception:
            continue
        if a.get("/Subtype") != "/Widget":
            continue
        names, ft, v, ff, node, parent_id = [], None, None, None, a, 0
        depth = 0
        while node is not None and depth < 20:
            if "/T" in node:
                names.append(str(node["/T"]))
            if ft is None and "/FT" in node:
                ft = node["/FT"]
            if v is None and "/V" in node:
                v = node["/V"]
            if ff is None and "/Ff" in node:
                ff = int(node["/Ff"])
            p = node.get("/Parent")
            if p is not None and not parent_id:
                parent_id = getattr(p, "idnum", 0) or id(p.get_object())
            node = p.get_object() if p is not None else None
            depth += 1
        ff = ff or 0
        if not names or ft not in ("/Tx", "/Btn", "/Ch"):
            continue
        if ft == "/Btn" and ff & PUSHBUTTON:
            continue
        full = ".".join(reversed(names))
        try:
            x0, y0, x1, y1 = [float(z) for z in a["/Rect"]]
        except Exception:
            continue
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        w = Widget(key=full, name=full, short=_short(full), kind="text", x0=x0 - left, x1=x1 - left,
                   top=top_edge - y1, bottom=top_edge - y0, parent_id=parent_id)
        if ft == "/Btn":
            w.kind = "check"
            on = []
            try:
                on = [str(k) for k in a["/AP"]["/N"].keys() if str(k) != "/Off"]
            except Exception:
                pass
            w.state = on[0] if on else "/On"
            asv = a.get("/AS")
            if asv is not None:
                w.value = str(asv) != "/Off"
            else:
                w.value = v is not None and str(v) == w.state
            w.key = f"{full}#{w.state}"
        else:
            w.kind = "text" if ft == "/Tx" else "choice"
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            w.value = "" if v is None else str(v).strip()
        if w.key in seen:            # same field shown twice on one page: keep the first
            continue
        seen[w.key] = True
        out.append(w)
    return out


# ------------------------------------------------------------------ page template (mapping)
@dataclass
class PageTemplate:
    form: str = None                 # '1040', 'SCH C', '8863' ... or None
    cont: bool = False               # page says "Page 2" etc.
    lines: dict = field(default_factory=dict)        # widget key -> line label ('1a', '1a (h)')
    above: dict = field(default_factory=dict)        # widget key -> printed label (normalised)
    right: dict = field(default_factory=dict)        # check box key -> printed label to its right (normalised)
    row: dict = field(default_factory=dict)          # widget key -> whole printed row (normalised)
    desc: dict = field(default_factory=dict)         # line label -> printed description
    deps: dict = field(default_factory=dict)         # (dep no, attr) -> widget key
    fs: dict = field(default_factory=dict)           # check box key -> filing status code
    choice: dict = field(default_factory=dict)       # check box key -> (line, option) for Yes/No and pick-one boxes


def _words(page):
    ws = page.extract_words(x_tolerance=1.5, extra_attrs=["fontname", "size"])
    for w in ws:
        w["cy"] = (w["top"] + w["bottom"]) / 2
    return ws


def _join(ws):
    ws = sorted(ws, key=lambda w: (round(w["top"]), w["x0"]))
    return " ".join(w["text"] for w in ws)


def _right_label(wd, words, widgets):
    """Printed label to the right of a check box: its first text line, plus the next line when the
    label wraps (e.g. 'Child' / 'tax credit') and no other check box starts that next line."""
    near = [w for w in words if abs(w["cy"] - wd.cy) <= max(ROW_TOL, (wd.bottom - wd.top) / 2 + 1)
            and wd.x1 - 1 <= w["x0"] <= wd.x1 + 14]
    if not near:
        return ""
    first = min(near, key=lambda w: w["x0"])
    line, last = [], first["x0"]
    for w in sorted([w for w in words if abs(w["top"] - first["top"]) < 2 and w["x0"] >= first["x0"]],
                    key=lambda w: w["x0"]):
        if w["x0"] - last > 8 or len(line) >= 10 or any(o is not wd and o.kind == "check" and
                                                         o.x0 - 1 <= w["x0"] and o.x0 < w["x0"] and
                                                         o.x0 > first["x0"] for o in widgets
                                                         if abs(o.cy - wd.cy) <= 4):
            break
        line.append(w)
        last = w["x1"]
    x1 = line[-1]["x1"]
    nxt = [w for w in words if 0.5 < w["top"] - first["bottom"] < 6 and first["x0"] - 2 <= w["x0"] <= x1 + 2]
    blocked = any(o is not wd and o.kind == "check" and abs(o.x0 - wd.x0) < 4 and
                  nxt and abs(o.cy - nxt[0]["cy"]) < 5 for o in widgets)
    text = " ".join(w["text"] for w in line)
    if nxt and not blocked:
        text += " " + " ".join(w["text"] for w in sorted(nxt, key=lambda w: w["x0"]))
    return _norm_txt(text)


def _build_template(plpage, widgets):
    t = PageTemplate()
    t.form, t.cont = _identify(plpage)
    words = _words(plpage)
    words = _merge_line_numbers(words)
    labels = [w for w in words if _bold(w["fontname"]) and LABEL.match(w["text"].lower())
              and w.get("size", 9) < 12]                      # not the big '20' of the year in the corner
    # Numbered sub-boxes inside a sentence ("Check if: 1 Rollover  2 QCD  3 [  ]") are not lines:
    # they come as consecutive single digits on one row, unlike real line numbers.
    def sub_box(lb):
        if not re.fullmatch(r"\d", lb["text"]):
            return False
        n = int(lb["text"])
        return any(o is not lb and abs(o["cy"] - lb["cy"]) <= 3 and re.fullmatch(r"\d", o["text"])
                   and abs(int(o["text"]) - n) == 1 and abs(o["x0"] - lb["x0"]) < 130 for o in labels)
    labels = [lb for lb in labels if not sub_box(lb)]
    labels += _sub_letter_labels(words, labels, widgets)
    heads = [w for w in words if re.fullmatch(r"\([a-z]\)", w["text"])]

    def same_row(w, cy, tol=ROW_TOL):
        return abs(w["cy"] - cy) <= tol

    # --- printed labels for every widget
    for wd in widgets:
        tol = max(ROW_TOL, (wd.bottom - wd.top) / 2)
        above = [w for w in words if wd.top - 12 <= w["bottom"] <= wd.top + 2.5
                 and wd.x0 - 3 <= (w["x0"] + w["x1"]) / 2 <= wd.x1 + 3]
        left = [w for w in words if same_row(w, wd.cy, tol) and wd.x0 - 200 <= w["x1"] <= wd.x0 + 2]
        t.above[wd.key] = _norm_txt(_join(above) + " " + _join(left))
        t.row[wd.key] = _norm_txt(_join([w for w in words if abs(w["cy"] - wd.cy) <= 8]))
        if wd.kind == "check":
            t.right[wd.key] = _right_label(wd, words, widgets)

    # --- line numbers -> entry boxes
    texts = [wd for wd in widgets if wd.kind in ("text", "choice")]
    by_label = {}
    for wd in texts:
        cands = [lb for lb in labels if abs(lb["cy"] - wd.cy) <= ROW_TOL + 1
                 and lb["x1"] <= wd.x0 + 3 and wd.x0 - lb["x1"] <= LABEL_GAP]
        if cands:
            lb = max(cands, key=lambda lb: lb["x1"])
            by_label.setdefault((lb["text"], round(lb["top"])), []).append((wd, lb, True))
    # boxes further right on a labelled row (multi-column rows such as Schedule D)
    for wd in texts:
        if any(wd is x[0] for v in by_label.values() for x in v):
            continue
        row_labels = [lb for lb in labels if abs(lb["cy"] - wd.cy) <= ROW_TOL + 1 and lb["x1"] <= wd.x0]
        if row_labels:
            lb = max(row_labels, key=lambda lb: lb["x1"])
            k = (lb["text"], round(lb["top"]))
            if k in by_label:
                by_label[k].append((wd, lb, False))

    def column(wd, lb=None):
        return _column(wd, widgets, words)

    for (text, _), items in by_label.items():
        ln = text.lower()
        if len(items) == 1 and items[0][2]:
            t.lines[items[0][0].key] = ln
        else:
            for wd, lb, direct in items:
                col = column(wd, lb)
                if col:
                    t.lines[wd.key] = f"{ln} ({col})"
                elif direct:
                    t.lines[wd.key] = ln
        lb = items[0][1]
        # the description runs from the same number at the left margin (if printed twice) to the box
        twin = [o for o in labels if o["text"] == lb["text"] and same_row(o, lb["cy"], 3) and o["x1"] < lb["x0"]]
        start = min(twin, key=lambda o: o["x0"])["x1"] if twin else -1
        before = [w for w in words if same_row(w, lb["cy"], 3) and start < w["x0"] and w["x1"] < lb["x0"] - 2
                  and not (_bold(w["fontname"]) and LABEL.match(w["text"].lower()))]
        d = re.sub(r"(\s*\.\s*){2,}", " ", " ".join(w["text"] for w in sorted(before, key=lambda w: w["x0"])))
        if d.strip(" .") and ln not in t.desc:
            t.desc[ln] = d.strip(" .")[:70]

    if t.form == "1040" and not t.cont:
        _map_1040_page1(t, widgets, words)
    if t.form and t.form != "1040":
        _map_row_labels(t, widgets, labels)
        _map_tables(t, widgets, words)
        _map_lists(t, widgets, words, labels)
        _map_checks(t, widgets, words, labels)
        _map_header(t, widgets, words)
    return t


# ------------------------------------------------------------------ schedules: tables, lists, check boxes
HEAD_LETTER = re.compile(r"^\(([a-k])\)$|^([A-D])$")


def _merge_line_numbers(words):
    """'44' + 'a' printed as two bold pieces on one row -> one label '44a'."""
    out, skip = [], set()
    ws = sorted(words, key=lambda w: (round(w["top"]), w["x0"]))
    for i, w in enumerate(ws):
        if i in skip:
            continue
        if _bold(w["fontname"]) and re.fullmatch(r"\d{1,3}", w["text"]) and i + 1 < len(ws):
            n = ws[i + 1]
            if (_bold(n["fontname"]) and re.fullmatch(r"[a-z]", n["text"]) and abs(n["top"] - w["top"]) < 2
                    and 0 <= n["x0"] - w["x1"] < 3):
                m = dict(w, text=w["text"] + n["text"], x1=n["x1"])
                out.append(m)
                skip.add(i + 1)
                continue
        out.append(w)
    return out


def _sub_letter_labels(words, labels, widgets=()):
    """A bold 'b' at the left margin under '29a' is line 29b (rows with no number on the right); a bold
    'a' / 'b' / 'c' just before a box further along the row ('33 ... a [ ] Cost  b [ ] Lower of cost') is
    line 33a / 33b / 33c."""
    out = []
    for w in words:
        if not (_bold(w["fontname"]) and re.fullmatch(r"[a-z]", w["text"])):
            continue
        nxt = [wd for wd in widgets if abs(wd.cy - w["cy"]) <= 6 and wd.x0 >= w["x1"]]
        before_box = bool(nxt) and min(wd.x0 for wd in nxt) - w["x1"] < 160
        if not (w["x0"] < 130 or before_box):
            continue
        if any(lb["x0"] < w["x0"] and abs(lb["cy"] - w["cy"]) < 3 and re.fullmatch(r"\d{1,3}[a-z]", lb["text"])
               and w["x0"] - lb["x1"] < 3 for lb in labels):
            continue                                                 # already part of '44a'
        above = [lb for lb in labels if lb["x0"] < 130 and -3 < w["top"] - lb["top"] < 60
                 and re.fullmatch(r"\d{1,3}[a-z]?", lb["text"])]
        if not above:
            continue
        lb = max(above, key=lambda lb: lb["top"])
        num = re.match(r"\d+", lb["text"]).group(0)
        letters = lb["text"][len(num):]
        if letters and ord(w["text"]) <= ord(letters[-1]):
            continue
        if not letters and w["text"] != "a" and not any(o["text"] == num + "a" for o in out):
            continue                                                 # 'b' needs an 'a' before it
        out.append(dict(w, text=num + w["text"], sub=True))
    return out


def _column(wd, widgets, words, depth=0):
    """Column letter printed straight above a box ('(h)', or 'A'/'B'/'C' for properties); a box with no
    header of its own takes the column of the nearest box above it in the same column."""
    hs = []
    for h in words:
        m = HEAD_LETTER.match(h["text"])
        if not m or h["bottom"] > wd.top + 1 or wd.top - h["bottom"] > 330:
            continue
        if any(o is not h and abs(o["cy"] - h["cy"]) < 2 and 0 <= h["x0"] - o["x1"] < 5 for o in words):
            continue                                   # part of a sentence ('... columns (h) and (k)'), not a heading
        cx = (h["x0"] + h["x1"]) / 2
        pad = 22 if wd.kind == "check" else 3            # a small tick box sits inside a wider column
        if wd.x0 - pad <= cx <= wd.x1 + pad:
            hs.append((wd.top - h["bottom"], m.group(1) or m.group(2)))
    own = None
    if hs:
        # a heading block can mention other columns lower down ('(h) ... combine with column (g)'):
        # within the nearest block, the letter printed highest is the column's own
        near = min(d for d, _ in hs)
        own = max((d, c) for d, c in hs if d - near < 45)
    if depth:
        return own[1] if own else None
    w = wd.x1 - wd.x0
    above = [o for o in widgets if o is not wd and o.kind != "check" and o.bottom <= wd.top + 1
             and wd.top - o.bottom < 330 and min(o.x1, wd.x1) - max(o.x0, wd.x0) > 0.6 * w
             and (abs(o.x0 - wd.x0) < 8 or abs(o.x1 - wd.x1) < 8) and (o.x1 - o.x0) < 1.6 * w]
    for o in sorted(above, key=lambda o: wd.top - o.bottom):
        if own and wd.top - o.bottom > own[0]:
            break                               # the heading is nearer than any box above
        c = _column(o, widgets, words, 1)
        if c:
            return c                            # a 'Totals' box under a table takes the table's column
    return own[1] if own else None


def _header_phrase(wd, words):
    """Printed column heading just above the first row of a table, e.g. 'fair rental days'."""
    ws = [w for w in words if 0 <= wd.top - w["bottom"] < 45 and wd.x0 - 3 <= (w["x0"] + w["x1"]) / 2 <= wd.x1 + 3
          and not re.fullmatch(r"[A-D]", w["text"])]
    return " ".join(w["text"] for w in sorted(ws, key=lambda w: (round(w["top"]), w["x0"]))).lower()


def _segs(name):
    return [re.sub(r"\[\d+\]$", "", s) for s in name.split(".")]


def _map_row_labels(t, widgets, labels):
    """A box far along a row from its line number, e.g. '2b Date of original divorce ... [    ]' or
    '44  a Business [   ]  b Commuting [   ]': the nearest sub-line label on the left, if no box is between."""
    taken = set(t.lines.values())
    for wd in widgets:
        if wd.kind == "check" or wd.key in t.lines:
            continue
        row = [lb for lb in labels if lb.get("sub") and abs(lb["cy"] - wd.cy) <= 6 and lb["x1"] <= wd.x0]
        if not row:
            continue
        lb = max(row, key=lambda lb: lb["x1"])
        if any(o is not wd and abs(o.cy - wd.cy) <= 6 and lb["x1"] < o.x0 < wd.x0 for o in widgets):
            continue
        ln = lb["text"].lower()
        if ln not in taken:
            t.lines[wd.key] = ln
            taken.add(ln)


def _map_tables(t, widgets, words):
    """Rows the IRS form itself names as tables (Schedule D lines 1a-10, Schedule E properties A-C and
    lines 28/33/38): each box gets 'line', 'line row' and/or '(column)', e.g. '1a (h)', '3 (B)', '28 A (f)'."""
    groups = {}
    for wd in widgets:
        ln = row = None
        for sg in _segs(wd.name):
            m = re.fullmatch(r"Table_Line(\d{1,2})([a-z]?)(-[a-z])?", sg)
            if m:
                ln = m.group(1) if m.group(3) else m.group(1) + m.group(2)     # 28a-f -> 28, 1a -> 1a
                continue
            m = re.fullmatch(r"Row([A-F])", sg)
            if m:
                row = m.group(1)
                continue
            m = re.fullmatch(r"(?:Line|Row)(\d{1,2}[a-z]?)", sg)
            if m:
                if ln and re.fullmatch(r"\d", m.group(1)):
                    continue                                       # Table_Line38 > Row1: a single row
                ln = m.group(1)
        if ln:
            groups.setdefault((ln, row), []).append(wd)
    if not groups:
        return
    for (ln, row), ws in groups.items():
        base = ln + (f" {row}" if row else "")
        if len(ws) == 1 and ws[0].kind != "check":
            t.lines[ws[0].key] = base
            continue
        for wd in ws:
            col = _column(wd, widgets, words)
            if not col:
                ph = _header_phrase(wd, words)
                if not ph:
                    first = [o for o in widgets if o is not wd and abs(o.x0 - wd.x0) < 3 and o.top < wd.top]
                    if first:
                        ph = _header_phrase(min(first, key=lambda o: o.top), words)
                col = re.sub(r"[^a-z0-9 ]", "", ph)[:30].strip() or ("description" if wd.kind == "text" else None)
            name = f"{base} ({col})" if col else base
            if wd.kind == "check":
                t.choice[wd.key] = (name, "Checked")
            else:
                t.lines[wd.key] = name


LIST_LINES = {"SCH B": [("1", "2", "payer"), ("5", "6", "payer")], "SCH C": [("47b", "48", "description")]}


def _map_lists(t, widgets, words, labels):
    """Lists of names and amounts: Schedule B lines 1 and 5, Schedule C Part V -> '1 (payer 3)' / '1 (amount 3)'."""
    for start, end, what in LIST_LINES.get(t.form, []):
        s = [lb for lb in labels if lb["text"] == start]
        e = [lb for lb in labels if lb["text"] == end]
        if not s or not e:
            continue
        top = min(lb["top"] for lb in s) - 14
        bottom = min(lb["top"] for lb in e if lb["top"] > top) - 1
        ln = start if t.form == "SCH B" else end
        if t.form == "SCH C":
            top = max(lb["bottom"] for lb in s) + 12
            bottom = max(lb["top"] for lb in e) + 1
        rows = {}
        for wd in widgets:
            if wd.kind == "text" and top <= wd.top and wd.bottom <= bottom + 6:
                rows.setdefault(round(wd.cy / 4), []).append(wd)
        n = 0
        for _, ws in sorted(rows.items()):
            ws.sort(key=lambda w: w.x0)
            if len(ws) == 1 and ws[0].x1 - ws[0].x0 > 200 and n == 0 and t.form == "SCH B":
                pass
            n += 1
            amt = ws[-1] if len(ws) > 1 or ws[0].x0 > 400 else None
            for wd in ws:
                if t.lines.get(wd.key) == end:
                    continue
                t.lines[wd.key] = f"{ln} ({'amount' if wd is amt else what} {n})"
        t.desc[ln] = {("SCH B", "1"): "Interest payer list", ("SCH B", "5"): "Dividend payer list",
                      ("SCH C", "48"): "Other expenses list"}.get((t.form, ln), t.desc.get(ln, ""))


def _map_checks(t, widgets, words, labels):
    """Check boxes on schedules. Boxes of one field that answer one question (Yes/No, Cash/Accrual/Other)
    become one line whose value is the option ticked; a lone box becomes a line valued Checked/Unchecked.
    The question is the nearest line number (or capital letter A-J at the margin) on or above its row."""
    letters = [w for w in words if _bold(w["fontname"]) and re.fullmatch(r"[A-J]", w["text"]) and w["x0"] < 60]
    marks = labels + letters
    used_lines = set(t.lines.values())

    def question(wd):
        c = [lb for lb in marks if lb["x1"] <= wd.x0 + 1 and lb["cy"] <= wd.cy + 4 and wd.cy - lb["cy"] < 70]
        if not c:
            return None
        same_row = [lb for lb in c if abs(lb["cy"] - wd.cy) <= 5]
        lb = max(same_row, key=lambda lb: lb["x1"]) if same_row else max(c, key=lambda lb: (lb["cy"], lb["x1"]))
        return lb["text"] if re.fullmatch(r"[A-J]", lb["text"]) else lb["text"].lower()

    def option(wd):
        near = [w for w in words if abs(w["cy"] - wd.cy) <= 5 and wd.x1 - 1 <= w["x0"] <= wd.x1 + 14]
        if near:
            w = min(near, key=lambda w: w["x0"])
            if w["text"].startswith("(") and len(near) > 1:
                w = sorted(near, key=lambda w: w["x0"])[1]
            return w["text"].strip("().,:;").capitalize() or None
        hd = [w for w in words if w["text"] in ("Yes", "No") and w["bottom"] <= wd.top + 1
              and wd.x0 - 6 <= (w["x0"] + w["x1"]) / 2 <= wd.x1 + 6 and wd.top - w["bottom"] < 320]
        if hd:
            return max(hd, key=lambda w: w["bottom"])["text"]
        return None

    fields = {}
    for wd in widgets:                       # Yes/No boxes are often separate fields c1_2[0], c1_2[1]
        if wd.kind == "check" and wd.key not in t.choice:
            fields.setdefault(re.sub(r"\[\d+\]$", "", wd.name), []).append(wd)
    taken = {}
    for name, kids in sorted(fields.items(), key=lambda kv: (min(k.top for k in kv[1]), min(k.x0 for k in kv[1]))):
        qs = [question(k) for k in kids]
        if len(kids) > 1 and len(set(qs)) == 1 and qs[0]:
            ln = qs[0]
            taken[ln] = taken.get(ln, 0) + 1
            if taken[ln] > 1:
                ln = f"{ln} ({taken[ln]})"                       # a second Yes/No under the same number
            for k in kids:
                t.choice[k.key] = (ln, option(k) or "Checked")
            continue
        for k, q in zip(kids, qs):
            if not q:
                continue
            o = option(k) or ""
            tag = o if re.fullmatch(r"\d{4}", o) else "check"         # '4 (4797)' / '14 (check)'
            ln = q if q not in used_lines and q not in taken else f"{q} ({tag})"
            if ln in taken:
                ln = f"{ln} {taken[ln] + 1}"
            taken[ln] = taken.get(ln, 0) + 1
            t.choice[k.key] = (ln, "Checked")


HEADER_BOXES = [("namesshown", "name"), ("nameofproprietor", "name"), ("socialsecuritynumber", "ssn")]


def _map_header(t, widgets, words):
    """Name / SSN at the top of every schedule; Schedule C boxes A-E and the address parts."""
    for wd in widgets:
        if wd.kind == "check" or wd.key in t.lines:
            continue
        lab = t.above.get(wd.key, "")
        if wd.top < 140:
            for key, ln in HEADER_BOXES:
                ln = f"{ln} (page 2)" if t.cont else ln
                if key in lab and ln not in t.lines.values():
                    t.lines[wd.key] = ln
                    break
        if wd.key in t.lines or t.form != "SCH C":
            continue
        tag = [w for w in words if _bold(w["fontname"]) and re.fullmatch(r"[A-E]", w["text"])
               and (-3 <= wd.top - w["bottom"] < 14 and wd.x0 - 12 <= w["x0"] <= wd.x0 + 8
                    or abs(w["cy"] - wd.cy) <= 6 and w["x0"] < 60 and wd.x0 - w["x1"] < 260)]
        if tag:
            t.lines[wd.key] = tag[0]["text"]
            continue
        for key, ln in (("aptno", "E apt"), ("citytown", "E city"), ("state", "E state"), ("zipcode", "E zip"),
                        ("otherspecify", "F other"), ("squarefootageofayour", "30 (a)"), ("ayourhome", "30 (a)"),
                        ("bthepartofyour", "30 (b)"), ("44abusiness", "44a"), ("44bcommuting", "44b")):
            if key in lab and ln not in t.lines.values():
                t.lines[wd.key] = ln
                break


def _map_1040_page1(t, widgets, words):
    """Filing status check boxes and the dependents table on Form 1040 page 1."""
    for wd in widgets:
        if wd.kind != "check":
            continue
        lab = t.right.get(wd.key, "")
        for name, code in FILING_STATUS.items():
            if len(name) > 3 and lab.startswith(_norm_txt(name)):
                t.fs[wd.key] = code
                break

    heads = sorted([w for w in words if w["text"] == "Dependent"], key=lambda w: w["x0"])
    texts = [wd for wd in widgets if wd.kind == "text"]
    checks = [wd for wd in widgets if wd.kind == "check"]
    if len(heads) >= 2:
        # 2025 layout: one COLUMN per dependent, one ROW per item
        cols = [((h["x0"] + h["x1"]) / 2, i) for i, h in enumerate(heads, 1)]
        top = heads[0]["bottom"]

        def col_of(wd):
            cx = (wd.x0 + wd.x1) / 2
            c, i = min(cols, key=lambda c: abs(c[0] - cx))
            return i if abs(c - cx) < 70 else None

        def row_y(*phrases):
            for w in words:
                if w["top"] < top:
                    continue
                for p in phrases:
                    ws = p.split()
                    if w["text"] == ws[0]:
                        rest = [x for x in words if abs(x["top"] - w["top"]) < 2 and 0 < x["x0"] - w["x1"] < 30]
                        if len(ws) == 1 or (rest and min(rest, key=lambda x: x["x0"])["text"].lower() == ws[1].lower()):
                            return w["cy"]
            return None

        rows = {"first_name": row_y("First name"), "last_name": row_y("Last name"),
                "ssn": row_y("SSN"), "relationship": row_y("Relationship")}
        for attr, y in rows.items():
            if y is None:
                continue
            for wd in texts:
                if abs(wd.cy - y) <= 6 and wd.top > top:
                    i = col_of(wd)
                    if i:
                        t.deps.setdefault((i, attr), wd.key)
        credits_y = row_y("Credits")
        for wd in checks:
            if wd.top <= top or credits_y is None or abs(wd.cy - credits_y) > 12:
                continue
            i, lab = col_of(wd), t.right.get(wd.key, "")
            if i and lab.startswith("child"):
                t.deps[(i, "ctc")] = wd.key
            elif i and lab.startswith("credit"):
                t.deps[(i, "odc")] = wd.key
    else:
        # 2020-2024 layout: one ROW per dependent, columns (1) name (2) SSN (3) relationship (4) credits
        def head(p):
            for w in words:
                if w["text"] == p.split()[0]:
                    ctx = _norm_txt(_join([x for x in words if abs(x["top"] - w["top"]) < 2 and x["x0"] >= w["x0"]]))
                    if ctx.startswith(_norm_txt(p)):
                        return w
            return None
        h_name, h_ssn, h_rel = head("(1) First name"), head("(2) Social security"), head("(3) Relationship")
        if not (h_name and h_ssn and h_rel):
            return
        y0 = max(h_name["bottom"], h_ssn["bottom"]) + 1
        rows = sorted({round(wd.cy) for wd in texts if y0 < wd.top < y0 + 90 and wd.x0 < h_ssn["x0"] - 4})
        for i, y in enumerate(rows[:4], 1):
            for wd in texts:
                if abs(wd.cy - y) > 4:
                    continue
                if wd.x0 < h_ssn["x0"] - 6:
                    t.deps.setdefault((i, "name_parts"), [])
                    t.deps[(i, "name_parts")].append(wd.key)
                elif wd.x0 < h_rel["x0"] - 6:
                    t.deps[(i, "ssn")] = wd.key
                else:
                    t.deps.setdefault((i, "relationship"), wd.key)
            cks = sorted([wd for wd in checks if abs(wd.cy - y) <= 5], key=lambda w: w.x0)
            if len(cks) >= 2:
                t.deps[(i, "ctc")], t.deps[(i, "odc")] = cks[-2].key, cks[-1].key


# A small, thread-safe LRU cache of page templates keyed by the blank form's fingerprint.
_CACHE, _CACHE_LOCK, _CACHE_MAX = OrderedDict(), threading.Lock(), 512


def _fingerprint(page, widgets):
    h = hashlib.sha1()
    mb = page.mediabox
    h.update(f"{float(mb.width):.0f}x{float(mb.height):.0f}".encode())
    for w in sorted(widgets, key=lambda w: w.key):
        h.update(f"{w.key}|{w.kind}|{w.x0:.0f},{w.top:.0f},{w.x1:.0f},{w.bottom:.0f};".encode())
    return h.hexdigest()


def _template(fp, build):
    with _CACHE_LOCK:
        if fp in _CACHE:
            _CACHE.move_to_end(fp)
            return _CACHE[fp]
    t = build()
    with _CACHE_LOCK:
        _CACHE[fp] = t
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return t


# ------------------------------------------------------------------ the return
@dataclass
class IRSCopy:
    form: str
    instance: int
    pages: list = field(default_factory=list)      # (file name, page no)
    templates: list = field(default_factory=list)
    widgets: list = field(default_factory=list)    # per page: {key: Widget}


class IRSReturn:
    format_name = FORMAT_NAME

    def __init__(self, files, max_pages=60):
        """files: list of (file name, bytes) - one student's forms, in any order."""
        self.copies, self.page_map, self.notes = {}, [], []
        self.filing_status, self._deps = None, None
        self.n_pages = 0
        counts, seen_hashes = {}, set()
        files = sorted(files, key=lambda f: f[0].lower())
        any_widgets = False
        for fname, data in files:
            h = hashlib.sha256(data).hexdigest()
            if h in seen_hashes:
                self.notes.append(f"{fname}: identical to another uploaded file - ignored.")
                continue
            seen_hashes.add(h)
            try:
                rd = PdfReader(io.BytesIO(data))
                if rd.is_encrypted:
                    rd.decrypt("")
                pages = rd.pages
                n = len(pages)
            except Exception as e:
                raise ReaderError(f"{fname} is not a readable PDF ({type(e).__name__}).")
            if n > max_pages:
                raise ReaderError(f"{fname} has {n} pages; the limit is {max_pages}.")
            try:
                page_widgets = [_widgets(pg) for pg in pages]
            except Exception as e:
                raise ReaderError(f"{fname} is damaged and could not be read ({type(e).__name__}). "
                                  "Ask the student to save the form again and re-upload it.")
            if not any(page_widgets):
                # A printed / flattened copy holds no readable entries. Skipping it keeps it from taking
                # the place of the real (fillable) copy of the same form.
                self.n_pages += n
                for pno in range(1, n + 1):
                    self.page_map.append((f"{fname} p{pno}", "(printed copy - not read)"))
                self.notes.append(f"{fname}: no fillable boxes found - if it was printed to PDF, save the filled "
                                  "form instead (File > Save) and upload that copy. This file was not marked.")
                continue
            pl = None
            last, parents = None, {}
            try:
                for pno in range(1, n + 1):
                    self.n_pages += 1
                    pg = pages[pno - 1]
                    wds = page_widgets[pno - 1]
                    fp = _fingerprint(pg, wds)

                    def build():
                        nonlocal pl
                        if pl is None:
                            pl = pdfplumber.open(io.BytesIO(data))
                        return _build_template(pl.pages[pno - 1], wds)

                    tpl = _template(fp, build) if wds else build()
                    where = f"{fname} p{pno}"
                    if tpl.form is None:
                        self.page_map.append((where, "(not a graded form)"))
                        last = None
                        continue
                    if tpl.cont and last and last.form == tpl.form:
                        copy = last
                    else:
                        counts[tpl.form] = counts.get(tpl.form, 0) + 1
                        copy = IRSCopy(tpl.form, counts[tpl.form])
                        self.copies[(tpl.form, copy.instance)] = copy
                    copy.pages.append((fname, pno))
                    copy.templates.append(tpl)
                    copy.widgets.append({w.key: w for w in wds})
                    for w in wds:
                        if w.parent_id:
                            parents.setdefault(w.parent_id, set()).add((copy.form, copy.instance))
                    last = copy
                    self.page_map.append((where, copy.form if copy.instance == 1 else f"{copy.form} #{copy.instance}"))
            finally:
                if pl is not None:
                    pl.close()
            any_widgets = True
            if any(len(v) > 1 for v in parents.values()):
                self.notes.append(f"{fname}: several forms share field names in this file (it looks merged). "
                                  "Merging can mix up entries - upload each form as a separate PDF.")
        if not any_widgets:
            raise ReaderError("None of the files has fillable boxes. They look printed or flattened: open the "
                              "fillable IRS form, fill it in, save it (File > Save) and upload the saved file.")
        if not self.copies:
            raise ReaderError("No IRS form pages were recognised in the uploaded file(s).")
        multi = sorted(f"{f} ({n} copies)" for f, n in counts.items() if n > 1)
        if multi:
            self.notes.append("Forms appearing more than once: " + ", ".join(multi) + ". Each copy is marked as "
                              "'#2', '#3' in the order of the file names.")
        c = self.copies.get(("1040", 1))
        if c:
            for tpl, wmap in zip(c.templates, c.widgets):
                for k, code in tpl.fs.items():
                    if wmap.get(k) and wmap[k].value is True:
                        self.filing_status = code if self.filing_status is None else "MULTIPLE"
        if not self.all_fields():
            self.notes.append("No entries were found on any form - the forms look blank. If the student filled "
                              "them in, they were probably not saved (File > Save) before uploading.")

    # ---------------------------------------------------------------- helpers
    def forms_found(self):
        return sorted({f if i == 1 else f"{f} #{i}" for (f, i) in self.copies})

    @staticmethod
    def _items(copy):
        for pi, (tpl, wmap) in enumerate(zip(copy.templates, copy.widgets)):
            for k, w in wmap.items():
                yield pi, tpl, w

    def _line_widget(self, copy, ln):
        for _, tpl, w in self._items(copy):
            if w.key in tpl.lines and norm_line(tpl.lines[w.key]) == ln:
                return w
        return None

    @staticmethod
    def _amount(w):
        if w.kind == "check":
            return "Checked" if w.value else "Unchecked"
        v = w.value or ""
        if v == "":
            return 0.0
        n = _num(v)
        return n if n is not None else v

    def _text_by_label(self, copy, phrase, near=None, page=None):
        p = _norm_txt(phrase)
        hits = []
        for pi, tpl, w in self._items(copy):
            if w.kind == "check" or (page and pi != page - 1):
                continue
            lab = tpl.above.get(w.key, "")
            if p and p in lab:
                if near and _norm_txt(near) not in tpl.row.get(w.key, "") and not self._near_widget(copy, pi, w, near):
                    continue
                hits.append((pi, w.top, w.x0, lab.find(p), w))
        if not hits:
            return None
        # prefer the box whose label STARTS with the phrase, then the top-left one
        hits.sort(key=lambda h: (h[3] != 0, h[0], round(h[1]), h[2]))
        return hits[0][4]

    def _near_widget(self, copy, pi, w, near):
        n = _norm_txt(near)
        tpl, wmap = copy.templates[pi], copy.widgets[pi]
        return any(abs(o.cy - w.cy) <= 4 and n in tpl.above.get(o.key, "") for o in wmap.values() if o is not w)

    def _check_by_label(self, copy, phrase, near=None, page=None):
        p = _norm_txt(phrase)
        found = None
        for pi, tpl, w in self._items(copy):
            if w.kind != "check" or (page and pi != page - 1):
                continue
            if tpl.right.get(w.key, "").startswith(p):
                if near and _norm_txt(near) not in tpl.row.get(w.key, ""):
                    continue
                if w.value:
                    return True
                found = False
        return found

    # ---------------------------------------------------------------- dependents
    def dependents(self):
        if self._deps is not None:
            return self._deps
        self._deps = []
        c = self.copies.get(("1040", 1))
        if not c:
            return self._deps
        tpl, wmap = c.templates[0], c.widgets[0]
        if not tpl.deps:
            return self._deps
        get = lambda k: wmap[k].value if k in wmap else None
        for i in range(1, 5):
            if (i, "name_parts") in tpl.deps:
                name = " ".join(filter(None, (get(k) for k in tpl.deps[(i, "name_parts")])))
                first = last = ""
            else:
                first = get(tpl.deps.get((i, "first_name"))) or ""
                last = get(tpl.deps.get((i, "last_name"))) or ""
                name = " ".join(filter(None, (first, last)))
            d = {"name": name, "first_name": first, "last_name": last,
                 "ssn": get(tpl.deps.get((i, "ssn"))) or "",
                 "relationship": get(tpl.deps.get((i, "relationship"))) or "",
                 "ctc": "Checked" if get(tpl.deps.get((i, "ctc"))) else "Unchecked",
                 "odc": "Checked" if get(tpl.deps.get((i, "odc"))) else "Unchecked"}
            if any(d[k] for k in ("name", "ssn", "relationship")) or "Checked" in (d["ctc"], d["odc"]):
                self._deps.append(d)
            else:
                break
        return self._deps

    # ---------------------------------------------------------------- lookups (same as ReturnPDF.value)
    def value(self, form, line):
        f, inst = norm_form(form)
        raw = str(line or "").strip()
        ln = norm_line(raw)
        copy = self.copies.get((f, inst))
        name = f if inst == 1 else f"{f} #{inst}"
        if copy is None:
            return None, f"{name} not found in the return"
        m = re.match(r"^field:\s*(.+)$", raw, re.I)
        if m:
            want = m.group(1).strip()
            for _, _, w in self._items(copy):
                if want in (w.short, w.name):
                    return self._amount(w), ""
            return None, f"Field {want} not found on {name}"
        if f == "1040" and ln in ("fs", "filing status", "filing_status"):
            if self.filing_status == "MULTIPLE":
                return "More than one box checked", "Filing status: more than one box is checked"
            return (self.filing_status or ""), ("" if self.filing_status else "No filing status box is checked")
        m = re.match(r"^dep(\d)_(\w+)$", ln)
        if f == "1040" and m and m.group(2) in DEP_FIELDS + ("first_name", "last_name"):
            deps = self.dependents()
            i = int(m.group(1)) - 1
            if i >= len(deps):
                return ("Unchecked" if m.group(2) in ("ctc", "odc") else ""), ""
            return deps[i][m.group(2)], ""
        if f == "1040" and ln in F1040_FIELDS:
            kind, pg, phrase, near = F1040_FIELDS[ln]
            phrases = PHRASE_ALTERNATIVES.get(ln, [phrase])
            for ph in phrases:
                if kind == "text":
                    w = self._text_by_label(copy, ph, near, pg)
                    if w is not None:
                        return w.value, ""
                elif kind == "check":
                    c = self._check_by_label(copy, ph, near)
                    if c is not None:
                        return ("Checked" if c else "Unchecked"), ""
                else:
                    y, n_ = self._check_by_label(copy, "Yes", ph, pg), self._check_by_label(copy, "No", ph, pg)
                    if y is not None or n_ is not None:
                        return ("Yes" if y else "No" if n_ else ""), ""
            return None, f"'{phrase}' not found on Form 1040"
        m = re.match(r"^(text|check):\s*(.+?)(?:\s*\|\s*near:\s*(.+))?$", raw, re.I)
        if m:
            kind, phrase, near = m.group(1).lower(), m.group(2), m.group(3)
            if kind == "text":
                w = self._text_by_label(copy, phrase, near)
                return (w.value, "") if w is not None else (None, f"'{phrase}' not found on {name}")
            c = self._check_by_label(copy, phrase, near)
            return (("Checked" if c else "Unchecked"), "") if c is not None else (None, f"'{phrase}' not found on {name}")
        w = self._line_widget(copy, ln)
        if w is None:
            opts = self._choice(copy, ln)
            if opts is not None:
                return opts, ""
            return None, f"Line {ln} not found on {name}"
        return self._amount(w), ""

    @staticmethod
    def _describe(tpl, fname, ln):
        """'3 (B)' -> 'Rents received: property B'; '1 (payer 3)' -> 'Interest payer list: payer 3'."""
        m = re.match(r"^(\S+)(?: ([A-F]))?(?: \((.+)\))?$", ln)
        if not m:
            return tpl.desc.get(ln, "") or f"{fname} line {ln}"
        base, row, col = m.groups()
        d = tpl.desc.get(ln) if not (row or col) else tpl.desc.get(base, "")
        parts = [d or f"{fname} line {base}"]
        if row:
            parts.append(f"row {row}")
        if col:
            parts.append(f"property {col}" if re.fullmatch(r"[A-C]", col) else
                         f"column ({col})" if re.fullmatch(r"[a-k]", col) else col)
        return parts[0] + (": " + ", ".join(parts[1:]) if len(parts) > 1 else "")

    def _choice(self, copy, ln):
        """Value of a check-box question: the option ticked ('Yes', 'Cash', 'Checked'), '' if none,
        'Unchecked' for a lone box left empty; None if the form has no such question."""
        found, ticked = False, []
        for _, tpl, w in self._items(copy):
            c = tpl.choice.get(w.key)
            if c and norm_line(c[0]) == ln:
                found = True
                if w.value:
                    ticked.append(c[1])
        if not found:
            return None
        if len(ticked) > 1:
            return "More than one box checked"
        if ticked:
            return ticked[0]
        lone = sum(1 for _, tpl, w in self._items(copy) if tpl.choice.get(w.key, ("",))[0] and
                   norm_line(tpl.choice[w.key][0]) == ln) == 1
        return "Unchecked" if lone else ""

    # ---------------------------------------------------------------- everything filled in
    def all_fields(self):
        out = []
        for (f, inst), copy in self.copies.items():
            fname = f if inst == 1 else f"{f} #{inst}"
            used = set()
            if f == "1040" and inst == 1:
                if self.filing_status:
                    out.append({"form": fname, "line": "FS", "desc": "Filing status", "value": self.filing_status})
                for tpl in copy.templates:
                    used |= set(tpl.fs) | {v for k, v in tpl.deps.items() if isinstance(v, str)}
                    for k, v in tpl.deps.items():
                        if isinstance(v, list):
                            used |= set(v)
                for key in F1040_FIELDS:
                    v, _ = self.value("1040", key)
                    if v not in (None, "", "Unchecked"):
                        out.append({"form": fname, "line": key, "desc": key.replace("_", " ").capitalize(), "value": v})
                        w = self._named_widget(copy, key)
                        if w is not None:
                            used.add(w.key)
                for i, d in enumerate(self.dependents(), 1):
                    for k_ in DEP_FIELDS:
                        out.append({"form": fname, "line": f"dep{i}_{k_}", "desc": f"Dependent {i} {k_}", "value": d[k_]})
            seen = set()
            for _, tpl, w in self._items(copy):
                ln = tpl.lines.get(w.key)
                if ln and ln not in seen:
                    seen.add(ln)
                    used.add(w.key)
                    if w.value not in ("", None, False):
                        out.append({"form": fname, "line": ln, "value": self._amount(w), "prefer_desc": True,
                                    "desc": self._describe(tpl, fname, ln)})
            done_choice = set()
            for _, tpl, w in self._items(copy):
                c = tpl.choice.get(w.key)
                if not c:
                    continue
                used.add(w.key)
                if w.value and c[0] not in done_choice:
                    done_choice.add(c[0])
                    v = self._choice(copy, norm_line(c[0]))
                    out.append({"form": fname, "line": c[0], "value": v, "prefer_desc": True,
                                "desc": self._describe(tpl, fname, c[0])})
            for _, tpl, w in self._items(copy):
                if w.key in used or w.value in ("", None, False):
                    continue
                if w.kind == "check" and f == "1040" and inst == 1 and tpl.fs.get(w.key):
                    continue
                lab = tpl.right.get(w.key) if w.kind == "check" else tpl.above.get(w.key)
                out.append({"form": fname, "line": f"field: {w.short}", "value": self._amount(w),
                            "desc": f"Unlinked box ({lab[:40] if lab else w.short})", "unlinked": True,
                            "prefer_desc": True})
        return out

    def _named_widget(self, copy, key):
        kind, pg, phrase, near = F1040_FIELDS[key]
        if kind != "text":
            for ph in PHRASE_ALTERNATIVES.get(key, [phrase]):
                for pi, tpl, w in self._items(copy):
                    if w.kind == "check" and (tpl.right.get(w.key, "").startswith(_norm_txt(ph)) or
                                              (kind == "yesno" and _norm_txt(ph) in tpl.row.get(w.key, "")
                                               and tpl.right.get(w.key, "")[:3] in ("yes", "no"))):
                        if w.value:
                            return w
            return None
        return self._text_by_label(copy, phrase, near, pg)

    def unlinked(self):
        """Filled-in boxes the reader could not link to a line or label (shown to staff for checking)."""
        return [x for x in self.all_fields() if x.get("unlinked")]


def has_form_fields(data):
    """True if the PDF has at least one fillable data box (used to choose the reader)."""
    try:
        rd = PdfReader(io.BytesIO(data))
        if rd.is_encrypted:
            rd.decrypt("")
        for pg in rd.pages[:6]:
            if _widgets(pg):
                return True
    except Exception:
        return False
    return False
