"""
line_generator.py

Phase 6: LINE Generator

只讀 daily_brief.json，不呼叫 Gemini——跟 HTML Generator 一樣的設計原則，
Presentation 層完全跟 AI 分析層解耦。

用 LINE Messaging API 的 Broadcast 端點（不是 Push），原因：
    LINE Notify 已於 2025 年 3 月底終止服務，官方建議改用 Messaging API。
    Push API 需要事先知道「你的 LINE User ID」，通常得另外架一個 webhook
    才能取得；Broadcast API 直接發送給「這個官方帳號的所有好友」，
    對單人使用的場景（你是這個帳號唯一的好友）效果完全相同，
    但省掉抓 User ID 這道手續，設定起來簡單很多。
    免費額度每月 200 則訊息，一天一則遠低於這個上限。

使用方式：
    python notify/line_generator.py data/daily_brief.json --site-url https://carlos5l4.github.io/daily-intelligence/
    python notify/line_generator.py data/daily_brief.json --site-url ... --dry-run   # 只印出訊息內容，不真的發送
"""

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"

CATEGORY_NAMES = {
    "ai_technology": "AI科技",
    "restaurant_retail": "餐飲零售",
    "hr_organization": "HR組織",
    "markets_economy": "市場經濟",
    "major_news": "重大新聞",
}

WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def format_date_display(date_str: str) -> str:
    from datetime import datetime
    dt = datetime.fromisoformat(date_str)
    return f"{dt.year}年{dt.month}月{dt.day}日 星期{WEEKDAY_ZH[dt.weekday()]}"


def build_message(brief: dict, site_url: str) -> str:
    top3 = [it for it in brief["items"] if it.get("is_top3")]
    stats = brief.get("stats", {})

    top3_lines = []
    for i, item in enumerate(top3, 1):
        cat = CATEGORY_NAMES.get(item["category"], item["category"])
        top3_lines.append(f"{i}. [{cat}] {item['headline']}")

    total_selected = stats.get("total_selected", len(brief["items"]))
    reading_minutes = stats.get("estimated_reading_minutes", "?")

    lines = [
        f"每日情報｜{format_date_display(brief['date'])}",
        "",
        f"今日篩出 {total_selected} 則值得你注意",
        "",
        "頭版精選",
        *top3_lines,
        "",
        f"預估閱讀時間：約 {reading_minutes} 分鐘",
        "",
        "查看完整內容：",
        site_url,
    ]
    return "\n".join(lines)


def send_broadcast(message: str, channel_access_token: str) -> requests.Response:
    headers = {
        "Authorization": f"Bearer {channel_access_token}",
        "Content-Type": "application/json",
    }
    payload = {"messages": [{"type": "text", "text": message}]}
    return requests.post(LINE_BROADCAST_URL, headers=headers, json=payload, timeout=15)


def main():
    parser = argparse.ArgumentParser(description="Phase 6: daily_brief.json -> LINE 推播通知")
    parser.add_argument("daily_brief_file", help="data/daily_brief.json 路徑")
    parser.add_argument("--site-url", required=True, help="GitHub Pages 固定網址（docs/index.html 那個轉址網址）")
    parser.add_argument("--dry-run", action="store_true", help="只印出訊息內容，不真的發送，用於設定階段測試")
    args = parser.parse_args()

    with open(args.daily_brief_file, "r", encoding="utf-8") as f:
        brief = json.load(f)

    message = build_message(brief, args.site_url)

    if args.dry_run:
        print("── Dry Run：以下是即將發送的訊息內容 ──", file=sys.stderr)
        print(message, file=sys.stderr)
        print("\n（--dry-run 模式，實際上沒有發送）", file=sys.stderr)
        return

    load_dotenv()
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        print("錯誤：找不到 LINE_CHANNEL_ACCESS_TOKEN 環境變數，請確認 .env 檔案存在且內容正確。", file=sys.stderr)
        sys.exit(1)

    resp = send_broadcast(message, token)

    if resp.status_code == 200:
        print("LINE 通知發送成功", file=sys.stderr)
    else:
        print(f"LINE 通知發送失敗：HTTP {resp.status_code}\n{resp.text}", file=sys.stderr)
        # 通知失敗不該讓整條 pipeline 判定為 FAILED——Daily Brief 本體
        # （HTML）已經產生成功了，通知只是「最後一哩路」的加值服務，
        # 失敗了應該要能察覺（印出錯誤、結束碼非0），但不代表當天的
        # 情報系統本身沒有運作。
        sys.exit(1)


if __name__ == "__main__":
    main()
