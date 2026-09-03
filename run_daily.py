"""
run_daily.py

主控腳本(Orchestrator)

一鍵完成：
    sources.yaml
    → Fetcher（run_fetcher.py）
    → Stage 1（stage1_classify.py）
    → Score（score.py）
    → Stage 2（stage2_analyze.py）
    → HTML Generator（generate_html.py）

設計原則：
  1. 每個 Phase 仍然是獨立可執行的程式（前面每一支腳本都保留原樣，
     可以單獨重跑），這支主控腳本只負責「依序呼叫 + 收集統計 + 錯誤處理」，
     不重寫任何一個 Phase 的邏輯。之後新增 Phase，只需要在這裡多加一段呼叫，
     不需要改任何雲端排程設定。
  2. 任何一步失敗，立刻停止後續步驟（後面的步驟依賴前面步驟的輸出檔案，
     硬繼續執行只會用舊資料產生誤導性結果），並印出清楚的失敗原因。
  3. Gemini 相關的兩步（Stage 1、Stage 2）遇到暫時性錯誤時重試一次，
     因為之後要跑在 GitHub Actions 這種無人值守環境，網路或 API 短暫不穩
     不該讓一整天的 Daily Brief 直接開天窗。
  4. 執行完畢（不論成功或失敗）都會輸出一份 Run Summary，長期保存在
     logs/run_summary/ 底下（這是 Data Retention 原則裡明確說要保留的
     「必要的 metadata / usage statistics」，跟 raw_articles.json 這種
     暫存資料不同，這份要進 git）。

使用方式：
    python run_daily.py
    python run_daily.py --skip-html   # 除錯用，只跑到 Stage 2
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

PYTHON = sys.executable
RETRY_DELAY_SECONDS = 15


def child_env() -> dict:
    """強制子程序用 UTF-8 輸出，避免 Windows 預設地區編碼(如 cp950)
    跟這支主控腳本讀取子程序輸出時指定的 UTF-8 解碼對不上，
    導致背景執行緒丟出 UnicodeDecodeError（雖然不影響子程序本身成功與否，
    但會印出一堆看起來像是失敗的紅字追蹤訊息，容易誤導判讀）。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_step(name: str, cmd: list[str], retry_on_gemini_step: bool = False) -> tuple[bool, str]:
    """執行一個子步驟，回傳 (是否成功, stderr內容)。"""
    print(f"\n{'='*50}\n▶ {name}\n{'='*50}", file=sys.stderr)

    attempts = 2 if retry_on_gemini_step else 1
    last_stderr = ""

    for attempt in range(1, attempts + 1):
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=child_env()
        )
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

        if result.returncode == 0:
            return True, result.stderr

        last_stderr = result.stderr
        if attempt < attempts:
            print(f"⚠ {name} 第 {attempt} 次執行失敗，{RETRY_DELAY_SECONDS} 秒後重試...", file=sys.stderr)
            time.sleep(RETRY_DELAY_SECONDS)

    return False, last_stderr


def load_json_safe(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def patch_daily_brief_stats(daily_brief_path: str, raw_articles_path: str, stage1_path: str):
    """把 Fetcher 跟 Stage 1 的統計數字，補進 daily_brief.json 裡原本留空的欄位，
    這樣 HTML 報頭顯示的「今天分析N則資訊」才會是完整鏈路的真實數字。"""
    brief = load_json_safe(daily_brief_path)
    raw = load_json_safe(raw_articles_path)
    stage1 = load_json_safe(stage1_path)
    if not brief:
        return
    if raw:
        brief["stats"]["total_articles_collected"] = raw["stats"]["total_fetched"]
    if stage1:
        brief["stats"]["total_after_stage1"] = stage1["stats"]["kept"]
    with open(daily_brief_path, "w", encoding="utf-8") as f:
        json.dump(brief, f, ensure_ascii=False, indent=2)


def build_run_summary(status: str, failed_step: str | None,
                       raw_articles_path: str, stage1_path: str,
                       clusters_path: str, daily_brief_path: str,
                       html_generated: bool) -> dict:
    raw = load_json_safe(raw_articles_path) or {}
    stage1 = load_json_safe(stage1_path) or {}
    clusters = load_json_safe(clusters_path) or {}
    brief = load_json_safe(daily_brief_path) or {}

    source_stats = raw.get("source_stats", {})
    raw_stats = raw.get("stats", {})
    stage1_stats = stage1.get("stats", {})
    stage1_usage = stage1.get("usage", {})
    cluster_stats = clusters.get("stats", {})
    brief_usage = brief.get("usage", {})

    return {
        "date": date.today().isoformat(),
        "run_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "failed_step": failed_step,
        "sources": {
            "attempted": source_stats.get("attempted"),
            "succeeded": source_stats.get("succeeded"),
            "failed": source_stats.get("failed"),
            "failures": source_stats.get("failures", []),
        },
        "pipeline": {
            "raw_articles_fetched": raw_stats.get("total_fetched"),
            "after_recency_and_dedupe": raw_stats.get("final_count"),
            "stage1_kept": stage1_stats.get("kept"),
            "stage1_dropped": stage1_stats.get("dropped_low_relevance"),
            "news_clusters": cluster_stats.get("total_clusters"),
            "stage2_analyzed": cluster_stats.get("selected_for_stage2"),
            "final_brief_items": len(brief.get("items", [])),
        },
        "gemini_usage": {
            "stage1_total_tokens": stage1_usage.get("total_tokens"),
            "stage2_total_tokens": brief_usage.get("stage2_total_tokens"),
        },
        "html_generated": html_generated,
    }


def print_run_summary(summary: dict):
    p = summary["pipeline"]
    s = summary["sources"]
    g = summary["gemini_usage"]
    print(f"""
{'='*50}
Daily Intelligence Run Summary
{summary['date']}
{'='*50}

Sources attempted:       {s['attempted']}
Sources succeeded:       {s['succeeded']}
Sources failed:          {s['failed']}

Raw articles fetched:    {p['raw_articles_fetched']}
After recency+dedupe:    {p['after_recency_and_dedupe']}
Stage 1 kept:            {p['stage1_kept']}
Stage 1 dropped:         {p['stage1_dropped']}
News clusters:           {p['news_clusters']}
Stage 2 analyzed:        {p['stage2_analyzed']}
Final brief items:       {p['final_brief_items']}

Stage 1 tokens:          {g['stage1_total_tokens']}
Stage 2 tokens:          {g['stage2_total_tokens']}

HTML generated:          {'Yes' if summary['html_generated'] else 'No'}

Total status:            {summary['status']}
{f"Failed at step:          {summary['failed_step']}" if summary['failed_step'] else ""}
{'='*50}
""", file=sys.stderr)


def save_run_summary(summary: dict):
    log_dir = Path("logs/run_summary")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{summary['date']}.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"Run Summary 已存至 {log_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="一鍵執行完整 Daily Intelligence pipeline")
    parser.add_argument("--sources", default="config/sources.yaml")
    parser.add_argument("--profile", default="config/profile.yaml")
    parser.add_argument("--raw-articles", default="data/raw_articles.json")
    parser.add_argument("--stage1-results", default="data/stage1_results.json")
    parser.add_argument("--clusters", default="data/clusters_ranked.json")
    parser.add_argument("--daily-brief", default="data/daily_brief.json")
    parser.add_argument("--html-template", default="output/templates/daily_brief.html.jinja")
    parser.add_argument("--html-output-dir", default="docs/daily")
    parser.add_argument("--total-candidates", type=int, default=13)
    parser.add_argument("--top-count", type=int, default=3)
    parser.add_argument("--skip-html", action="store_true", help="除錯用，只跑到 Stage 2 不產生 HTML")
    args = parser.parse_args()

    failed_step = None

    ok, _ = run_step("Phase 3: Fetcher", [
        PYTHON, "pipeline/run_fetcher.py", args.sources, "--output", args.raw_articles,
    ])
    if not ok:
        failed_step = "Fetcher"

    if not failed_step:
        ok, _ = run_step("Phase 4: Stage 1 分類", [
            PYTHON, "pipeline/stage1_classify.py", args.raw_articles, args.profile,
            "--output", args.stage1_results,
        ], retry_on_gemini_step=True)
        if not ok:
            failed_step = "Stage 1"

    if not failed_step:
        ok, _ = run_step("事件分組與評分", [
            PYTHON, "pipeline/score.py", args.raw_articles, args.stage1_results, args.sources,
            "--output", args.clusters, "--total", str(args.total_candidates), "--top", str(args.top_count),
        ])
        if not ok:
            failed_step = "Score"

    if not failed_step:
        ok, _ = run_step("Phase 5: Stage 2 深度分析", [
            PYTHON, "pipeline/stage2_analyze.py", args.clusters, args.raw_articles,
            args.profile, args.sources, "--output", args.daily_brief,
        ], retry_on_gemini_step=True)
        if not ok:
            failed_step = "Stage 2"

    if not failed_step:
        patch_daily_brief_stats(args.daily_brief, args.raw_articles, args.stage1_results)
        # 歸檔進 data/daily_brief/日期.json — 這是長期保存的正式副本，
        # data/daily_brief.json 本身只是工作用的暫存檔，每次執行會被覆蓋。
        brief_data = load_json_safe(args.daily_brief)
        if brief_data:
            archive_dir = Path("data/daily_brief")
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / f"{brief_data['date']}.json"
            with open(archive_path, "w", encoding="utf-8") as f:
                json.dump(brief_data, f, ensure_ascii=False, indent=2)
            print(f"已歸檔至 {archive_path}", file=sys.stderr)

    html_generated = False
    if not failed_step and not args.skip_html:
        ok, _ = run_step("Phase 6: HTML Generator", [
            PYTHON, "output/generate_html.py", args.daily_brief,
            "--template", args.html_template, "--raw-articles", args.raw_articles,
            "--output-dir", args.html_output_dir,
        ])
        if not ok:
            failed_step = "HTML Generator"
        else:
            html_generated = True

    status = "SUCCESS" if not failed_step else "FAILED"

    summary = build_run_summary(status, failed_step, args.raw_articles, args.stage1_results,
                                 args.clusters, args.daily_brief, html_generated)
    print_run_summary(summary)
    save_run_summary(summary)

    sys.exit(0 if status == "SUCCESS" else 1)


if __name__ == "__main__":
    main()
