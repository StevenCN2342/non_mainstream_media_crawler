#!/usr/bin/env python3
"""Import the curated v9 media report into config/sites.yaml.

The report is treated strictly as data. Only the explicitly selected candidate
tables are read; prose, commands, citations, excluded rows, and observation-only
rows are ignored. Exact duplicate URLs are consolidated and retained as aliases.
"""

import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit


BASE_SECTIONS = (
    (22, 49, "综合新媒体与深度报道"),
    (50, 125, "财经与商业媒体"),
    (126, 320, "科技/AI/创投/产业垂直媒体"),
    (321, 352, "国际媒体中文版与政策智库"),
    (353, 386, "地产/能源/工业独立行业媒体"),
    (387, 426, "医疗/健康/生命科学独立媒体"),
)

ROUND4_INCLUDED = {
    "智能涌现",
    "电子工程专辑",
    "电子元件技术网",
    "半导体产业纵横",
    "中国能源网",
    "医疗器械创新网",
    "医药云端",
    "药闻社",
}


def normalize_url(url):
    parsed = urlsplit(url.strip().rstrip("/"))
    host = (parsed.hostname or "").lower()
    if not host or parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"无效 URL：{url}")
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), host + port, path, parsed.query, ""))


def split_row(line):
    columns = re.split(r"\s{2,}", line.strip())
    for index, value in enumerate(columns):
        if value.startswith(("http://", "https://")) and index >= 2:
            return (
                columns[0].strip("*"),
                columns[index - 1].strip("*"),
                value,
                columns[index + 1].strip("`") if index + 1 < len(columns) else "",
            )
    return None


class Catalog:
    def __init__(self):
        self.records = []

    def remove_names(self, names):
        folded = {name.casefold() for name in names}
        self.records = [
            row
            for row in self.records
            if row["name"].casefold() not in folded
            and not folded.intersection(alias.casefold() for alias in row["aliases"])
        ]

    def add(self, name, url, category, source, tier="", replace_names=()):
        name = re.sub(r"\s+", " ", name).strip()
        url = normalize_url(url)
        if replace_names:
            self.remove_names(replace_names)

        for row in self.records:
            if normalize_url(row["url"]) == url:
                if name != row["name"] and name not in row["aliases"]:
                    row["aliases"].append(name)
                return

        for row in self.records:
            if row["name"].casefold() == name.casefold():
                row.update(url=url, category=category, source=source, tier=tier)
                return

        self.records.append(
            {
                "name": name,
                "url": url,
                "adapter": "auto",
                "category": category,
                "source": source,
                "tier": tier,
                "aliases": [],
            }
        )

    def add_aliases(self, name, aliases):
        for row in self.records:
            if row["name"].casefold() == name.casefold():
                for alias in aliases:
                    if alias != row["name"] and alias not in row["aliases"]:
                        row["aliases"].append(alias)
                return
        raise KeyError(name)


def import_report(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    catalog = Catalog()

    for start, end, category in BASE_SECTIONS:
        for line in lines[start - 1 : end]:
            match = re.search(r"\*\*(.+?)\*\*\s+<(https?://[^>]+)>", line)
            if match:
                catalog.add(match.group(1), match.group(2), category, "v9-base")

    for line in lines[470:515]:
        match = re.search(
            r"^(.*?)\s{2,}\*\*(.+?)\*\*\s+(https?://\S+)\s+`([^`]+)`",
            line.strip(),
        )
        if match:
            catalog.add(
                match.group(2), match.group(3), match.group(1), "v9-round2", match.group(4)
            )

    for line in lines[587:598]:
        row = split_row(line)
        if row:
            category, name, url, tier = row
            catalog.add(name, url, category, "v9-round3", tier)

    for line in lines[672:704]:
        row = split_row(line)
        if row and row[1] in ROUND4_INCLUDED:
            category, name, url, tier = row
            catalog.add(name, url, category, "v9-round4", tier)

    replacements = {
        "读懂财经": ("读懂财经",),
        "深蓝财经": ("深蓝财经",),
        "爱集微/集微": ("集微网", "爱集微", "爱集微/集微"),
        "汽车商业评论": ("汽车商业评论",),
    }
    for line in lines[767:784]:
        row = split_row(line)
        if row:
            category, name, url, tier = row
            catalog.add(
                name,
                url,
                category,
                "v9-round5",
                tier,
                replace_names=replacements.get(name, ()),
            )
            if name == "爱集微/集微":
                catalog.add_aliases(name, ("集微网", "爱集微"))

    for line in lines[853:861]:
        row = split_row(line)
        if row:
            category, name, url, tier = row
            replace = ("电子工程专辑",) if name.startswith("电子工程专辑") else ()
            catalog.add(name, url, category, "v9-round6", tier, replace_names=replace)
            if replace:
                catalog.add_aliases(name, replace)

    for start, end, source in ((924, 926, "v9-round7"), (1014, 1016, "v9-round8")):
        for line in lines[start - 1 : end]:
            row = split_row(line)
            if row:
                category, name, url, tier = row
                catalog.add(name, url, category, source, tier)

    # The report's fifth-round review explicitly says the earlier Yuanfudao URL
    # for 远川研究所 was wrong and must not continue to be used.
    catalog.remove_names(("远川研究所",))
    return catalog.records


def quote(value):
    return json.dumps(value, ensure_ascii=False)


def render(records, source_name):
    lines = [
        f"# Generated from {source_name}; do not edit by hand.",
        f"# {len(records)} unique catalog URLs; aliases preserve duplicate brand names.",
        "# Reachability and crawl permission still require per-site verification.",
        "# The obsolete 远川研究所/Yuanfudao record is intentionally excluded.",
        "sites:",
    ]
    for row in records:
        lines.extend(
            [
                f"  - name: {quote(row['name'])}",
                f"    url: {quote(row['url'])}",
                f"    adapter: {quote(row['adapter'])}",
                f"    category: {quote(row['category'])}",
                f"    source: {quote(row['source'])}",
            ]
        )
        if row["tier"]:
            lines.append(f"    tier: {quote(row['tier'])}")
        if row["aliases"]:
            lines.append("    aliases:")
            lines.extend(f"      - {quote(alias)}" for alias in row["aliases"])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    records = import_report(args.report)
    if len(records) < 175:
        raise SystemExit(f"提取数量异常：仅 {len(records)} 条")
    names = [row["name"].casefold() for row in records]
    urls = [normalize_url(row["url"]) for row in records]
    if len(names) != len(set(names)) or len(urls) != len(set(urls)):
        raise SystemExit("生成结果仍有重复名称或 URL")
    args.output.write_text(render(records, args.report.name), encoding="utf-8")
    print(f"已生成 {len(records)} 个唯一站点：{args.output}")


if __name__ == "__main__":
    main()
