import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from config.sites import SiteConfigError, SiteRegistry
from crawler.crawler import Crawler
from crawler.downloader import Downloader
from storage.json_writer import JsonWriter
from storage.sqlite import SQLiteStorage
from storage.task_journal import TaskJournal
from utils.memory_guard import MemoryLimitExceeded, monitor_process, start_self_watchdog
from utils.process_lock import AlreadyRunning, ProcessLock


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / ".runtime"
RUN_LOCK = RUNTIME_DIR / "crawler.lock"
TASK_DB = RUNTIME_DIR / "tasks.sqlite3"
DEFAULT_SITES = BASE_DIR / "config" / "sites.yaml"
BUSY_EXIT_CODE = 75


def build_parser():
    parser = argparse.ArgumentParser(description="非官方媒体爬虫")
    parser.add_argument("--url", action="append", help="文章 URL；可重复提供")
    parser.add_argument("--urls-file", help="每行一个 URL 的文本文件")
    parser.add_argument("--site", action="append", help="sites.yaml 中的站点名称")
    parser.add_argument("--all-sites", action="store_true", help="抓取 sites.yaml 全部 URL")
    parser.add_argument("--sites-file", default=str(DEFAULT_SITES))
    parser.add_argument("--list-sites", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--memory-limit-mib", type=int, default=100)
    parser.add_argument("--max-response-mib", type=int, default=20)
    parser.add_argument("--task-status", action="store_true")
    parser.add_argument("--retry-task", metavar="URL")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--worker", nargs=2, help=argparse.SUPPRESS)
    return parser


def load_urls(args):
    urls = list(args.url or [])
    if args.urls_file:
        with open(args.urls_file, encoding="utf-8") as handle:
            urls.extend(
                line.strip()
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            )
    if args.site or args.all_sites:
        registry = SiteRegistry(args.sites_file)
        urls.extend(registry.urls(args.site, args.all_sites))
    return list(dict.fromkeys(urls))


def run_worker(request_path, result_path):
    with open(request_path, encoding="utf-8") as handle:
        request = json.load(handle)
    limit_bytes = int(request["memory_limit_mib"] * 1024 * 1024)
    stopped = start_self_watchdog(limit_bytes, request["parent_pid"])
    try:
        crawler = Crawler(
            downloader=Downloader(
                max_bytes=int(request["max_response_mib"] * 1024 * 1024)
            )
        )
        article = crawler.crawl_url(request["url"])
        if not article:
            raise ValueError("没有抓取到可保存的文章")
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump({"article": article}, handle, ensure_ascii=False)
        return 0
    except Exception as exc:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump({"error": str(exc)}, handle, ensure_ascii=False)
        return 1
    finally:
        stopped.set()


def crawl_in_subprocess(url, memory_limit_mib, max_response_mib):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    try:
        for _ in range(2):
            with tempfile.NamedTemporaryFile(
                prefix="page-ipc-", suffix=".json", dir=RUNTIME_DIR, delete=False
            ) as handle:
                paths.append(handle.name)
        request_path, result_path = paths
        with open(request_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "url": url,
                    "parent_pid": os.getpid(),
                    "memory_limit_mib": memory_limit_mib,
                    "max_response_mib": max_response_mib,
                },
                handle,
                ensure_ascii=False,
            )
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(Path(__file__).resolve()),
                "--worker",
                request_path,
                result_path,
            ],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        code, peak = monitor_process(process, int(memory_limit_mib * 1024 * 1024))
        if code != 0:
            error = f"子进程异常退出 code={code}"
            try:
                with open(result_path, encoding="utf-8") as handle:
                    error += ": " + json.load(handle).get("error", "")
            except (OSError, ValueError, AttributeError):
                pass
            raise RuntimeError(error)
        with open(result_path, encoding="utf-8") as handle:
            article = json.load(handle)["article"]
        if peak:
            print(f"Worker memory peak: {peak // 1024 // 1024} MiB")
        return article
    finally:
        for path in paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def print_task_status(journal):
    print("任务汇总：" + json.dumps(journal.counts(), ensure_ascii=False))
    for row in journal.unfinished():
        print(json.dumps(row, ensure_ascii=False))


def process_result(crawler, journal, url, future):
    try:
        crawler.save_article(future.result())
        journal.set_status(url, "done")
        return True
    except MemoryLimitExceeded as exc:
        journal.set_status(url, "deferred", exc)
        print("Skipped by memory limit:", url, exc)
    except Exception as exc:
        journal.set_status(url, "failed", exc)
        print("Crawl error:", url, exc)
    return False


def run_crawl(args, urls):
    with ProcessLock(RUN_LOCK):
        journal = TaskJournal(TASK_DB, recover=True)
        if args.retry_failed:
            print(f"已恢复 {journal.retry_failed()} 个失败任务。")
        journal.add_many(urls)
        pending = journal.pending_urls(urls)
        if not pending:
            counts = journal.counts(urls)
            print("本次范围没有待处理任务：" + json.dumps(counts, ensure_ascii=False))
            return 0 if not (counts.get("failed") or counts.get("deferred")) else 2

        crawler = Crawler(SQLiteStorage(), JsonWriter())
        succeeded = 0
        try:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {}
                for url in pending:
                    journal.set_status(url, "running")
                    future = executor.submit(
                        crawl_in_subprocess,
                        url,
                        args.memory_limit_mib,
                        args.max_response_mib,
                    )
                    futures[future] = url
                for future in as_completed(futures):
                    succeeded += process_result(crawler, journal, futures[future], future)
        finally:
            for resource in (crawler.writer, crawler.storage):
                close = getattr(resource, "close", None)
                if close:
                    close()
        failed = len(pending) - succeeded
        print(f"本轮完成：成功 {succeeded}，失败或延后 {failed}。")
        if failed == 0:
            return 0
        counts = journal.counts(urls)
        return 3 if counts.get("deferred") and not counts.get("failed") else 2


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.worker:
        return run_worker(args.worker[0], args.worker[1])
    os.chdir(BASE_DIR)

    if args.workers < 1:
        parser.error("--workers 必须大于 0")
    if args.memory_limit_mib < 16:
        parser.error("--memory-limit-mib 至少设置为 16")
    if args.max_response_mib < 1:
        parser.error("--max-response-mib 至少设置为 1")

    try:
        if args.list_sites:
            for site in SiteRegistry(args.sites_file).list():
                print(f"{site['name']}\t{site['url']}")
            return 0
        if args.task_status:
            print_task_status(TaskJournal(TASK_DB))
            return 0
        if args.retry_task:
            with ProcessLock(RUN_LOCK):
                changed = TaskJournal(TASK_DB).retry(args.retry_task)
            print(f"已恢复 {changed} 个任务。")
            return 0 if changed else 1
        urls = load_urls(args)
        if not urls:
            parser.error("请提供 --url、--urls-file、--site 或 --all-sites")
        return run_crawl(args, urls)
    except AlreadyRunning:
        print("已有一个爬虫实例正在运行，本次退出。")
        return BUSY_EXIT_CODE
    except (OSError, SiteConfigError, ValueError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
