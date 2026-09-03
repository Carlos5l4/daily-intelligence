"""
fetch.py

Phase 3: Fetcher — 抓取階段

只做一件事：依照 config/sources.yaml 的清單，逐一發送 HTTP 請求、
用 feedparser 解析成 entries，回傳「來源設定 + 原始 entries」的配對。

刻意不在這裡做任何欄位轉換或篩選——那是 normalize.py 跟 dedupe_basic.py 的責任。
這支檔案只負責「把資料從外部世界搬進來」，職責單一，之後如果要換一種抓取方式
（例如改用非同步請求），只需要動這支檔案。
"""

import sys
from datetime import datetime, timezone

import feedparser
import requests
import yaml

REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; PersonalDailyIntelligence/1.0; +fetcher)"


def load_sources(sources_path: str) -> list[dict]:
    with open(sources_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def fetch_one(source: dict) -> dict:
    """
    抓取單一來源，回傳格式：
    {
        "source": <原始 source 設定 dict>,
        "entries": [<feedparser entry>, ...],
        "fetch_error": None 或錯誤訊息字串,
        "fetched_at": ISO8601 字串
    }
    抓取失敗時不會拋出例外中斷整個流程——單一來源失敗不該讓其他來源也抓不到，
    錯誤會記錄在 fetch_error 欄位，交給呼叫端決定怎麼處理（記錄/跳過/警告）。
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        resp = requests.get(
            source["url"], timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        return {
            "source": source,
            "entries": feed.entries,
            "fetch_error": None,
            "fetched_at": fetched_at,
        }
    except requests.RequestException as e:
        return {
            "source": source,
            "entries": [],
            "fetch_error": f"連線失敗: {e}",
            "fetched_at": fetched_at,
        }
    except Exception as e:
        return {
            "source": source,
            "entries": [],
            "fetch_error": f"未預期錯誤: {e}",
            "fetched_at": fetched_at,
        }


def fetch_all(sources_path: str) -> list[dict]:
    sources = load_sources(sources_path)
    results = []
    for source in sources:
        print(f"抓取中: {source.get('name')} ...", file=sys.stderr)
        results.append(fetch_one(source))
    return results
