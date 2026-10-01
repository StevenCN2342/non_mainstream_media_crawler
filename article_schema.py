"""Canonical JSONL article schema shared by every storage backend."""


CANONICAL_FIELDS = (
    "url",
    "title",
    "content",
    "siteName",
    "source",
    "category",
    "subCategory",
    "channel",
    "publishTime",
    "images",
    "crawledAt",
    "docNumber",
    "indexNumber",
)


def _first(record, *keys):
    for key in keys:
        if key not in record or record[key] is None:
            continue
        value = record[key]
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _optional_string(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"字段 {field} 必须是字符串或 null")
    return value if value.strip() else None


def normalize_article(record):
    """Return the fixed 13-field structure used by the converted dataset."""
    if not isinstance(record, dict):
        raise ValueError("文章记录必须是 object")

    required = {}
    for field in ("url", "title", "content"):
        value = record.get(field)
        if not isinstance(value, str):
            raise ValueError(f"字段 {field} 必须是字符串")
        if field in ("url", "title") and not value.strip():
            raise ValueError(f"字段 {field} 不能为空")
        required[field] = value

    images = record.get("images")
    if images is None:
        images = []
    if not isinstance(images, list) or any(not isinstance(item, str) for item in images):
        raise ValueError("字段 images 必须是字符串数组或 null")

    normalized = {
        **required,
        "siteName": _optional_string(
            _first(record, "siteName", "site_name", "media_name"), "siteName"
        ),
        "source": _optional_string(record.get("source"), "source"),
        "category": _optional_string(
            _first(record, "category", "media_category"), "category"
        ),
        "subCategory": _optional_string(
            _first(record, "subCategory", "sub_category"), "subCategory"
        ),
        "channel": _optional_string(record.get("channel"), "channel"),
        "publishTime": _optional_string(
            _first(record, "publishTime", "publish_time"), "publishTime"
        ),
        "images": list(images),
        "crawledAt": _optional_string(
            _first(record, "crawledAt", "crawled_at", "crawl_time"), "crawledAt"
        ),
        "docNumber": _optional_string(
            _first(record, "docNumber", "doc_number"), "docNumber"
        ),
        "indexNumber": _optional_string(
            _first(record, "indexNumber", "index_number"), "indexNumber"
        ),
    }
    if tuple(normalized) != CANONICAL_FIELDS:
        raise AssertionError("文章字段集合或顺序不符合统一规范")
    return normalized
