"""Simandhar Education look for every PDF the app produces: maroon headings and table headers,
Calibri-style type (Carlito, metric-compatible with Calibri and free to bundle - SIL Open Font Licence),
light-maroon panels and a thin maroon rule under the title."""
from pathlib import Path

from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

MAROON = colors.HexColor("#8E1B3A")
MAROON_LIGHT = colors.HexColor("#F7ECEF")     # call-out panels
ZEBRA = colors.HexColor("#FBF6F7")            # alternate table rows
LINE = colors.HexColor("#E4D3D8")
GREY = colors.HexColor("#5F5F5F")
RED = colors.HexColor("#C0392B")
AMBER = colors.HexColor("#B7791F")
GREEN = colors.HexColor("#2E7D4F")

FONT, FONT_BOLD = "Helvetica", "Helvetica-Bold"
_dir = Path(__file__).parent / "fonts"
try:
    pdfmetrics.registerFont(TTFont("Carlito", str(_dir / "Carlito-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Carlito-Bold", str(_dir / "Carlito-Bold.ttf")))
    pdfmetrics.registerFontFamily("Carlito", normal="Carlito", bold="Carlito-Bold", italic="Carlito",
                                  boldItalic="Carlito-Bold")
    FONT, FONT_BOLD = "Carlito", "Carlito-Bold"
except Exception:          # fonts missing: fall back to Helvetica rather than fail a report
    pass


def footer_text(institute, what, case_id):
    return f"{institute}  •  {what}  •  Case {case_id}"
