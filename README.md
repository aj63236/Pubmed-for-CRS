# 腸嚐新知

每日自動抓 PubMed 大腸直腸外科新論文，用 Claude 產生中文摘要，發佈到網站。

**網站：** https://aj63236.github.io/Pubmed-for-CRS/

---

## 這次改了什麼

| 問題 | 修正 |
|---|---|
| GitHub Actions 一直失敗（404） | **不是防火牆** —— 是 model 名稱 `claude-sonnet-4-20250514` 已廢棄。改用 `claude-sonnet-4-6`，Actions 現在可以跑了，**Mac 不用開機** |
| 網站每次開都要等 AI 跑很久 | 摘要在 Actions 裡先生成好存成 JSON，網站只是讀檔 → **秒開，零 API 呼叫** |
| 同一篇論文重複付費 | 加了 `data/_cache.json`，處理過的 PMID 不會再送一次 API |
| 版面上方一大片留白 | 內容改為從標題下方往下排，不再錨在底部 |
| 「01 / 02 / 03」大數字 | 拿掉（論文順序沒有意義）。改放**證據等級標籤**（RCT / 系統性回顧 / 回溯性研究），這才是掃文獻時第一個要看的 |
| 上滑不會換下一篇 | 完整摘要預設收 4 行、可「展開全文」，一張卡塞得進一個螢幕，上滑才會真的換頁 |
| 想要 iOS app | 加了 PWA —— iPhone Safari 開網站 → 分享 → **加入主畫面**，就有 app 圖示、全螢幕、可離線看昨天的內容。不用 Xcode |
| 每天只抓 1 天常常 0 篇 | 改用 `edat`（PubMed 上架日）抓最近 2 天，比 `pdat` 準 |

---

## 檔案

```
.
├── index.html                    網站
├── manifest.json                 PWA 設定
├── sw.js                         Service Worker（離線快取）
├── icons/                        App 圖示
├── fetch_papers.py               每日收集腳本
├── requirements.txt
├── .nojekyll
├── .github/workflows/
│   └── daily_fetch.yml           每天台灣時間 06:00 自動跑
└── data/
    ├── index.json                日期索引
    ├── _cache.json               PMID 快取（避免重複付費）
    └── YYYY-MM-DD.json           每日文獻
```

---

## 安裝（一次就好）

**1. 把所有檔案上傳到 repo**（覆蓋舊的）

⚠️ 注意資料夾結構要對：
- `daily_fetch.yml` 必須在 `.github/workflows/` 裡
- `index.json` 必須在 `data/` 裡

上傳時如果拖不進資料夾，用 **Add file → Create new file**，檔名直接打 `.github/workflows/daily_fetch.yml`（帶斜線，GitHub 會自動建資料夾）。

**2. 設好 API Key**

Repo → Settings → Secrets and variables → Actions → New repository secret
- Name: `ANTHROPIC_API_KEY`
- Secret: 你的 key

**3. 開 GitHub Pages**

Settings → Pages → Source: Deploy from a branch → main → / (root)

**4. 測跑一次**

Actions → 每日文獻收集 → Run workflow

跑完看 log 最後幾行，會顯示這次花了多少錢。

---

## 裝到 iPhone

1. **Safari**（一定要 Safari，Chrome 不行）打開網站
2. 底部「分享」按鈕
3. 選「**加入主畫面**」

之後就跟一般 app 一樣，有圖示、全螢幕、沒有網址列，離線也看得到上次讀的內容。

---

## 調整

改 `fetch_papers.py` 開頭的設定區：

```python
DAYS_BACK   = 2     # 抓最近幾天（太小會常常 0 篇）
MAX_RESULTS = 12    # 每天最多幾篇
MODEL       = "claude-sonnet-4-6"
```

搜尋關鍵字改 `PUBMED_QUERY`。目前排除了 case report 和純動物研究。

---

## 費用

PubMed API 免費。Claude 的部分：一篇約 $0.01–0.02（含完整摘要翻譯），一天 12 篇約 **$0.15**，一個月約 **$4–5 美元**。

有快取，所以重跑同一天不會再收費。

建議去 Console → Billing 設個 $10 的月上限。

---

## 出問題時

**Actions 顯示紅色 ✗**
→ 點進去看 log。`404` = model 名稱錯了；`401` = API Key 沒設或設錯。

**網站顯示 JSON 原始碼而不是網頁**
→ 根目錄少了 `index.html`。

**網站說「連不上資料」**
→ `data/index.json` 沒推上去，或 Pages 還沒部署完（等 1–2 分鐘）。

**改了東西但網站沒變**
→ 按網站右下角「更新」，或 Ctrl+Shift+R 強制重新整理。
