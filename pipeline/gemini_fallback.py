"""
gemini_fallback.py

Gemini 呼叫的共用防護：遇到「暫時性錯誤」(503 塞車 / 429 限流 / 500) 時
先在同一模型上等一下重試，仍失敗就自動切換到備援模型。

2026-11-10 起加入：gemini-3.5-flash-lite 持續回 503 超過 3 小時，
原本「15 秒後整步重跑一次」的機制撐不住，整天沒發報。

備援模型清單（2026-11-10 依 Firebase AI Logic 官方模型表確認皆為 Stable、無下架日期）：
  - gemini-3.8-flash：較新、較強，免費額度較少，但 Stage 1 只呼叫一次、Stage 2 約 14 次，足夠
  - gemini-3.1-flash-lite：舊一代 lite，作為最後防線
模型下架或改名時只要改 FALLBACK_MODELS 這一行。
"""

import sys
import time

from google.genai import errors

FALLBACK_MODELS = ["gemini-3.8-flash", "gemini-3.1-flash-lite"]
RETRIES_PER_MODEL = 2           # 同一模型最多試幾次
BACKOFF_SECONDS = [20, 40]      # 第 1、2 次失敗後各等多久
TRANSIENT_CODES = {429, 500, 503, 504}


def _is_transient(e: Exception) -> bool:
    return isinstance(e, errors.APIError) and getattr(e, "code", None) in TRANSIENT_CODES


def _is_model_missing(e: Exception) -> bool:
    return isinstance(e, errors.APIError) and getattr(e, "code", None) == 404


def generate_with_fallback(client, model: str, **kwargs):
    """
    跟 client.models.generate_content 用法相同，多回傳實際使用的模型。
    回傳 (response, model_used)
    """
    candidates = [model] + [m for m in FALLBACK_MODELS if m != model]
    last_error = None

    for m in candidates:
        for attempt in range(1, RETRIES_PER_MODEL + 1):
            try:
                response = client.models.generate_content(model=m, **kwargs)
                if m != model:
                    print(f"  ↪ 已改用備援模型 {m} 完成這次呼叫", file=sys.stderr)
                return response, m
            except Exception as e:
                last_error = e
                if _is_model_missing(e):
                    print(f"  ⚠ 模型 {m} 不存在或已下架(404)，改試下一個", file=sys.stderr)
                    break
                if not _is_transient(e):
                    raise
                code = getattr(e, "code", "?")
                if attempt < RETRIES_PER_MODEL:
                    wait = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                    print(f"  ⚠ {m} 回 {code}（第 {attempt} 次），{wait} 秒後重試", file=sys.stderr)
                    time.sleep(wait)
                else:
                    print(f"  ⚠ {m} 回 {code}，已重試 {RETRIES_PER_MODEL} 次，切換下一個模型", file=sys.stderr)

    raise last_error
