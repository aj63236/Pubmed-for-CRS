# 腸嚐新知

每日自動抓 PubMed 大腸直腸外科新論文，用 Claude 產生中文摘要，發佈到網站。

**網站：** https://aj63236.github.io/Pubmed-for-CRS/

---

## 這一版的改動

- **拿掉日期選單** — 一路往下滾就會自動載入更早的文章，滾到底顯示「已經到最早的文獻了」。換日時第一篇會標「新的一天」。
- **加上 Impact Factor** — 顯示在期刊標籤上（例如 `Ann Surg · IF 10.1`）。IF ≥ 10 的期刊整顆標籤會亮起來。
- **可回補歷史文獻** — `backfill.py` 一次抓一整段日期。

---

## ⚠️ 關於 Impact Factor（請讀這段）

IF 是 **Clarivate JCR 的付費專有資料，沒有免費 API**。

我查證過，各家免費 IF 網站**數字互相矛盾** —— 同一本 *Diseases of Colon & Rectum*，有網站寫 1.87，有的寫 3.2。原因是有些網站把 Scopus 的 CiteScore 當成 IF 在報，那不是同一個東西。

所以 `journals.json` 裡的數字是**參考值，不是權威值**。

**請用三總的 Web of Science / JCR 帳號核對一次**，把數字改成正確的。五分鐘的事，改完就永遠正確，每年 6 月 JCR 更新時再回來改一次。

```json
"impact_factors": {
  "ann surg": 10.1,          ← 改成 JCR 上的正確數字
  "dis colon rectum": 3.2,
  ...
}
```

key 用小寫、去句點，對應 PubMed 的期刊縮寫（`Ann Surg`、`Dis Colon Rectum`）。查不到的期刊不顯示 IF，不影響其他功能。

---

## 回補歷史文獻（不用裝任何東西）

在 GitHub 網頁上點按鈕就能跑，不需要 Mac、不需要 Python、不需要 git。

### 步驟

**1. Actions → 回補歷史文獻 → Run workflow**

會跳出四個欄位：

| 欄位 | 填什麼 |
|---|---|
| 起始日 | `2026-06-01` |
| 結束日 | 留空 = 到今天 |
| 每天最多抓幾篇 | `8`（想省錢就填小一點） |
| **試算模式** | **第一次務必勾選 ✅** |

**2. 先跑試算（不花錢）**

勾著「試算模式」跑一次。跑完點進 log，最後會看到：

```
要處理的日期：38 天
要抓的論文：  260 篇
  ├─ 需生成： 260 篇
  └─ 已快取： 0 篇
預估費用：    約 $9.10 USD
```

**3. 覺得可以接受，再跑一次，這次把「試算模式」取消勾選**

這次會真的呼叫 Claude，跑完自動 commit 並推上去，網站就有資料了。

38 天 × 8 篇大概要跑 30-50 分鐘，可以關掉瀏覽器，Actions 會在雲端繼續跑。

### 安全機制

- **試算模式預設是勾選的** —— 不會不小心花到錢
- **總量上限 400 篇** —— 日期填錯（例如填成 2020 年）會直接停下來，不會燒錢
- **快取** —— 中途失敗可以直接重跑，已處理的論文不會重複收費
- **已完成的日期會跳過** —— 重跑不會重做

---

---

## 檔案

```
index.html                  網站（無限捲動）
manifest.json / sw.js       PWA
icons/                      App 圖示
journals.json               ⚠️ IF 對照表，請自行核對
fetch_papers.py             每日收集（GitHub Actions 用）
backfill.py                 回補歷史（本機手動跑）
.github/workflows/
  daily_fetch.yml           每天台灣時間 06:00 自動跑
data/
  index.json                日期索引
  _cache_v3.json            PMID 快取，避免重複付費
  YYYY-MM-DD.json           每日文獻
```

---

## 每日自動更新

已設定好，每天台灣時間 **早上 6:00** GitHub Actions 自動跑，不需要你的電腦開機。

手動觸發：Actions → 每日文獻收集 → Run workflow

---

## 裝到 iPhone

1. **Safari**（一定要 Safari）打開網站
2. 底部「分享」→「**加入主畫面**」

之後有 app 圖示、全螢幕、可離線看。

---

## 費用

- PubMed API：免費
- Claude：一篇約 $0.035
- 每天 12 篇 → 約 **$12/月**
- 每天 6 篇 → 約 **$6/月**（想省錢就把 `fetch_papers.py` 的 `MAX_RESULTS` 調小）
- 回補 6/1 至今（約 260 篇）→ 一次性約 **$9**

建議 console.anthropic.com → Billing 設 $20 月上限。

---

## 出問題時

**Actions 紅色 ✗** → 看 log。`404` = model 名稱錯；`401` = API Key 沒設好。

**網站沒更新** → 按右下角「更新」，或 Ctrl+Shift+R。

**IF 沒顯示** → 那本期刊不在 `journals.json` 裡，自己加進去即可。
