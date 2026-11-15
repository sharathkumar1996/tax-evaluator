"""
readers.py - one entry point for reading a student's return, whatever it was made with.

    ret = read_return([(file_name, pdf_bytes), ...])

* Files with fillable boxes (IRS fillable forms, saved after filling) -> irs_reader.IRSReturn.
  A student may upload one PDF per form; they are combined into one return.
* Otherwise the file is treated as a return printed from ATX -> grader_core.ReturnPDF.
  Several ATX PDFs for one student are joined in file-name order.

Both readers offer the same methods (value, all_fields, forms_found, page_map, notes,
unlinked), so everything after reading - marking, reports, database - is shared.
Problems the user can fix are raised as ReaderError with a plain-language message.
"""
import io

from pypdf import PdfReader, PdfWriter

from grader_core import ReturnPDF
from irs_reader import FORMAT_NAME as IRS_NAME
from irs_reader import IRSReturn, ReaderError, has_form_fields

ATX_NAME = "ATX printout"
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


def detect_format(files):
    return IRS_NAME if any(has_form_fields(d) for _, d in files) else ATX_NAME


def _join_pdfs(files):
    w = PdfWriter()
    for _, data in sorted(files, key=lambda f: f[0].lower()):
        for p in PdfReader(io.BytesIO(data)).pages:
            w.add_page(p)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def read_return(files):
    """files: list of (file name, bytes) belonging to ONE student's return."""
    _check_limits(files)
    if detect_format(files) == IRS_NAME:
        return IRSReturn(files, max_pages=MAX_PAGES)
    try:
        data = files[0][1] if len(files) == 1 else _join_pdfs(files)
        ret = ReturnPDF(data)
    except ReaderError:
        raise
    except Exception as e:
        raise ReaderError(f"Could not read the PDF ({type(e).__name__}). Is it a complete return printed from ATX?")
    if ret.n_pages > MAX_PAGES:
        raise ReaderError(f"The return has {ret.n_pages} pages; the limit is {MAX_PAGES}.")
    if not ret.copies:
        raise ReaderError("No IRS form pages were recognised. Upload the complete return printed to PDF from ATX, "
                          "or the filled-in IRS fillable forms saved as PDFs.")
    return ret
