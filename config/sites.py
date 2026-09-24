from pathlib import Path
from urllib.parse import urlparse

import yaml


class SiteConfigError(ValueError):
    pass


class SiteRegistry:
    def __init__(self, path):
        self.path = Path(path)
        self.sites = self._load()

    def _load(self):
        try:
            with self.path.open(encoding="utf-8") as handle:
                document = yaml.safe_load(handle) or {}
        except OSError as exc:
            raise SiteConfigError(f"无法读取站点配置：{self.path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise SiteConfigError(f"站点配置不是有效 YAML：{self.path}: {exc}") from exc

        rows = document.get("sites")
        if not isinstance(rows, list):
            raise SiteConfigError("站点配置必须包含 sites 列表")

        sites = []
        names = set()
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise SiteConfigError(f"sites 第 {index} 项必须是对象")
            name = str(row.get("name", "")).strip()
            url = str(row.get("url", "")).strip()
            if not name or not url:
                raise SiteConfigError(f"sites 第 {index} 项缺少 name 或 url")
            if name.casefold() in names:
                raise SiteConfigError(f"站点名称重复：{name}")
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise SiteConfigError(f"站点 URL 无效：{url}")
            names.add(name.casefold())
            sites.append({**row, "name": name, "url": url})
        return sites

    def list(self):
        return list(self.sites)

    def find(self, query):
        query = query.strip().casefold()
        exact = [site for site in self.sites if site["name"].casefold() == query]
        if exact:
            return exact[0]
        matches = [site for site in self.sites if query in site["name"].casefold()]
        if not matches:
            raise SiteConfigError(f"没有找到站点：{query}")
        if len(matches) > 1:
            names = "、".join(site["name"] for site in matches)
            raise SiteConfigError(f"站点名称不唯一：{names}")
        return matches[0]

    def urls(self, names=None, all_sites=False):
        if all_sites:
            return [site["url"] for site in self.sites]
        return [self.find(name)["url"] for name in names or []]
