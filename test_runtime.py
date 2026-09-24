import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from config.sites import SiteConfigError, SiteRegistry
import main as crawler_main
import daily_crawler
from storage.task_journal import TaskJournal
from utils.process_lock import ProcessLock


BASE_DIR = Path(__file__).resolve().parent


@contextmanager
def test_directory():
    root = BASE_DIR / ".test-work"
    root.mkdir(exist_ok=True)
    path = root / uuid.uuid4().hex
    path.mkdir()
    try:
        yield str(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


class SiteRegistryTests(unittest.TestCase):
    def write_config(self, folder, content):
        path = Path(folder) / "sites.yaml"
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return path

    def test_load_find_and_all_urls(self):
        with test_directory() as folder:
            path = self.write_config(
                folder,
                """
                sites:
                  - name: Alpha News
                    url: https://alpha.example/news
                  - name: Beta Media
                    url: https://beta.example/article
                """,
            )
            registry = SiteRegistry(path)
            self.assertEqual(registry.find("alpha")["name"], "Alpha News")
            self.assertEqual(
                registry.urls(all_sites=True),
                ["https://alpha.example/news", "https://beta.example/article"],
            )

    def test_rejects_duplicate_names_and_invalid_urls(self):
        with test_directory() as folder:
            path = self.write_config(
                folder,
                """
                sites:
                  - name: Duplicate
                    url: https://one.example/
                  - name: duplicate
                    url: not-a-url
                """,
            )
            with self.assertRaises(SiteConfigError):
                SiteRegistry(path)


class TaskJournalTests(unittest.TestCase):
    def test_recovery_retry_and_counts(self):
        with test_directory() as folder:
            path = Path(folder) / "tasks.sqlite3"
            first = TaskJournal(path)
            first.add_many(["https://a.example", "https://b.example"])
            first.set_status("https://a.example", "running")
            first.set_status("https://b.example", "failed", "temporary")

            read_only = TaskJournal(path)
            self.assertEqual(read_only.counts()["running"], 1)

            recovered = TaskJournal(path, recover=True)
            self.assertEqual(
                recovered.pending_urls(["https://a.example"]),
                ["https://a.example"],
            )
            self.assertEqual(recovered.retry_failed(), 1)
            self.assertEqual(recovered.counts(), {"pending": 2})


class ProcessSafetyTests(unittest.TestCase):
    def test_process_lock_excludes_another_process(self):
        with test_directory() as folder:
            lock_path = Path(folder) / "run.lock"
            code = (
                "from utils.process_lock import ProcessLock,AlreadyRunning; "
                "import sys; "
                "\ntry:\n with ProcessLock(sys.argv[1]): pass\n"
                "except AlreadyRunning: raise SystemExit(75)\n"
            )
            with ProcessLock(lock_path):
                result = subprocess.run(
                    [sys.executable, "-c", code, str(lock_path)],
                    cwd=BASE_DIR,
                    check=False,
                )
            self.assertEqual(result.returncode, 75)
            result = subprocess.run(
                [sys.executable, "-c", code, str(lock_path)],
                cwd=BASE_DIR,
                check=False,
            )
            self.assertEqual(result.returncode, 0)

    def test_worker_watchdog_exits_when_parent_is_missing(self):
        code = (
            "from utils.memory_guard import start_self_watchdog; import time; "
            "start_self_watchdog(1024**3, parent_pid=99999999, poll_seconds=.01); "
            "time.sleep(2)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=BASE_DIR, timeout=5, check=False
        )
        self.assertEqual(result.returncode, 87)

    def test_real_page_subprocess_returns_article(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = (
                    "<html><head><title>Integration title</title></head>"
                    "<body><p>first paragraph</p><p>second paragraph</p></body></html>"
                ).encode()
                self.send_response(200)
                # No charset: this reproduces streamed responses for which requests
                # defaults to ISO-8859-1. Encoding detection must use saved bytes,
                # not response.apparent_encoding after the stream was consumed.
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        previous_runtime = crawler_main.RUNTIME_DIR
        try:
            with test_directory() as folder:
                crawler_main.RUNTIME_DIR = Path(folder)
                url = f"http://127.0.0.1:{server.server_port}/article"
                article = crawler_main.crawl_in_subprocess(url, 100, 1)
            self.assertEqual(article["title"], "Integration title")
            self.assertIn("first paragraph", article["content"])
        finally:
            crawler_main.RUNTIME_DIR = previous_runtime
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class SchedulerTests(unittest.TestCase):
    def test_scheduler_validates_and_adds_retry(self):
        arguments = daily_crawler.normalize_crawler_args(
            ["--", "--url", "https://example.com", "--workers", "2"]
        )
        self.assertIn("--retry-failed", arguments)

    def test_scheduler_rejects_read_only_action(self):
        with self.assertRaises(ValueError):
            daily_crawler.normalize_crawler_args(["--", "--task-status"])


class CrawlJournalIntegrationTests(unittest.TestCase):
    def test_success_is_saved_and_not_repeated(self):
        with test_directory() as folder:
            folder = Path(folder)
            previous_cwd = Path.cwd()
            previous_values = (
                crawler_main.RUNTIME_DIR,
                crawler_main.RUN_LOCK,
                crawler_main.TASK_DB,
            )
            crawler_main.RUNTIME_DIR = folder / ".runtime"
            crawler_main.RUN_LOCK = crawler_main.RUNTIME_DIR / "crawler.lock"
            crawler_main.TASK_DB = crawler_main.RUNTIME_DIR / "tasks.sqlite3"
            args = SimpleNamespace(
                retry_failed=False,
                workers=2,
                memory_limit_mib=100,
                max_response_mib=1,
            )
            url = "https://article.example/one"
            article = {"url": url, "title": "saved", "content": "body"}
            try:
                os.chdir(folder)
                with patch.object(
                    crawler_main, "crawl_in_subprocess", return_value=article
                ) as crawl:
                    self.assertEqual(crawler_main.run_crawl(args, [url]), 0)
                    self.assertEqual(crawler_main.run_crawl(args, [url]), 0)
                    self.assertEqual(crawl.call_count, 1)
                self.assertIn('"title": "saved"', (folder / "articles.json").read_text())
                self.assertEqual(TaskJournal(crawler_main.TASK_DB).counts(), {"done": 1})
            finally:
                os.chdir(previous_cwd)
                (
                    crawler_main.RUNTIME_DIR,
                    crawler_main.RUN_LOCK,
                    crawler_main.TASK_DB,
                ) = previous_values


if __name__ == "__main__":
    unittest.main()
