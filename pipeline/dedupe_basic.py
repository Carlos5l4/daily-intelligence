"""
dedupe_basic.py

Phase 3: 24小時時間過濾 + URL/Title 基本去重

這裡只做「程式規則」層級的過濾，不做語意判斷——語意層級的去重
（判斷「這是同一件事被不同媒體報導」）是 Stage 1 的責任，不在這支檔案的範圍內。

這裡做兩件事：
1. 時間過濾：只保留 published_at 在過去 N 小時內的文章。
   published_at 為 None 的文章（RSS 沒給發布時間）預設保留，
   而不是直接捨棄——因為捨棄掉可能誤殺原本就值得看的內容，
   風寧可讓 Stage 1 看到它、多花一點點 token，也不要在這一關就漏接。
2. 基本去重：
   - 完全相同的 URL（用 RawArticle.id 判斷）只保留第一次出現的
   - 標題正規化後完全相同的也視為重複（例如 Google News 抓到的文章，
     跟原本某個直接訂閱的媒體來源抓到同一篇，標題會逐字相同）
   這裡不做模糊比對／相似度計算，那樣的判斷屬於 Stage 1 的 cluster_id 分組邏輯。
"""

import re
from datetime import datetime, timedelta, timezone

RECENCY_HOURS = 24

# 每個來源最多保留幾則（取最新的）。Google News 四個來源每天各抓滿 100 則，
# 是 Stage 1 單次輸入過大的主因。改這個數字就能調整鬆緊；設 None 代表不限。
MAX_ARTICLES_PER_SOURCE = 40


def normalize_title_for_dedupe(title: str) -> str:
    """去除空白、標點符號、轉小寫，用來判斷兩個標題是否「本質上相同」。"""
    t = title.lower()
    t = re.sub(r"[\s\u3000]+", "", t)          # 移除所有空白（含全形空白）
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", t)   # 移除標點符號，保留文字與中文字元
    return t


def filter_by_recency(articles: list[dict], hours: int = RECENCY_HOURS) -> tuple[list[dict], int]:
    """
    回傳 (保留的文章, 被時間過濾掉的數量)
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    kept = []
    dropped_count = 0
    for article in articles:
        published_at = article.get("published_at")
        if published_at is None:
            kept.append(article)  # 無法判斷時間的，保留給 Stage 1 處理
            continue
        try:
            published_dt = datetime.fromisoformat(published_at)
        except ValueError:
            kept.append(article)
            continue
        if published_dt >= cutoff:
            kept.append(article)
        else:
            dropped_count += 1
    return kept, dropped_count


def dedupe_articles(articles: list[dict]) -> tuple[list[dict], int]:
    """
    回傳 (去重後的文章, 被去重掉的數量)
    """
    seen_ids = set()
    seen_titles = set()
    kept = []
    dropped_count = 0

    for article in articles:
        article_id = article["id"]
        normalized_title = normalize_title_for_dedupe(article["title"])

        if article_id in seen_ids or normalized_title in seen_titles:
            dropped_count += 1
            continue

        seen_ids.add(article_id)
        seen_titles.add(normalized_title)
        kept.append(article)

    return kept, dropped_count


def cap_per_source(articles: list[dict], limit: int | None = MAX_ARTICLES_PER_SOURCE) -> tuple[list[dict], int]:
    """
    回傳 (限量後的文章, 被限量剔除的數量)
    每個 source_id 依 published_at 由新到舊排序，只留前 limit 則；
    沒有發布時間的排在最後。原本的文章順序其餘維持不變。
    """
    if not limit:
        return articles, 0
    by_source: dict[str, list[dict]] = {}
    for a in articles:
        by_source.setdefault(a.get("source_id"), []).append(a)
    keep_ids = set()
    for items in by_source.values():
        items_sorted = sorted(items, key=lambda a: a.get("published_at") or "", reverse=True)
        keep_ids.update(a["id"] for a in items_sorted[:limit])
    kept = [a for a in articles if a["id"] in keep_ids]
    return kept, len(articles) - len(kept)


def process(articles: list[dict], recency_hours: int = RECENCY_HOURS) -> dict:
    """
    完整流程：先做時間過濾，再做去重。
    回傳完整統計資訊，方便寫進 daily_brief 的 stats 欄位追蹤 pipeline 健康度。
    """
    after_recency, dropped_by_recency = filter_by_recency(articles, recency_hours)
    deduped, dropped_by_dedupe = dedupe_articles(after_recency)
    final, dropped_by_source_cap = cap_per_source(deduped)

    return {
        "articles": final,
        "stats": {
            "total_fetched": len(articles),
            "dropped_by_recency_filter": dropped_by_recency,
            "dropped_by_dedupe": dropped_by_dedupe,
            "dropped_by_source_cap": dropped_by_source_cap,
            "final_count": len(final),
        },
    }
