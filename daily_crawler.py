#!/usr/bin/env python3
import argparse
from datetime import datetime
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import main as crawler_main
from utils.process_lock import AlreadyRunning, ProcessLock


BASE_DIR = Path(__file__).resolve().parent
ENTRYPOINT = BASE_DIR / "main.py"
RUNTIME_DIR = BASE_DIR / ".runtime" / "scheduler_logs"
SCHEDULER_LOCK = RUNTIME_DIR / "scheduler.lock"
CRAWLER_LOCK = BASE_DIR / ".runtime" / "crawler.lock"
BUSY_EXIT_CODE = crawler_main.BUSY_EXIT_CODE
PARTIAL_EXIT_CODE = 2  # 主爬虫已遍历完本轮，但有站点或文章失败
DEFERRED_EXIT_CODE = 3
log = logging.getLogger("NonMainstreamScheduler")


def build_parser():
    parser = argparse.ArgumentParser(
        description="独立调度非官方媒体爬虫；调度器退出不终止已启动的主爬虫。"
    )
    parser.add_argument(
        "--interval-seconds", type=int, default=0,
        help="兼容旧命令；只允许 0，完成一轮后立即开始下一轮",
    )
    parser.add_argument("--retry-seconds", type=int, default=60)
    parser.add_argument(
        "--loop-count", type=int, default=1,
        help="完成遍历的轮数（个别失败仍计入）；0 表示持续运行"
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "crawler_args",
        nargs=argparse.REMAINDER,
        help="在 -- 后提供 main.py 参数",
    )
    return parser


def normalize_crawler_args(values):
    values = list(values)
    if values[:1] == ["--"]:
        values.pop(0)
    if not values:
        values = ["--all-sites", "--workers", "2"]
    if "--retry-failed" not in values:
        values.append("--retry-failed")
    parsed = crawler_main.build_parser().parse_args(values)
    if parsed.worker or parsed.task_status or parsed.retry_task or parsed.list_sites:
        raise ValueError("调度器参数必须是实际采集任务，不能使用内部或只读命令")
    if not (parsed.url or parsed.urls_file or parsed.site or parsed.all_sites):
        raise ValueError("调度器缺少 URL 或站点范围")
    return values


def launch_crawler(arguments, now=None):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    output = RUNTIME_DIR / ("crawl_" + now.strftime("%Y%m%d_%H%M%S_%f") + ".log")
    command = [sys.executable, "-u", str(ENTRYPOINT)] + arguments
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    with output.open("ab") as handle:
        process = subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            **options,
        )
    log.info("启动采集进程 PID=%s，日志=%s", process.pid, output)
    return process


def run_scheduler(arguments, retry_delay, loop_count):
    completed = 0
    attempt = 0  # 当前轮已实际启动的采集尝试；完成遍历后归零
    process = None
    next_launch = 0.0
    stopping = False

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    while not stopping and (loop_count == 0 or completed < loop_count):
        now = time.monotonic()
        if process is not None:
            code = process.poll()
            if code is None:
                time.sleep(1)
                continue
            process = None
            if code in (0, PARTIAL_EXIT_CODE, DEFERRED_EXIT_CODE):
                if code == 0:
                    log.info("第 %s/%s 轮遍历完成，无失败；共尝试 %s 次，重试 %s 次",
                             completed + 1, loop_count or "∞", attempt,
                             max(0, attempt - 1))
                elif code == PARTIAL_EXIT_CODE:
                    log.warning(
                        "第 %s/%s 轮遍历完成，但有站点或文章失败（退出码=%s）；"
                        "仍计入轮数，共尝试 %s 次，重试 %s 次；"
                        "站点失败见采集日志，文章失败保留任务记录",
                        completed + 1, loop_count or "∞", code,
                        attempt, max(0, attempt - 1),
                    )
                else:
                    log.warning(
                        "第 %s/%s 轮遍历完成，但有内存延后任务（退出码=%s）；"
                        "仍计入轮数，共尝试 %s 次，重试 %s 次；"
                        "延后任务保留记录，不视为采集成功",
                        completed + 1, loop_count or "∞", code,
                        attempt, max(0, attempt - 1),
                    )
                completed += 1
                attempt = 0
                if loop_count and completed >= loop_count:
                    log.info("目标轮次全部完成：%s/%s", completed, loop_count)
                else:
                    log.info("累计完成遍历 %s/%s 轮；立即开始下一轮",
                             completed, loop_count or "∞")
                    next_launch = now
            elif code == BUSY_EXIT_CODE:
                log.info(
                    "第 %s/%s 轮第 %s 次尝试未启动：其他采集进程仍在运行；"
                    "%s 秒后进行第 %s 次尝试",
                    completed + 1, loop_count or "∞", attempt,
                    retry_delay, attempt + 1,
                )
                next_launch = now + retry_delay
            else:
                log.error(
                    "第 %s/%s 轮第 %s 次尝试异常中断，退出码=%s；"
                    "不计入轮数，%s 秒后进行第 %s 次尝试",
                    completed + 1, loop_count or "∞", attempt,
                    code, retry_delay, attempt + 1,
                )
                next_launch = now + retry_delay
            continue

        if now < next_launch:
            time.sleep(min(1, next_launch - now))
            continue

        try:
            with ProcessLock(CRAWLER_LOCK):
                pass
        except AlreadyRunning:
            log.info("第 %s/%s 轮等待运行锁；当前已尝试 %s 次，%s 秒后再检查",
                     completed + 1, loop_count or "∞", attempt, retry_delay)
            next_launch = now + retry_delay
            time.sleep(1)
            continue
        attempt += 1
        log.info("开始第 %s/%s 轮，第 %s 次尝试（本轮重试 %s 次）",
                 completed + 1, loop_count or "∞", attempt, attempt - 1)
        try:
            process = launch_crawler(arguments)
        except OSError:
            log.exception("第 %s/%s 轮第 %s 次尝试启动失败；%s 秒后重试",
                          completed + 1, loop_count or "∞", attempt, retry_delay)
            next_launch = now + retry_delay

    if stopping:
        if process is not None and process.poll() is None:
            log.info("调度器停止；已完成遍历 %s/%s 轮，当前轮已尝试 %s 次；"
                     "独立采集进程 PID=%s 继续运行",
                     completed, loop_count or "∞", attempt, process.pid)
        else:
            log.info("调度器停止；已完成遍历 %s/%s 轮，当前轮已尝试 %s 次",
                     completed, loop_count or "∞", attempt)
    return 0


def main(argv=None):
    options = build_parser().parse_args(argv)
    if options.interval_seconds != 0:
        raise ValueError(
            "--interval-seconds 已停用；请删除该参数或设为 0，"
            "每轮完成后会立即开始下一轮"
        )
    if options.retry_seconds < 1:
        raise ValueError("--retry-seconds 必须大于 0")
    if options.loop_count < 0:
        raise ValueError("--loop-count 不能小于 0")
    arguments = normalize_crawler_args(options.crawler_args)
    if options.check:
        print("调度配置通过。")
        print("主爬虫参数：", arguments)
        print("每轮遍历完成后立即开始下一轮；异常中断后按重试间隔再次尝试。")
        return 0

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(RUNTIME_DIR / "scheduler.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    try:
        with ProcessLock(SCHEDULER_LOCK):
            log.info(
                "调度器启动：目标遍历轮数=%s，完成后立即开始下一轮，参数=%s",
                options.loop_count or "持续",
                arguments,
            )
            return run_scheduler(
                arguments,
                options.retry_seconds,
                options.loop_count,
            )
    except AlreadyRunning:
        log.error("已有一个调度器正在运行，本次退出")
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
