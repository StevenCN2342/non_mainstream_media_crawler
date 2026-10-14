#!/usr/bin/env python3
import argparse
from datetime import datetime, timedelta, timezone
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
DAILY_HOUR = 18
DAILY_MINUTE = 0
UTC_OFFSET_HOURS = 8
DEFAULT_LOOP_COUNT = 50
POLL_SECONDS = 1


def build_parser():
    parser = argparse.ArgumentParser(
        description="独立调度非官方媒体爬虫；调度器退出不终止已启动的主爬虫。"
    )
    parser.add_argument(
        "--interval-seconds", type=int, default=0,
        help="兼容旧命令；只允许 0，每天的各轮之间立即连续运行",
    )
    parser.add_argument("--retry-seconds", type=int, default=60)
    parser.add_argument(
        "--loop-count", type=int, default=DEFAULT_LOOP_COUNT,
        help="每天完成遍历的轮数（默认 50；个别失败仍计入）"
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--run-now", action="store_true",
        help="启动后立即先执行当天批次，完成后继续等待每日定时任务",
    )
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


def schedule_timezone():
    return timezone(timedelta(hours=UTC_OFFSET_HOURS))


def next_scheduled_time(now):
    """返回 now 之后最近一次北京时间每日开跑时间。"""
    target = now.replace(hour=DAILY_HOUR, minute=DAILY_MINUTE,
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


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


def run_scheduler(arguments, retry_delay, loop_count, should_stop=lambda: False):
    completed = 0
    attempt = 0  # 当前轮已实际启动的采集尝试；完成遍历后归零
    process = None
    next_launch = 0.0
    while not should_stop() and completed < loop_count:
        now = time.monotonic()
        if process is not None:
            code = process.poll()
            if code is None:
                time.sleep(POLL_SECONDS)
                continue
            process = None
            if code in (0, PARTIAL_EXIT_CODE, DEFERRED_EXIT_CODE):
                if code == 0:
                    log.info("第 %s/%s 轮遍历完成，无失败；共尝试 %s 次，重试 %s 次",
                             completed + 1, loop_count, attempt,
                             max(0, attempt - 1))
                elif code == PARTIAL_EXIT_CODE:
                    log.warning(
                        "第 %s/%s 轮遍历完成，但有站点或文章失败（退出码=%s）；"
                        "仍计入轮数，共尝试 %s 次，重试 %s 次；"
                        "站点失败见采集日志，文章失败保留任务记录",
                        completed + 1, loop_count, code,
                        attempt, max(0, attempt - 1),
                    )
                else:
                    log.warning(
                        "第 %s/%s 轮遍历完成，但有内存延后任务（退出码=%s）；"
                        "仍计入轮数，共尝试 %s 次，重试 %s 次；"
                        "延后任务保留记录，不视为采集成功",
                        completed + 1, loop_count, code,
                        attempt, max(0, attempt - 1),
                    )
                completed += 1
                attempt = 0
                if completed >= loop_count:
                    log.info("目标轮次全部完成：%s/%s", completed, loop_count)
                else:
                    log.info("累计完成遍历 %s/%s 轮；立即开始下一轮",
                             completed, loop_count)
                    next_launch = now
            elif code == BUSY_EXIT_CODE:
                log.info(
                    "第 %s/%s 轮第 %s 次尝试未启动：其他采集进程仍在运行；"
                    "%s 秒后进行第 %s 次尝试",
                    completed + 1, loop_count, attempt,
                    retry_delay, attempt + 1,
                )
                next_launch = now + retry_delay
            else:
                log.error(
                    "第 %s/%s 轮第 %s 次尝试异常中断，退出码=%s；"
                    "不计入轮数，%s 秒后进行第 %s 次尝试",
                    completed + 1, loop_count, attempt,
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
                     completed + 1, loop_count, attempt, retry_delay)
            next_launch = now + retry_delay
            time.sleep(POLL_SECONDS)
            continue
        attempt += 1
        log.info("开始第 %s/%s 轮，第 %s 次尝试（本轮重试 %s 次）",
                 completed + 1, loop_count, attempt, attempt - 1)
        try:
            process = launch_crawler(arguments)
        except OSError:
            log.exception("第 %s/%s 轮第 %s 次尝试启动失败；%s 秒后重试",
                          completed + 1, loop_count, attempt, retry_delay)
            next_launch = now + retry_delay

    if should_stop():
        if process is not None and process.poll() is None:
            log.info("调度器停止；已完成遍历 %s/%s 轮，当前轮已尝试 %s 次；"
                     "独立采集进程 PID=%s 继续运行",
                     completed, loop_count, attempt, process.pid)
        else:
            log.info("调度器停止；已完成遍历 %s/%s 轮，当前轮已尝试 %s 次",
                     completed, loop_count, attempt)
    return completed


def main(argv=None):
    options = build_parser().parse_args(argv)
    if options.interval_seconds != 0:
        raise ValueError(
            "--interval-seconds 已停用；请删除该参数或设为 0，"
            "每轮完成后会立即开始下一轮"
        )
    if options.retry_seconds < 1:
        raise ValueError("--retry-seconds 必须大于 0")
    if options.loop_count < 1:
        raise ValueError("--loop-count 必须大于 0")
    arguments = normalize_crawler_args(options.crawler_args)
    if options.check:
        now = datetime.now(schedule_timezone())
        print("调度配置通过。")
        print("主爬虫参数：", arguments)
        print(f"每天北京时间 {DAILY_HOUR:02d}:{DAILY_MINUTE:02d} 启动 "
              f"{options.loop_count} 轮；当天各轮连续运行。")
        print("按当前时间计算，下次启动：", next_scheduled_time(now).isoformat())
        if options.run_now:
            print("已指定 --run-now：正式启动时会立即先执行一批，完成后继续每日定时。")
        print("异常中断后按重试间隔再次尝试。")
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
    stopping = False

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    tz = schedule_timezone()
    try:
        with ProcessLock(SCHEDULER_LOCK):
            log.info(
                "每日调度器启动：每天 %02d:%02d 开始，每日目标轮数=%s，参数=%s",
                DAILY_HOUR, DAILY_MINUTE, options.loop_count,
                arguments,
            )
            run_immediately = options.run_now
            while not stopping:
                if run_immediately:
                    target = datetime.now(tz)
                    run_immediately = False
                    log.info("收到 --run-now，立即开始手动批次")
                else:
                    target = next_scheduled_time(datetime.now(tz))
                    log.info("等待下次任务：%s", target.isoformat())
                    while not stopping:
                        remaining = (target - datetime.now(tz)).total_seconds()
                        if remaining <= 0:
                            break
                        time.sleep(min(POLL_SECONDS, remaining))
                    if stopping:
                        break
                log.info("开始 %s 的每日任务，共 %s 轮",
                         target.date().isoformat(), options.loop_count)
                completed = run_scheduler(
                    arguments, options.retry_seconds, options.loop_count,
                    should_stop=lambda: stopping,
                )
                if completed >= options.loop_count:
                    log.info("%s 的每日任务已完成，共 %s 轮；等待下一天",
                             target.date().isoformat(), completed)
            log.info("停止后续调度；已启动的主爬虫继续完成当前轮。")
            return 0
    except AlreadyRunning:
        log.error("已有一个调度器正在运行，本次退出")
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
