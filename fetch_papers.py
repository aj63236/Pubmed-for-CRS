#!/usr/bin/env python3
"""
腸嚐新知 — 每日自動文獻收集腳本 v2
輸出三段結構：【中文摘要】【臨床重點】【臨床影響】
"""

import os, json, time, datetime, requests, xml.etree.ElementTree as ET
from pathlib import Path

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DAYS_BACK   = 1
MAX_RESULTS = 10
OUTPUT_DIR  = Path(__file__).parent / "data"

PUBMED_QUERY = (
    'colorectal surgery[MeSH Terms] OR "colorectal cancer"[tiab] '
    'OR "rectal surgery"[tiab] OR "colectomy"[tiab] '
    'OR "laparoscopic colorectal"[tiab] OR "robotic colorectal"[tiab] '
    'OR "total mesorectal excision"[tiab] OR "ileostomy"[tiab] '
    'OR "anastomotic leak"[tiab] OR "rectal cancer"[tiab]'
)

PROMPT = """你是大腸直腸外科的資深主治醫師。請針對以下論文，用繁體中文輸出三個段落，每段用指定標記開頭，不需要其他前言或標題。

【中文摘要】（2-3句）完整翻譯並說明研究設計、對象人數、主要方法。
【臨床重點】（2-3句）條列主要數據結果，包含具體數字、統計顯著性。
【臨床影響】（2-3句）此研究對臨床實務的意義、改變了什麼、對哪類患者最重要。

論文標題：{title}
期刊：{journal}
原文摘要：{abstract}"""

def get_date_range():
    today = datetime.date.today()
    start = today - datetime.timedelta(days=DAYS_BACK)
    fmt = lambda d: d.strftime("%Y/%m/%d")
    return fmt(start), fmt(today)

def search_pubmed(min_date, max_date):
    r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params={
        "db": "pubmed", "term": PUBMED_QUERY,
        "datetype": "pdat", "mindate": min_date, "maxdate": max_date,
        "retmax": MAX_RESULTS, "retmode": "json", "sort": "pub date"
    }, timeout=15)
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])

def fetch_details(ids):
    if not ids: return []
    r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", params={
        "db": "pubmed", "id": ",".join(ids), "retmode": "xml"
    }, timeout=20)
    r.raise_for_status()
    return parse_xml(r.text)

def t(el, sel):
    node = el.find(sel)
    return node.text.strip() if node is not None and node.text else ""

def parse_xml(xml_text):
    root = ET.fromstring(xml_text)
    papers = []
    for art in root.findall(".//PubmedArticle"):
        pmid    = t(art, ".//PMID")
        title   = t(art, ".//ArticleTitle")
        journal = t(art, ".//Journal/Title") or t(art, ".//ISOAbbreviation")
        year    = t(art, ".//PubDate/Year") or t(art, ".//Year") or ""
        month   = t(art, ".//PubDate/Month") or ""
        abstract = " ".join(
            n.text.strip() for n in art.findall(".//AbstractText") if n.text
        ) or "（無摘要）"
        authors = []
        for a in art.findall(".//Author")[:4]:
            ln = t(a, "LastName"); fn = t(a, "ForeName")
            if ln: authors.append(f"{ln} {fn[0]}" if fn else ln)
        if title and pmid:
            papers.append({"pmid": pmid, "title": title, "journal": journal,
                           "year": year, "month": month,
                           "abstract": abstract[:2000],
                           "authors": ", ".join(authors)})
    return papers

def generate_summary(paper):
    if not ANTHROPIC_API_KEY:
        return "（請設定 ANTHROPIC_API_KEY）"
    prompt = PROMPT.format(
        title=paper["title"], journal=paper["journal"],
        abstract=paper["abstract"]
    )
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json",
                     "x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 700,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40)
        r.raise_for_status()
        return r.json()["content"][0]["text"]
    except Exception as e:
        return f"摘要產生失敗：{e}"

def save(papers, date_str):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_DIR / f"{date_str}.json", "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "papers": papers}, f, ensure_ascii=False, indent=2)

    index_file = OUTPUT_DIR / "index.json"
    index = []
    if index_file.exists():
        with open(index_file, encoding="utf-8") as f:
            index = json.load(f)
    index = [x for x in index if x["date"] != date_str]
    index.insert(0, {"date": date_str, "count": len(papers)})
    index = index[:90]
    with open(index_file, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"✅ 儲存完成：{date_str}，共 {len(papers)} 篇")

def main():
    today = datetime.date.today().strftime("%Y-%m-%d")
    print(f"\n{'='*50}\n腸嚐新知 每日文獻收集  {today}\n{'='*50}")
    min_d, max_d = get_date_range()
    print(f"🔍 搜尋：{min_d} ～ {max_d}")
    ids = search_pubmed(min_d, max_d)
    print(f"📄 找到 {len(ids)} 篇")
    if not ids:
        save([], today); return
    papers = fetch_details(ids)
    for i, p in enumerate(papers):
        print(f"  [{i+1}/{len(papers)}] {p['title'][:55]}...")
        p["summary"] = generate_summary(p)
        p["id"] = f"{today}-{i+1}"
        time.sleep(0.5)
    save(papers, today)
    print(f"\n🎉 完成！")

if __name__ == "__main__":
    main()
