"""Bounded, same-site article URL discovery from a catalog homepage."""

import re
import time
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests

from crawler.downloader import Downloader


USER_AGENT = "MediaCrawler"
ARTICLE_PATH = re.compile(
    r"(?:/\d{4}/\d{1,2}/|\d{5,}|/articles?/[^/]+|/detail/[^/]+|"
    r"/story(?:/|\?)|/posts?/[^/]+|/news/[^/]+|\.s?html?$)", re.I
)
CHANNEL_PATH = re.compile(
    r"(?:/news/?$|/latest/?$|/category/|/channel/|/section/|"
    r"/articles/?$|/tech/?$|/finance/?$|/world/?$|/china/?$)", re.I
)
IGNORE_SUFFIX = re.compile(
    r"\.(?:jpg|jpeg|png|gif|webp|svg|pdf|zip|mp4|mp3|css|js)(?:$|\?)", re.I
)


def _host(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normalize_link(href, base_url, site_url):
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    url = urljoin(base_url, href.strip())
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or _host(url) != _host(site_url):
        return None
    if IGNORE_SUFFIX.search(parsed.path) or parsed.username or parsed.password:
        return None
    # Fragments identify a location in the same page, not another article.
    return urlunparse(parsed._replace(fragment=""))


def classify_link(url, text):
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    if path == "/" or re.search(r"/(?:login|register|tag|search|author)/", path, re.I):
        return None
    if re.search(r"(?:[?&](?:sid|id)=\d{3,})", parsed.query, re.I):
        return "article"
    if ARTICLE_PATH.search(path) and (len(text) >= 4 or re.search(r"\d{5,}", path)):
        return "article"
    if CHANNEL_PATH.search(path):
        return "channel"
    return None


def extract_links(html, page_url, site_url):
    soup = BeautifulSoup(html, "lxml")
    articles, channels = [], []
    for anchor in soup.find_all("a", href=True):
        url = normalize_link(anchor["href"], page_url, site_url)
        if not url:
            continue
        kind = classify_link(url, anchor.get_text(" ", strip=True))
        if kind == "article":
            articles.append(url)
        elif kind == "channel":
            channels.append(url)
    return list(dict.fromkeys(articles)), list(dict.fromkeys(channels))


class RobotsPolicy:
    def __init__(self, site_url):
        parsed = urlparse(site_url)
        self.url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        self.parser = RobotFileParser()
        self.available = False

    def load(self):
        try:
            response = requests.get(
                self.url, timeout=10, headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            )
            if response.status_code in (404, 410):
                self.parser.allow_all = True
                self.available = True
            elif response.status_code == 200 and _host(response.url) == _host(self.url):
                self.parser.parse(response.text.splitlines())
                self.available = True
        except requests.RequestException:
            pass
        return self.available

    def allows(self, url):
        return self.available and self.parser.can_fetch(USER_AGENT, url)


def discover_articles(site_url, max_articles=10, max_pages=3, max_response_mib=20,
                      max_articles_per_page=10):
    """Bound listing-page requests and article links selected from each page."""
    policy = RobotsPolicy(site_url)
    if not policy.load():
        raise ValueError(f"robots.txt 无法确认抓取许可：{policy.url}")
    if not policy.allows(site_url):
        raise ValueError(f"robots.txt 禁止抓取：首页 {site_url}")
    downloader = Downloader(max_bytes=max_response_mib * 1024 * 1024)
    pages = [site_url]
    visited = set()
    articles = []
    while pages and len(visited) < max_pages and len(articles) < max_articles:
        page = pages.pop(0)
        if page in visited or not policy.allows(page):
            continue
        visited.add(page)
        if len(visited) > 1:
            time.sleep(0.5)
        html = downloader.fetch(page)
        if not html:
            if page == site_url:
                raise ValueError(f"首页下载失败或拒绝访问：{site_url}")
            continue
        found, channels = extract_links(html, page, site_url)
        selected_on_page = 0
        for url in found:
            if url not in articles and policy.allows(url):
                articles.append(url)
                selected_on_page += 1
                if selected_on_page >= max_articles_per_page or len(articles) >= max_articles:
                    break
        for url in channels:
            if url not in visited and url not in pages and policy.allows(url):
                pages.append(url)
    return articles
