#!/usr/bin/env python3
"""
腸嚐新知 — 每日 PubMed 文獻收集 + AI 摘要
=========================================
1. 用 Entrez date (edat) 抓「最近幾天新上架 PubMed」的大腸直腸外科論文
2. 對每篇呼叫 Claude 產生：中文標題 / 完整中文摘要 / 臨床重點 / 臨床影響
3. 已處理過的 PMID 會存進 cache，不會重複計費
4. 輸出結構化 JSON 到 data/，網站直接讀取（零延遲）

環境變數：ANTHROPIC_API_KEY
"""

import os, re, json, time, datetime, requests
import xml.etree.ElementTree as ET
from pathlib import Path

# ─────────────────────────── 設定 ───────────────────────────
API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# 想換模型就改這裡，或設環境變數 MODEL
# claude-sonnet-5   ← 預設。8/31 前促銷價 $2/$10，比 Sonnet 4.6 便宜且更新
# claude-haiku-4-5-20251001  ← 便宜 3 倍，但批判性分析與 NNT 計算會變差
# claude-opus-4-8   ← 最強但貴一倍
MODEL       = os.environ.get("MODEL", "").strip() or "claude-sonnet-5"
DAYS_BACK   = 2                       # 抓最近 N 天新上架的論文（1 天常常是 0 篇）
MAX_RESULTS = 12                      # 每天最多處理幾篇
MAX_RETRY   = 3

# 每 1M token 單價（USD）。價格會變，以 Anthropic 官網為準。
PRICING = {
    "claude-sonnet-5":   (2.00, 10.00),   # 8/31 前促銷；9/1 起變 (3, 15)
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8":   (5.00, 25.00),
    "claude-haiku-4-5":  (1.00,  5.00),
}


def price_of(model):
    for k, v in PRICING.items():
        if model.startswith(k):
            return v
    return (3.00, 15.00)   # 未知模型，用旗艦價估，寧可高估


ROOT      = Path(__file__).parent
DATA_DIR  = ROOT / "data"
CACHE_FILE = DATA_DIR / "_cache_v3.json"  # pmid -> 已生成的分析（v3 schema）
JOURNALS_FILE = ROOT / "journals.json"  # 期刊 Impact Factor 對照表（可自行編輯）


def load_journals():
    """載入期刊 IF 對照表。查不到的期刊不顯示 IF，不影響其他功能。"""
    try:
        raw = json.loads(JOURNALS_FILE.read_text(encoding="utf-8"))
        return raw.get("impact_factors", {}), raw.get("_year", "")
    except Exception:
        return {}, ""


IF_TABLE, IF_YEAR = load_journals()


def lookup_if(journal):
    """用 PubMed 的 ISOAbbreviation 查 IF。正規化：小寫、去句點、去多餘空白。"""
    if not journal:
        return None
    key = journal.lower().replace(".", "").strip()
    key = re.sub(r"\s+", " ", key)
    return IF_TABLE.get(key)

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
NOT (case reports[Publication Type])
NOT ("animals"[MeSH Terms] NOT "humans"[MeSH Terms])
"""

PROMPT = """你是大腸直腸外科資深主治醫師，同時熟悉實證醫學（EBM）與論文批判性閱讀。

讀者是台灣醫學中心的大腸直腸外科主治／總醫師，早上花三分鐘滑手機看今日文獻。
你的任務**不是取代讀全文**，而是幫他決定：**這篇值不值得花時間去讀全文**。

請輸出**純 JSON**（不要 markdown、不要 ```、不要任何前後文字）。

⚠️ 最重要的規則：
你手上只有**摘要**。allocation concealment、盲法、ITT、失訪率、試驗註冊、利益衝突這些，摘要通常不會寫。
**絕對不可以憑空推測或編造。** 沒寫的就列進 unassessable，不要硬填。
醫師會拿這個做臨床判斷，寧可誠實說不知道，也不要猜。

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

  "unassessable": ["因摘要資訊不足而無法評估的項目。例如：盲法、allocation concealment、ITT、失訪率、試驗註冊、利益衝突、成本分析"]
}}

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
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 只保留最近 600 篇，避免無限膨脹
    if len(cache) > 600:
        cache = dict(list(cache.items())[-600:])
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# ───────────────────────── PubMed ──────────────────────────
def search_pubmed():
    """用 edat（PubMed 上架日）搜尋，比 pdat 更貼近『今天有什麼新文章』"""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=DAYS_BACK)
    fmt = lambda d: d.strftime("%Y/%m/%d")

    r = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={
            "db": "pubmed",
            "term": " ".join(PUBMED_QUERY.split()),
            "datetype": "edat",
            "mindate": fmt(start),
            "maxdate": fmt(today),
            "retmax": MAX_RESULTS,
            "retmode": "json",
            "sort": "pub_date",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


def fetch_details(ids):
    if not ids:
        return []
    r = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
        timeout=30,
    )
    r.raise_for_status()
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
            out.append({
                "pmid": pmid,
                "title": title,
                "journal": journal,
                "impact_factor": lookup_if(journal),
                "if_year": IF_YEAR,
                "year": year,
                "month": month,
                "doi": doi,
                "ptype": ptype,
                "authors": ", ".join(authors),
                "abstract": abstract,
            })
    return out


# ───────────────────────── Claude ──────────────────────────
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


def summarize(paper):
    if not API_KEY:
        return None

    prompt = PROMPT.format(
        title=paper["title"],
        journal=paper["journal"],
        ptype=paper["ptype"],
        abstract=paper["abstract"][:6000],
    )

    for attempt in range(1, MAX_RETRY + 1):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "content-type": "application/json",
                    "x-api-key": API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": MODEL,
                    "max_tokens": 3500,
                    "messages": [{"role": "user", "content": prompt}],
                },
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
            text = body["content"][0]["text"]
            usage = body.get("usage", {})

            data = json.loads(clean_json(text))

            # 基本欄位驗證
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

            def one_of(v, allowed, default=""):
                v = str(v or "").strip()
                return v if v in allowed else (v if v else default)

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

                "relevance":      one_of(data.get("relevance"),
                                         {"高度相關", "中度相關", "低度相關"}),
                "relevance_why":  str(data.get("relevance_why", "")).strip(),
                "novelty":        one_of(data.get("novelty"),
                                         {"可能改變實務", "再確認已知", "仍屬早期", "結果為陰性"}),

                "one_liner":      str(data.get("one_liner", "")).strip(),
                "action":         str(data.get("action", "")).strip(),

                "pico":           {k: str(pico.get(k, "")).strip() for k in ("P", "I", "C", "O")},
                "key_numbers":    as_list(data.get("key_numbers"), 4),
                "cautions":       as_list(data.get("cautions"), 3),
                "unassessable":   as_list(data.get("unassessable")),
            }
            clean["_usage"] = {
                "in": usage.get("input_tokens", 0),
                "out": usage.get("output_tokens", 0),
            }
            return clean

        except json.JSONDecodeError:
            log(f"      ⚠️  JSON 解析失敗（第 {attempt} 次），重試...")
            time.sleep(2)
        except Exception as e:
            log(f"      ⚠️  {type(e).__name__}: {e}（第 {attempt} 次）")
            time.sleep(3 * attempt)

    log("      ❌ 重試用盡，此篇略過摘要")
    return None


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
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "count": len(papers),
        "papers": papers,
    }
    day_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # index.json —— 只收錄有內容的日期，0 篇不進日期選單
    idx_file = DATA_DIR / "index.json"
    idx = []
    if idx_file.exists():
        try:
            idx = json.loads(idx_file.read_text(encoding="utf-8"))
        except Exception:
            idx = []

    idx = [x for x in idx if x.get("date") != date_str]
    if papers:
        idx.insert(0, {"date": date_str, "count": len(papers)})
    idx.sort(key=lambda x: x["date"], reverse=True)
    idx_file.write_text(
        json.dumps(idx[:120], ensure_ascii=False, indent=1), encoding="utf-8"
    )

    log(f"   ✅ data/{date_str}.json（{len(papers)} 篇）")
    log(f"   ✅ data/index.json（{len(idx)} 天有內容）")


# ───────────────────────── 主程式 ──────────────────────────
def main():
    today = datetime.date.today().strftime("%Y-%m-%d")
    log("=" * 56)
    log(f"  腸嚐新知 · 每日文獻收集   {today}")
    log("=" * 56)

    if not API_KEY:
        log("⚠️  未偵測到 ANTHROPIC_API_KEY —— 只會存原文，不產生中文摘要")

    log(f"\n🔍 搜尋 PubMed（最近 {DAYS_BACK} 天新上架）...")
    ids = search_pubmed()
    log(f"   命中 {len(ids)} 篇")

    if not ids:
        log("   本期無新論文")
        save([], today)
        return

    log("\n⬇️  下載詳細資料...")
    papers = fetch_details(ids)
    log(f"   有摘要可用：{len(papers)} 篇")

    cache = load_cache()
    tok_in = tok_out = 0
    n_cached = n_new = n_fail = 0

    log(f"\n🤖 AI 處理中（model: {MODEL}）...")
    for i, p in enumerate(papers, 1):
        short = p["title"][:52] + ("…" if len(p["title"]) > 52 else "")
        pmid = p["pmid"]

        if pmid in cache:
            p.update(cache[pmid])
            n_cached += 1
            log(f"  [{i:2d}/{len(papers)}] 💾 快取  {short}")
            continue

        log(f"  [{i:2d}/{len(papers)}] 🧠 生成  {short}")
        result = summarize(p)

        if result:
            usage = result.pop("_usage", {})
            tok_in += usage.get("in", 0)
            tok_out += usage.get("out", 0)
            p.update(result)
            cache[pmid] = result
            n_new += 1
        else:
            n_fail += 1

        if i < len(papers):
            time.sleep(0.4)

    save_cache(cache)

    log("\n💾 儲存結果...")
    save(papers, today)

    p_in, p_out = price_of(MODEL)
    cost = tok_in / 1e6 * p_in + tok_out / 1e6 * p_out
    log("\n" + "─" * 56)
    log(f"  新生成 {n_new} 篇 · 快取 {n_cached} 篇 · 失敗 {n_fail} 篇")
    log(f"  Token: {tok_in:,} in / {tok_out:,} out")
    log(f"  本次費用約 ${cost:.4f} USD")
    log("─" * 56)
    log("\n🎉 完成\n")


if __name__ == "__main__":
    main()
