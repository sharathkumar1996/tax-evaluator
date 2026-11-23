"""Students who are not on our roster are flagged and shaded in Simandhar maroon everywhere."""
import io

import pandas as pd
from openpyxl import load_workbook

from roster_flag import (MAROON_HEX, MAROON_LIGHT_HEX, NOT_ON_ROSTER, add_roster_column, callout_html, excel_bytes,
                         not_on_roster, off_roster_ids, style_off_roster)

ROSTER = ["164594", "164603"]


def test_roster_column_marks_unknown_ids_and_ignores_blank_ones():
    df = pd.DataFrame({"File": ["a", "b", "c"], "Student ID": ["164594", "999001", ""]})
    out = add_roster_column(df, ROSTER)
    assert list(out.columns) == ["File", "Student ID", "Roster"]
    assert list(out["Roster"]) == ["On roster", NOT_ON_ROSTER, ""]
    assert off_roster_ids(df["Student ID"], ROSTER) == ["999001"]


def test_rows_are_shaded_in_brand_colours_on_screen_and_in_excel():
    df = add_roster_column(pd.DataFrame({"Student ID": ["164594", "999001"]}), ROSTER)
    html = style_off_roster(df).to_html()
    assert MAROON_LIGHT_HEX.lower() in html.lower() and MAROON_HEX.lower() in html.lower()
    ws = load_workbook(io.BytesIO(excel_bytes([("Results", df)])))["Results"]
    assert ws["A3"].fill.fgColor.rgb.endswith(MAROON_LIGHT_HEX[1:]) and ws["A3"].font.bold
    assert not ws["A2"].font.bold
    assert NOT_ON_ROSTER in callout_html(["999001"]) and "999001" in callout_html(["999001"])


def test_not_on_roster_lists_evaluated_ids_missing_from_the_roster():
    evs = [{"student_id": "164594", "case_id": "M01"}, {"student_id": "999001", "case_id": "M01", "file_name": "x.pdf"},
           {"student_id": "999001", "case_id": "PRAC"}]
    df = not_on_roster(evs, ROSTER, case_ids=["M01"])
    assert df.to_dict("records") == [{"Student ID": "999001", "Roster": NOT_ON_ROSTER, "Returns evaluated": 1,
                                      "Cases": "M01", "Last file": "x.pdf"}]
