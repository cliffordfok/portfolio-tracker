# Portfolio Tracker C+

一個無 framework、無 bundler、無 build step 的 GitHub Pages 美股投資組合儀表板。模擬倉與真實倉使用獨立 master ledger，所有寫入經同一個安全 writer；瀏覽器只會讀取衍生快照，不會接觸 VPS、GitHub token 或私人 JSONL audit trail。

> `data/` 目前全部是虛構示範數據，可直接部署並看到完整畫面。請勿把真實 master ledger 提交到 GitHub。

## 功能

- 模擬倉、真實倉、對比分析三個頁籤
- FIFO lot matching、買賣費用分攤、逐筆及累計已實現損益
- 目前持倉、平均成本、市值、未實現損益
- NAV、TWR、最大回撤、Sharpe ratio、closed-episode 勝率
- 模擬倉／真實倉／SPY 百分比回報比較
- 1M、3M、6M、1Y、ALL 全域時間篩選
- 所有表格可匯出 CSV
- Tab 切換重新讀取 JSON；快照失敗時使用三個 sample JSON 作後備
- Responsive、keyboard tabs、focus state、semantic tables
- 缺少報價時 NAV 及跨 gap 指標為空，不會假設零回報
- GitHub Contents API crash recovery、manual-edit fail-closed、最多三次 retry

## 資料流

```text
Hermes paper cron ─┐
                   ├─> LedgerStore ─> paper.jsonl ─┐
Hermes /trade ─────┘                               │
                                                   ├─> derived snapshot
Quote cron ───────────> LedgerStore ─> market.jsonl┤
                                                   │
Hermes live trade ─────> LedgerStore ─> live.jsonl ┘

derived snapshot ─> GitHub Contents API ─> portfolio-data branch
                                      └─> GitHub Pages dashboard fetch
```

Master JSONL、locks、publication state 和 PAT 全部只存在 VPS。GitHub repository 只包含程式、示範 JSON 及公開衍生快照。每次 durable append 都先建立 `rebuild.pending`；只有 snapshot atomic replace 成功後才會清除，因此 rebuild 失敗不會遺失更新。

## Repository 結構

```text
/
├── index.html
├── css/style.css
├── js/
│   ├── app.js
│   ├── charts.js
│   ├── config.js
│   ├── data.js
│   └── utils.js
├── data/
│   ├── paper.json
│   ├── live.json
│   ├── benchmark.json
│   └── portfolio-snapshot.json
├── backend/
│   ├── portfolio_tracker/
│   │   ├── schemas.py
│   │   ├── resolver.py
│   │   ├── replay.py
│   │   ├── ledger.py
│   │   ├── snapshot.py
│   │   ├── publisher.py
│   │   └── cli.py
│   ├── integrations/hermes_bridge.py
│   ├── seed_demo.py
│   └── tests/
├── config/
└── systemd/
```

## 本機預覽

不需要安裝 dependency。

```powershell
cd portfolio-tracker
python -m http.server 8080
```

開啟 `http://localhost:8080/`。不要直接雙擊 `index.html`，因為瀏覽器不允許 `file://` 頁面 fetch JSON。

## 測試

Backend 使用 Python standard library：

```powershell
cd backend
python -m unittest discover -s tests -t . -v
python -m compileall -q portfolio_tracker integrations seed_demo.py tests
```

Frontend 測試只需要 Node，不需要 `npm install`：

```powershell
cd ..
node --test tests/frontend.test.js
node --check js/app.js
node --check js/data.js
node --check js/charts.js
node --check js/utils.js
```

`package.json` 只用來告訴 Node 以 ES module 解析 `.js`；沒有 dependencies、scripts 或 build。

## Hermes 讀寫

所有 writer 都必須經 `backend/integrations/hermes_bridge.py` 或底層 `LedgerStore.append()`。不要由 cron、Telegram handler 或 agent 直接修改 JSONL。

### 建立 portfolio

```bash
cd /opt/portfolio-tracker/backend
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  open \
  --portfolio paper \
  --event-id paper-open-2026 \
  --occurred-at 2026-01-02T14:00:00Z \
  --initial-cash 100000
```

真實倉使用另一個 stable ID：

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  open \
  --portfolio live \
  --event-id live-open-2026 \
  --occurred-at 2026-01-02T14:00:00Z \
  --initial-cash 50000
```

### 寫入交易

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  trade \
  --portfolio live \
  --event-id live-telegram-IMPORT_ID \
  --occurred-at 2026-07-24T15:30:00Z \
  --action BUY \
  --symbol AAPL \
  --shares 10 \
  --price 215.25 \
  --fee 0 \
  --note "manual /trade"
```

模擬倉把 `--portfolio` 改成 `paper`，並可加入：

```text
--reason "entry signal" --strategy "momentum"
```

同一次重試必須重用完全相同的 `event_id` 和 payload：

- 同 ID、同 payload：idempotent no-op
- 同 ID、不同 payload：拒絕並回報 conflict

### 入金／出金

正數為入金、負數為出金：

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  cash-flow \
  --portfolio live \
  --event-id live-cash-IMPORT_ID \
  --occurred-at 2026-07-24T13:00:00Z \
  --amount 5000 \
  --note "deposit"
```

### 修訂及取消

AMEND 只可改 `note`、`fee`、`reason`、`strategy`。VOID／AMEND 只可指向原始 BUY、SELL 或 CASH_FLOW，不能指向另一個 correction event。

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  amend \
  --portfolio live \
  --event-id live-amend-IMPORT_ID \
  --occurred-at 2026-07-24T16:00:00Z \
  --target live-telegram-IMPORT_ID \
  --fee 1.25
```

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  void \
  --portfolio live \
  --event-id live-void-IMPORT_ID \
  --occurred-at 2026-07-24T16:05:00Z \
  --target live-telegram-IMPORT_ID
```

### 報價

Quote provider 由部署者選擇。任何 cron/provider 只需把 daily close 傳給 bridge：

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  quote \
  --event-id market-aapl-2026-07-24 \
  --occurred-at 2026-07-24T20:30:00Z \
  --session-date 2026-07-24 \
  --symbol AAPL \
  --close 218.40
```

SPY trading calendar／benchmark：

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  quote \
  --event-id market-spy-benchmark-2026-07-24 \
  --occurred-at 2026-07-24T20:30:00Z \
  --session-date 2026-07-24 \
  --symbol SPY \
  --close 620.10 \
  --benchmark
```

同一個 session 必須為所有未平倉 symbol 提供 close。任何一個缺失，該日整個 portfolio NAV 會標記為 `INSUFFICIENT_MARKET_DATA`。

### Hermes 讀取

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  read \
  --portfolio live
```

輸出只有 derived holdings、trades、NAV 與 metrics，不需要 agent 自行重算 FIFO。

## GitHub Pages

1. 建立 public repository：`portfolio-tracker`。
2. 將本 repository 內容放在 `main` root。
3. Repository Settings → Pages → Deploy from branch → `main` / `(root)`。
4. 手動建立 `portfolio-data` branch；publisher 不會自動建立或 force push branch。
5. 建立 fine-grained PAT，只允許該 repository 的 **Contents: Read and write**。
6. PAT 只放 VPS `PORTFOLIO_GITHUB_TOKEN` environment，永遠不要放入 repository、JSON、systemd unit 或 log。
7. 把 `js/config.js` 的 `snapshotUrl` 改成公開 data branch：

```js
snapshotUrl:
  "https://raw.githubusercontent.com/YOUR_USER/portfolio-tracker/portfolio-data/data/portfolio-snapshot.json",
```

Frontend 每次 refresh 會加入 cache-busting query。若使用 jsDelivr，更新可能有額外 CDN cache delay。

本專案沒有 GitHub Actions；GitHub Pages 直接由 branch root 提供靜態檔案。

## VPS publisher

先複製 example environment 到 repository 以外的位置，填入實際值並限制權限：

```bash
sudo install -d -m 0700 /etc/portfolio-tracker
sudo install -m 0600 config/portfolio.env.example /etc/portfolio-tracker/portfolio.env
```

`systemd/*.example` 是模板，未包含真實 token，亦不會自行安裝。部署時核對：

- project 路徑 `/opt/portfolio-tracker`
- runtime 路徑 `/var/lib/portfolio-tracker`
- service user `portfolio`
- Python 路徑
- repository／branch／snapshot path

Rebuild path unit 會即時處理 `rebuild.pending`，rebuild timer 每五分鐘再次從 JSONL source heads 重建，覆蓋 marker 建立失敗或 process crash 的極端情況。Publisher 寫入 `publication-attempt.json` 後才 PUT。若 PUT 成功但 response timeout／process crash，下次會以 intended content hash 採納成功 commit；未知 remote edit 會 fail closed。

## 重新生成示範數據

必須使用一個全新的 runtime directory，seed script 不會刪除既有 ledger：

```powershell
cd backend
python seed_demo.py --runtime ../../work/new-demo-runtime --output ../data
```

## 上線前仍需決定

- 獲准公開／再分發資料的 quote provider
- 真實倉 `initial_cash` 及 effective date
- 模擬倉初始資金及 effective date
- GitHub repository、`portfolio-data` branch、fine-grained PAT
- Hermes cron／Telegram handler 使用的 stable Import ID 規則

這些是部署設定，不需要改動 ledger、FIFO 或 snapshot 架構。

## 安全與私隱

- Public dashboard 代表所有 snapshot fields 都會公開，包括交易日期、股數、價格和 notes。
- 不想公開的 note 必須在寫入前移除，不能依賴 frontend 隱藏。
- `.gitignore` 阻擋 master JSONL、runtime state、token 和 repair backups。
- Publisher 只接受環境變數 token，CLI error 不會輸出 token。
- 不要把真實 broker statement、account number、email、Telegram user data 或 PAT 放入 sample JSON。
