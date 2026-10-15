#!/usr/bin/env python3
"""
腸嚐新知 — 每日 PubMed 文獻收集 + AI 摘要
=========================================
1. 用 Entrez date (edat) 抓「最近幾天新上架 PubMed」的大腸直腸外科論文
2. 對每篇呼叫 GPT-6.1 Sol（OpenAI Batch） 產生：中文標題 / 完整中文摘要 / 臨床重點 / 臨床影響
3. 已處理過的 PMID 會存進 cache，不會重複計費
4. 輸出結構化 JSON 到 data/，網站直接讀取（零延遲）

環境變數：OPENAI_API_KEY（遷移相容：ANTHROPIC_API_KEY）
"""

import os, re, json, math, time, datetime, uuid, hashlib, subprocess, requests
from openai_summary import response_request, response_text
import xml.etree.ElementTree as ET
from pathlib import Path

# ─────────────────────────── 設定 ───────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Auto uses OpenAI whenever configured. The migration fallback is only for a
# missing key, never for an OpenAI request failure or an unavailable model.
PROVIDER = os.environ.get("LLM_PROVIDER", "auto").strip().lower() or "auto"
if PROVIDER == "auto":
    PROVIDER = "openai" if OPENAI_API_KEY or not ANTHROPIC_API_KEY else "anthropic"
if PROVIDER not in {"openai", "anthropic"}:
    raise ValueError("LLM_PROVIDER must be auto, openai, or anthropic")
API_KEY = OPENAI_API_KEY if PROVIDER == "openai" else ANTHROPIC_API_KEY
# Separate names prevent an old repository MODEL=claude-* overriding Sol.
MODEL = ((os.environ.get("OPENAI_MODEL", "").strip() or "gpt-6.1-sol")
         if PROVIDER == "openai" else
         (os.environ.get("MODEL", "").strip() or "claude-sonnet-5"))
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "medium").strip()
OPENAI_MAX_OUTPUT_TOKENS = int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "12000"))
DAYS_BACK   = 2      # 抓最近 N 天新上架的論文
SCAN_LIMIT   = 120   # 每天最多「掃描」幾篇（跟 PubMed 拿資料是免費的）
MAX_RESULTS  = 10    # 每天取幾篇「高分文章」（付費）
INCLUDE_REVIEWS = True   # 除了高分文章，是否額外納入當天所有 Review
REVIEW_LIMIT = 8     # Review 最多幾篇

# 🛑 每日硬上限：追蹤關鍵字設太寬、或某天 Review 暴量，都可能讓選片數失控。
#    超過就砍到這個數字（但保證追蹤命中的不會被砍掉）。
DAILY_HARD_LIMIT = 25
MAX_RETRY   = 3
MAX_TOKENS  = 8500

# ─────────────────────── 批次（Batch API）───────────────────────
# 每天 06:00 自動跑、沒有人在等結果 —— 這正是 Batch API 的適用情境，
# input/output 都是同步價的 5 折。
#
# 代價是不即時：多數批次 1 小時內結束，但官方 SLA 是 24 小時。
# 所以「送出」和「取回」必須能拆開在不同次 workflow 完成，
# 否則 runner 一逾時，錢付了、結果卻拿不到。
#   → 送出後把 batch_id 寫進 data/_batch_pending.json（會 commit）
#   → 下次跑先檢查有沒有待領的批次，有就先領回來，不會重複付費
USE_BATCH        = os.environ.get("USE_BATCH", "1").strip() != "0"
BATCH_WAIT_MIN   = int(os.environ.get("BATCH_WAIT_MIN", "45"))   # 這次最多等幾分鐘
BATCH_POLL_SEC   = 30                                            # 官方建議 30–60 秒
PENDING_FILE     = "_batch_pending.json"

# 每 1M token 單價（USD）；以供應商實際帳單為準。
PRICING = {
    "gpt-6.1-sol":      (2.00, 10.00),
    "claude-sonnet-5":   (3.00, 15.00),   # 2026-09-01 起，舊路徑相容
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8":   (5.00, 25.00),
    "claude-haiku-4-5":  (1.00,  5.00),
}
BATCH_DISCOUNT = 0.5      # Batch API 一律 5 折（input 與 output 都是）


def api_headers(provider=None, multipart=False):
    provider = provider or PROVIDER
    if provider == "openai":
        headers = {"Authorization": "Bearer " + OPENAI_API_KEY}
    else:
        headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"}
    if not multipart:
        headers["content-type"] = "application/json"
    return headers


def summary_request(prompt):
    if PROVIDER == "openai":
        return response_request(prompt, MODEL, OPENAI_REASONING_EFFORT,
                                OPENAI_MAX_OUTPUT_TOKENS)
    return {"model": MODEL, "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}]}


# 研究設計權重：外科醫師眼中，一篇 DCR 的 RCT 勝過一篇 Lancet 的綜述
PTYPE_WEIGHT = {
    "Randomized Controlled Trial": 12,
    "Meta-Analysis":               12,
    "Practice Guideline":          12,
    "Systematic Review":           10,
    "Guideline":                   10,
    "Clinical Trial, Phase III":    9,
    "Clinical Trial":               6,
    "Multicenter Study":            5,
    "Comparative Study":            3,
    "Observational Study":          2,
    "Review":                       1,
}

# IF 上限：避免超高分綜合期刊的綜述蓋過專科期刊的 RCT
IF_CAP        = 25
IF_WEIGHT     = 0.5
PTYPE_MULT    = 2.0


def rank_score(paper):
    """排序分數 = 期刊 IF（上限 25，權重 0.5）+ 研究設計權重 × 2

    舉例：
      Dis Colon Rectum 的 RCT    → 3.2×0.5 + 12×2 = 25.6
      Lancet 的綜述              → 25 ×0.5 +  1×2 = 14.5   ← RCT 勝出，正確
      Lancet 的 RCT              → 25 ×0.5 + 12×2 = 36.5   ← 當然最高
      不明期刊的病例系列          → 0  ×0.5 +  0×2 =  0
    """
    jif = paper.get("impact_factor") or 0
    best_ptype = max(
        (PTYPE_WEIGHT.get(pt, 0) for pt in paper.get("ptype_list", [])),
        default=0,
    )
    return min(jif, IF_CAP) * IF_WEIGHT + best_ptype * PTYPE_MULT


# ═══════════════ 舊資料自動修補 ═══════════════
#
# `added_at`（收錄時間）是後來才加的欄位。在它出現之前收集的論文沒有這個欄位，
# 驗證器會把它們全部判定為不合格 → 好資料被擋掉。
#
# 教訓：加必要欄位的時候，一定要同時寫遷移程式。
# 這支是冪等的，每天跑一次不會有副作用。

_TZ_RE = re.compile(r"(Z|[+\-]\d{2}:?\d{2})$")


def _has_tz(s):
    return bool(_TZ_RE.search(str(s or "")))


def _to_tw(s):
    """把沒有時區的時間戳當成 UTC（舊版在 GitHub Actions 上跑，拿到的是 UTC），
    轉成台灣時間。不然網站會顯示成 22:00 而不是隔天 06:00。"""
    try:
        dt = datetime.datetime.fromisoformat(str(s))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(TZ).isoformat(timespec="seconds")
    except Exception:
        return s


def heal_legacy():
    """補齊舊資料缺的 added_at，並修正沒有時區的時間戳。"""
    n_files = n_papers = 0

    for f in sorted(DATA_DIR.glob("20*-*-*.json")):
        try:
            day = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue

        changed = False

        gen = day.get("generated_at", "")
        if gen and not _has_tz(gen):
            gen = _to_tw(gen)
            day["generated_at"] = gen
            changed = True

        # 沒有 generated_at 就退回「該日早上 6 點」（workflow 的排程時間）
        fallback = gen or f"{day.get('date', f.stem)}T06:00:00+08:00"

        for p in day.get("papers", []):
            a = p.get("added_at")
            if not a:
                p["added_at"] = fallback
                changed = True
                n_papers += 1
            elif not _has_tz(a):
                p["added_at"] = _to_tw(a)
                changed = True
                n_papers += 1

        if changed:
            f.write_text(json.dumps(day, ensure_ascii=False, indent=1), encoding="utf-8")
            n_files += 1

    if n_papers:
        log(f"🩹 修補舊資料：{n_files} 個檔案、{n_papers} 篇補上 added_at / 修正時區")
    return n_papers


# ═══════════════ 效果量：Claude 抽數字，Python 算 ═══════════════
#
# 為什麼不讓模型自己算：
#   1. 多步驟算術容易出錯，尤其是符號方向
#   2. NNT / NNH 搞反是真正的臨床錯誤 —— 你會拿去跟病人講
#   3. 最陰險的：只有 HR / OR / RR 時，ARR 根本算不出來，
#      但模型會硬算一個看起來合理的數字。Python 可以直接拒絕。

def _risk(arm):
    """從 {events,total} 或 {rate} 取出風險（0–1）。取不到就 None。"""
    if not isinstance(arm, dict):
        return None, None

    ev, tot = arm.get("events"), arm.get("total")
    if ev is not None and tot:
        try:
            ev, tot = float(ev), float(tot)
        except (TypeError, ValueError):
            return None, None
        if tot <= 0 or ev < 0 or ev > tot:
            return None, None
        return ev / tot, f"{ev:.0f}/{tot:.0f}"

    rate = arm.get("rate")
    if rate is not None:
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            return None, None
        if not (0 <= rate <= 100):
            return None, None
        return rate / 100, None

    return None, None


def compute_effects(effect_data):
    """把 Claude 抽出來的事件數／發生率，算成 ARR 與 NNT／NNH。

    算不出來就誠實回報「摘要資料不足」，不猜。
    """
    out = []
    if not isinstance(effect_data, list):
        return out

    for e in effect_data[:4]:
        if not isinstance(e, dict):
            continue

        outcome = str(e.get("outcome", "")).strip()
        if not outcome:
            continue

        ri, si = _risk(e.get("intervention"))
        rc, sc = _risk(e.get("control"))

        # 沒有兩組的絕對數字 → 拒絕計算
        if ri is None or rc is None:
            out.append({
                "outcome": outcome,
                "ok": False,
                "note": "摘要只給相對指標（HR／OR／RR）或資料不全，無法計算絕對風險",
            })
            continue

        # event_is_bad：事件本身是壞事嗎？（復發、洩漏、感染 = 壞事）
        bad = e.get("event_is_bad")
        bad = True if bad is None else bool(bad)

        # 好處的方向：壞事變少 = 好；好事變多 = 好
        arr = (rc - ri) if bad else (ri - rc)

        item = {
            "outcome":   outcome,
            "ok":        True,
            "i_label":   str(e.get("intervention", {}).get("label", "介入組")),
            "c_label":   str(e.get("control", {}).get("label", "對照組")),
            "i_rate":    round(ri * 100, 1),
            "c_rate":    round(rc * 100, 1),
            "i_raw":     si,
            "c_raw":     sc,
            "arr":       round(abs(arr) * 100, 1),
            "benefit":   arr > 0,
            "event_bad": bad,
        }

        if abs(arr) < 0.0005:                 # 差異 < 0.05%，NNT 沒有意義
            item["nnt"] = None
            item["kind"] = "none"
            item["note"] = "兩組差異幾乎為零，NNT 無臨床意義"
        else:
            n = 1 / abs(arr)
            item["nnt"]  = int(round(n))
            item["kind"] = "NNT" if arr > 0 else "NNH"   # 方向錯 = 臨床錯誤
            item["note"] = ""

        out.append(item)

    return out


# ═══════════════ 相關文獻檢索 ═══════════════
# 把過去收集的所有論文當成一個小型資料庫，用 TF-IDF 餘弦相似度
# 找出跟新論文最相關的前作。純本機計算，不花任何錢。

_STOP = set("""
the a an and or of for in on with to from by at as is are was were be been being
this that these those we our us their its it they i you he she
study studies patient patients group groups compared comparison versus vs
result results conclusion conclusions background methods method objective
purpose aim aims between after before during using used use
significant significantly associated association
risk rate rates ratio odds outcome outcomes primary secondary endpoint
value values median mean total number all both not no more less higher lower
may can could should also however although while than then thus therefore
data analysis analyses among within per each other others such based
""".split())


def _tokens(text):
    """英文詞元。PubMed 是英文，比中文翻譯穩定，所以只用英文標題+摘要。"""
    return [w for w in re.findall(r"[a-z][a-z\-]{2,}", (text or "").lower())
            if w not in _STOP]


def _vec(toks, idf, default_idf):
    """TF-IDF 向量（已正規化，之後點積 = 餘弦相似度）"""
    tf = {}
    for t in toks:
        tf[t] = tf.get(t, 0) + 1
    v = {t: (1 + math.log(f)) * idf.get(t, default_idf) for t, f in tf.items()}
    n = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {t: x / n for t, x in v.items()}


def build_corpus():
    """把 data/ 裡所有日檔讀成語料庫。這就是「資料庫」。"""
    docs = []
    for f in sorted(DATA_DIR.glob("20*-*-*.json")):
        try:
            day = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for p in day.get("papers", []):
            if not p.get("pmid"):
                continue
            docs.append({
                "pmid":      p["pmid"],
                "date":      day.get("date", ""),
                "title_zh":  p.get("title_zh") or p.get("title", ""),
                "score":     p.get("score"),
                "one_liner": p.get("one_liner", ""),
                "toks":      _tokens(p.get("title", "") + " " + p.get("abstract", "")),
            })

    if not docs:
        return [], {}, 1.0

    N = len(docs)
    df = {}
    for d in docs:
        for t in set(d["toks"]):
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((N + 1) / (c + 1)) + 1 for t, c in df.items()}
    default_idf = math.log(N + 1) + 1        # 語料庫沒見過的詞 → 最高鑑別度

    for d in docs:
        d["vec"] = _vec(d["toks"], idf, default_idf)

    return docs, idf, default_idf


def find_related(paper, corpus, k=3, min_sim=0.15):
    """找出跟這篇最相關的前作。

    min_sim=0.15 是實測出來的：0.10 太鬆，兩篇只因為都提到 colorectal cancer
    就會被配在一起（餘弦 0.11），那種假關聯餵給模型很危險。
    """
    docs, idf, default_idf = corpus
    if not docs:
        return []

    v = _vec(_tokens(paper.get("title", "") + " " + paper.get("abstract", "")),
             idf, default_idf)
    if not v:
        return []

    hits = []
    for d in docs:
        if d["pmid"] == paper["pmid"]:
            continue
        sim = sum(x * d["vec"].get(t, 0.0) for t, x in v.items())
        if sim >= min_sim:
            hits.append((sim, d))

    hits.sort(key=lambda x: -x[0])
    return [{
        "pmid":      d["pmid"],
        "date":      d["date"],
        "title_zh":  d["title_zh"],
        "score":     d["score"],
        "one_liner": d["one_liner"],
        "sim":       round(sim, 3),
    } for sim, d in hits[:k]]


def is_review(paper):
    """narrative review / systematic review 都算。"""
    return any("review" in pt.lower() for pt in paper.get("ptype_list", []))


def select(papers, max_results=None, review_limit=None):
    """選片，三個來源合起來去重：

      1. 追蹤命中     —— 無視分數。你要追的東西不該被 IF 決定。
      2. 高分文章     —— 前 N 名
      3. Review       —— 綜述分數天生低，另外撈，不跟高分文章搶名額

    回傳 (選中的清單, 說明字串)
    """
    max_results  = MAX_RESULTS  if max_results  is None else max_results
    review_limit = REVIEW_LIMIT if review_limit is None else review_limit

    # 先標記每篇命中了什麼
    for p in papers:
        p["watched"] = match_watch(p)

    ranked = sorted(papers, key=rank_score, reverse=True)
    chosen, seen = [], set()

    # 1) 追蹤命中（最優先，分數再低也要）
    n_watch = 0
    for p in ranked:
        if n_watch >= WATCH_LIMIT:
            break
        if p["watched"] and p["pmid"] not in seen:
            chosen.append(p)
            seen.add(p["pmid"])
            n_watch += 1

    # 2) 高分文章
    n_top = 0
    for p in ranked:
        if n_top >= max_results:
            break
        if p["pmid"] not in seen:
            chosen.append(p)
            seen.add(p["pmid"])
            n_top += 1

    # 3) Review
    n_rev = 0
    if INCLUDE_REVIEWS:
        for p in ranked:
            if n_rev >= review_limit:
                break
            if p["pmid"] in seen or not is_review(p):
                continue
            chosen.append(p)
            seen.add(p["pmid"])
            n_rev += 1

    chosen.sort(key=rank_score, reverse=True)

    bits = []
    if n_watch: bits.append(f"🔖 追蹤 {n_watch} 篇")
    bits.append(f"高分 {n_top} 篇")
    if n_rev:   bits.append(f"Review {n_rev} 篇")
    return chosen, " + ".join(bits)


def price_of(model, batch=None):
    """每 1M token 單價。batch=True 時套用 Batch API 的 5 折。

    預設跟著 USE_BATCH 走，這樣 log 印出來的費用永遠是實際會被收的錢 ——
    不然會出現「log 說 $15，帳單只有 $7.5」這種對不起來的情況。
    """
    if batch is None:
        batch = USE_BATCH
    p = (3.00, 15.00)          # 未知模型，用旗艦價估，寧可高估
    for k, v in PRICING.items():
        if model.startswith(k):
            p = v
            break
    return (p[0] * BATCH_DISCOUNT, p[1] * BATCH_DISCOUNT) if batch else p


# Per-run usage ledger. Output tokens already INCLUDE reasoning tokens.
USAGE_LEDGER = []


def record_usage(body, batch):
    usage = body.get("usage") or {}
    model = body.get("model") or body.get("_summary_model") or MODEL
    is_openai = "output" in body or body.get("object") == "response"
    p_in, p_out = price_of(model, batch=batch)
    tin, tout = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", 0) if is_openai else usage.get("cache_read_input_tokens", 0)
    writes = details.get("cache_write_tokens", 0) if is_openai else usage.get("cache_creation_input_tokens", 0)
    uncached = max(0, tin - cached - writes) if is_openai else tin
    cache_rate = 0.05 if model.startswith("gpt-6.1-sol") else 0.1
    estimate = (uncached * p_in + cached * p_in * cache_rate +
                writes * p_in * 1.25 + tout * p_out) / 1e6
    entry = {"provider": "openai" if is_openai else "anthropic", "model": model,
             "batch": batch, "input_tokens": tin, "output_tokens": tout,
             "cached_tokens": cached, "cache_write_tokens": writes,
             "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
             "estimated_cost_usd": estimate}
    USAGE_LEDGER.append(entry)
    return entry


# ── 時區 ──
# GitHub Actions 跑在 UTC。workflow 排在 UTC 22:00（= 台灣隔天 06:00），
# 若直接用 datetime.now() / date.today()，會拿到 UTC 的「前一天 22 點」，
# 日期標錯、時間也對不上。台灣沒有日光節約，固定 +8 即可。
TZ = datetime.timezone(datetime.timedelta(hours=8))


def now_tw():
    return datetime.datetime.now(TZ)


def today_tw():
    return now_tw().date()


def stamp():
    """帶時區的 ISO 時間字串，瀏覽器會自動轉成使用者的本地時間顯示。"""
    return now_tw().isoformat(timespec="seconds")


ROOT      = Path(__file__).parent
DATA_DIR  = ROOT / "data"
CACHE_FILE = DATA_DIR / "_cache_v3.json"  # pmid -> 已生成的分析（v3 schema）
JOURNALS_FILE = ROOT / "journals.json"  # 期刊 Impact Factor 對照表
WATCH_FILE    = ROOT / "watch.json"     # 追蹤關鍵字（命中就無視分數強制納入）


def load_watch():
    try:
        w = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
        return (
            [k.lower() for k in w.get("keywords", []) if k.strip()],
            [j.lower() for j in w.get("journals", []) if j.strip()],
            int(w.get("limit", 6)),
            [f for f in w.get("focus", []) if str(f).strip()],
        )
    except Exception:
        return [], [], 0, []


WATCH_KW, WATCH_JOURNALS, WATCH_LIMIT, WATCH_FOCUS = load_watch()


def match_watch(paper):
    """這篇命中了哪些追蹤項目？回傳命中的字串清單（沒命中就是空的）。"""
    hits = []

    hay = (paper.get("title", "") + " " + paper.get("abstract", "")).lower()
    for kw in WATCH_KW:
        if kw in hay:
            hits.append(kw)

    jrn = (paper.get("journal", "") + " " + paper.get("journal_full", "")).lower()
    for j in WATCH_JOURNALS:
        if j in jrn:
            hits.append(j)

    # 去重但保留順序
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def load_journals():
    """載入 JCR 對照表。ISSN 比對最可靠，名稱比對只當後備。"""
    try:
        raw = json.loads(JOURNALS_FILE.read_text(encoding="utf-8"))
        return raw.get("by_issn", {}), raw.get("by_name", {}), raw.get("_year", "")
    except Exception:
        return {}, {}, ""


def _norm_name(n):
    """⚠️ 絕對不可以砍掉 journal / of / the 這些字。

    砍掉的話：
        "Journal of Clinical Oncology" (IF 44.7)  ─┐
        "Clinical Oncology"            (IF  2.5)  ─┴─→ 都變成 "clinical oncology"
    後者會把前者蓋掉，JCO 的 IF 就從 44.7 變成 2.5。
    同理 Gastroenterology (29.7) 會被 Journal of Gastroenterology (5.7) 蓋掉。
    """
    n = (n or "").lower()
    n = re.sub(r"\s*:.*$", "", n)          # 只砍副標題
    n = n.replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


IF_BY_ISSN, IF_BY_NAME, IF_YEAR = load_journals()


def lookup_if(paper):
    """依序嘗試：ISSN → ISSN-Linking → 期刊全名 → 期刊縮寫。
    ISSN 最可靠；名稱比對因原始 PDF 雙欄排版錯亂，偶有誤差，所以放後面。"""
    for issn in (paper.get("issn"), paper.get("issn_linking")):
        if issn:
            v = IF_BY_ISSN.get(issn.upper())
            if v is not None:
                return v
    for nm in (paper.get("journal_full"), paper.get("journal")):
        if nm:
            v = IF_BY_NAME.get(_norm_name(nm))
            if v is not None:
                return v
    return None

# 大腸直腸外科搜尋策略：核心主題 + 排除雜訊
PUBMED_QUERY = """
(
  "colorectal surgery"[MeSH Terms]
  OR "colorectal neoplasms"[MeSH Terms]
  OR "rectal neoplasms"[MeSH Terms]
  OR "colonic neoplasms"[MeSH Terms]
  OR "colorectal cancer"[tiab]
  OR "rectal cancer"[tiab]
  OR "colon cancer"[tiab]
  OR "colectomy"[tiab]
  OR "proctectomy"[tiab]
  OR "total mesorectal excision"[tiab]
  OR "TaTME"[tiab]
  OR "anastomotic leak"[tiab]
  OR "ileostomy"[tiab]
  OR "colostomy"[tiab]
  OR "hemorrhoidectomy"[tiab]
  OR "anal fistula"[tiab]
  OR "diverticulitis"[tiab]
  OR "inflammatory bowel disease"[tiab]
  OR "robotic colorectal"[tiab]
  OR "laparoscopic colorectal"[tiab]
)
AND (english[Language])
AND (hasabstract)
AND (humans[MeSH Terms])
NOT (case reports[Publication Type])
"""

PROMPT = """你是大腸直腸外科資深主治醫師，同時熟悉實證醫學（EBM）與論文批判性閱讀。

讀者是台灣醫學中心的大腸直腸外科主治／總醫師，早上花三分鐘滑手機看今日文獻。
你的任務**不是取代讀全文**，而是幫他決定：**這篇值不值得花時間去讀全文**。

請輸出**純 JSON**（不要 markdown、不要 ```、不要任何前後文字）。

⚠️ 最重要的規則：
你手上只有**摘要**。allocation concealment、盲法、ITT、失訪率、試驗註冊、利益衝突這些，摘要通常不會寫。
**絕對不可以憑空推測或編造。** 沒寫的就列進 unassessable，不要硬填。
醫師會拿這個做臨床判斷，寧可誠實說不知道，也不要猜。
novelty 與 context_verdict 是不同欄位，必須各自使用列出的選項；
「與前作矛盾」只能填在 context_verdict，不能當成 novelty。

輸出格式：

{{
  "title_zh": "標題繁中翻譯，保留 TME、ERAS、pCR、LARS 等專有縮寫",

  "abstract_zh": "原文摘要的完整繁中翻譯。逐句完整，不縮減、不省略任何數據、p 值、信賴區間。",

  "evidence_level": "擇一：RCT / 系統性回顧 / 統合分析 / 前瞻性世代 / 回溯性研究 / 病例對照 / 橫斷面 / 診斷性研究 / 病例系列 / 綜述 / 其他",

  "score": 7,
  "score_reason": "1-2 句話：為什麼給這個分數（可信度 + 對台灣大腸直腸外科的實用性，滿分 10）",

  "relevance": "擇一：高度相關 / 中度相關 / 低度相關",
  "relevance_why": "一句話，為什麼。（低度相關的例子：純基礎研究、非外科介入、罕見到台灣幾乎不會遇到）",

  "novelty": "擇一：可能改變實務 / 再確認已知 / 仍屬早期 / 結果為陰性",

  "one_liner": "**一句話講完這篇在幹嘛**。句型：在＿＿＿（族群，含人數）中，比較＿＿＿與＿＿＿，發現對＿＿＿有＿＿＿程度的影響（附最關鍵的那個數字）。到這裡就停 —— 不要寫「適合／不適合用在誰」，那是 action 的工作，不要重複。控制在 60 字內。",

  "action": "**明天上班可以怎麼做**。具體到病人類型，用祈使句。若這篇不足以改變任何做法，就誠實寫「目前不需改變做法」＋一句話說為什麼。控制在 60 字內。",

  "pico": {{
    "P": "研究對象／病人族群（含人數）",
    "I": "介入、暴露或檢查",
    "C": "比較對象（若無對照組請寫「無對照組」）",
    "O": "主要 outcome；次要 outcome"
  }},

  "effect_data": [
    {{
      "outcome": "結果名稱（例：五年局部復發）",
      "event_is_bad": true,
      "intervention": {{"label": "機器手臂", "events": 10, "total": 243}},
      "control":      {{"label": "腹腔鏡",   "events": 20, "total": 243}}
    }}
  ],

  "key_numbers": [
    "3 條。每條 ≤ 45 字。務必給絕對值，不要只給相對風險；能算 ARR／NNT 就附上（例：局部復發 4.2% vs 8.1%，ARR 3.9%，NNT≈26）",
    "挑最紮實、最不受偏誤影響的那個",
    "陰性結果也要寫（例：整體存活無差異 p=0.58）"
  ],

  "cautions": [
    "**正好 3 條**，每條 ≤ 45 字，只寫最致命的。要涵蓋兩類問題：（一）臨床陷阱 —— 主要 outcome 是不是替代指標？composite outcome 有沒有掩蓋真相？次群組是不是事後分析？作者把相關性講成因果、過度詮釋、或把短期推論成長期？（二）數字紅旗 —— 只報相對風險沒報絕對風險？只有 p 值沒有信賴區間？信賴區間過寬？樣本數過小？無對照組？",
    "陷阱 2",
    "陷阱 3"
  ],

  "unassessable": ["因摘要資訊不足而無法評估的項目。例如：盲法、allocation concealment、ITT、失訪率、試驗註冊、利益衝突、成本分析"],

  "context_verdict": "擇一：再確認 / 與前作矛盾 / 延伸前作 / 無相關前作",
  "context": "2-3 句話，說明這篇跟【下面列出的、你之前看過的論文】是什麼關係。⚠️ 若下面沒有列出任何論文，或列出的其實跟這篇無關，就把 context_verdict 設為「無相關前作」，context 寫「無相關前作」，**絕對不要硬掰關聯**。若真的相關：一致 → 說再確認了什麼；矛盾 → 明確指出哪裡矛盾、哪一篇證據等級較高、該信哪個；延伸 → 說補上了什麼缺口。"
}}

{related_block}
━━━━━━━━━━ effect_data 的規則（很重要）━━━━━━━━━━

你只負責**抽數字**，ARR / NNT 由程式計算，你不要自己算。

1. 只有在摘要提供**兩組的絕對數字**時才填 effect_data：
   - 事件數 + 總人數 → 用 events / total
   - 或發生率(%)     → 用 rate（0–100 的數字）
2. **只有 HR / OR / RR / p 值時，effect_data 請留空陣列 []。**
   不可以自己把 HR 換算成 ARR —— 那在數學上做不到。
3. event_is_bad：事件本身是壞事嗎？
   - 復發、洩漏、感染、死亡、併發症 → true
   - 完全緩解、存活、保肛成功       → false
4. 最多 3 個最重要的 outcome。
5. 寧可留空，也不要編數字。
6. 若 schema 要求 events、total、rate 都存在，未提供的數字填 null，不能用 0 代替未知。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

論文標題：{title}
期刊：{journal}
發表型態：{ptype}
原文摘要：
{abstract}
"""


# ───────────────────────── 工具函式 ─────────────────────────
def log(msg=""):
    print(msg, flush=True)


def load_cache():
    if not CACHE_FILE.exists():
        return {}
    # A corrupt cache is not an empty cache: fail closed to prevent rebilling.
    cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    if not isinstance(cache, dict) or any(not isinstance(v, dict) for v in cache.values()):
        raise ValueError("Invalid summary cache; refusing paid regeneration")
    return cache


def save_cache(cache):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 只保留最近 600 篇，避免無限膨脹
    if len(cache) > 600:
        cache = dict(list(cache.items())[-600:])
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CACHE_FILE)


# ───────────────────────── PubMed ──────────────────────────
def ncbi_get(url, params, timeout=25, tries=4):
    """呼叫 NCBI，帶指數退避重試。

    GitHub Actions 的 IP 是共用的，NCBI 對共用 IP 會限流（429），
    偶爾也會 5xx。原本沒有重試 —— 只要 NCBI 打個嗝，
    raise_for_status() 就拋例外，整個 workflow 掛掉。
    """
    last = None
    for i in range(1, tries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 3 * i
                log(f"      ⏳ NCBI {r.status_code}，{wait}s 後重試（{i}/{tries}）")
                time.sleep(wait)
                last = f"HTTP {r.status_code}"
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = str(e)
            if i < tries:
                wait = 3 * i
                log(f"      ⏳ NCBI 連線問題，{wait}s 後重試（{i}/{tries}）：{e}")
                time.sleep(wait)

    raise RuntimeError(f"NCBI 重試 {tries} 次都失敗：{last}")


def search_pubmed():
    """用 edat（PubMed 上架日）搜尋，比 pdat 更貼近『今天有什麼新文章』"""
    today = today_tw()
    start = today - datetime.timedelta(days=DAYS_BACK)
    fmt = lambda d: d.strftime("%Y/%m/%d")

    r = ncbi_get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        {
            "db": "pubmed",
            "term": " ".join(PUBMED_QUERY.split()),
            "datetype": "edat",
            "mindate": fmt(start),
            "maxdate": fmt(today),
            "retmax": SCAN_LIMIT,
            "retmode": "json",
            "sort": "pub_date",
        },
    )
    return r.json().get("esearchresult", {}).get("idlist", [])


def fetch_details(ids):
    if not ids:
        return []
    r = ncbi_get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
        timeout=35,
    )
    return parse_xml(r.text)


def _t(el, path):
    n = el.find(path)
    return n.text.strip() if n is not None and n.text else ""


def _full(el, path):
    """取整個節點的文字（含 <i>、<sub> 等 inline 標籤）。
    PubMed 標題常有 <i>Escherichia coli</i> 這類斜體，用 .text 會被截斷。"""
    n = el.find(path)
    if n is None:
        return ""
    return "".join(n.itertext()).strip()


def parse_xml(xml_text):
    root = ET.fromstring(xml_text)
    out = []
    for art in root.findall(".//PubmedArticle"):
        pmid = _t(art, ".//PMID")
        title = _full(art, ".//ArticleTitle").rstrip(".")
        journal = _t(art, ".//Journal/ISOAbbreviation") or _t(art, ".//Journal/Title")
        journal_full = _t(art, ".//Journal/Title")
        issn = _t(art, ".//Journal/ISSN")
        issn_linking = _t(art, ".//MedlineJournalInfo/ISSNLinking")

        year = _t(art, ".//PubDate/Year") or _t(art, ".//Year")
        month = _t(art, ".//PubDate/Month")
        # 有些文章只有 <MedlineDate>2026 Jul-Aug</MedlineDate>，沒有分開的 Year/Month
        if not year:
            medline = _t(art, ".//PubDate/MedlineDate")
            if medline:
                bits = medline.split()
                year = bits[0] if bits else ""
                month = bits[1].split("-")[0] if len(bits) > 1 else ""

        # 摘要（保留 Structured Abstract 的段落標籤）
        parts = []
        for n in art.findall(".//AbstractText"):
            txt = "".join(n.itertext()).strip()
            if txt:
                label = n.get("Label")
                parts.append(f"{label}: {txt}" if label else txt)
        abstract = "\n".join(parts)

        # DOI
        doi = ""
        for aid in art.findall(".//ArticleId"):
            if aid.get("IdType") == "doi" and aid.text:
                doi = aid.text.strip()

        # 發表型態
        ptypes = [p.text for p in art.findall(".//PublicationType") if p.text]
        ptypes = [p for p in ptypes if p not in ("Journal Article", "English Abstract")]
        ptype = ", ".join(ptypes[:2]) or "Journal Article"

        # 作者（含 collective name，多中心試驗常見 study group）
        authors = []
        for a in art.findall(".//Author")[:6]:
            ln, fn = _t(a, "LastName"), _t(a, "ForeName")
            if ln:
                authors.append(f"{ln} {fn[0]}" if fn else ln)
            else:
                coll = _t(a, "CollectiveName")
                if coll:
                    authors.append(coll)

        if pmid and title and abstract:
            rec = {
                "pmid": pmid,
                "title": title,
                "journal": journal,
                "journal_full": journal_full,
                "issn": issn,
                "issn_linking": issn_linking,
            }
            out.append({
                **rec,
                "impact_factor": lookup_if(rec),
                "if_year": IF_YEAR,
                "year": year,
                "month": month,
                "doi": doi,
                "ptype": ptype,
                "ptype_list": ptypes,          # 排序用
                "authors": ", ".join(authors),
                "abstract": abstract,
            })
    return out


# ───────────────────────── Claude ──────────────────────────
def extract_text(body):
    """從 API 回應中取出文字內容。

    ⚠️ 不能假設 content[0] 就是文字！
    推理型模型（Sonnet 5、Opus 4.8 等）會先回一個 thinking 區塊：
        content[0] = {"type": "thinking", ...}   ← 沒有 "text" 鍵
        content[1] = {"type": "text", "text": "..."}
    所以要掃過所有區塊，只挑出 type == "text" 的。
    """
    if "output" in body or body.get("object") == "response":
        return response_text(body)
    blocks = body.get("content") or []
    parts = [
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    text = "".join(parts).strip()

    if not text:
        types = [b.get("type") for b in blocks if isinstance(b, dict)]
        stop = body.get("stop_reason")
        raise ValueError(
            f"回應中找不到 text 區塊（收到的區塊：{types or '空'}；stop_reason={stop}）"
        )
    return text


def clean_json(text):
    """從模型輸出中抽出 JSON。
    處理各種情況：```json 包裝、前言（Here is...）、後語（Hope this helps）。
    策略：先剝掉 code fence，再抓第一個 { 到最後一個 } 之間的內容。"""
    text = text.strip()
    # 剝掉 markdown code fence
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # 抓最外層的大括號區塊，去掉前後雜訊
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return text.strip()


def build_related_block(related):
    """把相關前作組成給模型看的區塊。沒有就明講沒有，避免它硬掰。"""
    if not related:
        return ("【你之前看過的相關論文】\n"
                "（沒有 —— 這是這個主題的第一篇。context_verdict 請填「無相關前作」）\n")
    lines = ["【你之前看過的相關論文】（相似度由高到低）"]
    for i, r in enumerate(related, 1):
        sc = f"評分 {r['score']}/10" if r.get("score") is not None else "評分 —"
        lines.append(f"{i}. [{r['date']}，{sc}] {r['title_zh']}")
        if r.get("one_liner"):
            lines.append(f"   {r['one_liner']}")
    lines.append("（若上面這些其實跟本篇無關，請誠實填「無相關前作」，不要硬掰）")
    return "\n".join(lines) + "\n"


def build_focus_block(paper):
    """命中追蹤時，要求模型用「你的研究角度」讀這篇，而不是泛泛而談。"""
    if not paper.get("watched") or not WATCH_FOCUS:
        return ""
    hits = "、".join(paper["watched"])
    lines = [
        "",
        "━━━━━━━━━━ ⚠️ 這篇命中你的追蹤主題 ━━━━━━━━━━",
        f"命中：{hits}",
        "",
        "請**特別**評估下列問題（這是使用者正在做的研究）：",
    ]
    lines += [f"  {i}. {f}" for i, f in enumerate(WATCH_FOCUS, 1)]
    lines += [
        "",
        "把這些評估寫進 key_numbers / cautions / action，不要另開欄位。",
        "若摘要無法回答某一項，就在 unassessable 裡指出來。",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]
    return "\n".join(lines)


def build_summary_prompt(paper, related=None):
    """建 prompt。同步與批次共用同一份，兩條路的分析結果才會一致。"""
    return PROMPT.format(
        title=paper["title"],
        journal=paper["journal"],
        ptype=paper["ptype"],
        abstract=paper["abstract"][:6000],
        related_block=build_related_block(related or []) + build_focus_block(paper),
    )


def parse_summary(body):
    """把一則 Messages 回應轉成乾淨欄位。

    同步回應與批次結果裡的 message 形狀完全相同，所以兩條路共用這個函式 ——
    批次不是「另一套解析」，只是換一個管道拿到同樣的東西。

    解析不出來就 raise，由呼叫端決定要不要重試。
    """
    usage = body.get("usage", {})

    # 若因 max_tokens 被截斷，JSON 一定不完整
    if body.get("stop_reason") == "max_tokens":
        raise ValueError("輸出被 max_tokens 截斷，JSON 不完整")

    text = extract_text(body)
    data = json.loads(clean_json(text))
    if not isinstance(data, dict):
        raise ValueError("摘要 JSON 必須是 object")

    if not data.get("abstract_zh"):
        raise ValueError("abstract_zh 為空")

    def as_list(v, cap=None):
        """模型偶爾把陣列回成字串，統一轉成陣列"""
        if isinstance(v, list):
            out = [str(x).strip() for x in v if str(x).strip()]
        elif isinstance(v, str):
            out = [p.strip(" ·-•　") for p in re.split(r"[\n；;]", v) if p.strip()]
        else:
            out = []
        return out[:cap] if cap else out

    def one_of(field, allowed):
        v = data.get(field)
        v = "" if v is None else (v.strip() if isinstance(v, str) else v)
        if not isinstance(v, str) or v not in allowed:
            # 不猜測臨床分類，也不把非法值寫進快取後才讓整日驗證失敗。
            # 同步呼叫端會重試；批次呼叫端會略過此篇摘要、保留其他結果。
            raise ValueError(f"{field} 不是合法值 → {v!r}")
        return v

    pico = data.get("pico")
    pico = pico if isinstance(pico, dict) else {}

    score = data.get("score")
    try:
        score = max(0, min(10, int(round(float(score)))))
    except (TypeError, ValueError):
        score = None

    clean = {
        "title_zh":       str(data.get("title_zh", "")).strip(),
        "abstract_zh":    str(data.get("abstract_zh", "")).strip(),
        "evidence_level": str(data.get("evidence_level", "")).strip(),

        "score":          score,
        "score_reason":   str(data.get("score_reason", "")).strip(),

        "relevance":      one_of("relevance",
                                 {"高度相關", "中度相關", "低度相關"}),
        "relevance_why":  str(data.get("relevance_why", "")).strip(),
        "novelty":        one_of("novelty",
                                 {"可能改變實務", "再確認已知", "仍屬早期", "結果為陰性"}),

        "one_liner":      str(data.get("one_liner", "")).strip(),
        "action":         str(data.get("action", "")).strip(),

        "pico":           {k: str(pico.get(k, "")).strip() for k in ("P", "I", "C", "O")},
        "effects":        compute_effects(data.get("effect_data")),
        "key_numbers":    as_list(data.get("key_numbers"), 4),
        "cautions":       as_list(data.get("cautions"), 3),
        "unassessable":   as_list(data.get("unassessable")),
        "context_verdict": one_of("context_verdict",
                                  {"再確認", "與前作矛盾", "延伸前作", "無相關前作", ""}),
        "context":        str(data.get("context", "")).strip(),
    }
    for field in ("title_zh", "abstract_zh", "evidence_level", "one_liner", "action"):
        if not isinstance(data.get(field), str) or not clean[field]:
            raise ValueError(f"{field} 為空或型別不符")
    if score is None or not clean["key_numbers"] or not clean["cautions"]:
        raise ValueError("score / key_numbers / cautions 不完整")
    if not any(clean["pico"].values()):
        raise ValueError("pico 四個欄位全空")
    if "output" in body or body.get("object") == "response":
        if (not isinstance(data.get("cautions"), list) or len(data["cautions"]) != 3
                or len(clean["cautions"]) != 3 or not all(clean["pico"].values())):
            raise ValueError("OpenAI 摘要須有三條非空 cautions 與完整 PICO")
        clean["summary_provider"] = "openai"
        clean["summary_model"] = body.get("model") or body.get("_summary_model") or MODEL
    else:
        clean["summary_provider"] = "anthropic"
        clean["summary_model"] = body.get("model") or body.get("_summary_model") or "claude-sonnet-5"
    clean["_usage"] = {
        "in": usage.get("input_tokens", 0),
        "out": usage.get("output_tokens", 0),
        "reasoning": (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
        "cached": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
        "cache_write": (usage.get("input_tokens_details") or {}).get("cache_write_tokens", 0),
    }
    clean["summary_usage"] = usage
    return clean


def summarize(paper, related=None):
    """同步（即時）路徑。批次關閉、或批次沒跑完要補的時候用。"""
    if not API_KEY:
        return None

    prompt = build_summary_prompt(paper, related)

    for attempt in range(1, MAX_RETRY + 1):
        body = None          # 讓 except 區塊能檢查實際收到什麼
        try:
            r = requests.post(
                ("https://api.openai.com/v1/responses" if PROVIDER == "openai" else
                 "https://api.anthropic.com/v1/messages"),
                headers=api_headers(),
                json=summary_request(prompt),
                timeout=90,
            )

            if r.status_code == 429:                     # rate limit
                wait = 5 * attempt
                log(f"      ⏳ 429 rate limit，{wait}s 後重試...")
                time.sleep(wait)
                continue

            if r.status_code == 404:
                log(f"      ❌ Model '{MODEL}' 不存在。請確認 model string。")
                return None

            if r.status_code in (401, 403):
                log("      ❌ API Key 無效或無權限。")
                return None

            r.raise_for_status()
            body = r.json()
            record_usage(body, batch=False)
            return parse_summary(body)

        except json.JSONDecodeError:
            # 模型輸出的 JSON 壞掉 —— 值得重試，下次可能就好了
            log(f"      ⚠️  模型回的 JSON 解析失敗（第 {attempt} 次），重試...")
            time.sleep(2)

        except (KeyError, TypeError, AttributeError, IndexError) as e:
            # 這是程式碼 bug，不是暫時性問題。重試只會白花錢 —— 直接放棄。
            log(f"      ❌ 程式錯誤（不重試，避免浪費錢）：{type(e).__name__}: {e}")
            if body is not None:
                shapes = [b.get("type") for b in (body.get("content") or []) if isinstance(b, dict)]
                log(f"         回應區塊：{shapes}  stop_reason={body.get('stop_reason')}")
            return None

        except requests.RequestException as e:
            # 網路問題 —— 值得重試
            log(f"      ⚠️  網路錯誤（第 {attempt} 次）：{e}")
            time.sleep(3 * attempt)

        except Exception as e:
            log(f"      ⚠️  {type(e).__name__}: {e}（第 {attempt} 次）")
            if body is not None and attempt == 1:
                shapes = [b.get("type") for b in (body.get("content") or []) if isinstance(b, dict)]
                log(f"         回應區塊：{shapes}  stop_reason={body.get('stop_reason')}")
            time.sleep(3 * attempt)

    log("      ❌ 重試用盡，此篇略過摘要")
    return None


# ─────────────────────── Batch API ───────────────────────
#
# 流程：送出 → 輪詢 → 取回 .jsonl → 依 custom_id 對回論文。
# 結果順序「不保證」跟送出順序一致，所以一律用 custom_id 比對，不能用索引。


def _api(method, url, provider=None, retries=True, **kw):
    """Retry only transient reads/uploads, never ambiguous paid batch creation."""
    attempts = MAX_RETRY if retries else 1
    for attempt in range(1, attempts + 1):
        try:
            r = requests.request(method, url,
                headers=api_headers(provider, multipart="files" in kw), timeout=120, **kw)
        except requests.RequestException:
            if attempt == attempts:
                raise
        else:
            if r.status_code != 429 and r.status_code < 500:
                # A 4xx is not transient. Do not catch and accidentally retry it.
                r.raise_for_status()
                return r
            if attempt == attempts:
                r.raise_for_status()
                raise RuntimeError(f"HTTP {r.status_code}")
        time.sleep(5 * attempt)
    raise RuntimeError("重試用盡")


def batch_submit(jobs, submission_id=None):
    """Upload JSONL then create an OpenAI Responses batch (24-hour window)."""
    if PROVIDER == "openai":
        lines = [json.dumps({"custom_id": cid, "method": "POST",
                  "url": "/v1/responses", "body": summary_request(prompt)},
                  ensure_ascii=False) for cid, prompt in jobs]
        if len({cid for cid, _ in jobs}) != len(jobs):
            raise ValueError("Duplicate batch custom_id")
        data = ("\n".join(lines) + "\n").encode("utf-8")
        file_id = _api("POST", "https://api.openai.com/v1/files",
            data={"purpose": "batch"}, files={"file": (
                f"pubmed-{submission_id or uuid.uuid4().hex}.jsonl", data,
                "application/jsonl")}).json()["id"]
        if submission_id:
            update_pending(input_file_id=file_id, input_sha256=hashlib.sha256(data).hexdigest(),
                           state="submitting")
        payload = {"input_file_id": file_id, "endpoint": "/v1/responses",
                   "completion_window": "24h",
                   "metadata": {"app": "pubmed-for-crs",
                                "submission_id": submission_id or uuid.uuid4().hex}}
        return _api("POST", "https://api.openai.com/v1/batches",
                    retries=False, json=payload).json()["id"]
    payload = {"requests": [{"custom_id": cid, "params": summary_request(prompt)}
                            for cid, prompt in jobs]}
    if submission_id:
        update_pending(state="submitting")
    return _api("POST", "https://api.anthropic.com/v1/messages/batches",
                retries=False, json=payload).json()["id"]


def batch_status(batch_id, provider=None):
    provider = provider or PROVIDER
    base = ("https://api.openai.com/v1/batches" if provider == "openai"
            else "https://api.anthropic.com/v1/messages/batches")
    return _api("GET", f"{base}/{batch_id}", provider=provider).json()


def batch_ended(status_obj, provider=None):
    provider = provider or PROVIDER
    if provider == "openai":
        return status_obj.get("status") in {"completed", "failed", "expired", "cancelled"}
    return status_obj.get("processing_status") == "ended"


def batch_wait(batch_id, max_wait_min=None, provider=None):
    """A wait timeout leaves the paid job pending; it is never resubmitted."""
    provider = provider or PROVIDER
    if max_wait_min is None:
        max_wait_min = BATCH_WAIT_MIN
    deadline = time.time() + max_wait_min * 60
    while True:
        st = batch_status(batch_id, provider)
        if batch_ended(st, provider):
            log(f"   批次結束：{st.get('status') or st.get('processing_status')} "
                f"{st.get('request_counts', {})}")
            return st
        if time.time() >= deadline:
            log("   ⏸️  批次仍在處理；已保存 batch_id，下次直接領回，不重送。")
            return None
        time.sleep(BATCH_POLL_SEC)


def batch_fetch(status_obj, provider=None):
    """Download success AND error files; join unordered results by custom_id."""
    provider = provider or PROVIDER
    if provider == "openai":
        urls = [f"https://api.openai.com/v1/files/{status_obj[k]}/content"
                for k in ("output_file_id", "error_file_id") if status_obj.get(k)]
    else:
        urls = [status_obj["results_url"]] if status_obj.get("results_url") else []
    out, bad = {}, {"errored": 0, "expired": 0, "_seen_ids": []}
    for url in urls:
        r = _api("GET", url, provider=provider)
        for line in r.text.splitlines():
            if not line.strip():
                continue
            # Malformed result files must retain pending state for recovery,
            # rather than silently discarding paid summaries.
            row = json.loads(line)
            cid = row.get("custom_id")
            if not cid:
                raise ValueError("Batch result missing custom_id")
            if cid in bad["_seen_ids"]:
                raise ValueError(f"Duplicate batch result: {cid}")
            bad["_seen_ids"].append(cid)
            if provider == "openai":
                res = row.get("response") or {}
                if not row.get("error") and res.get("status_code") == 200:
                    if cid in out:
                        raise ValueError(f"Duplicate batch result: {cid}")
                    out[cid] = res.get("body") or {}
                    continue
                err = row.get("error") or (res.get("body") or {}).get("error") or {}
                kind = "expired" if err.get("code") == "batch_expired" else "errored"
            else:
                res = row.get("result") or {}
                if res.get("type") == "succeeded":
                    out[cid] = res.get("message") or {}
                    continue
                kind = "expired" if res.get("type") == "expired" else "errored"
                err = (res.get("error") or {}).get("error") or {}
            bad[kind] += 1
            bad.setdefault("_errors", []).append(row)
            log(f"      ⚠️  {cid} {kind}：{err.get('code') or err.get('type', '')}")
    if status_obj.get("errors"):
        log(f"   ⚠️  批次驗證錯誤：{status_obj['errors']}")
    return out, bad


# ── 待領批次的存續 ──
# runner 會被銷毀，所以 batch_id 必須寫進 data/（會 commit）才活得過這次執行。


def pending_path():
    return DATA_DIR / PENDING_FILE


def save_pending(batch_id, date_str, papers, submission_id=None):
    """Persist provider/model too: a legacy Anthropic job stays Anthropic."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"batch_id": batch_id, "date": date_str, "submitted_at": stamp(),
               "provider": PROVIDER, "model": MODEL, "papers": papers,
               "submission_id": submission_id,
               "state": "submitted" if batch_id else "preparing"}
    _write_pending(payload)
    log(f"   📌 批次狀態已存檔（{batch_id or submission_id}）")


def _write_pending(payload):
    path = pending_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    # In Actions, commit BEFORE a paid create request and again after receiving
    # its ID. A runner timeout/cancellation cannot erase the only recovery key.
    if os.environ.get("BATCH_CHECKPOINT", "0") == "1":
        root = Path(__file__).resolve().parent
        subprocess.run(["bash", str(root / ".github/scripts/checkpoint_batch.sh")],
                       cwd=root, check=True)


def update_pending(**changes):
    pending = load_pending()
    if pending is None:
        raise RuntimeError("Pending batch journal disappeared")
    pending.update(changes)
    _write_pending(pending)


def load_pending():
    f = pending_path()
    if not f.exists():
        return None
    # A corrupt journal is a blocker, never permission to submit another job.
    pending = json.loads(f.read_text(encoding="utf-8"))
    if not isinstance(pending, dict):
        raise ValueError("Invalid pending batch journal")
    return pending


def recover_openai_submission(pending):
    """Find a created job after a lost POST response, without paid resubmission."""
    marker = pending.get("submission_id")
    if not marker or pending.get("state") != "submitting":
        return None
    matches, after = [], None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        page = _api("GET", "https://api.openai.com/v1/batches", provider="openai",
                    params=params).json()
        matches += [b["id"] for b in page.get("data", [])
                    if (b.get("metadata") or {}).get("submission_id") == marker]
        if not page.get("has_more"):
            break
        next_after = page.get("last_id")
        if not next_after or next_after == after:
            raise ValueError("Invalid batches pagination")
        after = next_after
    if len(matches) > 1:
        raise RuntimeError("Multiple batches have this submission_id; manual review required")
    return matches[0] if matches else None


def clear_pending():
    f = pending_path()
    if f.exists():
        f.unlink()


def merge_batch_results(papers, messages, cache, now):
    """把批次結果併回論文，回傳 (成功數, 失敗數, tok_in, tok_out)。"""
    ok = fail = tok_in = tok_out = 0
    for p in papers:
        body = messages.get(p["pmid"])
        if not body:
            fail += 1
            p.setdefault("added_at", now)
            continue
        usage = body.get("usage") or {}
        tok_in += usage.get("input_tokens", 0)
        tok_out += usage.get("output_tokens", 0)
        record_usage(body, batch=True)
        try:
            result = parse_summary(body)
        except Exception as e:
            log(f"      ⚠️  {p['pmid']} 解析失敗：{type(e).__name__}: {e}")
            fail += 1
            p.setdefault("added_at", now)
            continue
        usage = result.pop("_usage", {})
        result["added_at"] = now
        p.update(result)
        cache[p["pmid"]] = result
        ok += 1
    return ok, fail, tok_in, tok_out


def preserve_batch_results(papers, messages, bad, status_obj, provider=None):
    """Keep the latest raw result for auditing failed/refused/incomplete summaries."""
    path = DATA_DIR / "_batch_results.json"
    payload = {"batch_id": status_obj.get("id"), "provider": provider or PROVIDER,
               "retrieved_at": stamp(), "status": status_obj,
               "messages": messages, "failures": bad}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    expected = {p["pmid"] for p in papers}
    seen = set(bad.get("_seen_ids", []))
    # Validation failure can have no rows because no request was executed.
    if status_obj.get("status") == "cancelled":
        # OpenAI cancellation promises partial results, not rows for work that
        # never started. Reconcile the completed/failed counts before accepting.
        counts = status_obj.get("request_counts") or {}
        if (len(messages) != counts.get("completed", 0) or
                bad["errored"] + bad["expired"] != counts.get("failed", 0)):
            raise ValueError("Incomplete cancelled-batch results; retaining pending journal")
    elif status_obj.get("status") != "failed" and seen != expected:
        raise ValueError("Incomplete or mismatched batch results; retaining pending journal")
    if seen - expected:
        raise ValueError("Unexpected custom_id in batch results")


def merge_day(papers, date_str):
    """Recover only changed papers without dropping previously cached day rows."""
    path = DATA_DIR / f"{date_str}.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    combined = {p["pmid"]: p for p in existing.get("papers", [])}
    for p in papers:
        old = combined.get(p["pmid"], {})
        # A failed/retried batch must never downgrade a previously good paper.
        if old.get("title_zh") and old.get("abstract_zh") and not p.get("title_zh"):
            continue
        combined[p["pmid"]] = p
    save(list(combined.values()), date_str)


def summarize_batch(papers, date_str, cache, now):
    """把一批論文送去 Batch API 並等結果。

    回傳 (成功, 失敗, tok_in, tok_out, 是否還在跑)。
    最後那個是 True 時，代表批次尚未結束 —— 呼叫端**不可以**當成失敗去重送。
    """
    if pending_path().exists():
        raise RuntimeError("An unresolved batch already exists; refusing duplicate submission")
    jobs = [(p["pmid"], build_summary_prompt(p, p.get("related"))) for p in papers]

    log(f"   📤 送出批次：{len(jobs)} 篇（Batch API，同步價 5 折）")
    # Local request validation must fail before creating a recovery journal.
    for _, prompt in jobs:
        summary_request(prompt)
    submission_id = uuid.uuid4().hex
    save_pending(None, date_str, papers, submission_id)
    try:
        batch_id = batch_submit(jobs, submission_id)
    except requests.HTTPError as exc:
        # A definitive rejected request did not create a paid batch. Network
        # timeouts and 5xx remain ambiguous and MUST keep their journal.
        status = exc.response.status_code if exc.response is not None else 0
        if status in {400, 401, 403, 404, 413, 415, 422, 429}:
            clear_pending()
        raise
    update_pending(batch_id=batch_id, state="submitted")

    st = batch_wait(batch_id)
    if st is None:
        return 0, 0, 0, 0, True

    messages, _bad = batch_fetch(st)
    preserve_batch_results(papers, messages, _bad, st)
    ok, fail, ti, to = merge_batch_results(papers, messages, cache, now)
    # Persist good results before removing the only durable recovery handle.
    save_cache(cache)
    merge_day(papers, date_str)
    clear_pending()
    return ok, fail, ti, to, False


def resume_pending(cache):
    """接手上次沒領完的批次。回傳 True 表示這次有寫出資料。"""
    pend = load_pending()
    if not pend:
        return False

    batch_id = pend.get("batch_id")
    date_str = pend.get("date")
    papers   = pend.get("papers") or []
    provider = pend.get("provider", "anthropic")  # old journals were Anthropic
    if provider not in {"openai", "anthropic"} or not papers or not date_str:
        raise ValueError("Invalid pending batch metadata; keeping journal")
    if pend.get("state") == "preparing":
        log("   尚未送出付費批次；清除前置上傳中斷的存檔，下次可安全重試。")
        clear_pending()
        return False
    key = OPENAI_API_KEY if provider == "openai" else ANTHROPIC_API_KEY
    if not key:
        log(f"   ⚠️  待領批次需要 {provider} API key；保留存檔，不送新批次。")
        return False
    if not batch_id and provider == "openai":
        batch_id = recover_openai_submission(pend)
        if batch_id:
            update_pending(batch_id=batch_id, state="submitted")
    if not batch_id:
        log("   ⚠️  批次送出狀態未確認；保留存檔，請在供應商後台確認後再處理。")
        return False

    log(f"\n📥 有上次沒領完的批次：{batch_id}（{date_str}，{len(papers)} 篇）")

    try:
        st = batch_status(batch_id, provider)
    except Exception as e:
        log(f"   ⚠️  查不到批次狀態：{e}（保留存檔，下次再試）")
        return False

    if not batch_ended(st, provider):
        st = batch_wait(batch_id, provider=provider)
        if st is None:
            return False

    messages, _bad = batch_fetch(st, provider)
    preserve_batch_results(papers, messages, _bad, st, provider)
    for body in messages.values():
        body["_summary_model"] = pend.get("model", "claude-sonnet-5")
    now = stamp()
    usage_start = len(USAGE_LEDGER)
    ok, fail, ti, to = merge_batch_results(papers, messages, cache, now)
    log(f"   ✅ 領回 {ok} 篇（失敗 {fail} 篇）")

    if ok:
        merge_day(papers, date_str)
        save_cache(cache)
        estimate = sum(u["estimated_cost_usd"] for u in USAGE_LEDGER[usage_start:])
        log(f"   💰 這批估計費用約 ${estimate:.4f} USD（以帳單為準）")

    clear_pending()
    return bool(ok)


# ───────────────────────── 儲存 ────────────────────────────
def save(papers, date_str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    day_file = DATA_DIR / f"{date_str}.json"

    # 保護：如果今天已經有非空的資料，而這次跑出來是空的，
    # 不要覆蓋掉（避免補跑或抓取失敗時清空當天內容）
    if not papers and day_file.exists():
        try:
            existing = json.loads(day_file.read_text(encoding="utf-8"))
            if existing.get("papers"):
                log(f"   ⏭️  {date_str} 已有 {len(existing['papers'])} 篇，本次為空 → 保留原檔")
                return
        except Exception:
            pass

    payload = {
        "date": date_str,
        "generated_at": stamp(),
        "count": len(papers),
        "papers": papers,
    }
    day_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    rebuild_index()
    rebuild_search_index()


def rebuild_index():
    """index.json 是「衍生資料」，每次從實際的日檔完全重建。

    ⚠️ 原本是「增量維護」：讀舊的 → 加一天 → 寫回去。
       問題是只要 index.json 被覆蓋掉一次（例如上傳新版時被空檔蓋掉），
       前面所有日期就全部消失 —— 日檔明明還在，網站卻看不到。

       改成每次從 data/ 裡實際存在的日檔重建，就不可能不同步。
    """
    idx = []
    for f in sorted(DATA_DIR.glob("20*-*-*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        n = len(d.get("papers") or [])
        if n:                                   # 0 篇的日子不進日期選單
            idx.append({"date": d.get("date", f.stem), "count": n})

    idx.sort(key=lambda x: x["date"], reverse=True)
    (DATA_DIR / "index.json").write_text(
        json.dumps(idx[:120], ensure_ascii=False, indent=1), encoding="utf-8")

    log(f"   ✅ data/index.json（{len(idx)} 天 · {sum(x['count'] for x in idx)} 篇）")
    return idx


def rebuild_search_index():
    """把所有日檔壓成一份輕量索引，讓網站能全站搜尋 / 顯示收藏 / 算未讀。

    欄位刻意用單字母縮寫，因為這檔案會被手機下載。
    """
    items = []
    for f in sorted(DATA_DIR.glob("20*-*-*.json"), reverse=True):
        try:
            day = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for p in day.get("papers", []):
            if not p.get("pmid"):
                continue
            blob = " ".join(filter(None, [
                p.get("title_zh"), p.get("title"), p.get("journal"),
                p.get("one_liner"), p.get("action"),
                " ".join(p.get("key_numbers") or []),
                " ".join(p.get("cautions") or []),
                (p.get("abstract_zh") or "")[:400],
            ])).lower()
            items.append({
                "p": p["pmid"],
                "d": day.get("date", ""),
                "t": p.get("title_zh") or p.get("title", ""),
                "j": p.get("journal", ""),
                "f": p.get("impact_factor"),
                "s": p.get("score"),
                "w": p.get("watched") or [],
                "a": p.get("added_at", ""),
                "x": blob,
            })

    (DATA_DIR / "search.json").write_text(
        json.dumps(items, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")

    kb = (DATA_DIR / "search.json").stat().st_size / 1024
    log(f"   ✅ data/search.json（{len(items)} 篇可搜尋，{kb:.0f} KB）")


# ───────────────────────── 主程式 ──────────────────────────
def main():
    USAGE_LEDGER.clear()
    today = today_tw().strftime("%Y-%m-%d")
    log("=" * 56)
    log(f"  腸嚐新知 · 每日文獻收集   {today}")
    log("=" * 56)

    if not API_KEY:
        log("⚠️  未偵測到 API key —— 只會存原文，不產生中文摘要")

    if PROVIDER == "anthropic" and not OPENAI_API_KEY:
        log("⚠️  尚未設定 OPENAI_API_KEY，暫用原 Anthropic 路徑；設定後新摘要自動使用 Sol。")

    # 先修補舊資料 + 重建索引（在任何 return 之前）
    # 索引重建放這裡，是為了「就算今天一篇新論文都沒有，
    # 被覆蓋掉的 index.json 也會被修回來」。
    heal_legacy()
    rebuild_index()

    # 先接手上次沒領完的批次。
    # 那些結果**已經付過錢**了，不領回來就是純粹的損失 —— 而且
    # 若不先清掉 pending，下面又會送一批新的，等於同一批論文付兩次。
    if pending_path().exists():
        try:
            resume_pending(load_cache())
        except Exception as e:
            log(f"⚠️  領取待處理批次時出錯：{type(e).__name__}: {e}")
            log("   存檔保留，下次再試。")
        if pending_path().exists():
            log("⏸️  待領批次尚未解決，這次不送新摘要，避免重複付費。")
            return

    log(f"\n🔍 搜尋 PubMed（最近 {DAYS_BACK} 天新上架）...")
    ids = search_pubmed()
    log(f"   命中 {len(ids)} 篇")

    if not ids:
        log("   本期無新論文")
        save([], today)
        return

    log("\n⬇️  下載詳細資料（免費）...")
    papers = fetch_details(ids)
    log(f"   有摘要可用：{len(papers)} 篇")

    # ── 選片：追蹤命中 + 高分 + Review ──
    total = len(papers)
    papers, note = select(papers)

    # 🛑 每日硬上限 —— 防止追蹤關鍵字設太寬導致暴量燒錢
    if len(papers) > DAILY_HARD_LIMIT:
        log(f"\n🛑 選出 {len(papers)} 篇，超過每日硬上限 {DAILY_HARD_LIMIT} 篇。")
        log("   常見原因：追蹤關鍵字設太寬，或當天 Review 暴量。")
        watched_ps = [p for p in papers if p.get("watched")]
        rest       = [p for p in papers if not p.get("watched")]
        room       = max(0, DAILY_HARD_LIMIT - len(watched_ps))
        papers = sorted(watched_ps[:DAILY_HARD_LIMIT] + rest[:room],
                        key=rank_score, reverse=True)
        log(f"   → 保留 {len(papers)} 篇（🔖 追蹤命中優先保留，不會被砍）")
        note += f"（已套用硬上限 {DAILY_HARD_LIMIT}）"

    log(f"\n🏅 選片：{note}（掃了 {total} 篇）")
    for p in papers:
        jif = p.get("impact_factor")
        jtag = f"IF {jif}" if jif else "IF ?"
        mk = "🔖" if p.get("watched") else ("📖" if is_review(p) else "  ")
        log(f"   {rank_score(p):5.1f} {mk} [{jtag:>8}] {p['ptype'][:26]:<26} {p['title'][:38]}")
    if total > len(papers):
        log(f"   （捨棄 {total - len(papers)} 篇）")

    cache = load_cache()
    tok_in = tok_out = 0
    n_cached = n_new = n_fail = 0

    # ── 把過去所有論文讀成語料庫，用來找相關前作 ──
    log("\n📚 建立語料庫（過去收集的所有論文）...")
    corpus = build_corpus()
    log(f"   {len(corpus[0])} 篇可比對")

    log(f"\n🤖 AI 處理中（model: {MODEL}"
        + ("，Batch API 5 折" if USE_BATCH else "，同步") + "）...")
    now = stamp()

    # ── 第一輪：相關前作 + 快取（都不花錢）──
    # 先把能免費解決的解決掉，剩下的才送模型。
    todo = []
    for i, p in enumerate(papers, 1):
        short = p["title"][:52] + ("…" if len(p["title"]) > 52 else "")
        pmid  = p["pmid"]

        related = find_related(p, corpus)      # 本機算，免費
        p["related"] = related
        rel = f"  ↔ {len(related)} 篇相關" if related else ""

        if pmid in cache:
            p.update(cache[pmid])
            p["related"]  = related            # 相關前作永遠用最新算的
            p["added_at"] = cache[pmid].get("added_at") or now
            n_cached += 1
            log(f"  [{i:2d}/{len(papers)}] 💾 快取  {short}{rel}")
        else:
            todo.append(p)
            log(f"  [{i:2d}/{len(papers)}] 📝 待分析 {short}{rel}")

    # ── 第二輪：真正要花錢的那些 ──
    still_running = False

    if not todo:
        log("\n   全部命中快取，不需要呼叫 API。")

    elif USE_BATCH and API_KEY:
        ok, fail, ti, to, still_running = summarize_batch(todo, today, cache, now)
        n_new  += ok
        n_fail += fail
        tok_in += ti
        tok_out += to

        if still_running:
            # 批次還在跑。已經付費，結果之後領。
            # 這次仍然把「已知的部分」存檔 —— 快取命中的論文今天就看得到，
            # 待分析的那些會在下次 workflow 補上摘要。
            for p in todo:
                p.setdefault("added_at", now)
            log("\n💾 先存已完成的部分（批次結果下次補上）...")
            merge_day(papers, today)
            save_cache(cache)
            log("\n" + "═" * 56)
            log(f"  批次處理中 —— {len(todo)} 篇的摘要會在下次執行時補齊")
            log("  可手動再跑一次 workflow 提前領取")
            log("═" * 56 + "\n")
            return

    else:
        # 同步路徑（USE_BATCH=0，或沒有 API key）
        for i, p in enumerate(todo, 1):
            short = p["title"][:52] + ("…" if len(p["title"]) > 52 else "")
            log(f"  [{i:2d}/{len(todo)}] 🧠 生成  {short}")
            result = summarize(p, p.get("related"))
            if result:
                usage = result.pop("_usage", {})
                tok_in  += usage.get("in", 0)
                tok_out += usage.get("out", 0)
                result["added_at"] = now
                p.update(result)
                cache[p["pmid"]] = result
                n_new += 1
            else:
                n_fail += 1
            p.setdefault("added_at", now)
            if i < len(todo):
                time.sleep(0.4)

    for p in papers:
        p.setdefault("added_at", now)         # 摘要失敗也要有時間

    save_cache(cache)

    log("\n💾 儲存結果...")
    merge_day(papers, today)

    p_in, p_out = price_of(MODEL)
    cost = sum(u["estimated_cost_usd"] for u in USAGE_LEDGER)

    n_watch = sum(1 for p in papers if p.get("watched"))
    n_rev   = sum(1 for p in papers if is_review(p))
    n_eff   = sum(1 for p in papers if p.get("effects"))

    log("\n" + "═" * 56)
    log("  今日結算")
    log("─" * 56)
    log(f"  PubMed 掃描        {total} 篇")
    log(f"  送 AI 分析      {len(papers)} 篇")
    log(f"    🔖 追蹤命中       {n_watch} 篇")
    log(f"    📖 Review        {n_rev} 篇")
    log(f"  快取命中           {n_cached} 篇（不收費）")
    log(f"  實際新分析         {n_new} 篇")
    log(f"  失敗              {n_fail} 篇")
    log(f"  可驗算 ARR/NNT     {n_eff} 篇")
    log("─" * 56)
    log(f"  Token   {tok_in:,} in / {tok_out:,} out")
    log(f"  model   {MODEL}  (${p_in}/${p_out} per MTok)")
    log(f"  本次領回 / 同步回應估計費用  約 ${cost:.4f} USD（含推理；以帳單為準）")
    log("═" * 56)
    log("\n🎉 完成\n")


if __name__ == "__main__":
    main()
