#!/usr/bin/env python3
"""
腸嚐新知 — 每日 PubMed 文獻收集腳本 v3
只負責抓 PubMed 資料並儲存，Claude 摘要改由瀏覽器即時產生
"""

import json, time, datetime, requests, xml.etree.ElementTree as ET
from pathlib import Path

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
        ) or ""
        authors = []
        for a in art.findall(".//Author")[:5]:
            ln = t(a, "LastName"); fn = t(a, "ForeName")
            if ln: authors.append(f"{ln} {fn[0]}" if fn else ln)
        if title and pmid:
            papers.append({
                "id": f"{datetime.date.today().strftime('%Y-%m-%d')}-{len(papers)+1}",
                "pmid": pmid, "title": title, "journal": journal,
                "year": year, "month": month,
                "abstract": abstract,
                "authors": ", ".join(authors)
            })
    return papers

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
    with open(index_file, "w", encoding="utf-8") as f:
        json.dump(index[:90], f, ensure_ascii=False, indent=2)

    print(f"✅ 完成：{date_str}，共 {len(papers)} 篇")

def main():
    today = datetime.date.today().strftime("%Y-%m-%d")
    print(f"\n腸嚐新知 每日文獻收集  {today}")
    min_d, max_d = get_date_range()
    print(f"搜尋：{min_d} ～ {max_d}")
    ids = search_pubmed(min_d, max_d)
    print(f"找到 {len(ids)} 篇")
    if not ids:
        save([], today); return
    papers = fetch_details(ids)
    print(f"解析完成，儲存中...")
    save(papers, today)

if __name__ == "__main__":
    main()
