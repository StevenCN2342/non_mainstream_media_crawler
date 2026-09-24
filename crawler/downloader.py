import requests
from charset_normalizer import from_bytes


class ResponseTooLarge(Exception):
    pass


class Downloader:

    def __init__(self, max_bytes=20 * 1024 * 1024):
        self.max_bytes = max_bytes

    def fetch(self, url):
        try:
            with requests.get(
                url,
                timeout=10,
                stream=True,
                headers={"User-Agent": "MediaCrawler"},
            ) as response:
                response.raise_for_status()

                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > self.max_bytes:
                    raise ResponseTooLarge(
                        f"response is larger than {self.max_bytes} bytes"
                    )

                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ResponseTooLarge(
                            f"response exceeded {self.max_bytes} bytes"
                        )
                    chunks.append(chunk)

                content = b"".join(chunks)
                encoding = response.encoding
                if not encoding or encoding.lower() == "iso-8859-1":
                    match = from_bytes(content).best()
                    if match and match.encoding:
                        encoding = match.encoding
                return content.decode(encoding or "utf-8", errors="replace")

        except Exception as e:
            print("Download error:", e)
            return ""
