#!/usr/bin/env python3
"""
腸嚐新知 — 資料驗證
===================
在 commit 之前檢查 data/ 裡的 JSON。壞資料就不要推上網站。

用法：
    python validate_data.py            # 檢查全部
    python validate_data.py 2026-07-13 # 只檢查某一天

驗證失敗 → exit code 1 → workflow 不會 commit。

⚠️ 這裡的欄位名稱必須跟 fetch_papers.py 的輸出一致。
   改 schema 的時候兩邊要一起改，不然這支會把好資料判成壞資料。
"""

import json, sys, re
from pathlib import Path

DATA = Path(__file__).parent / "data"

# ── 每篇一定要有的（不管有沒有 API key）──
BASE_REQUIRED = ["pmid", "title", "journal", "added_at"]

# ── AI 分析過的才會有（沒有 API key 時整批都沒有，那是合法狀態）──
AI_REQUIRED = [
    "title_zh", "abstract_zh", "evidence_level",
    "score", "relevance", "novelty",
    "one_liner", "action",
    "key_numbers", "cautions", "pico",
]

ENUMS = {
    "relevance":       {"高度相關", "中度相關", "低度相關"},
    "novelty":         {"可能改變實務", "再確認已知", "仍屬早期", "結果為陰性"},
    "context_verdict": {"再確認", "與前作矛盾", "延伸前作", "無相關前作", ""},
}

VALID_EVIDENCE = {
    "RCT", "系統性回顧", "統合分析", "前瞻性世代", "回溯性研究",
    "病例對照", "橫斷面", "診斷性研究", "病例系列", "綜述", "其他",
}

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def check_paper(p, where):
    """回傳 (errors, warnings)"""
    errs, warns = [], []
    pmid = p.get("pmid", "?")
    tag = f"{where} PMID {pmid}"

    for f in BASE_REQUIRED:
        if not p.get(f):
            errs.append(f"{tag}：缺少必要欄位 `{f}`")

    if not ISO.match(str(p.get("added_at", ""))):
        errs.append(f"{tag}：added_at 格式不對（應為帶時區的 ISO）→ {p.get('added_at')!r}")

    # 有沒有跑過 AI？用 title_zh 判斷
    has_ai = bool(p.get("title_zh"))
    if not has_ai:
        warns.append(f"{tag}：沒有中文分析（ANTHROPIC_API_KEY 沒設？）")
        return errs, warns

    for f in AI_REQUIRED:
        v = p.get(f)
        if v is None or v == "" or v == []:
            errs.append(f"{tag}：AI 欄位 `{f}` 是空的")

    # score
    sc = p.get("score")
    if sc is not None and not (isinstance(sc, int) and 0 <= sc <= 10):
        errs.append(f"{tag}：score 必須是 0–10 的整數 → {sc!r}")

    # enum
    for f, allowed in ENUMS.items():
        v = p.get(f, "")
        if v and v not in allowed:
            errs.append(f"{tag}：`{f}` 不是合法值 → {v!r}（合法：{'、'.join(sorted(x for x in allowed if x))}）")

    ev = p.get("evidence_level", "")
    if ev and ev not in VALID_EVIDENCE:
        warns.append(f"{tag}：evidence_level 少見值 → {ev!r}")

    # 陣列型
    for f in ("key_numbers", "cautions", "unassessable", "watched", "related", "effects"):
        if f in p and not isinstance(p[f], list):
            errs.append(f"{tag}：`{f}` 必須是陣列 → {type(p[f]).__name__}")

    # PICO
    pico = p.get("pico")
    if pico is not None:
        if not isinstance(pico, dict):
            errs.append(f"{tag}：pico 必須是物件")
        elif not any(pico.get(k) for k in ("P", "I", "C", "O")):
            errs.append(f"{tag}：pico 四個欄位全空")

    # 程式驗算過的效果量
    for e in (p.get("effects") or []):
        if not isinstance(e, dict):
            errs.append(f"{tag}：effects 元素不是物件")
            continue
        if e.get("ok"):
            if e.get("kind") not in ("NNT", "NNH", "none"):
                errs.append(f"{tag}：effects.kind 不合法 → {e.get('kind')!r}")
            nnt = e.get("nnt")
            if nnt is not None and (not isinstance(nnt, int) or nnt <= 0):
                errs.append(f"{tag}：NNT/NNH 必須是正整數 → {nnt!r}")

    return errs, warns


def check_day(f):
    errs, warns = [], []
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"{f.name}：JSON 壞掉 → {e}"], []

    if not isinstance(d, dict):
        return [f"{f.name}：最外層必須是物件"], []

    if d.get("date") != f.stem:
        errs.append(f"{f.name}：date 欄位（{d.get('date')}）跟檔名對不上")

    papers = d.get("papers")
    if not isinstance(papers, list):
        return errs + [f"{f.name}：papers 必須是陣列"], warns

    if d.get("count") is not None and d["count"] != len(papers):
        errs.append(f"{f.name}：count={d['count']} 但實際有 {len(papers)} 篇")

    seen = set()
    for p in papers:
        if not isinstance(p, dict):
            errs.append(f"{f.name}：papers 元素不是物件")
            continue
        pm = p.get("pmid")
        if pm in seen:
            errs.append(f"{f.name}：PMID {pm} 重複")
        seen.add(pm)
        e, w = check_paper(p, f.name)
        errs += e
        warns += w

    return errs, warns


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None

    files = sorted(DATA.glob("20*-*-*.json"))
    if only:
        files = [f for f in files if f.stem == only]
        if not files:
            print(f"❌ 找不到 data/{only}.json")
            sys.exit(1)

    print("═" * 56)
    print("  資料驗證")
    print("═" * 56)

    if not files:
        print("  沒有日檔可驗證（第一次跑？）")
        print("═" * 56)
        return

    all_errs, all_warns, n_papers = [], [], 0

    for f in files:
        e, w = check_day(f)
        d = json.loads(f.read_text(encoding="utf-8"))
        n = len(d.get("papers", []))
        n_papers += n
        all_errs += e
        all_warns += w
        mark = "❌" if e else ("⚠️ " if w else "✅")
        print(f"  {mark} {f.name:<20} {n:>3} 篇")

    # index.json 要跟日檔對得上
    idx_f = DATA / "index.json"
    if idx_f.exists():
        try:
            idx = json.loads(idx_f.read_text(encoding="utf-8"))
            for row in idx:
                day = DATA / f"{row['date']}.json"
                if not day.exists():
                    all_errs.append(f"index.json 指向不存在的 {row['date']}.json")
        except Exception as e:
            all_errs.append(f"index.json 壞掉 → {e}")

    print("─" * 56)
    print(f"  {len(files)} 天 · {n_papers} 篇")

    if all_warns:
        print(f"\n  ⚠️  {len(all_warns)} 個警告：")
        for w in all_warns[:10]:
            print(f"     {w}")
        if len(all_warns) > 10:
            print(f"     …還有 {len(all_warns)-10} 個")

    if all_errs:
        print(f"\n  ❌ {len(all_errs)} 個錯誤：")
        for e in all_errs[:20]:
            print(f"     {e}")
        if len(all_errs) > 20:
            print(f"     …還有 {len(all_errs)-20} 個")
        print("\n" + "═" * 56)
        print("  🛑 驗證失敗 —— 不會 commit，網站維持舊資料")
        print("═" * 56)
        sys.exit(1)

    print("\n  ✅ 全部通過")
    print("═" * 56)


if __name__ == "__main__":
    main()
