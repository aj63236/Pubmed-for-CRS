#!/usr/bin/env python3
"""
腸嚐新知 — 每日自動文獻收集腳本
用法: python fetch_papers.py
需先設定環境變數 ANTHROPIC_API_KEY
"""

import os, json, time, datetime, requests, xml.etree.ElementTree as ET
from pathlib import Path

# ─────────────────────────────
# 設定區
# ─────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DAYS_BACK = 1          # 每天抓前一天
MAX_RESULTS = 10       # 最多幾篇
OUTPUT_DIR = Path(__file__).parent.parent / "data"

PUBMED_QUERY = (
    'colorectal surgery[MeSH Terms] OR "colorectal cancer"[tiab] '
    'OR "rectal surgery"[tiab] OR "colectomy"[tiab] '
    'OR "laparoscopic colorectal"[tiab] OR "robotic colorectal"[tiab] '
    'OR "total mesorectal excision"[tiab] OR "ileostomy"[tiab]'
)

SUMMARY_PROMPT = """你是大腸直腸外科資深醫師助理。請用繁體中文，針對以下論文用2-3句話做臨床重點摘要。
格式：先寫研究目的，再寫主要發現，最後寫臨床意義。語氣簡潔專業，直接給摘要不需前言。

論文標題：{title}
期刊：{journal}
原文摘要：{abstract}"""

# ─────────────────────────────
# PubMed 查詢
# ─────────────────────────────
def get_date_range():
    today = datetime.date.today()
    start = today - datetime.timedelta(days=DAYS_BACK)
    fmt = lambda d: d.strftime("%Y/%m/%d")
    return fmt(start), fmt(today)

def search_pubmed(query, min_date, max_date):
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    params = {
        "db": "pubmed", "term": query,
        "datetype": "pdat", "mindate": min_date, "maxdate": max_date,
        "retmax": MAX_RESULTS, "retmode": "json", "sort": "pub date"
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])

def fetch_details(ids):
    if not ids:
        return []
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    params = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return parse_xml(r.text)

def parse_xml(xml_text):
    root = ET.fromstring(xml_text)
    papers = []
    for art in root.findall(".//PubmedArticle"):
        def txt(sel):
            el = art.find(sel)
            return el.text.strip() if el is not None and el.text else ""

        pmid = txt(".//PMID")
        title = txt(".//ArticleTitle")
        journal = txt(".//Journal/Title") or txt(".//ISOAbbreviation")
        year = txt(".//PubDate/Year") or txt(".//Year") or ""
        month = txt(".//PubDate/Month") or ""

        abstract_parts = [el.text.strip() for el in art.findall(".//AbstractText") if el.text]
        abstract = " ".join(abstract_parts) or "（無摘要）"

        authors = []
        for a in art.findall(".//Author")[:4]:
            ln = txt_from(a, "LastName")
            fn = txt_from(a, "ForeName")
            if ln:
                authors.append(f"{ln} {fn[0]}" if fn else ln)

        if title and pmid:
            papers.append({
                "pmid": pmid, "title": title, "journal": journal,
                "year": year, "month": month,
                "abstract": abstract[:2000],
                "authors": ", ".join(authors),
            })
    return papers

def txt_from(el, tag):
    child = el.find(tag)
    return child.text.strip() if child is not None and child.text else ""

# ─────────────────────────────
# Claude 摘要
# ─────────────────────────────
def generate_summary(paper):
    if not ANTHROPIC_API_KEY:
        return "（請設定 ANTHROPIC_API_KEY 環境變數）"
    prompt = SUMMARY_PROMPT.format(
        title=paper["title"],
        journal=paper["journal"],
        abstract=paper["abstract"]
    )
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}]
    }
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers=headers, json=body, timeout=30)
        r.raise_for_status()
        return r.json()["content"][0]["text"]
    except Exception as e:
        return f"摘要產生失敗：{e}"

# ─────────────────────────────
# 儲存 JSON
# ─────────────────────────────
def save_to_json(papers, date_str):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 今日檔案
    day_file = OUTPUT_DIR / f"{date_str}.json"
    with open(day_file, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "papers": papers}, f, ensure_ascii=False, indent=2)
    print(f"✅ 已儲存：{day_file}")

    # 更新 index.json（給網站讀取）
    index_file = OUTPUT_DIR / "index.json"
    index = []
    if index_file.exists():
        with open(index_file, encoding="utf-8") as f:
            index = json.load(f)

    # 加入今日，去重，最多保留 90 天
    today_entry = {"date": date_str, "count": len(papers)}
    index = [x for x in index if x["date"] != date_str]
    index.insert(0, today_entry)
    index = index[:90]

    with open(index_file, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"✅ 已更新 index.json（共 {len(index)} 天）")

# ─────────────────────────────
# 主程式
# ─────────────────────────────
def main():
    today = datetime.date.today().strftime("%Y-%m-%d")
    print(f"\n{'='*50}")
    print(f"腸嚐新知 — 每日文獻收集  {today}")
    print(f"{'='*50}")

    min_date, max_date = get_date_range()
    print(f"🔍 搜尋日期：{min_date} ～ {max_date}")

    ids = search_pubmed(PUBMED_QUERY, min_date, max_date)
    print(f"📄 找到 {len(ids)} 篇論文")

    if not ids:
        print("本日無新論文，建議擴大搜尋天數")
        save_to_json([], today)
        return

    print("⬇️  取得詳細資料...")
    papers = fetch_details(ids)
    print(f"✅ 解析完成，{len(papers)} 篇")

    print("\n🤖 開始 AI 摘要生成...")
    for i, p in enumerate(papers):
        print(f"  [{i+1}/{len(papers)}] {p['title'][:60]}...")
        p["summary"] = generate_summary(p)
        p["id"] = f"{today}-{i+1}"
        time.sleep(0.5)  # 避免 rate limit

    save_to_json(papers, today)
    print(f"\n🎉 完成！今日共 {len(papers)} 篇文獻已儲存")

if __name__ == "__main__":
    main()
