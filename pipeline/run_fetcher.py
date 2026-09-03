"""
run_fetcher.py

Phase 3 主程式：串起 fetch.py → normalize.py → dedupe_basic.py 三個模組。

流程：
    config/sources.yaml
    → fetch.py（抓取原始 RSS）
    → normalize.py（轉換成 RawArticle schema）
    → dedupe_basic.py（24小時過濾 + 基本去重）
    → data/raw_articles.json

這一階段刻意不接 Gemini（照 Phase 3 的範圍界定），
目的是先確認「來源取得層」本身穩定，再往下走 Stage 1。

使用方式：
    python pipeline/run_fetcher.py config/sources.yaml --output data/raw_articles.json
"""

import argparse
import json
import sys
from pathlib import Path

# 讓這支主程式可以找到同目錄下的 fetch.py / normalize.py / dedupe_basic.py
sys.path.insert(0, str(Path(__file__).parent))

from fetch import fetch_all
from normalize import normalize_all
from dedupe_basic import process


def main():
    parser = argparse.ArgumentParser(description="Phase 3 Fetcher: sources.yaml -> raw_articles.json")
    parser.add_argument("sources_file", help="config/sources.yaml 路徑")
    parser.add_argument("--output", default="data/raw_articles.json", help="輸出檔案路徑")
    parser.add_argument("--recency-hours", type=int, default=24, help="時間過濾的時數門檻，預設 24")
    args = parser.parse_args()

    # 1. Fetch
    fetch_results = fetch_all(args.sources_file)

    # 每個來源的抓取狀況，跑完印出來方便你一眼看出哪個來源今天抓失敗
    print("\n── 各來源抓取狀況 ──", file=sys.stderr)
    for result in fetch_results:
        name = result["source"].get("name")
        if result["fetch_error"]:
            print(f"  ✗ {name}: {result['fetch_error']}", file=sys.stderr)
        else:
            print(f"  ✓ {name}: {len(result['entries'])} 則", file=sys.stderr)

    # 2. Normalize
    articles = normalize_all(fetch_results)

    # 3. 24小時過濾 + 去重
    result = process(articles, recency_hours=args.recency_hours)

    # 4. 寫出 raw_articles.json
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_data = {
        "generated_at": fetch_results[0]["fetched_at"] if fetch_results else None,
        "stats": result["stats"],
        "source_stats": {
            "attempted": len(fetch_results),
            "succeeded": sum(1 for r in fetch_results if not r["fetch_error"]),
            "failed": sum(1 for r in fetch_results if r["fetch_error"]),
            "failures": [
                {"name": r["source"].get("name"), "error": r["fetch_error"]}
                for r in fetch_results if r["fetch_error"]
            ],
        },
        "articles": result["articles"],
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    stats = result["stats"]
    print("\n── 統計摘要 ──", file=sys.stderr)
    print(f"  總共抓到: {stats['total_fetched']} 則", file=sys.stderr)
    print(f"  被時間過濾: {stats['dropped_by_recency_filter']} 則（超過 {args.recency_hours} 小時）", file=sys.stderr)
    print(f"  被去重過濾: {stats['dropped_by_dedupe']} 則", file=sys.stderr)
    print(f"  最終保留: {stats['final_count']} 則", file=sys.stderr)
    print(f"\n已寫入 {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
