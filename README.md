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

## 回補 6/1 到現在的文章

在**你的電腦**上跑（不是 GitHub），因為要花錢，所以先試算：

```bash
# 1. clone 下來（如果還沒有）
git clone https://github.com/aj63236/Pubmed-for-CRS.git
cd Pubmed-for-CRS

# 2. 安裝套件
pip install requests

# 3. 設 API Key
export ANTHROPIC_API_KEY="sk-ant-你的key"

# 4. 試算 —— 不花錢，只告訴你會抓幾篇、多少錢
python backfill.py --from 2026-06-01 --dry-run
```

會印出類似：

```
要處理的日期：38 天
要抓的論文：  260 篇
  ├─ 需生成： 260 篇
  └─ 已快取： 0 篇
預估費用：    約 $6.40 USD
```

**確認可以接受再真的跑：**

```bash
python backfill.py --from 2026-06-01
```

跑完推上去：

```bash
git add data/
git commit -m "回補 6/1 起的歷史文獻"
git push
```

### 想省錢

```bash
python backfill.py --from 2026-06-01 --max-per-day 5 --dry-run
```

中途斷掉可直接重跑，已完成的日期會跳過，已處理的論文走快取不會重複收費。


---

## 每篇論文會產出什麼

設計原則：這個 app 的功能是幫你**決定要不要花時間讀全文**，不是取代讀全文。所以只留下能支撐這個決定的資訊。

**三個判讀標籤**（掃一眼就能決定去留）
- `評分 7/10` — 可信度 + 對台灣大腸直腸外科的實用性。顏色分級：≥8 綠、6-7 琥珀、<6 紅
- `高度相關 / 中度 / 低度` — 低相關的整顆標籤會**變暗**，讓你直接滑過去
- `可能改變實務 / 再確認已知 / 仍屬早期 / 結果為陰性`

**四塊核心**
1. **一句話結論** — 在誰身上、比了什麼、發現什麼（附關鍵數字）
2. **明天可以怎麼做** — 具體到病人類型的行動建議。不足以改變做法時會誠實說「目前不需改變做法」
3. **關鍵數據**（3 條）— 一律給絕對值，能算 ARR／NNT 就附上。陰性結果也會寫
4. **要小心**（3 條）— 臨床陷阱（替代指標、composite outcome 掩蓋真相、事後次群組、過度詮釋）＋ 數字紅旗（CI 過寬、只有相對風險、樣本太小）合在一起

**可展開**：PICO 表 + 原文摘要完整中譯

### ⚠️ 「摘要沒寫」那一行

PubMed 摘要**通常不會寫** allocation concealment、盲法、ITT、失訪率、試驗註冊、利益衝突。

Prompt 已明確禁止模型編造這些 —— 沒寫的就列進「摘要沒寫」那行，提醒你需查全文。

**看到那行，代表那幾項要自己去翻全文。不要因為卡片看起來完整，就以為分析是完整的。**

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
