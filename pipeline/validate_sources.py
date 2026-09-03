"""
validate_sources.py

Phase 1: Sources Validation

對 sources_candidates.yaml 裡的每一個候選來源,檢查：
  1. RSS URL 是否有效(HTTP 200 且可解析)
  2. 是否近期仍持續更新(最新一則 published 是否在 RECENCY_DAYS 天內)
  3. RSS / Atom 是否可以正常解析(feedparser 無 bozo 錯誤)
  4. published_at 是否可靠(每則 item 是否都有 pubDate/updated)
  5. 是否有 description / content
  6. 是否需要另外抓文章頁面(description 是否過短，判斷是否僅為標題重複）
  7. 語言(粗略以 feed 內容判斷，中文/英文/其他）
  8. 建議 Priority（Core / Supplementary，依可靠度與命中率簡單評分）
  9. 錯誤訊息（如驗證失敗，記錄原因）

不自動判斷「是否容易與其他來源高度重複」——這件事需要跨來源比對實際抓到的文章，
留給 Stage 1 的語意分類/去重處理，此腳本只做單一來源本身的健康度檢查。
可信度（credibility）也不自動判斷，因為這是編輯判斷，不是程式可以客觀量化的事，
建議人工在 candidates 檔案裡先標註，腳本只驗證「這個來源技術上能不能穩定用」。

使用方式：
    pip install feedparser requests pyyaml --break-system-packages
    python validate_sources.py sources_candidates.yaml --output validation_report.yaml

驗證通過的來源會被標記 status: valid，並可用 --emit-sources 直接產生正式的 sources.yaml
（只包含 valid 的項目，符合「不把失效來源放入正式檔案」的原則）。
"""

import argparse
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import requests
import yaml

RECENCY_DAYS = 14          # 超過這麼多天沒更新，視為「可能停止更新」
MIN_DESC_LENGTH = 40       # description 短於這個字數，視為「可能需要另外抓文章頁」
REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; PersonalDailyIntelligence/1.0; +validation-script)"


def detect_language(texts):
    """粗略判斷語言：中文字元占比高於 20% 視為中文，否則視為英文/其他。"""
    sample = " ".join(texts)[:2000]
    if not sample.strip():
        return "unknown"
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", sample))
    ratio = cjk_count / max(len(sample), 1)
    if ratio > 0.15:
        return "zh"
    return "en"


def validate_one(source: dict) -> dict:
    result = {
        "id": source.get("id"),
        "name": source.get("name"),
        "url": source.get("url"),
        "category": source.get("category"),
        "status": "unknown",
        "errors": [],
    }

    url = source.get("url")
    if not url or not urlparse(url).scheme:
        result["status"] = "invalid"
        result["errors"].append("URL 缺失或格式錯誤")
        return result

    # 1. HTTP 存活檢查
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        result["http_status"] = resp.status_code
        if resp.status_code >= 400:
            result["status"] = "invalid"
            result["errors"].append(f"HTTP {resp.status_code}")
            return result
    except requests.RequestException as e:
        result["status"] = "invalid"
        result["errors"].append(f"連線失敗: {e}")
        return result

    # 3. RSS/Atom 解析
    feed = feedparser.parse(resp.content)
    result["parse_bozo"] = bool(feed.bozo)
    if feed.bozo:
        result["errors"].append(f"解析警告: {getattr(feed, 'bozo_exception', '未知錯誤')}")

    entries = feed.entries
    if not entries:
        result["status"] = "invalid"
        result["errors"].append("Feed 內沒有任何 item")
        return result

    result["item_count"] = len(entries)

    # 2. 近期是否更新 + 4. published_at 可靠度
    published_dates = []
    missing_date_count = 0
    for e in entries:
        dt_struct = e.get("published_parsed") or e.get("updated_parsed")
        if dt_struct:
            published_dates.append(datetime(*dt_struct[:6], tzinfo=timezone.utc))
        else:
            missing_date_count += 1

    result["missing_published_at_count"] = missing_date_count
    result["published_at_reliable"] = missing_date_count == 0

    if published_dates:
        latest = max(published_dates)
        days_since = (datetime.now(timezone.utc) - latest).days
        result["latest_published_at"] = latest.isoformat()
        result["days_since_latest"] = days_since
        result["actively_updating"] = days_since <= RECENCY_DAYS
        if days_since > RECENCY_DAYS:
            result["errors"].append(f"最新項目已 {days_since} 天未更新（門檻 {RECENCY_DAYS} 天）")
    else:
        result["actively_updating"] = False
        result["errors"].append("所有 item 都沒有可用的發布時間")

    # 5 & 6. description / content 完整度
    desc_lengths = []
    for e in entries[:10]:
        desc = e.get("summary") or e.get("description") or ""
        content_list = e.get("content")
        content_text = content_list[0].get("value", "") if content_list else ""
        text_len = max(len(desc), len(content_text))
        desc_lengths.append(text_len)

    avg_desc_len = sum(desc_lengths) / len(desc_lengths) if desc_lengths else 0
    result["avg_description_length"] = round(avg_desc_len, 1)
    result["has_description"] = avg_desc_len > 0
    result["needs_article_fetch"] = avg_desc_len < MIN_DESC_LENGTH

    # 7. 語言
    sample_titles = [e.get("title", "") for e in entries[:10]]
    sample_descs = [e.get("summary", "") for e in entries[:10]]
    result["language"] = detect_language(sample_titles + sample_descs)

    # 綜合判定
    if result["errors"] and not result.get("actively_updating", False):
        result["status"] = "invalid"
    elif result["errors"]:
        result["status"] = "warning"
    else:
        result["status"] = "valid"

    # 8. 建議 priority（簡單啟發式：valid 且近3天內更新 -> core；其餘 supplementary）
    if result["status"] == "valid" and result.get("days_since_latest", 999) <= 3:
        result["suggested_priority"] = "core"
    elif result["status"] == "valid":
        result["suggested_priority"] = "supplementary"
    else:
        result["suggested_priority"] = "exclude"

    return result


def main():
    parser = argparse.ArgumentParser(description="Validate candidate RSS sources against Phase 1 checklist.")
    parser.add_argument("candidates_file", help="sources_candidates.yaml 路徑")
    parser.add_argument("--output", default="validation_report.yaml", help="驗證報告輸出路徑")
    parser.add_argument("--emit-sources", default=None, help="若指定路徑，會另外產生只含 valid 來源的 sources.yaml")
    args = parser.parse_args()

    with open(args.candidates_file, "r", encoding="utf-8") as f:
        candidates = yaml.safe_load(f)["candidates"]

    report = []
    for c in candidates:
        print(f"驗證中: {c.get('name')} ...", file=sys.stderr)
        report.append(validate_one(c))

    with open(args.output, "w", encoding="utf-8") as f:
        yaml.safe_dump({"validated_at": datetime.now(timezone.utc).isoformat(), "results": report},
                        f, allow_unicode=True, sort_keys=False)

    valid_count = sum(1 for r in report if r["status"] == "valid")
    warning_count = sum(1 for r in report if r["status"] == "warning")
    invalid_count = sum(1 for r in report if r["status"] == "invalid")
    print(f"\n完成：valid={valid_count}, warning={warning_count}, invalid={invalid_count}", file=sys.stderr)

    if args.emit_sources:
        sources_out = []
        for c, r in zip(candidates, report):
            if r["status"] in ("valid", "warning"):
                sources_out.append({
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "url": c.get("url"),
                    "category": c.get("category"),
                    "language": r.get("language"),
                    "credibility": c.get("credibility", "medium"),
                    "priority": r.get("suggested_priority"),
                    "needs_article_fetch": r.get("needs_article_fetch"),
                    "personal_weight": c.get("personal_weight", 0.5),
                })
        with open(args.emit_sources, "w", encoding="utf-8") as f:
            yaml.safe_dump({"sources": sources_out}, f, allow_unicode=True, sort_keys=False)
        print(f"已產生 {args.emit_sources}（僅含 valid/warning 來源）", file=sys.stderr)


if __name__ == "__main__":
    main()
