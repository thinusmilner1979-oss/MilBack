import os
import sqlite3
import threading
import time

STATE_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "milback")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    job     TEXT NOT NULL,
    src     TEXT NOT NULL,
    dst     TEXT NOT NULL,
    size    INTEGER NOT NULL,
    mtime   REAL NOT NULL,
    digest  TEXT,
    copied  REAL NOT NULL,
    PRIMARY KEY (job, src)
);
CREATE TABLE IF NOT EXISTS jobs (
    job         TEXT PRIMARY KEY,
    last_verify REAL NOT NULL DEFAULT 0
);
"""


def job_key(src, dst):
    return f"{os.path.abspath(src)}\x00{os.path.abspath(dst)}"


class StateIndex:
    """Remembers what was copied so an incremental run can skip a file without
    asking the destination about it. Over a network share that question costs a
    round trip per file, which is the dominant cost of a repeat backup."""

    def __init__(self, path=None):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.path = path or os.path.join(STATE_DIR, "index.sqlite3")
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        # Survives a mid-run power loss without corrupting the index.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.commit()

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    def load_job(self, job):
        with self._lock:
            rows = self._db.execute(
                "SELECT src, size, mtime, digest FROM files WHERE job = ?", (job,)
            ).fetchall()
        return {r[0]: (r[1], r[2], r[3]) for r in rows}

    def record(self, job, src, dst, size, mtime, digest=None):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO files (job, src, dst, size, mtime, digest, copied) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job, src, dst, size, mtime, digest, time.time()))

    def record_many(self, rows):
        if not rows:
            return
        now = time.time()
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO files (job, src, dst, size, mtime, digest, copied) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(j, s, d, sz, mt, dg, now) for j, s, d, sz, mt, dg in rows])

    def forget(self, job, sources):
        if not sources:
            return
        with self._lock:
            self._db.executemany(
                "DELETE FROM files WHERE job = ? AND src = ?",
                [(job, s) for s in sources])

    def commit(self):
        with self._lock:
            self._db.commit()

    def last_verify(self, job):
        with self._lock:
            row = self._db.execute(
                "SELECT last_verify FROM jobs WHERE job = ?", (job,)).fetchone()
        return row[0] if row else 0.0

    def mark_verified(self, job):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO jobs (job, last_verify) VALUES (?, ?)",
                (job, time.time()))
            self._db.commit()

    def prune_missing(self, job, seen_sources):
        with self._lock:
            rows = self._db.execute(
                "SELECT src FROM files WHERE job = ?", (job,)).fetchall()
            gone = [r[0] for r in rows if r[0] not in seen_sources]
            if gone:
                self._db.executemany(
                    "DELETE FROM files WHERE job = ? AND src = ?",
                    [(job, s) for s in gone])
                self._db.commit()
        return gone
