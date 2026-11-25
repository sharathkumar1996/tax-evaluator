"""
readers.py - one entry point for reading a student's return.

    ret = read_return([(file_name, pdf_bytes), ...])

Returns are IRS fillable forms, saved after filling in. A student may upload one PDF per form
(1040, Schedule 1, Schedule C ...); they are combined into one return by irs_reader.IRSReturn.
A printed or flattened PDF (no fillable boxes left) is refused with advice to upload the saved form.
An empty (0-byte) file is skipped with a note, like a form that was not submitted; the rest is still marked.
Problems the user can fix are raised as ReaderError with a plain-language message.
"""
import io

from pypdf import PdfReader

from irs_reader import FORMAT_NAME as IRS_NAME  # noqa: F401  (re-exported for the app and tests)
from irs_reader import IRSReturn, ReaderError

MAX_FILE_MB = 25
MAX_FILES_PER_RETURN = 40
MAX_PAGES = 120


def _check_limits(files):
    if not files:
        raise ReaderError("No files were uploaded.")
    if len(files) > MAX_FILES_PER_RETURN:
        raise ReaderError(f"{len(files)} files for one return; the limit is {MAX_FILES_PER_RETURN}.")
    for name, data in files:
        if not data:
            continue                                  # empty files are skipped in read_return, not fatal
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            raise ReaderError(f"{name} is larger than {MAX_FILE_MB} MB.")
        if not data[:1024].lstrip().startswith(b"%PDF"):
            raise ReaderError(f"{name} is not a PDF file.")


def _opens(data):
    try:
        PdfReader(io.BytesIO(data)).pages[0]
        return True
    except Exception:
        return False


def read_return(files):
    """files: list of (file name, bytes) belonging to ONE student's return."""
    _check_limits(files)
    # An empty (0-byte) file is treated like a form that was not submitted: the other forms are still
    # marked and the missing form loses its marks, the same as for a student who left the file out.
    empty = [n for n, d in files if not d]
    files = [(n, d) for n, d in files if d]
    if not files:
        raise ReaderError(f"{', '.join(empty)}: the file is empty. Ask the student to upload the saved form again.")
    try:
        ret = IRSReturn(files, max_pages=MAX_PAGES)
    except ReaderError as e:
        damaged = [n.split("/")[-1] for n, d in files if not _opens(d)]      # checked only when reading failed
        if damaged:
            raise ReaderError(f"{', '.join(damaged)}: the file is damaged or incomplete and cannot be opened. "
                              "Ask the student to upload it again.") from e
        raise
    for name in empty:
        ret.page_map.insert(0, (name, "(empty file - not read)"))
        ret.notes.insert(0, f"{name}: the file is empty (nothing was saved in it) - treated as not submitted, "
                            "so this form was not marked. Ask the student to save the filled form and upload it again.")
    return ret
