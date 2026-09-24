from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading


VALID_STATUSES = {"pending", "running", "done", "failed", "deferred"}


class TaskJournal:
    """Durable URL state used for restart-safe batch crawling."""

    def __init__(self, path, recover=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with self._connect() as db, db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks(
                    url TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            if recover:
                db.execute(
                    "UPDATE tasks SET status='pending', "
                    "error='上次运行中断，已自动恢复' WHERE status='running'"
                )

    def _connect(self):
        return closing(sqlite3.connect(self.path, timeout=30))

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def add_many(self, urls):
        now = self._now()
        with self.lock, self._connect() as db, db:
            db.executemany(
                "INSERT OR IGNORE INTO tasks(url,status,updated_at) "
                "VALUES(?, 'pending', ?)",
                ((url, now) for url in urls),
            )

    def pending_urls(self, urls):
        with self.lock, self._connect() as db:
            states = {
                url: db.execute(
                    "SELECT status FROM tasks WHERE url=?", (url,)
                ).fetchone()[0]
                for url in urls
            }
        return [url for url in urls if states[url] == "pending"]

    def set_status(self, url, status, error=""):
        if status not in VALID_STATUSES:
            raise ValueError(f"无效任务状态：{status}")
        attempts_sql = ", attempts=attempts+1" if status == "running" else ""
        with self.lock, self._connect() as db, db:
            changed = db.execute(
                f"UPDATE tasks SET status=?, error=?, updated_at=?{attempts_sql} "
                "WHERE url=?",
                (status, str(error), self._now(), url),
            ).rowcount
        if changed != 1:
            raise KeyError(url)

    def retry(self, url):
        with self.lock, self._connect() as db, db:
            changed = db.execute(
                "UPDATE tasks SET status='pending', error='', updated_at=? "
                "WHERE url=? AND status != 'done'",
                (self._now(), url),
            ).rowcount
        return changed

    def retry_failed(self):
        with self.lock, self._connect() as db, db:
            return db.execute(
                "UPDATE tasks SET status='pending', error='', updated_at=? "
                "WHERE status='failed'",
                (self._now(),),
            ).rowcount

    def counts(self, urls=None):
        with self.lock, self._connect() as db:
            if urls is None:
                rows = db.execute(
                    "SELECT status, count(*) FROM tasks GROUP BY status"
                ).fetchall()
            else:
                counts = {}
                for url in urls:
                    row = db.execute(
                        "SELECT status FROM tasks WHERE url=?", (url,)
                    ).fetchone()
                    if row:
                        counts[row[0]] = counts.get(row[0], 0) + 1
                rows = counts.items()
        return dict(rows)

    def unfinished(self):
        with self.lock, self._connect() as db:
            return db.execute(
                "SELECT url,status,attempts,error,updated_at FROM tasks "
                "WHERE status != 'done' ORDER BY updated_at,url"
            ).fetchall()
