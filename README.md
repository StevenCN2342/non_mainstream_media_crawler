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

`config/sites.yaml` 已从《非官方独立新闻媒体官网全量汇总与爬虫协议实测报告（清洗合并版）v9》生成，包含 175 个去重后的官网入口 URL，并保留合并品牌别名、媒体分类、来源轮次和候选分层。这些地址尚未在当前环境逐站复测可达性和抓取许可。

条目格式：

```yaml
sites:
  - name: 界面新闻
    url: https://www.jiemian.com
    adapter: auto
    category: 综合新媒体与深度报道
    source: v9-base
```

查看、选择或运行全部配置：

```bash
.venv/bin/python main.py --list-sites
.venv/bin/python main.py --site 界面新闻 --workers 1
.venv/bin/python main.py --all-sites --workers 2
```

别名同样可以用于 `--site`，例如“集微网”会解析到合并后的“爱集微/集微”记录。

`--site` 和 `--all-sites` 现在会从官网首页及少量栏目页发现文章链接，再逐篇抓取；首页和栏目页本身不作为文章保存。每站每轮默认最多读取 3 个首页/栏目页、发现 10 篇文章；`--max-pages-per-site` 限制列表页数量，`--max-articles-per-page` 限制每个列表页选取的文章链接数，`--max-articles-per-site` 限制全站总文章数。例如设置 5、5、25，最多读取 5 个列表页并选取 25 篇文章，文章页面的 HTTP 请求另计。发现步骤也在独立网页子进程中运行，继续受内存限制和父进程监控。已完成的文章 URL 会被任务记录跳过；下一轮仍会重新检查首页，以便发现新文章。

发现阶段只接受同站链接，遵守 robots.txt；robots.txt 无法确认、拒绝访问或站点返回 403 时会报告失败，不尝试绕过。部分站点使用 JavaScript、付费墙、特殊文章路径或跨域内容入口，通用规则可能发现 0 篇，需按站点补充配置或解析逻辑。175 个名录条目不代表 175 个站点均已实测可采集，建议先用少量 `--site` 运行并查看日志与结果。

如需从原始报告重新生成名录：

```bash
python scripts/import_v9_report.py /path/to/report_v9.md config/sites.yaml
```

文章保存在项目目录下的 `articles.db` 和 `articles.json`，任务状态保存在 `.runtime/tasks.sqlite3`。`articles.json` 的站点模式结果还包含 `site_name` 和 `category`。如果某网站页面能访问但没有可解析的标题和正文，文章任务会标为 `failed`，不会把空文章当成成功。

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
