"""
db.py - permanent storage for students, cases (answer keys), evaluations and mistakes.

* Production: PostgreSQL (Neon). Set `database_url` in Streamlit Secrets, e.g.
  database_url = "postgresql://user:password@ep-xxx.neon.tech/neondb?sslmode=require"
* Testing / laptop: a local SQLite file is used when database_url starts with "sqlite:///".
* If no database_url is set the app still works, but nothing is saved after the tab closes.

To keep the database small, only the items a student got wrong (plus unexpected entries
and unreadable items) are stored. Correct items are rebuilt from the case's answer key
when a report is regenerated.
"""
import base64
import hashlib
import sqlite3
from datetime import datetime, timezone

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS students (
        student_id TEXT PRIMARY KEY,
        name TEXT,
        batch TEXT,
        updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS cases (
        case_id TEXT PRIMARY KEY,
        title TEXT,
        tax_year INTEGER,
        total_marks REAL,
        n_steps INTEGER,
        key_file TEXT,
        key_b64 TEXT,
        key_hash TEXT,
        uploaded_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS evaluations (
        student_id TEXT NOT NULL,
        case_id TEXT NOT NULL,
        file_name TEXT,
        score REAL,
        out_of REAL,
        pct REAL,
        file_ready INTEGER,
        bottom_line INTEGER,
        errors INTEGER,
        critical_errors INTEGER,
        unexpected INTEGER,
        needs_review INTEGER,
        key_hash TEXT,
        evaluated_at TEXT,
        evaluated_by TEXT,
        PRIMARY KEY (student_id, case_id))""",
    """CREATE TABLE IF NOT EXISTS mistakes (
        student_id TEXT NOT NULL,
        case_id TEXT NOT NULL,
        step INTEGER NOT NULL,
        category TEXT,
        form TEXT,
        line TEXT,
        description TEXT,
        correct_value TEXT,
        student_value TEXT,
        marks REAL,
        awarded REAL,
        result TEXT,
        feedback TEXT,
        note TEXT,
        critical INTEGER,
        needs_review INTEGER,
        PRIMARY KEY (student_id, case_id, step))""",
    "CREATE INDEX IF NOT EXISTS ix_eval_case ON evaluations (case_id)",
    "CREATE INDEX IF NOT EXISTS ix_mistakes_case ON mistakes (case_id)",
]


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _to_text(v):
    if v is None:
        return None
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


class DB:
    def __init__(self, url):
        self.url = url
        self.kind = "sqlite" if url.startswith("sqlite") else "pg"
        self.conn = None
        self._connect()
        for s in SCHEMA:
            self.run(s)

    # ------------------------------------------------------------ plumbing
    def _connect(self):
        if self.kind == "sqlite":
            path = self.url.split("sqlite:///", 1)[1] or ":memory:"
            self.conn = sqlite3.connect(path, check_same_thread=False)
        else:
            import psycopg
            # prepare_threshold=None: no server-side prepared statements, so Neon's pooled
            # ("-pooler") and direct connection strings both work
            self.conn = psycopg.connect(self.url, autocommit=True, connect_timeout=15, prepare_threshold=None)

    def _sql(self, sql):
        return sql if self.kind == "sqlite" else sql.replace("?", "%s")

    def _exec(self, fn):
        """Run fn(cursor); reconnect once if the server closed an idle connection (Neon scales to zero)."""
        for attempt in (1, 2):
            try:
                cur = self.conn.cursor()
                out = fn(cur)
                if self.kind == "sqlite":
                    self.conn.commit()
                return out
            except Exception as e:
                name = type(e).__name__
                if attempt == 1 and self.kind == "pg" and name in ("OperationalError", "InterfaceError"):
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                    self._connect()
                    continue
                raise

    def run(self, sql, params=()):
        self._exec(lambda c: c.execute(self._sql(sql), params))

    def many(self, sql, rows):
        if rows:
            self._exec(lambda c: c.executemany(self._sql(sql), rows))

    def rows(self, sql, params=()):
        def f(c):
            c.execute(self._sql(sql), params)
            cols = [d[0] for d in c.description]
            return [dict(zip(cols, r)) for r in c.fetchall()]
        return self._exec(f)

    # ------------------------------------------------------------ students
    def upsert_students(self, recs):
        """recs: list of (student_id, name, batch)."""
        t = now()
        self.many("""INSERT INTO students (student_id, name, batch, updated_at) VALUES (?, ?, ?, ?)
                     ON CONFLICT (student_id) DO UPDATE SET name = excluded.name, batch = excluded.batch,
                     updated_at = excluded.updated_at""", [(s, n, b, t) for s, n, b in recs])

    def students(self):
        return self.rows("SELECT student_id, name, batch FROM students ORDER BY batch, student_id")

    def student(self, sid):
        r = self.rows("SELECT student_id, name, batch FROM students WHERE student_id = ?", (sid,))
        return r[0] if r else None

    # ------------------------------------------------------------ cases
    def save_case(self, key, key_bytes, file_name):
        h = hashlib.sha256(key_bytes).hexdigest()[:16]
        self.run("""INSERT INTO cases (case_id, title, tax_year, total_marks, n_steps, key_file, key_b64, key_hash, uploaded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (case_id) DO UPDATE SET title = excluded.title, tax_year = excluded.tax_year,
                    total_marks = excluded.total_marks, n_steps = excluded.n_steps, key_file = excluded.key_file,
                    key_b64 = excluded.key_b64, key_hash = excluded.key_hash, uploaded_at = excluded.uploaded_at""",
                 (key.case_id, key.title, key.tax_year, key.total, len(key.steps), file_name,
                  base64.b64encode(key_bytes).decode(), h, now()))
        return h

    def cases(self):
        return self.rows("SELECT case_id, title, tax_year, total_marks, n_steps, key_file, key_hash, uploaded_at "
                         "FROM cases ORDER BY case_id")

    def case_key_bytes(self, case_id):
        r = self.rows("SELECT key_b64 FROM cases WHERE case_id = ?", (case_id,))
        return base64.b64decode(r[0]["key_b64"]) if r else None

    def case_eval_count(self, case_id):
        return self.rows("SELECT COUNT(*) AS n FROM evaluations WHERE case_id = ?", (case_id,))[0]["n"]

    # ------------------------------------------------------------ evaluations
    def evaluated(self, case_id):
        return {r["student_id"] for r in self.rows("SELECT student_id FROM evaluations WHERE case_id = ?", (case_id,))}

    def save_evaluation(self, sid, case_id, file_name, m, rows, key_hash, by):
        """m: metrics dict from metrics.return_metrics; rows: graded rows."""
        self.run("""INSERT INTO evaluations (student_id, case_id, file_name, score, out_of, pct, file_ready, bottom_line,
                    errors, critical_errors, unexpected, needs_review, key_hash, evaluated_at, evaluated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (student_id, case_id) DO UPDATE SET file_name = excluded.file_name, score = excluded.score,
                    out_of = excluded.out_of, pct = excluded.pct, file_ready = excluded.file_ready,
                    bottom_line = excluded.bottom_line, errors = excluded.errors,
                    critical_errors = excluded.critical_errors, unexpected = excluded.unexpected,
                    needs_review = excluded.needs_review, key_hash = excluded.key_hash,
                    evaluated_at = excluded.evaluated_at, evaluated_by = excluded.evaluated_by""",
                 (sid, case_id, file_name, m["score"], m["out_of"], m["pct"], int(m["file_ready"]),
                  None if m["bottom_line"] is None else int(m["bottom_line"]), m["errors"], m["critical_errors"],
                  m["unexpected"], m["needs_review"], key_hash, now(), by))
        self.run("DELETE FROM mistakes WHERE student_id = ? AND case_id = ?", (sid, case_id))
        keep = [r for r in rows if r["Result"] != "Correct" or r.get("Needs Review")]
        self.many("""INSERT INTO mistakes (student_id, case_id, step, category, form, line, description, correct_value,
                     student_value, marks, awarded, result, feedback, note, critical, needs_review)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  [(sid, case_id, int(r["Step"]), r["Category"], str(r["Form"]), str(r["Line"]), r["Description"],
                    _to_text(r["Correct Value"]), _to_text(r["Student Value"]), r["Marks"], r["Awarded"], r["Result"],
                    r["Feedback"], r.get("Note", ""), int(bool(r.get("Critical"))), int(bool(r.get("Needs Review"))))
                   for r in keep])

    def evaluations(self):
        return self.rows("""SELECT e.*, s.name, s.batch FROM evaluations e
                            LEFT JOIN students s ON s.student_id = e.student_id
                            ORDER BY e.case_id, e.student_id""")

    def mistakes(self, sid=None, case_id=None):
        sql, p = "SELECT * FROM mistakes WHERE 1 = 1", []
        if sid:
            sql += " AND student_id = ?"
            p.append(sid)
        if case_id:
            sql += " AND case_id = ?"
            p.append(case_id)
        return self.rows(sql + " ORDER BY case_id, step", tuple(p))

    def delete_evaluation(self, sid, case_id):
        self.run("DELETE FROM mistakes WHERE student_id = ? AND case_id = ?", (sid, case_id))
        self.run("DELETE FROM evaluations WHERE student_id = ? AND case_id = ?", (sid, case_id))

    def size_mb(self):
        if self.kind == "pg":
            return self.rows("SELECT pg_database_size(current_database()) / 1048576.0 AS mb")[0]["mb"]
        r = self.rows("SELECT page_count * page_size / 1048576.0 AS mb FROM pragma_page_count(), pragma_page_size()")
        return r[0]["mb"]
