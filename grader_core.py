"""
grader_core.py - shared building blocks: form and line names, the Excel answer key,
and grading a return against it.

* The return itself is read by irs_reader.IRSReturn (IRS fillable forms); grade() only
  needs its value(form, line) and all_fields() methods.
* A blank box reads as 0. A form that is not in the upload reads as "missing".
* Grading is strict: a figure either matches the key (within the rounding tolerance) or
  loses the step's marks. Optional: entries on lines the key says nothing about
  ("unexpected entries") can each deduct marks.
"""
import io
import re
from dataclasses import dataclass

from openpyxl import load_workbook

# ------------------------------------------------------------------ settings
HEADER_FRACTION = 0.16   # top part of the page used to identify the form

LABEL = re.compile(r"^\d{1,3}[a-z]{0,2}$")

FILING_STATUS = {
    "single": "S", "s": "S",
    "married filing jointly": "MFJ", "mfj": "MFJ",
    "married filing separately": "MFS", "mfs": "MFS",
    "head of household": "HOH", "hoh": "HOH",
    "qualifying widow(er)": "QW", "qualifying widow": "QW", "qw": "QW", "qss": "QW",
    "qualifying surviving spouse": "QW",
}


# ------------------------------------------------------------ form names
def norm_form(name):
    """'Schedule C' / 'Sch C' / 'SCH C #2' / 'Form 8863' / '1040' -> canonical id."""
    s = str(name or "").strip().upper()
    inst = 1
    m = re.search(r"#\s*(\d+)\s*$", s)
    if m:
        inst = int(m.group(1))
        s = s[: m.start()].strip()
    s = re.sub(r"^FORM\s+", "", s)
    m = re.match(r"^(SCHEDULE|SCHED|SCH)\.?\s*([A-Z0-9-]+)$", s)
    if m:
        s = "SCH " + m.group(2)
    if s in ("1040-SR", "1040SR"):
        s = "1040"
    return s, inst


def norm_line(line):
    s = str(line or "").strip().lower()
    s = re.sub(r"^line\s*", "", s)
    if s.endswith(".0"):          # Excel turned 31 into 31.0
        s = s[:-2]
    return s


def _identify(page):
    """Return (form_id, is_continuation) for a page, or (None, False)."""
    h = page.height * HEADER_FRACTION
    try:
        head = page.crop((0, 0, page.width, h)).extract_text() or ""
    except Exception:
        head = ""
    full = (page.extract_text() or "")[:400]
    for txt in (head, full):
        cont = bool(re.search(r"\bPage\s+\d\b", txt))
        m = re.search(r"\bSCHEDULE\s+([A-Z0-9]{1,4}(?:-[A-Z]{1,3})?)\b[\s\S]{0,140}?\(Form\s+1040", txt, re.I)
        if m:
            return "SCH " + m.group(1).upper(), cont
        if "U.S. Individual Income Tax Return" in txt or re.search(r"\bForm\s+1040(-SR)?\s*\(\d{4}\)", txt):
            return "1040", cont
        # The form's own web address is the most reliable name (Form 8949 prints its big number on a
        # line of its own, so "Form" is followed by the tax year there).
        m = re.search(r"irs\.gov/Form(W-2G?|\d{3,4}[A-Z]{0,2})\b", txt)
        if m and not m.group(1).startswith("1040"):
            return m.group(1).upper(), cont
        for m in re.finditer(r"(?:^|\n)\s*Form\s+(W-2G?|\d{3,4}[A-Z]{0,2}(?:-[A-Z]{1,3})?)\b", txt):
            f = m.group(1)
            if f.startswith("1040") or re.fullmatch(r"20[1-3]\d", f):
                continue                                          # 'Form 2025' is the tax year, not a form
            return f.upper(), cont
    return None, False


# ------------------------------------------------------------ text helpers
def _norm_txt(t):
    return re.sub(r"[^a-z0-9]", "", str(t).lower())


# Named fields on Form 1040 that are not numbered lines -> how to read them
F1040_FIELDS = {
    "first_name": ("text", 1, "Your first name and middle initial", None),
    "last_name": ("text", 1, "Last name", "Your first name"),
    "ssn": ("text", 1, "Your social security number", None),
    "spouse_first_name": ("text", 1, "If joint return, spouse's first name", None),
    "spouse_last_name": ("text", 1, "Last name", "spouse's first name"),
    "spouse_ssn": ("text", 1, "Spouse's social security number", None),
    "address": ("text", 1, "Home address (number and street)", None),
    "apt": ("text", 1, "Apt. no.", None),
    "city": ("text", 1, "City, town, or post office", None),
    "state": ("text", 1, "State", "ZIP code"),
    "zip": ("text", 1, "ZIP code", None),
    "foreign_country": ("text", 1, "Foreign country name", None),
    "virtual_currency": ("yesno", 1, "virtual currency", None),
    "occupation": ("text", 2, "Your occupation", None),
    "spouse_occupation": ("text", 2, "Spouse's occupation", None),
    "you_as_dependent": ("check", 1, "You as a dependent", None),
    "spouse_as_dependent": ("check", 1, "Your spouse as a dependent", None),
    "you_born_before": ("check", 1, "Were born before January 2", None),
    "you_blind": ("check", 1, "Are blind", None),
    "spouse_born_before": ("check", 1, "Was born before January 2", None),
    "spouse_blind": ("check", 1, "Is blind", None),
}
DEP_FIELDS = ("name", "ssn", "relationship", "ctc", "odc")
FIELD_CATEGORY = {"fs": "Filing Status", "dep": "Dependents"}


# ------------------------------------------------------------ answer key
KEY_COLS = ["Step", "Category", "Form", "Line", "Description", "Correct Value", "Marks",
            "Tolerance ($)", "Feedback if Wrong"]

LINES_1040_2020 = {
    "1": "Wages, salaries, tips", "2a": "Tax-exempt interest", "2b": "Taxable interest",
    "3a": "Qualified dividends", "3b": "Ordinary dividends", "4a": "IRA distributions", "4b": "IRA distributions - taxable",
    "5a": "Pensions and annuities", "5b": "Pensions and annuities - taxable", "6a": "Social security benefits",
    "6b": "Social security benefits - taxable", "7": "Capital gain or (loss)", "8": "Other income from Schedule 1",
    "9": "Total income", "10a": "Adjustments from Schedule 1", "10b": "Charitable contributions (standard deduction filers)",
    "10c": "Total adjustments to income", "11": "Adjusted gross income", "12": "Standard or itemized deduction",
    "13": "Qualified business income deduction", "14": "Add lines 12 and 13", "15": "Taxable income", "16": "Tax",
    "17": "Amount from Schedule 2, line 3", "18": "Add lines 16 and 17", "19": "Child tax credit / credit for other dependents",
    "20": "Amount from Schedule 3, line 7", "21": "Add lines 19 and 20", "22": "Tax after credits",
    "23": "Other taxes (Schedule 2, line 10)", "24": "Total tax", "25a": "Federal tax withheld - Form(s) W-2",
    "25b": "Federal tax withheld - Form(s) 1099", "25c": "Federal tax withheld - other forms",
    "25d": "Total federal tax withheld", "26": "Estimated tax payments", "27": "Earned income credit",
    "28": "Additional child tax credit", "29": "American opportunity credit", "30": "Recovery rebate credit",
    "31": "Amount from Schedule 3, line 13", "32": "Total other payments and refundable credits",
    "33": "Total payments", "34": "Amount overpaid", "35a": "Refund", "36": "Applied to 2021 estimated tax",
    "37": "Amount you owe", "38": "Estimated tax penalty",
}


def guess_category(form, line):
    f, _ = norm_form(form)
    ln = norm_line(line)
    if f != "1040":
        return ("Schedule " + f[4:]) if f.startswith("SCH ") else f"Form {f}"
    if ln in ("fs", "filing status"):
        return "Filing Status"
    if ln.startswith("dep"):
        return "Dependents"
    if ln in F1040_FIELDS:
        return "Personal Information"
    m = re.match(r"^(\d+)", ln)
    n = int(m.group(1)) if m else 0
    return ("Income" if n <= 9 else "Adjustments" if n <= 11 else "Deductions" if n <= 15
            else "Tax & Credits" if n <= 24 else "Payments & Refund")


def describe(form, line, fallback=""):
    f, _ = norm_form(form)
    ln = norm_line(line)
    if f == "1040":
        if ln in LINES_1040_2020:
            return LINES_1040_2020[ln]
        if ln == "fs":
            return "Filing status"
        m = re.match(r"^dep(\d)_(\w+)$", ln)
        if m:
            return f"Dependent {m.group(1)} - " + {"name": "name", "ssn": "SSN", "relationship": "relationship",
                                                   "ctc": "child tax credit box", "odc": "credit for other dependents box"}[m.group(2)]
        if ln in F1040_FIELDS:
            return FIELD_DESC.get(ln, ln.replace("_", " ").capitalize())
    return fallback


FIELD_DESC = {
    "first_name": "First name and middle initial", "last_name": "Last name", "ssn": "Social security number",
    "spouse_first_name": "Spouse's first name and initial", "spouse_last_name": "Spouse's last name",
    "spouse_ssn": "Spouse's social security number", "address": "Home address", "apt": "Apartment number",
    "city": "City / town", "state": "State", "zip": "ZIP code", "foreign_country": "Foreign country",
    "virtual_currency": "Virtual currency question (Yes/No)", "occupation": "Occupation",
    "spouse_occupation": "Spouse's occupation", "you_as_dependent": "Someone can claim you as a dependent",
    "spouse_as_dependent": "Someone can claim your spouse as a dependent",
    "you_born_before": "You were born before 2 Jan 1956", "you_blind": "You are blind",
    "spouse_born_before": "Spouse was born before 2 Jan 1956", "spouse_blind": "Spouse is blind",
}


class KeyError_(Exception):
    pass


@dataclass
class AnswerKey:
    case_id: str
    title: str
    tax_year: int
    steps: list
    unexpected_marks: float = None    # None = don't check; 0 = list only; >0 = deduct per entry

    @property
    def total(self):
        return sum(s["Marks"] for s in self.steps)

    @property
    def categories(self):
        out = []
        for s in self.steps:
            if s["Category"] not in out:
                out.append(s["Category"])
        return out


def load_key(data: bytes) -> AnswerKey:
    wb = load_workbook(io.BytesIO(data), data_only=True)
    if "Answer Key" not in wb.sheetnames:
        raise KeyError_("The workbook has no sheet named 'Answer Key'. Use the template.")
    ws = wb["Answer Key"]
    info, hdr_row = {}, None
    for r in range(1, 30):
        a = ws.cell(r, 1).value
        if a == "Step" and ws.cell(r, 3).value == "Form":
            hdr_row = r
            break
        if isinstance(a, str):
            info[a.strip().rstrip(":").lower()] = ws.cell(r, 2).value
    if hdr_row is None:
        raise KeyError_("Could not find the table header row (Step, Category, Form, Line ...).")
    hdr = [str(ws.cell(hdr_row, c).value or "").strip() for c in range(1, len(KEY_COLS) + 1)]
    if hdr != KEY_COLS:
        raise KeyError_(f"Column headers don't match the current template. Expected: {', '.join(KEY_COLS)}. "
                        "Download the latest template from the app.")
    steps, problems = [], []
    for r in range(hdr_row + 1, ws.max_row + 1):
        row = dict(zip(KEY_COLS, [ws.cell(r, c).value for c in range(1, len(KEY_COLS) + 1)]))
        if row["Form"] in (None, "") and row["Line"] in (None, ""):
            continue
        where = f"Row {r}"
        if row["Form"] in (None, "") or row["Line"] in (None, ""):
            problems.append(f"{where}: Form and Line are both required.")
            continue
        if row["Correct Value"] is None:
            row["Correct Value"] = ""          # blank = the box must be empty
        try:
            row["Marks"] = float(row["Marks"])
        except (TypeError, ValueError):
            problems.append(f"{where}: Marks must be a number.")
            continue
        row["Tolerance ($)"] = float(row["Tolerance ($)"] or 0)
        row["Category"] = str(row["Category"] or guess_category(row["Form"], row["Line"])).strip()
        row["Description"] = row["Description"] or describe(row["Form"], row["Line"], f"{row['Form']} line {row['Line']}")
        row["Step"] = row["Step"] if row["Step"] is not None else len(steps) + 1
        f, inst = norm_form(row["Form"])
        row["_key"] = (f, inst, norm_line(row["Line"]))
        steps.append(row)
    if problems:
        raise KeyError_("\n".join(problems))
    if not steps:
        raise KeyError_("The answer key has no steps.")
    try:
        year = int(info.get("tax year") or 2020)
    except ValueError:
        year = 2020
    um = info.get("marks deducted per unexpected entry")
    try:
        um = None if um in (None, "") else float(um)
    except ValueError:
        um = None
    return AnswerKey(str(info.get("case id") or "CASE"), str(info.get("case title") or ""), year, steps, um)


# ------------------------------------------------------------- grading
def _is_num(x):
    if isinstance(x, bool):
        return False
    try:
        float(str(x).replace(",", "").replace("$", ""))
        return True
    except (TypeError, ValueError):
        return False


def _num(x):
    return float(str(x).replace(",", "").replace("$", ""))


def _clean_text(x):
    t = re.sub(r"\s+", " ", str(x)).strip().strip(",.;").upper()
    return t


def _match(student, correct, tol):
    if student is None:
        return False
    c, s = correct, student
    if isinstance(c, str) and c.strip() == "":
        return (str(s).strip() == "") or (_is_num(s) and _num(s) == 0) or str(s) == "Unchecked"
    if _is_num(c) and _is_num(s) and not re.search(r"\d-\d", str(c)):
        return abs(_num(s) - _num(c)) <= float(tol or 0) + 1e-9
    cs, ss_ = str(c).strip().lower(), str(s).strip().lower()
    if cs in FILING_STATUS or ss_ in FILING_STATUS:
        return FILING_STATUS.get(cs, cs.upper()) == FILING_STATUS.get(ss_, ss_.upper())
    if re.fullmatch(r"[\d\s-]{9,11}", str(c).strip()):          # SSN / EIN: compare digits
        return re.sub(r"\D", "", str(c)) == re.sub(r"\D", "", str(s))
    if cs in ("x", "checked", "yes-checked"):
        return ss_ in ("x", "checked", "\u2713", "\u2714")
    if cs in ("unchecked", "blank"):
        return ss_ in ("unchecked", "")
    return _clean_text(c) == _clean_text(s)


def grade(ret, key: AnswerKey):
    rows = []
    for s in key.steps:
        val, note = ret.value(s["Form"], s["Line"])
        ok = _match(val, s["Correct Value"], s["Tolerance ($)"])
        rows.append({
            "Step": s["Step"], "Category": s["Category"], "Form": s["Form"], "Line": s["Line"],
            "Description": s["Description"], "Correct Value": s["Correct Value"], "Student Value": val,
            "Marks": s["Marks"], "Awarded": s["Marks"] if ok else 0.0,
            "Result": "Correct" if ok else "Wrong",
            "Feedback": "" if ok else (s["Feedback if Wrong"] or ""),
            "Note": note, "Needs Review": val is None and "not found in the return" not in note,
        })
    if key.unexpected_marks is not None:
        keyed = {s["_key"] for s in key.steps}
        n = len(rows)
        for fld in ret.all_fields():
            f, inst = norm_form(fld["form"])
            k_ = (f, inst, norm_line(fld["line"]))
            ln_ = norm_line(fld["line"])
            if (k_ in keyed or ln_.startswith("dep") or ln_ in F1040_FIELDS or ln_ == "fs"
                    or fld.get("unlinked")):
                continue            # only amounts on numbered lines count as unexpected entries
            v = fld["value"]
            if v in ("", 0, 0.0, None, "Unchecked"):
                continue
            n += 1
            rows.append({
                "Step": n, "Category": "Unexpected entries", "Form": fld["form"], "Line": fld["line"],
                "Description": (fld["desc"] if fld.get("prefer_desc") and fld.get("desc") else
                                describe(fld["form"], fld["line"], f"{fld['form']} line {fld['line']}")),
                "Correct Value": "", "Student Value": v, "Marks": 0.0, "Awarded": -float(key.unexpected_marks),
                "Result": "Unexpected entry",
                "Feedback": "This line should be blank for this case. Remove the entry and check where it came from.",
                "Note": "", "Needs Review": False})
    return rows


def summarise(rows):
    cats = {}
    for r in rows:
        c = cats.setdefault(r["Category"], {"Category": r["Category"], "Awarded": 0.0, "Marks": 0.0,
                                             "Steps": 0, "Mistakes": 0})
        c["Awarded"] += r["Awarded"]
        c["Marks"] += r["Marks"]
        c["Steps"] += 1
        c["Mistakes"] += r["Result"] != "Correct"
    for c in cats.values():
        c["Pct"] = max(0, c["Awarded"]) / c["Marks"] if c["Marks"] else 0
    out_of = sum(r["Marks"] for r in rows)
    total = max(0.0, sum(r["Awarded"] for r in rows))
    return list(cats.values()), total, out_of


FS_NAMES = {"S": "Single", "MFJ": "Married filing jointly", "MFS": "Married filing separately",
            "HOH": "Head of household", "QW": "Qualifying widow(er)"}


def fmt(v):
    if v is None:
        return "Not found"
    if isinstance(v, str) and v.strip().upper() in FS_NAMES:
        return FS_NAMES[v.strip().upper()]
    if _is_num(v) and not isinstance(v, str):
        v = float(v)
        return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    return str(v)
