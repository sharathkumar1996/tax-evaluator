"""
readers.py - one entry point for reading a student's return.

    ret = read_return([(file_name, pdf_bytes), ...])

Returns are IRS fillable forms, saved after filling in. A student may upload one PDF per form
(1040, Schedule 1, Schedule C ...); they are combined into one return by irs_reader.IRSReturn.
A printed or flattened PDF (no fillable boxes left) is refused with advice to upload the saved form.
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
            raise ReaderError(f"{name} is empty.")
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
    try:
        return IRSReturn(files, max_pages=MAX_PAGES)
    except ReaderError as e:
        damaged = [n.split("/")[-1] for n, d in files if not _opens(d)]      # checked only when reading failed
        if damaged:
            raise ReaderError(f"{', '.join(damaged)}: the file is damaged or incomplete and cannot be opened. "
                              "Ask the student to upload it again.") from e
        raise
