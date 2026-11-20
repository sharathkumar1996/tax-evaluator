"""
bulk.py - turns a bulk upload (PDFs and/or zip files) into one group of files per student.

Accepted layouts (any mix):
  S0123_C03.pdf                      one PDF per student (ATX printout, or merged forms)
  S0123_C03_1040.pdf, S0123_C03_SchC.pdf
                                     several PDFs per student: StudentID_CaseID_<anything>.pdf
  S0123_C03/1040.pdf, S0123_C03/SchC.pdf
                                     a folder per student named StudentID_CaseID (inside a zip, e.g. a
                                     OneDrive folder download); a zip inside that folder is opened too

When the case (and ideally the roster) is known, names that students get slightly wrong are still
matched, and every such match is reported as a warning on that student's row so it can be checked:
  164108-M01, "164231 _M01", "164233_M01 1" (OneDrive's copy suffix), Ravi_164236_M01, M01_164234,
  164237_M1 (= M01), a folder named only 164235 (no mock code), "Emp id164244.pdf".
Student folders with no PDF in them (empty, only a zip/images/Word files) are reported as skipped,
so a student who uploaded the wrong thing is not silently treated as "not submitted".

Safety limits protect the server from oversized or malicious zips ("zip bombs"): number of
files, size of each file, total unpacked size and compression ratio are all checked before
anything is unpacked. Zip members are read one at a time, only when that student is marked.
"""
import io
import re
import zipfile
from dataclasses import dataclass, field

MAX_MEMBERS = 5000
MAX_PDF_MB = 25
MAX_TOTAL_MB = 1500
MAX_RATIO = 200          # unpacked size / packed size for one member

FOLDER = re.compile(r"^([A-Za-z0-9-]+)_([A-Za-z0-9-]+)$")
FILE = re.compile(r"^([A-Za-z0-9-]+)_([A-Za-z0-9-]+)(?:_[^/]*)?\.pdf$", re.I)
CASE_LIKE = re.compile(r"^[A-Z]{1,3}-?\d{1,3}$")
MIN_ID_DIGITS = 5        # without a roster, a bare number must be this long to count as a student ID


@dataclass
class Group:
    student_id: str
    case_id: str
    files: list = field(default_factory=list)      # [(display name, loader)]
    warnings: list = field(default_factory=list)

    def load(self):
        return [(n, f()) for n, f in self.files]

    @property
    def label(self):
        return ", ".join(n for n, _ in self.files)


class BulkError(Exception):
    pass


def same_case(a, b):
    """'M1' == 'M01' == 'm-01'; 'C03' == 'C3'."""
    def norm(x):
        m = re.fullmatch(r"([A-Z]*)-?0*(\d+)", str(x or "").upper().strip())
        return (m.group(1), int(m.group(2))) if m else str(x or "").upper().strip()
    return norm(a) == norm(b)


def _check_zip(name, z):
    infos = [i for i in z.infolist()]
    if len(infos) > MAX_MEMBERS:
        raise BulkError(f"{name} contains {len(infos)} files; the limit is {MAX_MEMBERS}. Split it into parts.")
    total = 0
    for i in infos:
        total += i.file_size
        if i.file_size > MAX_PDF_MB * 1024 * 1024 and i.filename.lower().endswith((".pdf", ".zip")):
            raise BulkError(f"{i.filename} in {name} is larger than {MAX_PDF_MB} MB.")
        if i.compress_size and i.file_size / i.compress_size > MAX_RATIO and i.file_size > 1024 * 1024:
            raise BulkError(f"{name} looks unsafe (a file inflates more than {MAX_RATIO} times).")
    if total > MAX_TOTAL_MB * 1024 * 1024:
        raise BulkError(f"{name} unpacks to more than {MAX_TOTAL_MB} MB. Split it into parts.")
    return infos


def _junk(path):
    base = path.rstrip("/").split("/")[-1]
    return path.startswith("__MACOSX/") or "/__MACOSX/" in path or base.startswith(".") or base in ("Thumbs.db",
                                                                                                   "desktop.ini")


def _entries(name, data, depth=0, prefix=""):
    """Every item in an upload: (path, loader or None for a folder, kind) with kind 'pdf' / 'dir' / 'other'.
    A zip inside a zip (a student who zipped their folder) is opened once, as a folder of the same name."""
    if not name.lower().endswith(".zip"):
        yield prefix + name, (lambda d=data: d), ("pdf" if name.lower().endswith(".pdf") else "other")
        return
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        if depth:
            yield prefix + name, None, "other"
            return
        raise BulkError(f"{name} is not a valid zip file.")
    infos = _check_zip(name, z)
    for i in infos:
        path = prefix + i.filename.replace("\\", "/")
        if _junk(path):
            continue
        if i.is_dir():
            yield path.rstrip("/"), None, "dir"
        elif path.lower().endswith(".pdf"):
            yield path, (lambda z=z, n=i.filename: z.read(n)), "pdf"
        elif path.lower().endswith(".zip") and depth == 0:
            inner = path[:-4]                                    # "164239_M01/164239_M01.zip" -> that folder
            try:
                inner_items = list(_entries(path.split("/")[-1], z.read(i.filename), 1, inner + "/"))
            except BulkError:
                inner_items = None
            if not inner_items or inner_items == [(inner + "/" + path.split("/")[-1], None, "other")]:
                yield path, None, "other"                                # unreadable zip: report it as is
            else:
                yield inner, None, "dir"
                yield from inner_items
        else:
            yield path, None, "other"


class _Namer:
    """Finds (student ID, case ID, warning) in a folder or file name."""

    def __init__(self, case_id=None, roster_ids=None):
        self.case = (case_id or "").upper() or None
        self.roster = {str(x).strip().upper() for x in (roster_ids or []) if str(x).strip()}

    def _fix_case(self, case):
        if self.case and case != self.case and same_case(case, self.case):
            return self.case
        return case

    def strict(self, text, is_file):
        m = (FILE if is_file else FOLDER).match(text)
        if not m:
            return None
        sid, case = m.group(1).upper(), m.group(2).upper()
        if self.case:
            if not CASE_LIKE.match(case) and CASE_LIKE.match(sid) and same_case(sid, self.case):
                return case, self.case, f"name '{text}' has the mock code first"           # M01_164234
            if not CASE_LIKE.match(case):
                return None                                                             # e.g. Batch_1
            id_warn = None
            if self.roster and sid not in self.roster and not CASE_LIKE.match(sid):
                found = self._ids(text)
                if len(found) == 1 and found[0] != sid:
                    id_warn = f"name '{text}': ID {sid} is not on the roster - read as {found[0]}"
                    sid = found[0]
        else:
            id_warn = None
        fixed = self._fix_case(case)
        warn = f"name '{text}' has mock code {case}, read as {fixed}" if fixed != case else None
        return sid, fixed, id_warn or warn

    def _ids(self, text):
        toks = [t.upper() for t in re.findall(r"[A-Za-z0-9]+", text)]
        if self.roster:
            hit = [t for t in toks if t in self.roster]
            if not hit:
                hit = [d for d in re.findall(r"\d+", text) if d in self.roster]
            return list(dict.fromkeys(hit))
        return list(dict.fromkeys(d for d in re.findall(r"\d+", text) if len(d) >= MIN_ID_DIGITS))

    def loose(self, text, is_file):
        """Tolerant match - only when the case being marked is known."""
        if not self.case:
            return None
        stem = text[:-4] if is_file and text.lower().endswith(".pdf") else text
        ids = self._ids(stem)
        if len(ids) != 1:
            return None
        toks = re.findall(r"[A-Za-z]+-?\d+|[A-Za-z0-9]+", stem)
        cases = [t.upper() for t in toks if CASE_LIKE.match(t.upper()) and t.upper() != ids[0]]
        mine = [c for c in cases if same_case(c, self.case)]
        if mine:
            return ids[0], self.case, f"name '{text}' does not follow StudentID_{self.case} - matched to this student"
        if cases:
            return ids[0], cases[0], None                                               # another mock
        return ids[0], self.case, f"name '{text}' has no mock code - taken as {self.case}"

    def find(self, path):
        """(sid, case, warning, owner folder) for a PDF's path; None if no student can be identified."""
        parts = path.split("/")
        folders, base = parts[:-1], parts[-1]
        for k in range(len(folders) - 1, -1, -1):                     # nearest correctly named folder
            f = folders[k]
            r = self.strict(f.strip(), False)
            if r:
                warn = r[2] or (f"name '{f}' has extra spaces" if f != f.strip() else None)
                return r[0], r[1], warn, "/".join(folders[:k + 1])
        r = self.strict(base, True)
        if r:
            return (*r, None)
        for k in range(len(folders) - 1, -1, -1):                     # then nearest folder with an ID in it
            r = self.loose(folders[k], False)
            if r:
                return (*r, "/".join(folders[:k + 1]))
        r = self.loose(base, True)
        return (*r, None) if r else None


def group_uploads(uploads, case_id=None, roster_ids=None):
    """uploads: list of (name, bytes). case_id / roster_ids (optional) let near-miss names be matched.
    Returns (groups, skipped) where skipped = [(name, reason)]."""
    namer = _Namer(case_id, roster_ids)
    groups, skipped, dirs, others = {}, [], set(), []
    for name, data in uploads:
        try:
            entries = list(_entries(name, data))
        except BulkError as e:
            skipped.append((name, str(e)))
            continue
        for path, loader, kind in entries:
            if kind == "dir":
                dirs.add(path)
                continue
            if kind == "other":
                others.append(path)
                continue
            hit = namer.find(path)
            if hit is None:
                skipped.append((path, "Skipped - name is not StudentID_CaseID.pdf (or a folder StudentID_CaseID)"))
                continue
            sid, case, warn, owner = hit
            k = (sid.upper(), case.upper())
            g = groups.setdefault(k, Group(*k))
            g.files.append((path, loader))
            if warn and warn not in g.warnings:
                g.warnings.append(warn)
            if owner:
                g.__dict__.setdefault("_owners", set()).add(owner)

    owners = {o for g in groups.values() for o in g.__dict__.get("_owners", ())}
    for g in groups.values():
        n_owners = len(g.__dict__.get("_owners", ()))
        if n_owners > 1:
            g.warnings.append(f"files came from {n_owners} different folders - check they are one return")
        extra = [p for p in others if any(p.startswith(o + "/") for o in g.__dict__.get("_owners", ()))]
        if extra:
            g.warnings.append("ignored non-PDF files: " + ", ".join(p.split("/")[-1] for p in extra)[:200])

    # student folders that produced nothing: empty, or only zips / images / Word files
    reported = set()
    ancestors = set()
    for p in list(dirs) + others:
        bits = p.split("/")
        ancestors.update("/".join(bits[:k]) for k in range(1, len(bits)))
    for d in sorted(dirs | ancestors, key=lambda x: -x.count("/")):
        name = d.split("/")[-1]
        hit = namer.strict(name.strip(), False) or namer.loose(name, False)
        if not hit or any(o == d or o.startswith(d + "/") or d.startswith(o + "/") for o in owners):
            continue
        if any(r.startswith(d + "/") for r in reported):
            continue                                                  # inner folder already reported
        reported.add(d)
        inside = [p.split("/")[-1] for p in others if p.startswith(d + "/")]
        what = ("only " + ", ".join(inside))[:150] if inside else "nothing (empty folder)"
        skipped.append((d + "/", f"Skipped - student {hit[0]}'s folder has no PDF files: {what}"))
    for p in others:                                                  # stray non-PDF files elsewhere
        if not any(p.startswith(x + "/") for x in owners | reported):
            skipped.append((p, "Skipped - not a PDF"))

    out = sorted(groups.values(), key=lambda g: (g.case_id, g.student_id))
    for g in out:
        g.files.sort(key=lambda f: f[0].lower())
        g.__dict__.pop("_owners", None)
    return out, skipped
