import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

# Hamesha isi folder mein waste.db khule, chahe server kisi bhi folder se chalayein
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "waste.db")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def now():
    return datetime.now().isoformat(timespec="seconds")


def init_db():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS alert_state (
                alert_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'new',
                reason TEXT DEFAULT '',
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                assignee TEXT NOT NULL,
                priority TEXT NOT NULL,
                due_date TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                work_note TEXT DEFAULT '',
                created_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT
            );
            """
        )


def reset():
    # Settings jaan boojh kar yahan se nahi hatayi jati
    with conn() as c:
        c.executescript(
            "DELETE FROM alert_state; DELETE FROM tasks; DELETE FROM audit_log;"
        )


def log(actor, event, detail=""):
    with conn() as c:
        c.execute(
            "INSERT INTO audit_log (ts, actor, event, detail) VALUES (?,?,?,?)",
            (now(), actor, event, detail),
        )


def list_audit():
    with conn() as c:
        rows = c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 100")
        return [dict(r) for r in rows]


def get_state(alert_id):
    with conn() as c:
        r = c.execute(
            "SELECT state, reason FROM alert_state WHERE alert_id=?", (alert_id,)
        ).fetchone()
    return dict(r) if r else {"state": "new", "reason": ""}


def set_state(alert_id, state, reason=""):
    with conn() as c:
        c.execute(
            """INSERT INTO alert_state (alert_id, state, reason, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(alert_id) DO UPDATE SET
                 state=excluded.state, reason=excluded.reason,
                 updated_at=excluded.updated_at""",
            (alert_id, state, reason, now()),
        )


def create_task(alert_id, title, assignee, priority, due_date):
    with conn() as c:
        cur = c.execute(
            """INSERT INTO tasks (alert_id, title, assignee, priority, due_date, created_at)
               VALUES (?,?,?,?,?,?)""",
            (alert_id, title, assignee, priority, due_date, now()),
        )
        return cur.lastrowid


def list_tasks():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM tasks ORDER BY id DESC")]


def get_task(task_id):
    with conn() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(r) if r else None


def complete_task(task_id, note):
    with conn() as c:
        c.execute(
            "UPDATE tasks SET status='done', work_note=?, completed_at=? WHERE id=?",
            (note, now(), task_id),
        )


def assign_task(alert_id, title, assignee, priority, due_date):
    """Task banana aur state 'task_assigned' karna, ek hi transaction mein.
    Agar alert 'valid_issue' nahi (jaise dobara click), to None wapas aata hai."""
    with conn() as c:
        cur = c.execute(
            "UPDATE alert_state SET state='task_assigned', reason='', updated_at=? "
            "WHERE alert_id=? AND state='valid_issue'",
            (now(), alert_id),
        )
        if cur.rowcount != 1:
            return None
        cur = c.execute(
            """INSERT INTO tasks (alert_id, title, assignee, priority, due_date, created_at)
               VALUES (?,?,?,?,?,?)""",
            (alert_id, title, assignee, priority, due_date, now()),
        )
        return cur.lastrowid


def finish_task(task_id, note, alert_id):
    """Task complete karna aur alert state 'completed' karna, ek hi transaction mein."""
    with conn() as c:
        cur = c.execute(
            "UPDATE tasks SET status='done', work_note=?, completed_at=? "
            "WHERE id=? AND status!='done'",
            (note, now(), task_id),
        )
        if cur.rowcount != 1:
            return False
        c.execute(
            "UPDATE alert_state SET state='completed', reason='', updated_at=? WHERE alert_id=?",
            (now(), alert_id),
        )
        return True


# ---------- Settings (Admin page ke liye) ----------

def _ensure_settings_table(c):
    # Purani waste.db mein table na ho to yahin ban jati hai
    c.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT
        )"""
    )


def get_settings(defaults):
    """Saved settings wapas deta hai. Jo key save nahi, uski default value aati hai."""
    out = dict(defaults)
    with conn() as c:
        _ensure_settings_table(c)
        for r in c.execute("SELECT key, value FROM settings"):
            if r["key"] in out:
                try:
                    out[r["key"]] = float(r["value"])
                except ValueError:
                    pass
    return out


def set_settings(values):
    """values ek dict hai, jaise {"tariff": 65}. Har key save ya update hoti hai."""
    with conn() as c:
        _ensure_settings_table(c)
        for k, v in values.items():
            c.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                (k, str(v), now()),
            )