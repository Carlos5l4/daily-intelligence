"""
normalize.py

Phase 3: Normalize — 正規化階段

把 fetch.py 回傳的「原始 feedparser entries」轉換成 Phase 2 定義的 RawArticle schema：

{
    "id": "sha256(url) 前16碼",
    "source_id": "來源設定裡的 id",
    "source_name": "來源設定裡的 name",
    "category": "來源設定裡的 category",
    "title": "文章標題",
    "url": "文章連結",
    "description": "RSS 原始摘要",
    "content": "全文（若 RSS 有提供，否則為 None）",
    "author": "作者（若有）",
    "published_at": "ISO 8601 UTC，若無法解析則為 None",
    "fetched_at": "ISO 8601 UTC"
}

這支檔案不做任何「該不該保留這篇文章」的判斷（那是 dedupe_basic.py 的責任），
只負責把不同來源、格式不統一的原始資料，轉換成後續模組都看得懂的固定格式。

例外：「週報/週末回顧」型文章不算判斷該不該保留，而是體裁本身就不是「每日新訊」，
所以在這裡直接濾掉，不進入 raw_articles.json，也不會消耗 Stage1 的 Gemini 額度。
"""

import hashlib
import re
from datetime import datetime, timezone

_HN_BOILERPLATE_RE = re.compile(r'^<a href="[^"]*">Comments</a>$', re.IGNORECASE)

RECAP_KEYWORDS = [
    "週末精選", "週報", "本週回顧", "上週回顧", "一週回顧", "週末回顧", "本周回顧",
]


def is_recap_article(title: str, description: str) -> bool:
    text = (title or "") + (description or "")
    return any(kw in text for kw in RECAP_KEYWORDS)


def make_article_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_published_at(entry) -> str | None:
    dt_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not dt_struct:
        return None
    return datetime(*dt_struct[:6], tzinfo=timezone.utc).isoformat()


def extract_description(entry) -> str:
    raw = entry.get("summary", "") or entry.get("description", "") or ""
    if _HN_BOILERPLATE_RE.match(raw.strip()):
        return ""
    return strip_html(raw)


def extract_content(entry) -> str | None:
    content_list = entry.get("content")
    if content_list:
        raw = content_list[0].get("value")
        return strip_html(raw) if raw else None
    return None


def extract_author(entry) -> str | None:
    return entry.get("author")


def normalize_entry(entry, source: dict, fetched_at: str) -> dict | None:
    url = entry.get("link")
    title = entry.get("title")
    if not url or not title:
        return None

    description = extract_description(entry).strip()

    if is_recap_article(title, description):
        return None

    return {
        "id": make_article_id(url),
        "source_id": source.get("id"),
        "source_name": source.get("name"),
        "category": source.get("category"),
        "title": title.strip(),
        "url": url,
        "description": description,
        "content": extract_content(entry),
        "author": extract_author(entry),
        "published_at": extract_published_at(entry),
        "fetched_at": fetched_at,
    }


def normalize_all(fetch_results: list[dict]) -> list[dict]:
    articles = []
    for result in fetch_results:
        if result["fetch_error"]:
            continue
        source = result["source"]
        fetched_at = result["fetched_at"]
        for entry in result["entries"]:
            article = normalize_entry(entry, source, fetched_at)
            if article:
                articles.append(article)
    return articles