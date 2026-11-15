# Tax Return Evaluator (ATX or IRS fillable forms → evaluation report)

Staff upload a case **answer key** (Excel) once, then upload student returns one at a time or in bulk.
A return can be either
- an **ATX printout** (the complete return printed to PDF from ATX), or
- the **IRS fillable forms** (Form 1040, schedules and other forms from irs.gov, filled in and *saved*).
  One PDF per form is fine.

The app recognises which kind each return is. The app checks every item in the key and produces a
**PDF evaluation report** named `Evaluation_report_<StudentID>_<CaseID>.pdf`: overall score,
marks by category, and every mistake (form + line, student entry, correct entry, marks lost, feedback).

Marking is strict: any difference from the key loses that item's marks. Amounts entered on lines
that should be blank ("unexpected entries") can also deduct marks.

Nothing is stored on the server. The key, returns and results exist only in the open browser tab.

## Files
| File | Purpose |
|---|---|
| `app.py` | The Streamlit app |
| `readers.py` | Chooses the reader for each return and checks file limits |
| `irs_reader.py` | Reads IRS fillable forms (the values stored in the form's boxes) |
| `grader_core.py` | Reads ATX PDFs (1040 + schedules/forms) and marks returns against the key |
| `integrity.py`, `integrity_report.py` | Copying checks and the integrity report |
| `bulk.py` | Groups a bulk upload (PDFs / zips) into one return per student, with zip safety limits |
| `report_pdf.py` | Builds the student PDF report |
| `db.py` | Saves students, batches, case keys, scores and mistakes (Neon Postgres) |
| `metrics.py` | File-ready rate, bottom-line accuracy, average errors per return |
| `mgmt_report.py` | Management PDF / Excel and the consolidated student report |
| `answer_key_template.xlsx` | Blank key for trainers (downloadable inside the app) |
| `make_keys.py` | Template builder and the 'build key from a correct return' function |
| `requirements.txt`, `.streamlit/config.toml` | Deployment settings |
| `tests/` | Automated tests (`python -m pytest tests`), with a blank 2025 Form 1040 as a test form |

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
   - Repository: `your-name/tax-evaluator` · Branch: `irs-fillable` (this version) · Main file: `app.py`
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

## Saving results permanently (Neon database)

Without a database the app works, but results disappear when the browser tab closes.
With one, every evaluation is saved, and the **Management dashboard**, **Find a report** and
**Students & batches** tabs become available.

1. Create a free account at neon.com and a project (any name, region close to your users).
2. On the project dashboard click **Connect** and copy the connection string. It looks like
   `postgresql://neondb_owner:...@ep-....neon.tech/neondb?sslmode=require`
3. In Streamlit, open the app's **Settings → Secrets** and add a line:
   ```toml
   database_url = "postgresql://neondb_owner:...@ep-....neon.tech/neondb?sslmode=require"
   ```
4. Save. The app restarts, creates its tables, and the sidebar shows **Database connected**.

Only scores and mistakes are stored, not the returns: about 20 rows per return, so 7,200 returns
fit easily in the free 1 GB. The sidebar shows how much space is used.

## Erasing data
The **Data management** tab erases one case's results, a whole case, a batch, one student, results of IDs
not on the roster, or everything. It is protected by `admin_password` (Secrets); if that is not set, the
staff password is used. Erasing cannot be undone.

Tip: give the test app its own Neon branch (Neon: Branches -> Create branch) and use that branch's
connection string in the test app, so test data never mixes with real results.

## Going back to the previous version

The branch `database` is the ATX-only version with the database; `stable-v1` is the version without a database. To go back, open the app's
**Settings** in Streamlit and change the branch to `stable-v1`, or deploy a second app from it.
Data already saved in Neon is not affected.

## Run on a laptop instead
```
pip install -r requirements.txt
streamlit run app.py
```

## Writing answer keys
Fastest: in the app, open **Build a key from a correct return** and upload the trainer's correct
return (ATX printout or fillable forms). You get a workbook listing every filled-in item on every form. Set marks, categories and
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

## Bulk evaluation
Choose **Many returns at once (bulk)**, then upload several PDFs or zips. Name the files:
- `StudentID_CaseID.pdf`: one PDF per student (ATX printout), or
- `StudentID_CaseID_<anything>.pdf`: several PDFs per student (e.g. `S0123_C03_1040.pdf`, `S0123_C03_SchC.pdf`), or
- a folder `StudentID_CaseID` holding that student's PDFs (inside a zip).

Files for another case, badly named files and (optionally) students already evaluated are skipped and
listed. Upload large batches in parts of about 100 returns. Zips are checked before unpacking
(at most 5,000 files, 25 MB per PDF, 1.5 GB unpacked).

## IRS fillable forms
Students download the fillable forms from irs.gov, fill them in **Adobe Acrobat Reader** and save them
(**File > Save**). They must not use *Print to PDF*: printing removes the boxes and the app then
says so instead of marking. Don't merge the forms into one PDF; merging tools often mix up the
boxes, because every IRS form uses the same internal box names (the app warns if it sees this).

How the reader works: every entry is stored in a named box, so the value is exactly what the student
typed, whatever it looks like on screen. The reader links each box to its line from the blank form
itself: the box right of a bold line number is that line (`1a`, `11a`, `25d`; with the column
letter on multi-column rows, `1a (h)`); text boxes are found by the label above them; check boxes by
the label to their right. This link depends only on the blank form, so it is the same for every
student, and it is cached, so bulk marking is fast.

Answer keys use the same Line names as for ATX (see below). Keys are per tax year: the 2025 Form 1040
has lines such as `11a`, `11b` and `12e`. Build the key from the trainer's correct fillable return
(**Build a key from a correct return**) to get the right line names. A filled-in box the reader
cannot link to a line is listed under **What the system read**, and you can mark it with
`field: <box name>` in the key (e.g. `field: f1_47`).

Tested on the 2025 Form 1040 (every line, the filing status, the dependents table, check boxes and
name/address boxes). For each other form you use (Schedules 1-3, C, SE ...), run one trainer
return through the app and check **What the system read** before marking students.

## Integrity checks (copying)
The **Integrity checks** tab compares every pair of returns of the same case, across the selected batches,
and explains in plain words what links them. Evidence it looks for:

| Evidence | Points |
|---|---|
| Exactly the same PDF file (also when renamed or uploaded under another ID) | 100 |
| One file contains another student's filled-in work (saved over their file: viewers append each save) | 100 |
| Same PDF save ID (only when the viewer changed it after the blank form was downloaded) | 90 |
| Same created + saved time stamps, to the second, and same software | 70 |
| Same unusual wrong answers on the same lines (rarer mistakes count more; common ones are ignored) | up to 70 |
| Same original document ID (ATX printouts) | 60 |
| Every box identical, including how numbers and names were typed | 50 |
| Same computer user name in the file properties (ATX printouts) | 25 |
| 0 typed in the same boxes others leave blank / identical capitalisation and spacing | 15 each |
| Saved within 2 minutes of each other | 10 |

Score 90+ = **very likely copied**, 60+ = **likely**, 30+ = **review**. Students linked by "likely" or stronger are
shown as groups. A student file built on the trainer's correct return (uploaded under **Build a key**, with the same
Case ID) is listed separately. Values that every blank IRS form carries, or that most of the batch shares, are
ignored. The tab also lists evaluated IDs that are not on the roster and one name used under several IDs.
Summaries are batch-wise and student-wise, with a PDF and an Excel report.

These are signals for a conversation, not proof: compare the two returns and hear the students before acting.
Only returns evaluated with this version have file fingerprints; older results are compared on wrong answers only.
What is stored: hashes, PDF save IDs and time stamps, the file name and, for ATX printouts, the computer user name
in the file properties. The returns themselves are still not stored.

## Reliability
- One reader per return format, behind one interface: marking, the database and all reports are shared.
- Saving an evaluation, and every erase, is a single database transaction: saved completely or not at all.
- Database connections come from a pool and are checked before use (Neon closes idle connections);
  a dropped connection is retried automatically.
- Database upgrades only add columns and are versioned, so the older `database` branch can still use the
  same Neon database. Still, give a test app its own Neon branch.
- Each return in a bulk upload is marked and saved on its own: one bad file is reported as failed and
  the rest continue. Unexpected errors are written to the app log (Streamlit: **Manage app → Logs**).
- Limits on file size, pages and zip contents stop oversized uploads from crashing the app.

## Limits to know
- ATX reader calibrated on **ATX 2020** output. A different ATX year or version: run one return through it and
  check the "What the system read" panel.
- Form tables with many rows (e.g. Form 8949 transaction lists) are checked through their numbered
  total lines, not row by row.
- PDFs must be printed from ATX or saved from the fillable forms, not scanned.
