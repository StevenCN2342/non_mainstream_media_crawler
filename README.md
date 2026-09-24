# 非官方媒体爬虫

这是一个支持多线程调度、网页子进程隔离、进程锁、内存保护、断点任务记录和独立循环调度器的 Python 爬虫。

## 运行结构

```text
daily_crawler.py（可选的独立调度器）
└── main.py（持有 crawler.lock 的主爬虫，N 个调度线程）
    ├── 网页子进程 1（只下载、解析一个 URL）
    ├── 网页子进程 2
    └── 最多同时 N 个网页子进程
```

- 调度器使用独立进程会话启动主爬虫。停止调度器不会强行终止已经开始的一轮。
- 每个网页子进程监测自身内存和主爬虫 PID；主爬虫消失后，子进程自动退出。
- `.runtime/crawler.lock` 防止同一项目同时运行两个主爬虫。
- `.runtime/scheduler_logs/scheduler.lock` 防止重复启动调度器。
- `.runtime/tasks.sqlite3` 保存 `pending/running/done/failed/deferred` 状态。
- 主爬虫异常退出后，下次启动会把遗留的 `running` 状态恢复为 `pending`。

## 安装

要求 Python 3.9 或更高版本：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

## 采集范围

### 直接指定 URL

`--url` 可以重复使用：

```bash
.venv/bin/python main.py \
  --url https://example.com/article-1 \
  --url https://example.com/article-2 \
  --workers 2
```

也可以每行一个 URL：

```bash
.venv/bin/python main.py --urls-file urls.txt --workers 2
```

### 使用站点配置

编辑 `config/sites.yaml`：

```yaml
sites:
  - name: example
    url: https://example.com/article
    adapter: auto
```

查看、选择或运行全部配置：

```bash
.venv/bin/python main.py --list-sites
.venv/bin/python main.py --site example --workers 1
.venv/bin/python main.py --all-sites --workers 2
```

目前每个配置项的 `url` 会作为一个页面直接抓取；本项目尚不负责从媒体首页自动发现全部文章链接，因此配置中应填写需要抓取的文章页或列表上游生成的 URL。

## 内存与并发

```bash
.venv/bin/python main.py \
  --urls-file urls.txt \
  --workers 2 \
  --memory-limit-mib 100 \
  --max-response-mib 20
```

- `--workers` 是同时工作的线程数，也是最多同时存在的网页子进程数。
- `--memory-limit-mib` 是每个网页子进程的 RSS 采样上限，不是整个服务器的总上限。
- `--max-response-mib` 是单次 HTTP 响应体上限。
- 内存超限任务记录为 `deferred`，不会自动用相同内存限制反复尝试。

## 任务状态与恢复

```bash
# 查看汇总以及所有未完成任务
.venv/bin/python main.py --task-status

# 恢复一个 failed/deferred 任务；done 任务不会被重置
.venv/bin/python main.py --retry-task 'https://example.com/article'

# 将所有 failed 任务恢复为 pending，并运行本次指定范围
.venv/bin/python main.py --urls-file urls.txt --retry-failed
```

成功任务再次出现在输入中时会直接跳过。突然断电如果刚好发生在文章写入之后、任务标记为 `done` 之前，JSONL 仍存在小概率重复窗口；SQLite 的 `url UNIQUE` 不会重复插入。

## 独立调度器

先只检查参数：

```bash
.venv/bin/python daily_crawler.py \
  --check \
  --loop-count 50 \
  --interval-seconds 86400 \
  --retry-seconds 60 \
  -- --all-sites --workers 2 --memory-limit-mib 100
```

确认后运行：

```bash
nohup .venv/bin/python -u daily_crawler.py \
  --loop-count 50 \
  --interval-seconds 86400 \
  --retry-seconds 60 \
  -- --all-sites --workers 2 --memory-limit-mib 100 \
  > .runtime/scheduler-console.log 2>&1 &
```

- `--loop-count 0` 表示持续运行；正整数表示完成多少个成功轮次后退出。
- `--interval-seconds` 从一轮成功结束到下一轮启动之间计算。
- 普通失败按 `--retry-seconds` 重试，调度器会自动加入 `--retry-failed`。
- 内存延后任务保留为 `deferred`，需提高限制后用 `--retry-task` 恢复。
- 各轮日志位于 `.runtime/scheduler_logs/`。

停止调度器只停止后续调度，已启动的主爬虫继续完成当前轮；主爬虫仍持有运行锁，因此重新启动调度器不会造成重叠。

## 测试

```bash
.venv/bin/python -m unittest -v test_runtime.py
```

测试覆盖跨进程锁、父进程死亡检测、站点配置、任务恢复、调度参数以及真实本机 HTTP 页面子进程。
