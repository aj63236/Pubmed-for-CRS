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

## 選片機制：不是「最新的 10 篇」，是「最值得讀的 10 篇」

PubMed 每天有 **60-80 篇**符合條件的大腸直腸相關論文。你不可能每天讀 80 篇，也不該讓系統隨便挑 10 篇給你。

所以流程是：

1. **掃描當天全部論文**（跟 PubMed 拿資料是**免費**的，抓 80 篇跟抓 10 篇一樣不用錢）
2. **本機排序**（也免費）：`分數 = 期刊 IF（上限 25，權重 0.5）+ 研究設計權重 × 2`
3. **只把分數最高的 10 篇送去 Claude**（這步才花錢）

### 為什麼這樣排

研究設計的權重刻意壓過期刊 IF —— 因為對外科醫師來說，**一篇 Dis Colon Rectum 的 RCT，比一篇 Lancet 的綜述有用**。

實測排序：

| 分數 | 期刊 | 設計 | |
|---|---|---|---|
| 36.5 | Lancet (IF 98) | RCT | ✅ |
| 29.1 | Ann Surg (IF 10) | RCT | ✅ |
| 28.3 | Br J Surg (IF 8.6) | Meta-Analysis | ✅ |
| **25.6** | **Dis Colon Rectum (IF 3.2)** | **RCT** | ✅ ← 勝過下面的 Lancet 綜述 |
| 25.2 | Int J Colorectal Dis (IF 2.5) | RCT | ✅ |
| 14.5 | Lancet (IF 98) | 綜述 | ✅ |
| 1.9 | Sci Rep | 相關性研究 | ❌ 捨棄 |
| 0.0 | 不明期刊 | 病例系列 | ❌ 捨棄 |

### 想調整

`fetch_papers.py` 開頭：

```python
SCAN_LIMIT  = 120   # 每天掃描幾篇（免費，可以放心設大）
MAX_RESULTS = 10    # 每天分析幾篇（⚠️ 這個才花錢）

PTYPE_WEIGHT = {           # 研究設計權重，想更重視 RCT 就調高
    "Randomized Controlled Trial": 12,
    "Meta-Analysis":               12,
    ...
}
IF_CAP = 25                # 期刊 IF 上限，避免大期刊的綜述蓋過專科 RCT
```

⚠️ **排序靠的是 `journals.json` 的 IF 表。表裡沒有的期刊 IF 算 0**，只剩研究設計分數。所以把你常看的期刊補進 `journals.json`，排序才會準。

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

## 費用與模型

預設用 **claude-sonnet-5**（8/31 前促銷價 $2/$10，比 Sonnet 4.6 便宜且更新）。

| 用量 | 費用 |
|---|---|
| 每天 12 篇 | 約 **$8/月** |
| 每天 6 篇 | 約 **$4/月** |
| 回補 6/1 至今（約 260 篇） | 一次性約 **$6** |

PubMed API 免費。建議 console.anthropic.com → Billing 設 $20 月上限。

⚠️ **9/1 起 Sonnet 5 漲回 $3/$15**，屆時費用會變成約 $12/月。

### 想換模型

Settings → Secrets and variables → Actions → **Variables** 分頁 → New variable
- Name: `MODEL`
- Value: 下面其中一個

| 模型 | 每月(12篇/天) | 說明 |
|---|---|---|
| `claude-sonnet-5` | $8 | **預設**。促銷中 |
| `claude-haiku-4-5-20251001` | $4 | 便宜 3 倍，但**批判性分析與 NNT 計算會變差** |
| `claude-opus-4-8` | $21 | 最強，但貴一倍 |

### 關於「換 OpenAI 會不會比較便宜」

**不會。** 同層級的旗艦模型價格幾乎一樣：

| | 每月(12篇/天) |
|---|---|
| Claude Sonnet 4.6 | $12.42 |
| gpt-5.4 / gpt-5.6-terra | $12.06 |

一個月差 $0.36。OpenAI 那些超便宜的價格（mini / nano）是**小型模型**，不是同一個等級；而 Anthropic 也有小型模型（Haiku 4.5 $1/$5，比 gpt-5.6-luna 的 $1/$6 還便宜）。

**真正的問題不是「換哪家」，是「要不要用小模型」——那是品質問題，不是廠商問題。**

### 想省錢，先動這個

把 `fetch_papers.py` 的 `MAX_RESULTS` 從 12 調到 **6-8**。費用直接砍半，而且**完全沒有品質損失**——你只是每天少看幾篇，反正大部分日子也沒有 12 篇值得讀。

這比換小模型安全得多。

---

## 出問題時

**Actions 紅色 ✗** → 看 log。`404` = model 名稱錯；`401` = API Key 沒設好。

**網站沒更新** → 按右下角「更新」，或 Ctrl+Shift+R。

**IF 沒顯示** → 那本期刊不在 `journals.json` 裡，自己加進去即可。
