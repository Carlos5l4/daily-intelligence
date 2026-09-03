"""
stage1_classify.py

Phase 4: Stage 1 批次分類

讀取 raw_articles.json，用單一批次請求呼叫 Gemini，取得每篇文章的：
  - category（分類）
  - cluster_id（事件分組，供後續去重使用）
  - impact_score / novelty_score / personal_relevance_score（0-100 原始分數）
  - personal_relevance_reason（一句話說明，供你檢查判斷準不準）

Global Importance 不在這裡計算——那是程式碼另外套公式的事（見 score.py），
Gemini 這一關只負責給「原始素材」，不負責幫你做最終排序決策。
「keep」決策也不是問 Gemini，是這支程式依照 personal_relevance_score 門檻
自己算出來的，這樣門檻要調整時只改一個數字，不用重新設計 prompt。

使用方式：
    python pipeline/stage1_classify.py data/raw_articles.json config/profile.yaml \
        --output data/stage1_results.json --model gemini-2.5-flash-lite
"""

import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from google import genai
from google.genai import types

PERSONAL_RELEVANCE_KEEP_THRESHOLD = 50


PROMPT_TEMPLATE = """你是一個新聞情報分析助手，任務是幫使用者從大量文章中做初步分類與評分。

# 使用者背景(Interest Profile)
角色：{role}
關注領域：
{focus_areas}

# 你的任務
以下是今天收集到的 {article_count} 則文章(標題+摘要)。請針對每一篇進行：

1. 分類到以下其中一個類別：
   ai_technology / restaurant_retail / hr_organization / markets_economy / major_news

2. 事件分組(cluster_id)：
   如果多篇文章報導的是「同一件事」(例如同一場記者會、同一則公司公告被不同媒體轉述)，
   給予相同的 cluster_id(格式：c001, c002...)。
   判斷同一事件的標準是「核心事實相同」，不是「主題相似」——
   例如兩篇都在談「AI趨勢」但沒有共同的具體事件，不算同一 cluster。
   每個不重複的事件都給一個新的 cluster_id。

3. Impact Score(0-100)：
   這則新聞在其所屬領域內，客觀上的影響力/重要性有多大。
   評分不考慮「使用者是否在乎」，只考慮「這件事本身的份量」。
   參考標準：
   - 90-100：足以改變產業格局或政策方向的重大事件
   - 60-89：值得該領域從業者關注的重要進展
   - 30-59：一般性更新或漸進式變化
   - 0-29：例行公告、瑣碎更新

4. Novelty Score(0-100)：
   這則資訊相對於「已經普遍被知道的趨勢」有多新/多意外。
   已經被大量報導、老生常談的話題給低分，即使主題重要。

5. Personal Relevance Score(0-100)：
   根據上方的使用者 Interest Profile，這則新聞跟使用者的關注領域有多相關。
   請注意：
   - 這個分數跟 Impact Score 要獨立判斷，不要因為一則新聞很熱門就給高分
   - 如果真的關聯性很低，誠實給低分(0-20都可以)，不要為了讓每篇都「看起來有用」而勉強拉高
   - 給分時用一句話(personal_relevance_reason)簡短說明為什麼相關或不相關

# 輸出格式
請只輸出 JSON，不要有其他文字或 markdown 標記，格式如下：

{{
  "articles": [
    {{
      "id": "文章原始id",
      "category": "ai_technology",
      "cluster_id": "c001",
      "impact_score": 75,
      "novelty_score": 40,
      "personal_relevance_score": 85,
      "personal_relevance_reason": "..."
    }}
  ]
}}

# 待分類文章
{articles_json}
"""


def build_prompt(articles: list[dict], profile: dict) -> str:
    focus_areas_text = "\n".join(f"- {area}" for area in profile.get("focus_areas", []))

    slim_articles = [
        {"id": a["id"], "title": a["title"], "description": a["description"]}
        for a in articles
    ]

    return PROMPT_TEMPLATE.format(
        role=profile.get("role", ""),
        focus_areas=focus_areas_text,
        article_count=len(articles),
        articles_json=json.dumps(slim_articles, ensure_ascii=False, indent=2),
    )


def call_gemini(prompt: str, model: str, api_key: str) -> tuple[dict, dict]:
    """回傳 (解析後的 JSON dict, usage 統計 dict)"""
    client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    usage = {}
    if response.usage_metadata:
        usage = {
            "prompt_tokens": response.usage_metadata.prompt_token_count,
            "output_tokens": response.usage_metadata.candidates_token_count,
            "total_tokens": response.usage_metadata.total_token_count,
        }

    try:
        parsed = json.loads(response.text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Gemini 回傳的內容不是合法 JSON，無法解析。原始回應前500字：\n{response.text[:500]}"
        ) from e

    return parsed, usage


def merge_with_raw_articles(gemini_results: list[dict], raw_articles: list[dict]) -> list[dict]:
    """
    把 Gemini 的分類結果跟原始文章對齊，並依 personal_relevance_score 門檻
    計算出最終的 keep / drop_reason。
    """
    raw_by_id = {a["id"]: a for a in raw_articles}
    stage1_results = []

    for item in gemini_results:
        article_id = item.get("id")
        if article_id not in raw_by_id:
            print(f"  ⚠ 警告：Gemini 回傳了不存在於 raw_articles 的 id: {article_id}", file=sys.stderr)
            continue

        personal_relevance = item.get("personal_relevance_score", 0)
        keep = personal_relevance >= PERSONAL_RELEVANCE_KEEP_THRESHOLD

        stage1_results.append({
            "article_id": article_id,
            "category": item.get("category"),
            "cluster_id": item.get("cluster_id"),
            "impact_score": item.get("impact_score"),
            "novelty_score": item.get("novelty_score"),
            "personal_relevance_score": personal_relevance,
            "personal_relevance_reason": item.get("personal_relevance_reason"),
            "keep": keep,
            "drop_reason": None if keep else f"Personal Relevance ({personal_relevance}) 低於門檻 ({PERSONAL_RELEVANCE_KEEP_THRESHOLD})",
        })

    classified_ids = {r["article_id"] for r in stage1_results}
    missing_ids = set(raw_by_id.keys()) - classified_ids
    if missing_ids:
        print(f"  ⚠ 警告：有 {len(missing_ids)} 則文章 Gemini 沒有回傳分類結果，將標記為 drop", file=sys.stderr)
        for missing_id in missing_ids:
            stage1_results.append({
                "article_id": missing_id,
                "category": raw_by_id[missing_id].get("category"),
                "cluster_id": None,
                "impact_score": None,
                "novelty_score": None,
                "personal_relevance_score": None,
                "personal_relevance_reason": None,
                "keep": False,
                "drop_reason": "Gemini 未回傳此文章的分類結果",
            })

    return stage1_results


def main():
    parser = argparse.ArgumentParser(description="Phase 4 Stage 1: raw_articles.json -> stage1_results.json")
    parser.add_argument("raw_articles_file", help="data/raw_articles.json 路徑")
    parser.add_argument("profile_file", help="config/profile.yaml 路徑")
    parser.add_argument("--output", default="data/stage1_results.json", help="輸出檔案路徑")
    parser.add_argument("--model", default="gemini-3.5-flash-lite", help="使用的 Gemini 模型")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("錯誤：找不到 GEMINI_API_KEY 環境變數，請確認 .env 檔案存在且內容正確。", file=sys.stderr)
        sys.exit(1)

    with open(args.raw_articles_file, "r", encoding="utf-8") as f:
        raw_data = json.load(f)
    raw_articles = raw_data["articles"]

    with open(args.profile_file, "r", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    print(f"準備分類 {len(raw_articles)} 則文章，使用模型：{args.model}", file=sys.stderr)

    prompt = build_prompt(raw_articles, profile)

    print("呼叫 Gemini API 中...", file=sys.stderr)
    gemini_response, usage = call_gemini(prompt, args.model, api_key)

    gemini_results = gemini_response.get("articles", [])
    print(f"Gemini 回傳 {len(gemini_results)} 則分類結果", file=sys.stderr)

    stage1_results = merge_with_raw_articles(gemini_results, raw_articles)

    kept_count = sum(1 for r in stage1_results if r["keep"])
    dropped_count = len(stage1_results) - kept_count

    output_data = {
        "model": args.model,
        "usage": usage,
        "stats": {
            "total_classified": len(stage1_results),
            "kept": kept_count,
            "dropped_low_relevance": dropped_count,
        },
        "results": stage1_results,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print("\n── Stage 1 統計摘要 ──", file=sys.stderr)
    print(f"  分類總數: {len(stage1_results)}", file=sys.stderr)
    print(f"  保留(進入候選池): {kept_count}", file=sys.stderr)
    print(f"  剔除(相關性過低): {dropped_count}", file=sys.stderr)
    print(f"  Token 用量: 輸入 {usage.get('prompt_tokens')}, 輸出 {usage.get('output_tokens')}, 總計 {usage.get('total_tokens')}", file=sys.stderr)
    print(f"\n已寫入 {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
