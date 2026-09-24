import threading

from crawler.downloader import Downloader
from detector.media_detector import MediaDetector
from adapter.factory import AdapterFactory
from parser.article_parser import ArticleParser


class Crawler:

    def __init__(self, storage=None, writer=None, downloader=None):
        self.downloader = downloader or Downloader()
        self.storage = storage
        self.writer = writer
        self.detector = MediaDetector()
        self.parser = ArticleParser()
        self._save_lock = threading.Lock()

    def crawl_url(self, url):
        html = self.downloader.fetch(url)

        if not html:
            return None

        platform = self.detector.detect(html)
        adapter = AdapterFactory.create(platform)
        article = adapter.parse(html, url)
        article.update(self.parser.parse(html, url))
        return article

    def save_article(self, article):
        if not article:
            return

        with self._save_lock:
            if self.storage:
                self.storage.save(article)

            if self.writer:
                self.writer.save(article)

        print("Saved:", article.get("title", ""))

    def crawl_site(self, url):
        article = self.crawl_url(url)
        if not article:
            return
        self.save_article(article)
