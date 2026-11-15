"""
bulk.py - turns a bulk upload (PDFs and/or zip files) into one group of files per student.

Accepted layouts (any mix):
  S0123_C03.pdf                      one PDF per student (ATX printout, or merged forms)
  S0123_C03_1040.pdf, S0123_C03_SchC.pdf
                                     several PDFs per student: StudentID_CaseID_<anything>.pdf
  S0123_C03/1040.pdf, S0123_C03/SchC.pdf
                                     a folder per student named StudentID_CaseID (inside a zip)

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


@dataclass
class Group:
    student_id: str
    case_id: str
    files: list = field(default_factory=list)      # [(display name, loader)]

    def load(self):
        return [(n, f()) for n, f in self.files]

    @property
    def label(self):
        return ", ".join(n for n, _ in self.files)


class BulkError(Exception):
    pass


def _members(name, data):
    """(path, loader) for every PDF in an upload; raises BulkError for unsafe zips."""
    if not name.lower().endswith(".zip"):
        yield name, (lambda d=data: d)
        return
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise BulkError(f"{name} is not a valid zip file.")
    infos = [i for i in z.infolist() if not i.is_dir()]
    if len(infos) > MAX_MEMBERS:
        raise BulkError(f"{name} contains {len(infos)} files; the limit is {MAX_MEMBERS}. Split it into parts.")
    total = 0
    for i in infos:
        total += i.file_size
        if i.file_size > MAX_PDF_MB * 1024 * 1024 and i.filename.lower().endswith(".pdf"):
            raise BulkError(f"{i.filename} in {name} is larger than {MAX_PDF_MB} MB.")
        if i.compress_size and i.file_size / i.compress_size > MAX_RATIO and i.file_size > 1024 * 1024:
            raise BulkError(f"{name} looks unsafe (a file inflates more than {MAX_RATIO} times).")
    if total > MAX_TOTAL_MB * 1024 * 1024:
        raise BulkError(f"{name} unpacks to more than {MAX_TOTAL_MB} MB. Split it into parts.")
    for i in infos:
        path = i.filename.replace("\\", "/")
        base = path.split("/")[-1]
        if path.startswith("__MACOSX/") or base.startswith(".") or not base.lower().endswith(".pdf"):
            continue
        yield path, (lambda z=z, n=i.filename: z.read(n))


def group_uploads(uploads):
    """uploads: list of (name, bytes). Returns (groups, skipped) where skipped = [(name, reason)]."""
    groups, skipped = {}, []
    for name, data in uploads:
        try:
            members = list(_members(name, data))
        except BulkError as e:
            skipped.append((name, str(e)))
            continue
        for path, loader in members:
            parts = path.split("/")
            base = parts[-1]
            sid = case = None
            for folder in reversed(parts[:-1]):           # nearest folder named StudentID_CaseID
                m = FOLDER.match(folder)
                if m:
                    sid, case = m.group(1), m.group(2)
                    break
            if sid is None:
                m = FILE.match(base)
                if m:
                    sid, case = m.group(1), m.group(2)
            if sid is None:
                skipped.append((path, "Skipped - name is not StudentID_CaseID.pdf (or a folder StudentID_CaseID)"))
                continue
            k = (sid.upper(), case.upper())
            groups.setdefault(k, Group(*k)).files.append((path, loader))
    out = sorted(groups.values(), key=lambda g: (g.case_id, g.student_id))
    for g in out:
        g.files.sort(key=lambda f: f[0].lower())
    return out, skipped
