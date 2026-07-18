#!/usr/bin/env python3
"""
腸嚐新知 — 回歸測試
===================
每一條測試都對應一個「真的發生過」的 bug。
這些 bug 全都是**靜默的** —— 不會報錯，只會給你錯的答案。

用法：python tests.py
"""

import importlib.util, json, sys
from pathlib import Path

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("fp", ROOT / "fetch_papers.py")
fp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fp)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"\n       {detail}" if not cond and detail else ""))


# ══════════════════════════════════════════════════════
print("\n【1】期刊 IF —— JCO 那個靜默 bug")
print("     正規化砍掉 journal/of/the，會讓 JCO(44.7) 被 Clinical Oncology(2.5) 蓋掉")
# ══════════════════════════════════════════════════════

def jif(issn=None, title=None, iso=None):
    return fp.lookup_if({"issn": issn, "issn_linking": issn,
                         "journal_full": title, "journal": iso})

jco = jif("0732-183X", "Journal of clinical oncology", "J Clin Oncol")
check("JCO 的 IF > 40（不能是 2.5）", jco is not None and jco > 40, f"實得 {jco}")

clin_onc = jif(None, "Clinical Oncology", "Clin Oncol")
check("Clinical Oncology 的 IF < 10（跟 JCO 不同本）",
      clin_onc is None or clin_onc < 10, f"實得 {clin_onc}")

gastro = jif("0016-5085", "Gastroenterology", "Gastroenterology")
check("Gastroenterology 的 IF > 20（不能是 5.7）",
      gastro is not None and gastro > 20, f"實得 {gastro}")

# ISSN 才配得到的（名稱在資料庫裡叫別的）
bjs = jif("0007-1323", "The British journal of surgery", "Br J Surg")
check("Br J Surg 靠 ISSN 配到（名稱是 BJS-British…，對不上）",
      bjs is not None and bjs > 5, f"實得 {bjs}")

surg_endo = jif("0930-2794", "Surgical endoscopy", "Surg Endosc")
check("Surg Endosc 靠 ISSN 配到", surg_endo is not None, f"實得 {surg_endo}")

for nm, issn, lo, hi in [("Ann Surg", "0003-4932", 3, 15),
                         ("Dis Colon Rectum", "0012-3706", 1, 8),
                         ("Gut", "0017-5749", 15, 40)]:
    v = jif(issn)
    check(f"{nm} 的 IF 落在合理範圍 {lo}–{hi}",
          v is not None and lo <= v <= hi, f"實得 {v}")


# ══════════════════════════════════════════════════════
print("\n【2】ARR / NNT —— 方向搞反是真正的臨床錯誤")
# ══════════════════════════════════════════════════════

def eff(**kw):
    return fp.compute_effects([kw])[0]

r = eff(outcome="復發", event_is_bad=True,
        intervention={"events": 10, "total": 243},
        control={"events": 20, "total": 243})
check("介入組壞事較少 → NNT（有益）",
      r["ok"] and r["kind"] == "NNT" and r["benefit"], str(r))

r = eff(outcome="洩漏", event_is_bad=True,
        intervention={"events": 25, "total": 200},
        control={"events": 12, "total": 200})
check("介入組壞事較多 → NNH（有害），不可標成 NNT",
      r["ok"] and r["kind"] == "NNH" and not r["benefit"], str(r))

r = eff(outcome="完全緩解", event_is_bad=False,
        intervention={"rate": 38}, control={"rate": 12})
check("事件是好事、介入組較多 → NNT（有益）",
      r["ok"] and r["kind"] == "NNT" and r["benefit"], str(r))

r = eff(outcome="無病存活", event_is_bad=True,
        intervention={"hr": 0.72}, control={})
check("只有 HR → 必須拒絕計算，不可硬算",
      not r["ok"], str(r))

r = eff(outcome="感染", event_is_bad=True,
        intervention={"events": 500, "total": 100},
        control={"events": 20, "total": 100})
check("髒資料（事件數 > 總人數）→ 必須拒絕", not r["ok"], str(r))

r = eff(outcome="存活", event_is_bad=True,
        intervention={"rate": 84.1}, control={"rate": 84.1})
check("兩組相同 → NNT 標為無意義", r["ok"] and r["nnt"] is None, str(r))

r = eff(outcome="復發", event_is_bad=True,
        intervention={"rate": 4.2}, control={"rate": 8.1})
check("ARR 算對（8.1% − 4.2% = 3.9%）", r["ok"] and abs(r["arr"] - 3.9) < 0.05, str(r))
check("NNT 算對（1/0.039 ≈ 26）", r["ok"] and r["nnt"] == 26, str(r))


# ══════════════════════════════════════════════════════
print("\n【3】追蹤命中 —— 低分也必須強制納入")
# ══════════════════════════════════════════════════════

pool = [{"pmid": f"hi{i}", "journal": "Lancet", "journal_full": "Lancet",
         "impact_factor": 98.4, "ptype_list": ["Randomized Controlled Trial"],
         "ptype": "RCT", "title": f"High score paper {i}",
         "abstract": "rectal cancer surgery trial"} for i in range(20)]

pour = {"pmid": "pour", "journal": "Ann Coloproctol", "journal_full": "Annals of Coloproctology",
        "impact_factor": 2.2, "ptype_list": [], "ptype": "—",
        "title": "Postoperative urinary retention after hemorrhoidectomy",
        "abstract": "Retrospective review of hemorrhoidectomy and urinary retention."}

sel, note = fp.select(pool + [pour])
pmids = {p["pmid"] for p in sel}
check("0 分的 POUR 論文被 20 篇 Lancet RCT 包圍，仍被選中",
      "pour" in pmids, f"選中 {len(sel)} 篇，note={note}")
check("它的分數確實很低（證明是靠追蹤救回來的）",
      fp.rank_score(pour) < 10, f"分數 {fp.rank_score(pour):.1f}")
check("它被標記為 watched", bool(pour.get("watched")), str(pour.get("watched")))


# ══════════════════════════════════════════════════════
print("\n【4】選片 —— 去重、Review、上限")
# ══════════════════════════════════════════════════════

rev = [{"pmid": f"rv{i}", "journal": "J", "journal_full": "J", "impact_factor": 2.0,
        "ptype_list": ["Review"], "ptype": "Review",
        "title": f"Review {i}", "abstract": "narrative review"} for i in range(12)]
sel2, _ = fp.select(pool + rev)
check("Review 有被額外撈進來（不跟高分文章搶名額）",
      sum(1 for p in sel2 if fp.is_review(p)) > 0)
check(f"Review 不超過上限 {fp.REVIEW_LIMIT}",
      sum(1 for p in sel2 if fp.is_review(p)) <= fp.REVIEW_LIMIT)
check("沒有重複的 PMID",
      len({p["pmid"] for p in sel2}) == len(sel2))
check(f"選片數不超過每日硬上限 {fp.DAILY_HARD_LIMIT} 的合理範圍",
      len(sel2) <= fp.MAX_RESULTS + fp.REVIEW_LIMIT + fp.WATCH_LIMIT)


# ══════════════════════════════════════════════════════
print("\n【5】排序 —— 專科 RCT 要贏過大期刊綜述")
# ══════════════════════════════════════════════════════

dcr_rct = {"impact_factor": 3.1, "ptype_list": ["Randomized Controlled Trial"]}
lancet_review = {"impact_factor": 98.4, "ptype_list": ["Review"]}
check("Dis Colon Rectum 的 RCT > Lancet 的綜述",
      fp.rank_score(dcr_rct) > fp.rank_score(lancet_review),
      f"{fp.rank_score(dcr_rct):.1f} vs {fp.rank_score(lancet_review):.1f}")

lancet_rct = {"impact_factor": 98.4, "ptype_list": ["Randomized Controlled Trial"]}
check("但 Lancet 的 RCT 還是最高", fp.rank_score(lancet_rct) > fp.rank_score(dcr_rct))


# ══════════════════════════════════════════════════════
print("\n【6】PubMed 解析 —— 斜體標題 / 集體作者")
# ══════════════════════════════════════════════════════

XML = '''<?xml version="1.0"?><PubmedArticleSet><PubmedArticle><MedlineCitation>
<PMID>1</PMID><Article>
<Journal><ISSN IssnType="Print">0003-4932</ISSN>
<JournalIssue><PubDate><MedlineDate>2026 Jul-Aug</MedlineDate></PubDate></JournalIssue>
<Title>Annals of surgery</Title><ISOAbbreviation>Ann Surg</ISOAbbreviation></Journal>
<ArticleTitle>Effect of <i>Escherichia coli</i> on anastomotic healing</ArticleTitle>
<Abstract><AbstractText Label="METHODS">We randomized 200 patients.</AbstractText></Abstract>
<AuthorList><Author><LastName>Smith</LastName><ForeName>John</ForeName></Author>
<Author><CollectiveName>The COLOR III Study Group</CollectiveName></Author></AuthorList>
<PublicationTypeList><PublicationType>Randomized Controlled Trial</PublicationType></PublicationTypeList>
</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>'''

ps = fp.parse_xml(XML)
check("解析出 1 篇", len(ps) == 1)
if ps:
    p = ps[0]
    check("斜體標題不被截斷（要有 Escherichia coli）",
          "Escherichia coli" in p["title"], p["title"])
    check("集體作者沒被丟掉（多中心試驗常見）",
          "COLOR III" in p["authors"], p["authors"])
    check("MedlineDate 有解析出年份", p["year"] == "2026", p["year"])
    check("IF 有查到", p["impact_factor"] is not None, str(p["impact_factor"]))


# ══════════════════════════════════════════════════════
print("\n【7】模型回應 —— 推理模型會先吐 thinking 區塊")
# ══════════════════════════════════════════════════════

body = {"content": [{"type": "thinking", "thinking": "..."},
                    {"type": "text", "text": '{"a":1}'}], "stop_reason": "end_turn"}
try:
    t = fp.extract_text(body)
    check("thinking + text → 抓得到 text（不能假設 content[0]）", t == '{"a":1}', t)
except Exception as e:
    check("thinking + text → 抓得到 text", False, str(e))

for name, raw in [("```json 包裝", '```json\n{"a":1}\n```'),
                  ("前面有廢話",   'Here is the JSON:\n{"a":1}'),
                  ("後面有廢話",   '{"a":1}\n\nHope this helps!')]:
    try:
        json.loads(fp.clean_json(raw))
        check(f"JSON 清理：{name}", True)
    except Exception as e:
        check(f"JSON 清理：{name}", False, str(e))


# ══════════════════════════════════════════════════════
print("\n【8】相關文獻 —— 不准硬掰關聯")
# ══════════════════════════════════════════════════════

corpus_docs = [
    {"pmid": "a", "date": "2026-06-01", "title_zh": "TaTME 學習曲線", "score": 7, "one_liner": "",
     "toks": fp._tokens("Transanal total mesorectal excision learning curve 500 consecutive cases")},
    {"pmid": "b", "date": "2026-06-02", "title_zh": "痔瘡術後尿滯留", "score": 5, "one_liner": "",
     "toks": fp._tokens("Risk factors for postoperative urinary retention after hemorrhoidectomy")},
]
import math
N = len(corpus_docs)
df = {}
for d in corpus_docs:
    for t in set(d["toks"]):
        df[t] = df.get(t, 0) + 1
idf = {t: math.log((N + 1) / (c + 1)) + 1 for t, c in df.items()}
dflt = math.log(N + 1) + 1
for d in corpus_docs:
    d["vec"] = fp._vec(d["toks"], idf, dflt)
corpus = (corpus_docs, idf, dflt)

r = fp.find_related({"pmid": "x",
                     "title": "Tamsulosin to prevent urinary retention after haemorrhoidectomy",
                     "abstract": "randomised trial of tamsulosin for postoperative urinary retention"},
                    corpus)
check("POUR 新論文 → 找到 POUR 前作", any(x["pmid"] == "b" for x in r), str(r))
check("POUR 新論文 → 不會扯上 TaTME", not any(x["pmid"] == "a" for x in r), str(r))

r2 = fp.find_related({"pmid": "y",
                      "title": "Vitamin D supplementation and bone density in postmenopausal women",
                      "abstract": "randomised trial of vitamin D on bone mineral density osteoporosis"},
                     corpus)
check("完全無關的論文 → 必須回報「無相關前作」，不可硬掰", r2 == [], str(r2))


# ══════════════════════════════════════════════════════
print("\n【9】舊資料修補 —— 加必要欄位時必須同時寫遷移")
print("     added_at 是後來才加的。沒有遷移 → 129 篇好資料被驗證擋掉")
# ══════════════════════════════════════════════════════

import tempfile, shutil, datetime as _dt

tmp = Path(tempfile.mkdtemp())
_orig = fp.DATA_DIR
fp.DATA_DIR = tmp
try:
    (tmp / "2026-07-05.json").write_text(json.dumps({
        "date": "2026-07-05",
        "generated_at": "2026-07-05T22:03:11",       # 沒時區，其實是 UTC
        "count": 1,
        "papers": [{"pmid": "1", "title": "x", "journal": "J"}],   # 沒有 added_at
    }, ensure_ascii=False), encoding="utf-8")

    fixed = fp.heal_legacy()
    d = json.loads((tmp / "2026-07-05.json").read_text(encoding="utf-8"))
    added = d["papers"][0].get("added_at", "")

    check("舊論文被補上 added_at", fixed == 1 and bool(added), f"fixed={fixed} added={added!r}")
    check("added_at 帶時區（+08:00）", added.endswith("+08:00"), added)
    check("UTC 22:03 → 台灣隔天 06:03（時區換算正確）",
          added.startswith("2026-07-06T06:03"), added)
    check("generated_at 也被修正時區", d["generated_at"].endswith("+08:00"), d["generated_at"])

    # 冪等：再跑一次不應該再改動
    again = fp.heal_legacy()
    check("冪等（再跑一次不會重複修改）", again == 0, f"第二次又改了 {again} 篇")
finally:
    fp.DATA_DIR = _orig
    shutil.rmtree(tmp)


# ══════════════════════════════════════════════════════
print("\n【10】index.json —— 必須是「衍生資料」，不能增量維護")
print("      被覆蓋一次，前面所有日期就全消失（日檔還在，網站卻看不到）")
# ══════════════════════════════════════════════════════

tmp2 = Path(tempfile.mkdtemp())
_o = fp.DATA_DIR
fp.DATA_DIR = tmp2
try:
    for d, n in [("2026-07-05", 4), ("2026-07-06", 17), ("2026-07-13", 14)]:
        (tmp2 / f"{d}.json").write_text(json.dumps({
            "date": d, "count": n,
            "papers": [{"pmid": f"{d}-{i}", "title": "x",
                        "added_at": f"{d}T06:00:00+08:00"} for i in range(n)],
        }, ensure_ascii=False), encoding="utf-8")

    # 模擬索引被洗掉（例如上傳新版時被空檔覆蓋）
    (tmp2 / "index.json").write_text("[]", encoding="utf-8")

    idx = fp.rebuild_index()
    check("索引被清空後能從日檔完全重建", len(idx) == 3, f"重建出 {len(idx)} 天")
    check("篇數正確（4+17+14 = 35）",
          sum(x["count"] for x in idx) == 35, str(idx))
    check("日期由新到舊排序",
          [x["date"] for x in idx] == ["2026-07-13", "2026-07-06", "2026-07-05"], str(idx))

    # 0 篇的日子不該進日期選單
    (tmp2 / "2026-07-07.json").write_text(json.dumps(
        {"date": "2026-07-07", "count": 0, "papers": []}, ensure_ascii=False), encoding="utf-8")
    idx2 = fp.rebuild_index()
    check("0 篇的日子不進日期選單",
          not any(x["date"] == "2026-07-07" for x in idx2), str(idx2))
finally:
    fp.DATA_DIR = _o
    shutil.rmtree(tmp2)


# ══════════════════════════════════════════════════════
print("\n" + "═" * 56)
print(f"  通過 {len(PASS)} · 失敗 {len(FAIL)}")
print("═" * 56)
if FAIL:
    print("\n失敗的測試：")
    for f in FAIL:
        print(f"  ❌ {f}")
    sys.exit(1)
print("\n✅ 全部通過\n")
