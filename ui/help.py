"""Help: how the app works, short and task-first."""
import streamlit as st

from ui.core import header

TEXT = """
### The weekly loop
1. **Setup → Mock returns** – upload the answer key once per mock (or build a draft from the trainer's correct return).
2. **Evaluate** – pick the mock, then *One student* or *A batch*. Choose On time / Late (with exceptions for a batch).
3. **Submissions** – chase the *Improper* and *Not received* lists; fix On time / Late with a reason.
4. **Learners** – who needs attention. Click a learner for everything about them, including report downloads.
5. **Reports** – trainer, Deloitte, management and integrity files, from the sidebar filters.

The **sidebar filters** (batches, mock returns) apply to Learners, Submissions, Insights and Reports.
Enter **your name** in the sidebar so changes are recorded against you.

### How learners must save their return
- Fill in the IRS fillable PDFs in **Adobe Acrobat Reader** and **save** them (File > Save). One PDF per form.
- **Don't print to PDF** or flatten – that removes the boxes and the file is refused.
- Name the folder or files `StudentID_CaseID` (e.g. `164001_M02`). Near-misses (`MO2`, `M2`, spaces, hyphens) are
  matched and marked **CHECK NAME**. An empty (0-byte) file is treated as a form not submitted.

### How marks are given
- Every item in the key has its own marks; the total is scaled to **100**. One mistake costs only that item.
- An answer must match the key within the key's rounding tolerance.
- Amounts on lines that should be blank deduct the marks set in the key. A missing form makes its items wrong.
- Before sharing a report flagged *check*, open **What the system read**. Unreadable items are marked 0.

### Final course score
| Part | Marks | How |
|---|---|---|
| Return accuracy | 60 | 5 per mock return, in proportion to its score; not submitted = 0 |
| On-time submission | 20 | 1.67 per return submitted on time; late = 0 |
| MCQ tests | 20 | 5 per test, in proportion to its marks out of 50 |

Pass mark **70**. Change these under **Setup → Settings** (admin). Tick only the real mock returns there, so practice
cases don't count. *Progress so far* shows marks earned and marks possible so far; *Final result* gives Pass / At risk.

### Flags (Learners)
*Low class attendance*, *Missed mock returns*, *Improper submission* (sent but not accepted), *Missed MCQ tests*,
*Not performing well*, *Absent for everything*. Readiness: no flag = On track, one = Needs attention, two or more =
Needs urgent attention. Limits are under **Setup → Settings**.

### Attendance
Upload Rajashekhar's daily report under **Setup → Attendance**. 80%+ of the session = Present, left early = Below 80%
(not counted), not in the report = Absent. Learners are matched by Deloitte email, then name; link anyone unmatched
once and it is remembered.

### Integrity (Insights → Integrity)
Compares returns of the same mock: identical files, files saved over another learner's work, the same hidden save
IDs, the same unusual wrong answers, identical typing habits, and files built on the trainer's correct return.
It refreshes by itself when returns are added. Treat results as leads to check, not proof.

### Admin
**Run history** records every evaluation run – what was uploaded and why each file was evaluated, skipped or failed.
**Erase data** (admin password) removes a mock's results, a mock, a batch, a learner, or everything.

### Privacy
Learner PDFs are not stored – only scores, mistakes and the reader's notes. Without a database nothing is kept after
the tab closes.
"""


def page():
    header("Help", "How the app works.")
    st.markdown(TEXT)
