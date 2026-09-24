import sqlite3
import threading


class SQLiteStorage:

    def __init__(self):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(
            "articles.db",
            check_same_thread=False,
        )

        self.conn.execute(
            '\n            CREATE TABLE IF NOT EXISTS articles(\n\n            id INTEGER PRIMARY KEY,\n\n            url TEXT UNIQUE,\n\n            title TEXT,\n\n            content TEXT\n\n            )\n            '
        )

        self.conn.commit()

    def save(self, article):
        with self.lock:
            self.conn.execute(
                '\n                INSERT OR IGNORE\n\n                INTO articles\n\n                (url,title,content)\n\n                VALUES(?,?,?)\n\n                ',
                (
                    article.get("url"),
                    article.get("title"),
                    article.get("content"),
                ),
            )

            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()
