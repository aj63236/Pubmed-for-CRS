#!/usr/bin/env python3
"""
腸嚐新知 — 回補歷史文獻
========================
一次抓一段日期區間的論文（例如 6/1 到今天），逐日存成 JSON。

用法：
  # 先試算，不花錢，只告訴你會抓幾篇、大概多少錢
  python backfill.py --from 2026-06-01 --dry-run

  # 確認後真的跑
  python backfill.py --from 2026-06-01

  # 指定結束日、限制每天篇數
  python backfill.py --from 2026-06-01 --to 2026-07-12 --max-per-day 8

已經處理過的 PMID 會走快取，不會重複付費。
中途斷掉可以直接重跑，已完成的日期會跳過。
"""

import argparse, datetime, json, sys, time
from pathlib import Path

# 重用 fetch_papers 的所有邏輯
import fetch_papers as fp

# GitHub Actions 的 stdout 是管線，Python 預設做「區塊緩衝」（累積滿 8KB 才輸出），
# 會讓 log 看起來像卡住。這裡強制每一行立刻輸出。
_print = print


def print(*args, **kwargs):          # noqa: A001
    kwargs.setdefault("flush", True)
    _print(*args, **kwargs)


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += datetime.timedelta(days=1)


def search_one_day(day):
    """抓某一天新上架（edat）的所有論文 PMID（跟 PubMed 拿資料免費）"""
    import requests
    s = day.strftime("%Y/%m/%d")
    r = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={
            "db": "pubmed",
            "term": " ".join(fp.PUBMED_QUERY.split()),
            "datetype": "edat",
            "mindate": s,
            "maxdate": s,
            "retmax": fp.SCAN_LIMIT,
            "retmode": "json",
            "sort": "pub_date",
        },
        timeout=25,
    )
    r.raise_for_status()
    res = r.json().get("esearchresult", {})
    return res.get("idlist", []), int(res.get("count", 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True, help="起始日 YYYY-MM-DD")
    ap.add_argument("--to", dest="end", default=None, help="結束日 YYYY-MM-DD（預設今天）")
    ap.add_argument("--max-per-day", type=int, default=10, help="每天最多幾篇（預設 10）")
    ap.add_argument("--dry-run", action="store_true", help="只試算，不呼叫 Claude、不花錢")
    ap.add_argument("--skip-existing", action="store_true", default=True,
                    help="已存在的日期檔案就跳過（預設開啟）")
    ap.add_argument("--force", action="store_true", help="即使檔案已存在也重跑")
    ap.add_argument("--max-total", type=int, default=400,
                    help="安全上限：總共最多處理幾篇（預設 400，避免日期填錯燒錢）")
    args = ap.parse_args()

    # ── 防呆：max_per_day 填太大是最常見的燒錢方式 ──
    HARD_CAP = 30
    if args.max_per_day > HARD_CAP:
        print(f"⚠️  每天分析 {args.max_per_day} 篇太多了 —— 這個數字直接乘上費用。")
        print(f"    已自動壓到 {HARD_CAP} 篇。")
        print(f"    （PubMed 每天有 60-80 篇，系統會依期刊 IF 與研究設計排序，")
        print(f"      只分析分數最高的幾篇。填 10 就很夠了。）\n")
        args.max_per_day = HARD_CAP
    if args.max_per_day < 1:
        args.max_per_day = 1

    start = datetime.date.fromisoformat(args.start)
    end = datetime.date.fromisoformat(args.end) if args.end else datetime.date.today()

    if start > end:
        print("❌ 起始日晚於結束日")
        sys.exit(1)

    days = list(daterange(start, end))

    print("=" * 58)
    print(f"  腸嚐新知 · 回補歷史文獻")
    print(f"  區間：{start} ～ {end}（共 {len(days)} 天）")
    print(f"  每天上限：{args.max_per_day} 篇")
    print(f"  模型：{fp.MODEL}  (${fp.price_of(fp.MODEL)[0]}/${fp.price_of(fp.MODEL)[1]} per MTok)")
    if args.dry_run:
        print(f"  模式：🧪 試算（不花錢）")
    print("=" * 58)

    if not fp.API_KEY and not args.dry_run:
        print("\n❌ 沒有 ANTHROPIC_API_KEY，無法產生摘要。")
        print("   先設定：export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)

    # ── 第一階段：掃描每天有幾篇 ──
    print("\n🔍 掃描 PubMed...")
    plan = []          # [(day, ids)]
    total_available = 0

    for day in days:
        ds = day.strftime("%Y-%m-%d")
        day_file = fp.DATA_DIR / f"{ds}.json"

        if day_file.exists() and not args.force:
            try:
                existing = json.loads(day_file.read_text(encoding="utf-8"))
                if existing.get("papers"):
                    print(f"  {ds}  ⏭️  已有 {len(existing['papers'])} 篇，跳過")
                    continue
            except Exception:
                pass

        try:
            ids, total = search_one_day(day)
        except Exception as e:
            print(f"  {ds}  ⚠️  搜尋失敗：{e}")
            continue

        total_available += total
        if ids:
            plan.append((day, ids))
            keep = min(len(ids), args.max_per_day)
            print(f"  {ds}  掃描 {len(ids)} 篇 → 分析前 {keep} 篇")
        else:
            print(f"  {ds}  —")
        time.sleep(0.35)   # 對 NCBI 客氣一點

    # 實際會送去 Claude 的篇數 = 每天取前 max_per_day
    to_fetch = sum(min(len(ids), args.max_per_day) for _, ids in plan)

    # ── 估算成本（扣掉已在快取的） ──
    cache = fp.load_cache()
    # 保守估：把每天前 max_per_day 個 id 當作候選（實際排序後可能不同，但量級一樣）
    cand = [i for _, ids in plan for i in ids[:args.max_per_day]]
    n_cached = sum(1 for i in cand if i in cache)
    n_new = max(0, to_fetch - n_cached)

    # 一篇約 2,000 in + 2,600 out
    # ⚠️ Sonnet 5 等推理模型會先產生 thinking 區塊，那些也算 output token，
    #    所以 output 估得比純 JSON 高。實際費用會在跑完時印出來。
    p_in, p_out = fp.price_of(fp.MODEL)
    est = n_new * (2000 / 1e6 * p_in + 2600 / 1e6 * p_out)

    print("\n" + "─" * 58)
    print(f"  要處理的日期：{len(plan)} 天")
    print(f"  要抓的論文：  {to_fetch} 篇")
    print(f"    ├─ 需生成： {n_new} 篇")
    print(f"    └─ 已快取： {n_cached} 篇（不再收費）")
    print(f"  預估費用：    約 ${est:.2f} USD（含 thinking token，實際以跑完的數字為準）")
    print("─" * 58)

    if args.dry_run:
        print("\n🧪 這是試算，沒有花任何錢。")
        print("   確認 OK 的話，把 --dry-run 拿掉再跑一次。\n")
        return

    # ── 安全煞車：避免日期填錯導致大量計費 ──
    if n_new > args.max_total:
        print(f"\n🛑 停止：需要生成 {n_new} 篇，超過安全上限 {args.max_total} 篇。")
        print(f"   預估要花 ${est:.2f} USD，這可能不是你想要的。")
        print(f"   如果確定要跑，請縮小日期範圍、降低 --max-per-day，")
        print(f"   或明確提高 --max-total。\n")
        sys.exit(1)

    if n_new == 0:
        print("\n✅ 全部都在快取裡，沒有需要新生成的。")

    # ── 第二階段：實際抓取 + 摘要 ──
    print(f"\n🤖 開始處理（model: {fp.MODEL}）...\n")

    tok_in = tok_out = 0
    done_days = 0

    for day, ids in plan:
        ds = day.strftime("%Y-%m-%d")
        print(f"── {ds} ──")

        try:
            papers = fp.fetch_details(ids)
        except Exception as e:
            print(f"   ⚠️  下載失敗：{e}\n")
            continue

        # 依期刊 IF + 研究設計排序，只分析最值得讀的
        if len(papers) > args.max_per_day:
            papers.sort(key=fp.rank_score, reverse=True)
            print(f"   掃描 {len(papers)} 篇 → 取分數最高的 {args.max_per_day} 篇")
            papers = papers[:args.max_per_day]

        for i, p in enumerate(papers, 1):
            short = p["title"][:48] + ("…" if len(p["title"]) > 48 else "")
            pmid = p["pmid"]

            if pmid in cache:
                p.update(cache[pmid])
                print(f"   [{i}/{len(papers)}] 💾 {short}")
                continue

            print(f"   [{i}/{len(papers)}] 🧠 {short}")
            result = fp.summarize(p)
            if result:
                usage = result.pop("_usage", {})
                tok_in += usage.get("in", 0)
                tok_out += usage.get("out", 0)
                p.update(result)
                cache[pmid] = result
            time.sleep(0.4)

        fp.save(papers, ds)
        fp.save_cache(cache)      # 每天存一次，中途斷掉也不會白花錢
        done_days += 1
        print()

    cost = tok_in / 1e6 * p_in + tok_out / 1e6 * p_out
    print("─" * 58)
    print(f"  完成 {done_days} 天")
    print(f"  Token：{tok_in:,} in / {tok_out:,} out")
    print(f"  實際費用：約 ${cost:.2f} USD")
    print("─" * 58)
    print("\n🎉 回補完成！記得 git add data/ && git commit && git push\n")


if __name__ == "__main__":
    main()
