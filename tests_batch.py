"""Batch API 路徑的測試 —— 用假的 HTTP 層，不花錢也不需要 API key。

批次的失敗模式很貴：重複付費、結果拿不回來。
每一條測試都對應一個「真的會賠錢」的情境。
"""
import importlib.util, json, os, shutil, sys, tempfile
from pathlib import Path

os.environ["ANTHROPIC_API_KEY"] = "sk-test-fake"
os.environ["USE_BATCH"] = "1"
os.environ["BATCH_WAIT_MIN"] = "1"

spec = importlib.util.spec_from_file_location("fp", Path(__file__).parent / "fetch_papers.py")
fp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fp)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   → {detail}" if not cond and detail else ""))


# ─────────── 假的 Anthropic API ───────────
GOOD = {
    "title_zh": "中文標題", "abstract_zh": "中文摘要內容", "evidence_level": "中度",
    "score": 7, "score_reason": "理由", "relevance": "高度相關", "relevance_why": "因為",
    "novelty": "再確認已知", "one_liner": "一句話", "action": "行動",
    "pico": {"P": "p", "I": "i", "C": "c", "O": "o"},
    "key_numbers": ["數據"], "cautions": ["小心"], "unassessable": [],
    "context_verdict": "無相關前作", "context": "說明",
}


class FakeAPI:
    """記錄每一次呼叫，讓測試能斷言「送出幾次」—— 重複送出 = 重複付費。"""

    def __init__(self, poll_until_ended=1, bad_ids=()):
        self.submits, self.polls, self.fetches = 0, 0, 0
        self.poll_until_ended = poll_until_ended
        self.bad_ids = set(bad_ids)
        self.last_jobs = []

    def __call__(self, method, url, **kw):
        class R:
            status_code = 200

            def raise_for_status(self):
                pass

        r = R()
        if method == "POST" and url.endswith("/batches"):
            self.submits += 1
            self.last_jobs = kw["json"]["requests"]
            r.json = lambda: {"id": "msgbatch_test01", "processing_status": "in_progress"}
        elif method == "GET" and "/batches/" in url:
            self.polls += 1
            ended = self.polls >= self.poll_until_ended
            r.json = lambda: {
                "id": "msgbatch_test01",
                "processing_status": "ended" if ended else "in_progress",
                "request_counts": {"succeeded": len(self.last_jobs), "errored": 0,
                                   "expired": 0, "canceled": 0, "processing": 0},
                "results_url": "https://fake/results.jsonl" if ended else None,
            }
        elif method == "GET" and "results" in url:
            self.fetches += 1
            lines = []
            for j in self.last_jobs:
                cid = j["custom_id"]
                if cid in self.bad_ids:
                    lines.append(json.dumps({"custom_id": cid, "result": {
                        "type": "expired", "error": {"error": {"type": "expired", "message": "x"}}}}))
                else:
                    lines.append(json.dumps({"custom_id": cid, "result": {
                        "type": "succeeded",
                        "message": {"content": [{"type": "text", "text": json.dumps(GOOD)}],
                                    "stop_reason": "end_turn",
                                    "usage": {"input_tokens": 2000, "output_tokens": 2600}}}}))
            r.text = "\n".join(lines)
        else:
            raise AssertionError(f"未預期的呼叫 {method} {url}")
        return r


def mk(pmid, title="Paper"):
    return {"pmid": pmid, "title": title, "journal": "Ann Coloproctol",
            "ptype": "Journal Article", "abstract": "abstract text", "related": []}


def with_tmp_data(fn):
    tmp = Path(tempfile.mkdtemp())
    old = fp.DATA_DIR
    fp.DATA_DIR = tmp
    try:
        fn(tmp)
    finally:
        fp.DATA_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


print("\n── Batch API ──")

# 1. 定價確實是 5 折
sync = fp.price_of("claude-sonnet-5", batch=False)
batch = fp.price_of("claude-sonnet-5", batch=True)
check("Batch 價格是同步價的 5 折", batch == (sync[0] / 2, sync[1] / 2), f"{sync} → {batch}")
check("預設（USE_BATCH=1）回報批次價", fp.price_of("claude-sonnet-5") == batch)

# 2. 送出的 payload 形狀正確
def t_payload(tmp):
    api = FakeAPI()
    fp.requests.request = api
    papers = [mk("40111111"), mk("40222222")]
    cache = {}
    fp.summarize_batch(papers, "2026-07-22", cache, fp.stamp())
    j = api.last_jobs
    check("送出一個批次就涵蓋所有論文", api.submits == 1 and len(j) == 2, f"submits={api.submits}")
    check("custom_id 用 PMID", {x["custom_id"] for x in j} == {"40111111", "40222222"})
    check("custom_id 符合 API 規則 ^[a-zA-Z0-9_-]{1,64}$",
          all(x["custom_id"].replace("-", "").replace("_", "").isalnum()
              and 1 <= len(x["custom_id"]) <= 64 for x in j))
    check("params 帶了 model / max_tokens / messages",
          all({"model", "max_tokens", "messages"} <= set(x["params"]) for x in j))
with_tmp_data(t_payload)

# 3. 結果依 custom_id 對回去 —— 順序不保證
def t_out_of_order(tmp):
    api = FakeAPI()
    fp.requests.request = api
    papers = [mk("40111111", "A"), mk("40222222", "B"), mk("40333333", "C")]
    cache = {}
    ok, fail, ti, to, running = fp.summarize_batch(papers, "2026-07-22", cache, fp.stamp())
    check("三篇全部拿到摘要", ok == 3 and fail == 0, f"ok={ok} fail={fail}")
    check("每篇都有中文摘要", all(p.get("abstract_zh") for p in papers))
    check("token 用量有累加", ti == 6000 and to == 7800, f"{ti}/{to}")
    check("結果寫進快取（下次不再付費）", set(cache) == {"40111111", "40222222", "40333333"})
    check("批次完成後 pending 檔已清除", not fp.pending_path().exists())
with_tmp_data(t_out_of_order)

# 4. 最貴的失敗模式：批次沒跑完 → 絕不可以重送
def t_pending(tmp):
    api = FakeAPI(poll_until_ended=999)      # 永遠不會結束
    fp.requests.request = api
    papers = [mk("40111111"), mk("40222222")]
    ok, fail, ti, to, running = fp.summarize_batch(papers, "2026-07-22", {}, fp.stamp())
    check("等不到就回報『仍在處理』（不是失敗）", running is True and fail == 0)
    check("batch_id 已存檔，能活過 runner 銷毀", fp.pending_path().exists())
    pend = json.loads(fp.pending_path().read_text(encoding="utf-8"))
    check("存檔含 batch_id 與待領論文",
          pend["batch_id"] == "msgbatch_test01" and len(pend["papers"]) == 2)

    # 下一次執行：必須「領回」而不是「重送」
    api2 = FakeAPI(poll_until_ended=1)
    api2.last_jobs = [{"custom_id": "40111111"}, {"custom_id": "40222222"}]
    fp.requests.request = api2
    cache = {}
    got = fp.resume_pending(cache)
    check("下次執行會領回結果", got is True)
    check("⚠️ 領取時沒有重新送出（不會付兩次錢）", api2.submits == 0, f"submits={api2.submits}")
    check("領回後快取有兩篇", len(cache) == 2, str(list(cache)))
    check("領完後 pending 檔清除", not fp.pending_path().exists())
with_tmp_data(t_pending)

# 5. 部分失敗：expired 的不收費，但不能污染其他篇
def t_partial(tmp):
    api = FakeAPI(bad_ids={"40222222"})
    fp.requests.request = api
    papers = [mk("40111111"), mk("40222222"), mk("40333333")]
    cache = {}
    ok, fail, ti, to, running = fp.summarize_batch(papers, "2026-07-22", cache, fp.stamp())
    check("一篇 expired 不影響其他篇", ok == 2 and fail == 1, f"ok={ok} fail={fail}")
    check("失敗那篇不進快取（下次會重試）", "40222222" not in cache)
    check("失敗那篇仍有 added_at（不會擋下驗證）",
          all(p.get("added_at") for p in papers))
with_tmp_data(t_partial)

# 6. pending 檔不可以被當成當日資料檔
def t_glob(tmp):
    fp.save_pending("msgbatch_x", "2026-07-22", [mk("40111111")])
    check("pending 檔不符合日檔的 glob（不會混進索引）",
          not list(tmp.glob("20*-*-*.json")) and fp.pending_path().exists())
with_tmp_data(t_glob)

# 7. 同步與批次共用同一套解析
body = {"content": [{"type": "text", "text": json.dumps(GOOD)}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 2}}
parsed = fp.parse_summary(body)
check("parse_summary 同步／批次共用，欄位一致",
      parsed["abstract_zh"] == "中文摘要內容" and parsed["score"] == 7)
check("thinking 區塊在前也抓得到 text",
      fp.parse_summary({"content": [{"type": "thinking", "thinking": "…"},
                                    {"type": "text", "text": json.dumps(GOOD)}],
                        "stop_reason": "end_turn", "usage": {}})["score"] == 7)

print("\n" + "═" * 56)
print(f"  通過 {len(PASS)} · 失敗 {len(FAIL)}")
print("═" * 56)
if FAIL:
    print("\n失敗的測試：")
    for f in FAIL:
        print(f"  ❌ {f}")
    sys.exit(1)
print("\n✅ 全部通過\n")
