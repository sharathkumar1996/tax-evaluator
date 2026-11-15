"""
db.py - permanent storage for students, cases (answer keys), evaluations and mistakes.

* Production: PostgreSQL (Neon). Set `database_url` in Streamlit Secrets, e.g.
  database_url = "postgresql://user:password@ep-xxx-pooler.neon.tech/neondb?sslmode=require"
* Testing / laptop: a local SQLite file is used when database_url starts with "sqlite:///".
* If no database_url is set the app still works, but nothing is saved after the tab closes.

Design
------
* Every multi-statement change (saving an evaluation, erasing a batch ...) runs in ONE transaction:
  it is saved completely or not at all - never half-written.
* Connections come from a small pool (psycopg_pool), so several staff members can work at the
  same time without sharing one connection. Each connection is checked before use, which
  handles Neon closing idle connections when it scales to zero.
* A transaction that fails because the connection dropped is retried (up to 3 times, with a
  short wait). All writes are upserts or deletes, so retrying them is safe.
* The schema is versioned (table `schema_version`). Upgrades only ADD tables, columns and
  indexes, so an older copy of the app can keep using the same database. Upgrades take a
  database lock, so two app instances starting together cannot run them twice.

To keep the database small, only the items a student got wrong (plus unexpected entries
and unreadable items) are stored. Correct items are rebuilt from the case's answer key
when a report is regenerated.
"""
import base64
import hashlib
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

log = logging.getLogger("tax_evaluator.db")

BASE_SCHEMA = [
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

# (version, [statements]) - append only. Column additions use ("add_column", table, column, type).
MIGRATIONS = [
    (1, BASE_SCHEMA),
    (2, [("add_column", "evaluations", "source_format", "TEXT"),
         ("add_column", "evaluations", "reader_notes", "TEXT"),
         "CREATE INDEX IF NOT EXISTS ix_students_batch ON students (batch)"]),
    # integrity checks: small anonymous fingerprints of each return (see integrity.py)
    (3, ["""CREATE TABLE IF NOT EXISTS fingerprints (
            student_id TEXT NOT NULL,
            case_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            value TEXT NOT NULL,
            detail TEXT,
            PRIMARY KEY (student_id, case_id, kind, value))""",
         "CREATE INDEX IF NOT EXISTS ix_fp_match ON fingerprints (case_id, kind, value)"]),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]
TRANSIENT = ("OperationalError", "InterfaceError", "PoolTimeout", "ConnectionTimeout")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _to_text(v):
    if v is None:
        return None
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


class DB:
    def __init__(self, url, pool_size=5):
        self.url = url
        self.kind = "sqlite" if url.startswith("sqlite") else "pg"
        self._lock = threading.RLock()       # guards the single connection when there is no pool
        self._pool, self._conn = None, None
        self.pool_size = pool_size
        self._open()
        self._migrate()

    # ------------------------------------------------------------ connections
    def _open(self):
        if self.kind == "sqlite":
            path = self.url.split("sqlite:///", 1)[1] or ":memory:"
            self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
            self._conn.execute("PRAGMA foreign_keys = ON")
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            return
        import psycopg
        kwargs = {"autocommit": True, "prepare_threshold": None, "connect_timeout": 20}
        # prepare_threshold=None: no server-side prepared statements, so Neon's pooled
        # ("-pooler") and direct connection strings both work.
        # 1) Connect once directly. A wrong password/host then shows its real error message
        #    (a pool would only say "couldn't get a connection"). Retried once: a Neon compute
        #    that was asleep can take a few seconds to wake up.
        for attempt in (1, 2):
            try:
                self._conn = psycopg.connect(self.url, **kwargs)
                break
            except psycopg.OperationalError:
                if attempt == 2:
                    raise
                time.sleep(3)
        # 2) Hand over to a pool; if the pool cannot start, keep the working single connection.
        try:
            from psycopg_pool import ConnectionPool
            pool = ConnectionPool(self.url, min_size=1, max_size=self.pool_size, kwargs=kwargs, timeout=30,
                                  max_idle=240, check=ConnectionPool.check_connection, open=False,
                                  name="tax-evaluator")
            pool.open(wait=True, timeout=30)
        except Exception as e:                 # ImportError, PoolTimeout ...
            log.warning("connection pool not used (%s: %s); using one shared connection", type(e).__name__, e)
            return
        self._conn.close()
        self._conn, self._pool = None, pool

    def _reconnect(self):
        if self._pool is not None:
            return                              # the pool checks and replaces broken connections itself
        try:
            self._conn.close()
        except Exception:
            pass
        self._open()

    @contextmanager
    def _transaction(self):
        """Yield a cursor inside one transaction: committed on success, rolled back on any error."""
        if self._pool is not None:
            with self._pool.connection() as conn:
                with conn.transaction():
                    yield conn.cursor()
            return
        with self._lock:
            if self.kind == "pg":
                with self._conn.transaction():
                    yield self._conn.cursor()
            else:
                cur = self._conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                try:
                    yield cur
                except BaseException:
                    cur.execute("ROLLBACK")
                    raise
                cur.execute("COMMIT")

    def _sql(self, sql):
        return sql if self.kind == "sqlite" else sql.replace("?", "%s")

    def tx(self, fn, retries=3):
        """Run fn(cursor_wrapper) in one transaction, retrying the WHOLE unit if the connection dropped."""
        delay = 0.5
        for attempt in range(1, retries + 1):
            try:
                with self._transaction() as cur:
                    return fn(_Cursor(cur, self._sql))
            except Exception as e:
                if attempt < retries and self.kind == "pg" and type(e).__name__ in TRANSIENT:
                    log.warning("database connection problem (%s), retry %d", type(e).__name__, attempt)
                    time.sleep(delay)
                    delay *= 3
                    self._reconnect()
                    continue
                raise

    # convenience wrappers (each call is its own transaction)
    def run(self, sql, params=()):
        self.tx(lambda c: c.execute(sql, params))

    def many(self, sql, rows):
        if rows:
            self.tx(lambda c: c.executemany(sql, rows))

    def rows(self, sql, params=()):
        return self.tx(lambda c: c.fetch(sql, params))

    def close(self):
        try:
            if self._pool is not None:
                self._pool.close()
            elif self._conn is not None:
                self._conn.close()
        except Exception:
            pass

    def health(self):
        """True if the database answers a trivial query."""
        try:
            return self.rows("SELECT 1 AS ok")[0]["ok"] == 1
        except Exception as e:
            log.warning("health check failed: %s", e)
            return False

    # ------------------------------------------------------------ schema upgrades
    def _columns(self, c, table):
        if self.kind == "sqlite":
            return {r["name"] for r in c.fetch(f"PRAGMA table_info({table})")}
        return {r["column_name"] for r in c.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))}

    def _migrate(self):
        def up(c):
            if self.kind == "pg":
                c.execute("SELECT pg_advisory_xact_lock(724501)")     # one upgrader at a time
            c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            r = c.fetch("SELECT MAX(version) AS v FROM schema_version")
            current = (r[0]["v"] if r else None) or 0
            for version, steps in MIGRATIONS:
                if version <= current:
                    continue
                for s in steps:
                    if isinstance(s, tuple) and s[0] == "add_column":
                        _, table, col, typ = s
                        if col not in self._columns(c, table):
                            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                    else:
                        c.execute(s)
                c.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                log.info("database schema upgraded to version %d", version)
        self.tx(up)

    def schema_version(self):
        return self.rows("SELECT MAX(version) AS v FROM schema_version")[0]["v"]

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

    def save_evaluation(self, sid, case_id, file_name, m, rows, key_hash, by, source_format="", notes="",
                        fingerprints=None):
        """m: metrics dict from metrics.return_metrics; rows: graded rows. Saved atomically."""
        keep = [r for r in rows if r["Result"] != "Correct" or r.get("Needs Review")]
        mistakes = [(sid, case_id, int(r["Step"]), r["Category"], str(r["Form"]), str(r["Line"]), r["Description"],
                     _to_text(r["Correct Value"]), _to_text(r["Student Value"]), r["Marks"], r["Awarded"], r["Result"],
                     r["Feedback"], r.get("Note", ""), int(bool(r.get("Critical"))), int(bool(r.get("Needs Review"))))
                    for r in keep]

        def write(c):
            c.execute("""INSERT INTO evaluations (student_id, case_id, file_name, score, out_of, pct, file_ready, bottom_line,
                        errors, critical_errors, unexpected, needs_review, key_hash, evaluated_at, evaluated_by,
                        source_format, reader_notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (student_id, case_id) DO UPDATE SET file_name = excluded.file_name, score = excluded.score,
                        out_of = excluded.out_of, pct = excluded.pct, file_ready = excluded.file_ready,
                        bottom_line = excluded.bottom_line, errors = excluded.errors,
                        critical_errors = excluded.critical_errors, unexpected = excluded.unexpected,
                        needs_review = excluded.needs_review, key_hash = excluded.key_hash,
                        evaluated_at = excluded.evaluated_at, evaluated_by = excluded.evaluated_by,
                        source_format = excluded.source_format, reader_notes = excluded.reader_notes""",
                      (sid, case_id, file_name[:500], m["score"], m["out_of"], m["pct"], int(m["file_ready"]),
                       None if m["bottom_line"] is None else int(m["bottom_line"]), m["errors"], m["critical_errors"],
                       m["unexpected"], m["needs_review"], key_hash, now(), by, source_format, (notes or "")[:2000]))
            c.execute("DELETE FROM mistakes WHERE student_id = ? AND case_id = ?", (sid, case_id))
            if mistakes:
                c.executemany("""INSERT INTO mistakes (student_id, case_id, step, category, form, line, description,
                                 correct_value, student_value, marks, awarded, result, feedback, note, critical,
                                 needs_review) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", mistakes)
            if fingerprints is not None:
                self._write_fingerprints(c, sid, case_id, fingerprints)
        self.tx(write)

    # ------------------------------------------------------------ integrity fingerprints
    @staticmethod
    def _write_fingerprints(c, sid, case_id, fps):
        c.execute("DELETE FROM fingerprints WHERE student_id = ? AND case_id = ?", (sid, case_id))
        rows = list({(k, str(v)[:200]): (sid, case_id, k, str(v)[:200], str(d or "")[:300])
                     for k, v, d in fps}.values())
        if rows:
            c.executemany("INSERT INTO fingerprints (student_id, case_id, kind, value, detail) VALUES (?, ?, ?, ?, ?)",
                          rows)

    def save_fingerprints(self, sid, case_id, fps):
        """Fingerprints without an evaluation (the trainer's correct return, sid = integrity.TRAINER)."""
        self.tx(lambda c: self._write_fingerprints(c, sid, case_id, fps))

    def fingerprints(self, case_ids=None):
        sql = "SELECT student_id, case_id, kind, value, detail FROM fingerprints"
        if case_ids:
            marks = ", ".join("?" for _ in case_ids)
            return self.rows(f"{sql} WHERE case_id IN ({marks})", tuple(case_ids))
        return self.rows(sql)

    def evaluations(self, student_id=None):
        sql = """SELECT e.*, s.name, s.batch FROM evaluations e
                 LEFT JOIN students s ON s.student_id = e.student_id"""
        if student_id:
            return self.rows(sql + " WHERE e.student_id = ? ORDER BY e.case_id", (student_id,))
        return self.rows(sql + " ORDER BY e.case_id, e.student_id")

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
        def f(c):
            c.execute("DELETE FROM mistakes WHERE student_id = ? AND case_id = ?", (sid, case_id))
            c.execute("DELETE FROM fingerprints WHERE student_id = ? AND case_id = ?", (sid, case_id))
            c.execute("DELETE FROM evaluations WHERE student_id = ? AND case_id = ?", (sid, case_id))
        self.tx(f)

    # ------------------------------------------------------------ erasing data (each one atomic)
    def counts(self):
        def f(c):
            return {t: c.fetch(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
                    for t in ("students", "cases", "evaluations", "mistakes")}
        return self.tx(f)

    def batches(self):
        return [r["batch"] for r in self.rows("SELECT DISTINCT batch FROM students ORDER BY batch")]

    def erase_case_results(self, case_id):
        """Delete all evaluations of a case; keep the answer key (and the trainer's fingerprints)."""
        def f(c):
            c.execute("DELETE FROM fingerprints WHERE case_id = ? AND student_id <> '#TRAINER'", (case_id,))
            c.execute("DELETE FROM mistakes WHERE case_id = ?", (case_id,))
            c.execute("DELETE FROM evaluations WHERE case_id = ?", (case_id,))
        self.tx(f)

    def erase_case(self, case_id):
        """Delete a case's answer key and all its evaluations."""
        def f(c):
            c.execute("DELETE FROM fingerprints WHERE case_id = ?", (case_id,))
            c.execute("DELETE FROM mistakes WHERE case_id = ?", (case_id,))
            c.execute("DELETE FROM evaluations WHERE case_id = ?", (case_id,))
            c.execute("DELETE FROM cases WHERE case_id = ?", (case_id,))
        self.tx(f)

    def erase_student(self, sid):
        """Delete a student from the roster with all their evaluations."""
        def f(c):
            c.execute("DELETE FROM mistakes WHERE student_id = ?", (sid,))
            c.execute("DELETE FROM fingerprints WHERE student_id = ?", (sid,))
            c.execute("DELETE FROM evaluations WHERE student_id = ?", (sid,))
            c.execute("DELETE FROM students WHERE student_id = ?", (sid,))
        self.tx(f)

    def erase_batch(self, batch, roster_too=True):
        """Delete all evaluations of a batch's students (and the students themselves if roster_too)."""
        sub = "SELECT student_id FROM students WHERE batch = ?"

        def f(c):
            c.execute(f"DELETE FROM mistakes WHERE student_id IN ({sub})", (batch,))
            c.execute(f"DELETE FROM fingerprints WHERE student_id IN ({sub})", (batch,))
            c.execute(f"DELETE FROM evaluations WHERE student_id IN ({sub})", (batch,))
            if roster_too:
                c.execute("DELETE FROM students WHERE batch = ?", (batch,))
        self.tx(f)

    def erase_unlisted(self):
        """Delete evaluations of student IDs that are not on the roster."""
        sub = "SELECT student_id FROM students"

        def f(c):
            c.execute(f"DELETE FROM mistakes WHERE student_id NOT IN ({sub})")
            c.execute(f"DELETE FROM fingerprints WHERE student_id NOT IN ({sub}) AND student_id <> '#TRAINER'")
            c.execute(f"DELETE FROM evaluations WHERE student_id NOT IN ({sub})")
        self.tx(f)

    def erase_all(self):
        def f(c):
            for t in ("mistakes", "fingerprints", "evaluations", "students", "cases"):
                c.execute(f"DELETE FROM {t}")
        self.tx(f)

    def size_mb(self):
        if self.kind == "pg":
            return self.rows("SELECT pg_database_size(current_database()) / 1048576.0 AS mb")[0]["mb"]
        r = self.rows("SELECT page_count * page_size / 1048576.0 AS mb FROM pragma_page_count(), pragma_page_size()")
        return r[0]["mb"]


class _Cursor:
    """Small wrapper: '?' placeholders on both databases, rows as dicts."""

    def __init__(self, cur, sql):
        self._c, self._sql = cur, sql

    def execute(self, sql, params=()):
        self._c.execute(self._sql(sql), params)

    def executemany(self, sql, rows):
        self._c.executemany(self._sql(sql), rows)

    def fetch(self, sql, params=()):
        self._c.execute(self._sql(sql), params)
        if self._c.description is None:
            return []
        cols = [d[0] for d in self._c.description]
        return [dict(zip(cols, r)) for r in self._c.fetchall()]
