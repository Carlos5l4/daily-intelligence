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
"""

import hashlib
import re
from datetime import datetime, timezone

# Hacker News RSS 的 description 固定是這種「留言連結」樣板，沒有實際摘要內容，
# 送進 Gemini 沒有意義，偵測到就當作空字串處理，讓 Stage 1 知道只能依賴標題判斷。
_HN_BOILERPLATE_RE = re.compile(r'^<a href="[^"]*">Comments</a>$', re.IGNORECASE)


def make_article_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def strip_html(text: str | None) -> str:
    """移除 HTML 標籤，保留純文字。RSS 摘要/內文常混雜 <img>、<a> 等標籤，
    這些對 Gemini 分類沒有幫助，只會浪費 token，統一在正規化階段清乾淨。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)   # 移除標籤
    text = re.sub(r"\s+", " ", text)        # 合併多餘空白
    return text.strip()


def extract_published_at(entry) -> str | None:
    dt_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not dt_struct:
        return None
    return datetime(*dt_struct[:6], tzinfo=timezone.utc).isoformat()


def extract_description(entry) -> str:
    raw = entry.get("summary", "") or entry.get("description", "") or ""
    if _HN_BOILERPLATE_RE.match(raw.strip()):
        return ""  # Hacker News 的樣板留言連結，不是真的摘要
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
        # 沒有連結或標題的 item 無法使用，直接捨棄
        return None

    return {
        "id": make_article_id(url),
        "source_id": source.get("id"),
        "source_name": source.get("name"),
        "category": source.get("category"),
        "title": title.strip(),
        "url": url,
        "description": extract_description(entry).strip(),
        "content": extract_content(entry),
        "author": extract_author(entry),
        "published_at": extract_published_at(entry),
        "fetched_at": fetched_at,
    }


def normalize_all(fetch_results: list[dict]) -> list[dict]:
    """
    輸入 fetch_all() 的回傳值，輸出攤平後的 RawArticle 清單。
    fetch_error 不為 None 的來源（抓取失敗）會被跳過，不會產生任何文章。
    """
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
