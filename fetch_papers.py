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

import os, re, json, math, time, datetime, requests
import xml.etree.ElementTree as ET
from pathlib import Path

# ─────────────────────────── 設定 ───────────────────────────
API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# 想換模型就改這裡，或設環境變數 MODEL
# claude-sonnet-5   ← 預設。8/31 前促銷價 $2/$10，比 Sonnet 4.6 便宜且更新
# claude-haiku-4-5-20251001  ← 便宜 3 倍，但批判性分析與 NNT 計算會變差
# claude-opus-4-8   ← 最強但貴一倍
MODEL       = os.environ.get("MODEL", "").strip() or "claude-sonnet-5"
DAYS_BACK   = 2      # 抓最近 N 天新上架的論文
SCAN_LIMIT   = 120   # 每天最多「掃描」幾篇（跟 PubMed 拿資料是免費的）
MAX_RESULTS  = 10    # 每天取幾篇「高分文章」（付費）
INCLUDE_REVIEWS = True   # 除了高分文章，是否額外納入當天所有 Review
REVIEW_LIMIT = 8     # Review 最多幾篇（避免某天暴量燒錢）
MAX_RETRY   = 3

# 每 1M token 單價（USD）。價格會變，以 Anthropic 官網為準。
PRICING = {
    "claude-sonnet-5":   (2.00, 10.00),   # 8/31 前促銷；9/1 起變 (3, 15)
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8":   (5.00, 25.00),
    "claude-haiku-4-5":  (1.00,  5.00),
}


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


def price_of(model):
    for k, v in PRICING.items():
        if model.startswith(k):
            return v
    return (3.00, 15.00)   # 未知模型，用旗艦價估，寧可高估


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
        )
    except Exception:
        return [], [], 0


WATCH_KW, WATCH_JOURNALS, WATCH_LIMIT = load_watch()


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

  "unassessable": ["因摘要資訊不足而無法評估的項目。例如：盲法、allocation concealment、ITT、失訪率、試驗註冊、利益衝突、成本分析"],

  "context_verdict": "擇一：再確認 / 與前作矛盾 / 延伸前作 / 無相關前作",
  "context": "2-3 句話，說明這篇跟【下面列出的、你之前看過的論文】是什麼關係。⚠️ 若下面沒有列出任何論文，或列出的其實跟這篇無關，就把 context_verdict 設為「無相關前作」，context 寫「無相關前作」，**絕對不要硬掰關聯**。若真的相關：一致 → 說再確認了什麼；矛盾 → 明確指出哪裡矛盾、哪一篇證據等級較高、該信哪個；延伸 → 說補上了什麼缺口。"
}}

{related_block}
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
    today = today_tw()
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
            "retmax": SCAN_LIMIT,
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


def summarize(paper, related=None):
    if not API_KEY:
        return None

    prompt = PROMPT.format(
        title=paper["title"],
        journal=paper["journal"],
        ptype=paper["ptype"],
        abstract=paper["abstract"][:6000],
        related_block=build_related_block(related or []),
    )

    for attempt in range(1, MAX_RETRY + 1):
        body = None          # 讓 except 區塊能檢查實際收到什麼
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
                    "max_tokens": 8000,
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
            usage = body.get("usage", {})

            # 若因 max_tokens 被截斷，JSON 一定不完整 —— 直接重試（會加大額度）
            if body.get("stop_reason") == "max_tokens":
                raise ValueError("輸出被 max_tokens 截斷，JSON 不完整")

            text = extract_text(body)

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
                "context_verdict": one_of(data.get("context_verdict"),
                                          {"再確認", "與前作矛盾", "延伸前作", "無相關前作"}),
                "context":        str(data.get("context", "")).strip(),
            }
            clean["_usage"] = {
                "in": usage.get("input_tokens", 0),
                "out": usage.get("output_tokens", 0),
            }
            return clean

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

    rebuild_search_index()


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
    today = today_tw().strftime("%Y-%m-%d")
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

    log("\n⬇️  下載詳細資料（免費）...")
    papers = fetch_details(ids)
    log(f"   有摘要可用：{len(papers)} 篇")

    # ── 選片：高分文章 + 所有 Review ──
    total = len(papers)
    papers, note = select(papers)
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

    log(f"\n🤖 AI 處理中（model: {MODEL}）...")
    now = stamp()
    for i, p in enumerate(papers, 1):
        short = p["title"][:52] + ("…" if len(p["title"]) > 52 else "")
        pmid = p["pmid"]

        # 相關前作（本機算，免費）
        related = find_related(p, corpus)
        p["related"] = related

        if pmid in cache:
            p.update(cache[pmid])
            p["related"] = related            # 相關前作永遠用最新算的
            p["added_at"] = cache[pmid].get("added_at") or now
            n_cached += 1
            rel = f"  ↔ {len(related)} 篇相關" if related else ""
            log(f"  [{i:2d}/{len(papers)}] 💾 快取  {short}{rel}")
            continue

        rel = f"  ↔ {len(related)} 篇相關" if related else ""
        log(f"  [{i:2d}/{len(papers)}] 🧠 生成  {short}{rel}")
        result = summarize(p, related)

        if result:
            usage = result.pop("_usage", {})
            tok_in += usage.get("in", 0)
            tok_out += usage.get("out", 0)
            result["added_at"] = now          # 第一次收錄的時間，寫進快取
            p.update(result)
            cache[pmid] = result
            n_new += 1
        else:
            n_fail += 1

        p.setdefault("added_at", now)         # 摘要失敗也要有時間

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
