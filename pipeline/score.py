"""
score.py

Stage 1 與 Stage 2 之間的橋接階段。

這支程式不呼叫 Gemini，純粹是程式邏輯運算，負責三件事：

1. 依 cluster_id 把 Stage 1 判斷為「同一事件」的文章分組成 NewsCluster
2. 計算 Global Importance（呼應原始需求書的公式，但套用實際可取得的資料）：
       Global Importance = Impact×40% + Novelty×20% + Source Credibility×20% + Trend Strength×20%
   Impact / Novelty 取同一 cluster 內文章的平均值（多篇報導同一事件時，用平均而非只取一篇）。
   Source Credibility 讀 sources.yaml 設定，不是 AI 猜的。
   Trend Strength 是「同一事件有幾個不同來源報導」換算出來的分數——
   來源越多，代表這件事越可能是真正的趨勢，而非單一媒體的孤立報導。
3. 依 Personal Relevance 篩選候選（Stage 1 keep=true 已經做過一次），
   再用 Global Importance 排序，選出前 N 則進入 Stage 2 深度分析，
   其中前 3 則額外標記為「頭版精選」。

使用方式：
    python pipeline/score.py data/raw_articles.json data/stage1_results.json config/sources.yaml \
        --output data/clusters_ranked.json --total 13 --top 3
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

CREDIBILITY_SCORE = {"high": 100, "medium": 60, "low": 20}


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_sources_credibility(sources_path: str) -> dict[str, str]:
    import yaml
    with open(sources_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {s["id"]: s.get("credibility", "medium") for s in data.get("sources", [])}


def compute_trend_strength(source_count: int) -> float:
    """1個來源=0分，2個來源=50分，3個以上=100分（封頂）。
    來源數量越多，越可能是真正被多方報導的重要事件。"""
    return min(100.0, (source_count - 1) * 50.0)


def build_clusters(stage1_kept: list[dict], raw_by_id: dict, source_credibility: dict) -> list[dict]:
    """
    把 keep=true 的 Stage 1 結果依 cluster_id 分組。
    沒有 cluster_id 的文章（Gemini 沒給，理論上不該發生，但防呆處理）
    各自獨立成一個 cluster，用自己的 article_id 當 cluster_id。
    """
    groups: dict[str, list[dict]] = defaultdict(list)

    for result in stage1_kept:
        cid = result.get("cluster_id") or f"solo_{result['article_id']}"
        groups[cid].append(result)

    clusters = []
    for cluster_id, members in groups.items():
        article_ids = [m["article_id"] for m in members]

        # 找出這個 cluster 裡可信度最高的來源，當作 primary_article
        def credibility_rank(article_id: str) -> int:
            source_id = raw_by_id.get(article_id, {}).get("source_id")
            cred = source_credibility.get(source_id, "medium")
            return CREDIBILITY_SCORE.get(cred, 60)

        primary_article_id = max(article_ids, key=credibility_rank)

        avg_impact = sum(m["impact_score"] or 0 for m in members) / len(members)
        avg_novelty = sum(m["novelty_score"] or 0 for m in members) / len(members)
        max_relevance = max(m["personal_relevance_score"] or 0 for m in members)

        # cluster 的可信度：取這個 cluster 裡所有來源中最高的可信度
        best_credibility_score = max(credibility_rank(aid) for aid in article_ids)

        source_count = len(set(raw_by_id.get(aid, {}).get("source_id") for aid in article_ids))
        trend_strength = compute_trend_strength(source_count)

        global_importance = (
            avg_impact * 0.40
            + avg_novelty * 0.20
            + best_credibility_score * 0.20
            + trend_strength * 0.20
        )

        clusters.append({
            "cluster_id": cluster_id,
            "category": members[0].get("category"),
            "article_ids": article_ids,
            "primary_article_id": primary_article_id,
            "source_count": source_count,
            "trend_strength": round(trend_strength, 1),
            "global_importance": round(global_importance, 1),
            "personal_relevance_score": max_relevance,
        })

    return clusters


def select_for_stage2(clusters: list[dict], total: int, top: int) -> dict:
    """
    排序邏輯：候選池整體先依 Personal Relevance 排序，同分再依 Global Importance 排序，
    決定「同樣夠相關的情況下，哪個更值得優先看」。

    重點修正：不能讓單一類別（例如 Markets & Economy）霸佔整份候選清單。
    這裡用「每類別上限」機制，確保五大領域都有機會被看到，
    而不是讓 Global Importance 一路排到底、把其他類別擠出候選名單。
    上限值依類別數量動態計算，總數不夠分的類別，名額會釋出給其他類別遞補。
    """
    ranked = sorted(
        clusters,
        key=lambda c: (c["personal_relevance_score"], c["global_importance"]),
        reverse=True,
    )

    categories = sorted(set(c["category"] for c in clusters))
    num_categories = max(len(categories), 1)
    # 上限稍微寬鬆於平均值，讓真的很強的類別可以多拿一點，但不會無限制霸佔
    max_per_category = max(2, -(-total // num_categories) + 1)  # ceil(total/n) + 1

    selected = []
    category_counts = defaultdict(int)

    # 第一輪：套用每類別上限，確保多元性
    for cluster in ranked:
        if len(selected) >= total:
            break
        cat = cluster["category"]
        if category_counts[cat] < max_per_category:
            selected.append(cluster)
            category_counts[cat] += 1

    # 第二輪：如果第一輪因為上限卡住而還沒選滿，從剩下的候選裡依原排序遞補，不再管上限
    if len(selected) < total:
        selected_ids = {c["cluster_id"] for c in selected}
        for cluster in ranked:
            if len(selected) >= total:
                break
            if cluster["cluster_id"] not in selected_ids:
                selected.append(cluster)

    for i, cluster in enumerate(selected):
        cluster["is_top3"] = i < top

    return {
        "selected": selected,
        "not_selected_count": len(clusters) - len(selected),
    }


def main():
    parser = argparse.ArgumentParser(description="Stage 1/2 橋接：事件分組 + Global Importance 計算 + 候選篩選")
    parser.add_argument("raw_articles_file", help="data/raw_articles.json 路徑")
    parser.add_argument("stage1_results_file", help="data/stage1_results.json 路徑")
    parser.add_argument("sources_file", help="config/sources.yaml 路徑")
    parser.add_argument("--output", default="data/clusters_ranked.json")
    parser.add_argument("--total", type=int, default=13, help="送進 Stage 2 的候選總數")
    parser.add_argument("--top", type=int, default=3, help="標記為頭版精選的數量")
    args = parser.parse_args()

    raw_data = load_json(args.raw_articles_file)
    raw_by_id = {a["id"]: a for a in raw_data["articles"]}

    stage1_data = load_json(args.stage1_results_file)
    stage1_kept = [r for r in stage1_data["results"] if r.get("keep")]

    source_credibility = load_sources_credibility(args.sources_file)

    clusters = build_clusters(stage1_kept, raw_by_id, source_credibility)
    result = select_for_stage2(clusters, args.total, args.top)

    output_data = {
        "stats": {
            "total_kept_articles": len(stage1_kept),
            "total_clusters": len(clusters),
            "selected_for_stage2": len(result["selected"]),
            "not_selected": result["not_selected_count"],
        },
        "clusters": result["selected"],
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print("── 事件分組與篩選統計 ──")
    print(f"  Stage 1 保留文章數: {len(stage1_kept)}")
    print(f"  合併成事件數(cluster): {len(clusters)}")
    print(f"  送進 Stage 2: {len(result['selected'])} 則（其中頭版精選 {args.top} 則）")
    print(f"\n已寫入 {output_path}")

    print("\n── 送進 Stage 2 的清單預覽 ──")
    for c in result["selected"]:
        marker = "★頭版" if c["is_top3"] else "　　　"
        primary = raw_by_id.get(c["primary_article_id"], {})
        print(f"  {marker} [{c['category']}] 重要度{c['global_importance']} 相關度{c['personal_relevance_score']} | {primary.get('title', '')[:40]}")


if __name__ == "__main__":
    main()
