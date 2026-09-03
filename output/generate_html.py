"""
generate_html.py

Phase 6: HTML Generator

只讀 daily_brief.json，不會呼叫 Gemini——這是設計上的硬性原則，
確保 Presentation 層跟 AI 分析層完全解耦，HTML 產生失敗不會浪費任何 API 額度，
也代表這支程式可以無限次重跑、重新調整版面，不會有任何額外成本。

使用方式：
    python output/generate_html.py data/daily_brief.json \
        --template output/templates/daily_brief.html.jinja \
        --raw-articles data/raw_articles.json \
        --output-dir docs/daily
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

CATEGORY_ORDER = ["hr_organization", "restaurant_retail", "ai_technology", "markets_economy", "major_news"]

CATEGORY_NAMES = {
    "ai_technology": "人工智慧與科技",
    "restaurant_retail": "餐飲與零售",
    "hr_organization": "人力資源與組織管理",
    "markets_economy": "市場與總體經濟",
    "major_news": "台灣與全球重大新聞",
}

SOURCE_TRANSLATIONS = {
    "OpenAI News": "OpenAI官方新聞",
    "Google AI Blog": "Google人工智慧官方部落格",
    "Hacker News (Front Page)": "科技社群論壇",
    "Hacker News": "科技社群論壇",
    "TechCrunch": "美國科技媒體",
    "The Verge": "美國科技媒體",
    "QSR Magazine": "美國連鎖餐飲產業雜誌",
    "Nation's Restaurant News": "美國餐飲產業媒體",
    "MarketWatch": "美國財經媒體",
    "Federal Reserve Press Releases": "美國聯準會新聞稿",
    "Delivery Hero": "德國外送平台集團",
}

WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def translate_source(name: str) -> str:
    if name in SOURCE_TRANSLATIONS:
        return f"{name}({SOURCE_TRANSLATIONS[name]})"
    return name


def format_date_display(date_str: str) -> str:
    dt = datetime.fromisoformat(date_str)
    return f"{dt.year}年{dt.month}月{dt.day}日 星期{WEEKDAY_ZH[dt.weekday()]}"


def main():
    parser = argparse.ArgumentParser(description="Phase 6: daily_brief.json -> Daily Brief HTML")
    parser.add_argument("daily_brief_file", help="data/daily_brief.json 路徑")
    parser.add_argument("--template", default="output/templates/daily_brief.html.jinja")
    parser.add_argument("--raw-articles", default=None, help="選填，用來在報頭顯示「今天分析N則資訊」")
    parser.add_argument("--output-dir", default="docs/daily", help="輸出根目錄，會在底下建立以日期命名的資料夾")
    args = parser.parse_args()

    with open(args.daily_brief_file, "r", encoding="utf-8") as f:
        brief = json.load(f)

    total_collected = None
    if args.raw_articles:
        try:
            with open(args.raw_articles, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            # 用「過濾後實際送進 Stage 1 分析」的數字，不是抓取階段的原始總數——
            # 原始總數包含大量被 24 小時過濾掉、從未真正被分析過的舊文章，
            # 顯示那個數字會誤導使用者以為分析量比實際多很多。
            total_collected = raw_data.get("stats", {}).get("final_count")
        except FileNotFoundError:
            pass

    def dedupe_source_names(sources: list[dict]) -> list[str]:
        """同一事件常常被同一個來源（例如同一組 Google News 關鍵字搜尋）
        重複收錄好幾篇，來源名稱完全相同時只顯示一次，避免版面出現
        「Google News - 台股、Google News - 台股、Google News - 台股...」這種無意義的重複。"""
        seen = []
        for s in sources:
            translated = translate_source(s["name"])
            if translated not in seen:
                seen.append(translated)
        return seen

    items = brief["items"]
    for item in items:
        item["unique_source_names"] = dedupe_source_names(item.get("sources", []))

    top3_items = [it for it in items if it.get("is_top3")]
    top3_ids = {it["item_id"] for it in top3_items}

    categories_with_items = []
    for cat_key in CATEGORY_ORDER:
        cat_items = [it for it in items if it["category"] == cat_key and it["item_id"] not in top3_ids]
        categories_with_items.append((cat_key, cat_items))

    template_path = Path(args.template)
    env = Environment(loader=FileSystemLoader(str(template_path.parent)))
    env.globals["translate_source"] = translate_source
    template = env.get_template(template_path.name)

    html = template.render(
        date=brief["date"],
        date_display=format_date_display(brief["date"]),
        issue_number=brief["issue_number"],
        today_in_30_seconds=brief["today_in_30_seconds"],
        top3_items=top3_items,
        categories_with_items=categories_with_items,
        category_names=CATEGORY_NAMES,
        items=items,
        total_collected=total_collected,
        reading_minutes=brief["stats"].get("estimated_reading_minutes", max(3, round(len(items) * 0.5))),
    )

    output_dir = Path(args.output_dir) / brief["date"]
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "index.html"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    # 額外在 docs/ 根目錄寫一個「永遠指向最新一期」的固定網址，
    # 用 meta refresh 轉址到當天的日期資料夾。這個網址本身不會變，
    # 之後 LINE 通知直接連到這個固定網址就好，不用每天更新推播內容裡的連結。
    docs_root = Path(args.output_dir).parent
    latest_redirect_path = docs_root / "index.html"
    relative_target = f"daily/{brief['date']}/index.html"
    redirect_html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<meta http-equiv="refresh" content="0; url={relative_target}">'
        f'<title>每日情報</title></head>'
        f'<body>正在前往最新一期 <a href="{relative_target}">每日情報</a> …</body></html>'
    )
    with open(latest_redirect_path, "w", encoding="utf-8") as f:
        f.write(redirect_html)

    print(f"已產生 Daily Brief HTML：{output_path}")
    print(f"已更新固定網址轉址：{latest_redirect_path} → {relative_target}")
    print(f"  頭版精選：{len(top3_items)} 則")
    print(f"  分類報導：{len(items) - len(top3_items)} 則")
    for cat_key, cat_items in categories_with_items:
        print(f"    {CATEGORY_NAMES[cat_key]}：{len(cat_items)} 則")


if __name__ == "__main__":
    main()
