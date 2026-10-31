# Tax Return Evaluator (ATX → evaluation report)

Staff upload a case **answer key** (Excel) once, then upload **one student return** (ATX PDF,
all pages and schedules) at a time. The app checks every item in the key and produces a
**PDF evaluation report** named `Evaluation_report_<StudentID>_<CaseID>.pdf`: overall score,
marks by category, and every mistake (form + line, student entry, correct entry, marks lost, feedback).

Marking is strict: any difference from the key loses that item's marks. Amounts entered on lines
that should be blank ("unexpected entries") can also deduct marks.

Nothing is stored on the server. The key, returns and results exist only in the open browser tab.

## Files
| File | Purpose |
|---|---|
| `app.py` | The Streamlit app |
| `grader_core.py` | Reads ATX PDFs (1040 + schedules/forms) and marks them against the key |
| `report_pdf.py` | Builds the student PDF report |
| `answer_key_template.xlsx` | Blank key for trainers (downloadable inside the app) |
| `make_keys.py` | Template builder and the 'build key from a correct return' function |
| `requirements.txt`, `.streamlit/config.toml` | Deployment settings |
| `tests/make_mock_complex.py` | Test-only mock multi-schedule return |

## Deploy for free on Streamlit Community Cloud (about 15 minutes)

1. **GitHub.** Create a free account at github.com. Click **New repository**, name it
   `tax-evaluator`, choose **Private**, then **Create**.
2. **Upload the code.** In the new repo, click **Add file → Upload files**. Drag in *everything
   from this folder*, including the `.streamlit` folder, and click **Commit changes**.
   Never upload answer keys with real answers or student returns. `.gitignore` blocks them
   if you use git.
3. **Streamlit.** Go to share.streamlit.io, sign in with GitHub, and allow access to the
   private repo when asked.
4. Click **Create app** and choose to deploy from a GitHub repository. Then set:
   - Repository: `your-name/tax-evaluator` · Branch: `main` · Main file: `app.py`
   - App URL: choose one, e.g. `simandhar-tax-eval`
5. Open **Advanced settings**. Set Python to **3.12** and paste into **Secrets**:
   ```toml
   app_password = "choose-a-staff-password"
   institute = "Simandhar Education"
   ```
6. Click **Deploy**. The first build takes a few minutes. Share the URL and password with staff only.

Notes
- The free plan allows **one private app** (from a private repo) and any number of public apps.
  The staff password protects the app either way.
- An app with no visitors for 12 hours goes to sleep. The next visitor clicks
  the wake-up button and waits about a minute.
- To update the app, edit or re-upload files on GitHub. Streamlit redeploys automatically.

## Run on a laptop instead
```
pip install -r requirements.txt
streamlit run app.py
```

## Writing answer keys
Fastest: in the app, open **Build a key from a correct return** and upload the trainer's correct
ATX return. You get a workbook listing every filled-in item on every form. Set marks, categories and
feedback, then upload it as the key.

Each row is Category, Form (`1040`, `Schedule C`, `Schedule SE`, `Form 8863`, `Schedule C #2` ...),
Line, Correct Value, Marks, Tolerance and Feedback. Line can be:
- a printed line number: `1`, `2b`, `25a`, `31` (lettered lines like Schedule C `B` work too)
- a column on a multi-column row: `1a (h)`, `8a (d)` on Schedule D (the column letter printed above the row)
- a Form 1040 field: `FS`, `first_name`, `last_name`, `ssn`, `spouse_first_name`, `spouse_last_name`,
  `spouse_ssn`, `address`, `apt`, `city`, `state`, `zip`, `virtual_currency`, `occupation`,
  `spouse_occupation`, and the age/blindness/dependent-of-someone checkboxes
- dependents: `dep1_name`, `dep1_ssn`, `dep1_relationship`, `dep1_ctc`, `dep1_odc` (to `dep4_...`)
- any other box: `text: <printed label>` or `check: <printed label>` (add `| near: <words>` if needed)

Leave Correct Value blank when the box must be empty.

## Limits to know
- Calibrated on **ATX 2020** output. A different ATX year or version: run one return through it and
  check the "What the system read" panel.
- Form tables with many rows (e.g. Form 8949 transaction lists) are checked through their numbered
  total lines, not row by row.
- PDFs must be printed from ATX, not scanned.
