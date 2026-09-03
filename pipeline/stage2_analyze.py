"""
stage2_analyze.py

Phase 5: Stage 2 深度分析

只處理 score.py 篩選出的候選（預設13則），逐篇呼叫 Gemini，
產生完整的 DailyBriefItem：What Happened / Why It Matters / Why It Matters To Me /
Potential Impact / What To Watch Next / Keywords。

這裡是逐篇呼叫（不是像 Stage 1 那樣一次批次），原因：
1. 免費層級的 RPM（每分鐘請求數）有限制，逐篇之間需要節流（time.sleep），
   不能用 for 迴圈無間隔連續發送，否則會觸發 429 錯誤。
2. 深度分析需要看完整內容，每篇的 prompt 相對長，批次塞在一起容易讓
   Gemini 在長文本中「注意力分散」，逐篇處理品質更穩定。

跑完 13 則深度分析後，另外用一次輕量呼叫，把頭版精選 3 則的一句話摘要
合成一段「今日三十秒」，作為 Daily Brief 的開場摘要。

最終輸出符合 Phase 2 定義的 DailyBrief schema，HTML Generator 與 LINE Generator
之後都只讀這一份檔案，不會再呼叫 Gemini。

使用方式：
    python pipeline/stage2_analyze.py data/clusters_ranked.json data/raw_articles.json \
        config/profile.yaml config/sources.yaml --output data/daily_brief.json
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv
from google import genai
from google.genai import types

RPM_THROTTLE_SECONDS = 5  # 每次深度分析呼叫之間的等待秒數，避免觸發免費層級 RPM 限制


DEEP_ANALYSIS_PROMPT = """你是一個新聞情報分析助手。以下是一篇文章的完整內容，以及使用者的背景。
請針對這篇文章，為使用者產生結構化分析。

# 使用者背景
角色：{role}
關注領域：
{focus_areas}

# 文章內容
標題：{title}
來源：{source_name}
發布時間：{published_at}
內文：{content}

# 你的任務
產生以下欄位，全部使用繁體中文，語氣直接、有判斷、不要空泛：

1. headline_zh：把上方標題翻譯成繁體中文。這個欄位絕對不能是英文原文，
   也不能留空。專有名詞、產品名稱、公司名稱可以保留英文，但必須融入完整的
   中文句子中，不是「翻譯一半、英文一半」的混雜句子，更不能整句照抄原文標題。
   範例：
   - 原文「Nvidia to Acquire Hugging Face」→ 正確：「輝達（Nvidia）宣布將併購Hugging Face」
   - 原文「Claude for Commerce Agents」→ 正確：「Anthropic推出「Claude for Commerce Agents」商務代理工具」
   - 錯誤示範（絕對不要這樣做）：直接輸出「Nvidia to Acquire Hugging Face」（完全沒翻譯）
   如果原文標題本身已經是繁體中文，直接使用或做小幅潤飾即可。
2. one_sentence_summary：一句話講完這則新聞在講什麼(30字內)
3. what_happened：具體發生了什麼事(2-3句話，只陳述事實，不要加入你的評論)
4. why_it_matters：這件事為什麼重要——對這個領域/產業整體而言(2-3句話)
5. why_it_matters_to_me：針對上方使用者背景，這件事具體跟他的哪個關注領域有關、
   可能帶來什麼影響或值得思考的點(2-3句話)。
   如果老實說關聯性不強，直接寫「與你的關注領域關聯度較低，列入僅供參考」，不要硬掰關聯。
6. potential_impact：如果這個趨勢/事件持續發展，可能造成什麼影響(1-2句話)
7. what_to_watch_next：接下來應該觀察什麼指標或後續發展(1句話)
8. keywords：3-5個關鍵字

# 重要原則
- 只根據上方提供的文章內容進行分析，不要編造文章沒提到的細節或數據
- why_it_matters_to_me 這欄是整份分析裡最重要的部分，要真的做到「針對這個人」，
  不是套版式的「這對所有人都很重要」

# 輸出格式(只輸出JSON)
{{
  "headline_zh": "...",
  "one_sentence_summary": "...",
  "what_happened": "...",
  "why_it_matters": "...",
  "why_it_matters_to_me": "...",
  "potential_impact": "...",
  "what_to_watch_next": "...",
  "keywords": ["...", "..."]
}}
"""


TODAY_SUMMARY_PROMPT = """以下是今天三則頭版精選新聞的一句話摘要，請用約100-150字，
把這三件事整合成一段流暢的「今日三十秒」開場摘要，用繁體中文，語氣直接不空泛。
只輸出這段摘要文字，不要有其他說明或標題。

{summaries}
"""


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def call_gemini_json(client, prompt: str, model: str) -> tuple[dict, dict]:
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    usage = {}
    if response.usage_metadata:
        usage = {
            "prompt_tokens": response.usage_metadata.prompt_token_count,
            "output_tokens": response.usage_metadata.candidates_token_count,
            "total_tokens": response.usage_metadata.total_token_count,
        }
    return json.loads(response.text), usage


def call_gemini_text(client, prompt: str, model: str) -> tuple[str, dict]:
    response = client.models.generate_content(model=model, contents=prompt)
    usage = {}
    if response.usage_metadata:
        usage = {
            "prompt_tokens": response.usage_metadata.prompt_token_count,
            "output_tokens": response.usage_metadata.candidates_token_count,
            "total_tokens": response.usage_metadata.total_token_count,
        }
    return response.text.strip(), usage


def sum_usage(usage_list: list[dict]) -> dict:
    total = {"prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for u in usage_list:
        for k in total:
            total[k] += u.get(k, 0)
    return total


def analyze_cluster(client, cluster: dict, raw_by_id: dict, source_credibility_names: dict,
                     profile: dict, model: str) -> tuple[dict, dict]:
    primary = raw_by_id[cluster["primary_article_id"]]
    content_text = primary.get("content") or primary.get("description") or ""
    focus_areas_text = "\n".join(f"- {a}" for a in profile.get("focus_areas", []))

    prompt = DEEP_ANALYSIS_PROMPT.format(
        role=profile.get("role", ""),
        focus_areas=focus_areas_text,
        title=primary["title"],
        source_name=primary["source_name"],
        published_at=primary.get("published_at", ""),
        content=content_text,
    )

    parsed, usage = call_gemini_json(client, prompt, model)

    headline_zh = parsed.get("headline_zh") or primary["title"]
    # 簡單啟發式檢查：如果翻譯結果裡英文字母佔比過高，很可能 AI 沒有真的翻譯，
    # 印出警告方便你在燒機測試時追蹤這個問題還有沒有反覆發生。
    ascii_ratio = sum(1 for c in headline_zh if c.isascii() and c.isalpha()) / max(len(headline_zh), 1)
    if ascii_ratio > 0.75:
        print(f"    ⚠ 標題翻譯可能沒有生效（英文字母佔比 {ascii_ratio:.0%}）：{headline_zh[:40]}", file=sys.stderr)

    sources = []
    seen_source_names = set()
    for aid in cluster["article_ids"]:
        a = raw_by_id.get(aid)
        if a and a["source_name"] not in seen_source_names:
            sources.append({"name": a["source_name"], "url": a["url"], "published_at": a.get("published_at")})
            seen_source_names.add(a["source_name"])

    item = {
        "item_id": cluster["cluster_id"],
        "cluster_id": cluster["cluster_id"],
        "category": cluster["category"],
        "headline": headline_zh,
        "one_sentence_summary": parsed.get("one_sentence_summary"),
        "what_happened": parsed.get("what_happened"),
        "why_it_matters": parsed.get("why_it_matters"),
        "why_it_matters_to_me": parsed.get("why_it_matters_to_me"),
        "potential_impact": parsed.get("potential_impact"),
        "what_to_watch_next": parsed.get("what_to_watch_next"),
        "importance_score": cluster["global_importance"],
        "personal_relevance_score": cluster["personal_relevance_score"],
        "keywords": parsed.get("keywords", []),
        "sources": sources,
        "is_top3": cluster["is_top3"],
    }
    return item, usage


def main():
    parser = argparse.ArgumentParser(description="Phase 5 Stage 2: clusters_ranked.json -> daily_brief.json")
    parser.add_argument("clusters_file", help="data/clusters_ranked.json 路徑")
    parser.add_argument("raw_articles_file", help="data/raw_articles.json 路徑")
    parser.add_argument("profile_file", help="config/profile.yaml 路徑")
    parser.add_argument("sources_file", help="config/sources.yaml 路徑（目前保留參數位置，供未來擴充用）")
    parser.add_argument("--output", default="data/daily_brief.json")
    parser.add_argument("--model", default="gemini-3.5-flash-lite", help="使用的 Gemini 模型")
    parser.add_argument("--issue-number", type=int, default=1)
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("錯誤：找不到 GEMINI_API_KEY 環境變數。", file=sys.stderr)
        sys.exit(1)

    clusters_data = load_json(args.clusters_file)
    clusters = clusters_data["clusters"]

    raw_data = load_json(args.raw_articles_file)
    raw_by_id = {a["id"]: a for a in raw_data["articles"]}

    profile = load_yaml(args.profile_file)
    sources_config = load_yaml(args.sources_file)
    source_credibility_names = {s["id"]: s.get("credibility") for s in sources_config.get("sources", [])}

    client = genai.Client(api_key=api_key)

    items = []
    usage_list = []

    print(f"開始 Stage 2 深度分析，共 {len(clusters)} 則，每則間隔 {RPM_THROTTLE_SECONDS} 秒以避免觸發速率限制", file=sys.stderr)

    for i, cluster in enumerate(clusters):
        primary_title = raw_by_id.get(cluster["primary_article_id"], {}).get("title", "")[:40]
        print(f"  [{i+1}/{len(clusters)}] 分析中: {primary_title} ...", file=sys.stderr)
        try:
            item, usage = analyze_cluster(client, cluster, raw_by_id, source_credibility_names, profile, args.model)
            items.append(item)
            usage_list.append(usage)
        except Exception as e:
            print(f"    ✗ 這則分析失敗，跳過：{e}", file=sys.stderr)

        if i < len(clusters) - 1:
            time.sleep(RPM_THROTTLE_SECONDS)

    # 產生「今日三十秒」：用頭版精選 3 則的一句話摘要合成
    top3_items = [it for it in items if it["is_top3"]]
    summaries_text = "\n".join(f"- {it['one_sentence_summary']}" for it in top3_items)
    print("產生今日三十秒摘要中...", file=sys.stderr)
    today_summary_prompt = TODAY_SUMMARY_PROMPT.format(summaries=summaries_text)
    today_in_30_seconds, summary_usage = call_gemini_text(client, today_summary_prompt, args.model)
    usage_list.append(summary_usage)

    total_usage = sum_usage(usage_list)

    daily_brief = {
        "date": date.today().isoformat(),
        "issue_number": args.issue_number,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "today_in_30_seconds": today_in_30_seconds,
        "top3_item_ids": [it["item_id"] for it in top3_items],
        "items": items,
        "stats": {
            "total_articles_collected": None,  # 由主控腳本後續補上（來自 raw_articles 統計）
            "total_after_stage1": None,
            "total_selected": len(items),
            "estimated_reading_minutes": max(3, round(len(items) * 0.5)),
        },
        "usage": {
            "stage2_tokens_in": total_usage["prompt_tokens"],
            "stage2_tokens_out": total_usage["output_tokens"],
            "stage2_total_tokens": total_usage["total_tokens"],
        },
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(daily_brief, f, ensure_ascii=False, indent=2)

    print("\n── Stage 2 完成 ──", file=sys.stderr)
    print(f"  成功分析: {len(items)} / {len(clusters)}", file=sys.stderr)
    print(f"  Token 用量: 輸入 {total_usage['prompt_tokens']}, 輸出 {total_usage['output_tokens']}, 總計 {total_usage['total_tokens']}", file=sys.stderr)
    print(f"\n已寫入 {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
