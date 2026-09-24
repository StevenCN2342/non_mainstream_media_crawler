import json
import threading


class JsonWriter:

    def __init__(self):
        self.lock = threading.Lock()
        self.file = open(
            "articles.json",
            "a",
            encoding="utf-8",
        )

    def save(self, article):
        with self.lock:
            self.file.write(
                json.dumps(
                    article,
                    ensure_ascii=False,
                )
                + "\n"
            )

            self.file.flush()

    def close(self):
        with self.lock:
            self.file.close()
