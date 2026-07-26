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
- 2 分鐘 last-good snapshot cache；Tab 切換共用 cache，手動 refresh
  有 30 秒 cooldown，跨瀏覽器分頁共用每小時 60 次 request budget
- 快照失敗時先使用 last-good cache，再使用三個 sample JSON 作後備
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
│   │   ├── backup.py
│   │   ├── snapshot.py
│   │   ├── publisher.py
│   │   └── cli.py
│   ├── integrations/hermes_bridge.py
│   ├── seed_demo.py
│   └── tests/
├── config/
├── scripts/
│   ├── import_paper_log.py
│   ├── portfolio_cron.py
│   ├── verify_outbox_artifact.py
│   ├── install-vps.sh
│   └── verify-vps.sh
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

### 舊 Paper log 一次性匯入

`scripts/import_paper_log.py` 只用於既有 `paper_trading_log.jsonl` 的遷移與重試。
它會先完整驗證來源、保留 Stage A 已使用的 legacy event ID 規則，再逐筆經
`hermes_bridge.py` 寫入。先執行唯讀 preflight：

```bash
python3 scripts/import_paper_log.py --check-only
```

確認統計後才執行匯入：

```bash
python3 scripts/import_paper_log.py
```

這個 importer 不是持續 writer。新 Paper trade 必須由
`swing_trader.py` 先把完整 event（包括只生成一次的 immutable event ID）
寫入 durable outbox，再呼叫 bridge；bridge 成功或回覆 duplicate 後才可從
outbox 移除。

### Outbox artifact 部署前驗收

`scripts/verify_outbox_artifact.py` 會在完全不接觸 production state、log 或
ledger 的情況下，驗證 staging bundle 的 manifest/hash、以 zero-fuzz
重建 patch、檢查實際 patched source 的 lock/UTC/BUY 原子持久化路徑，並可
執行 bundle 內指向同一 patched file 的 real-artifact tests。例如：

```bash
python3 scripts/verify_outbox_artifact.py \
  --bundle-dir /data/portfolio/staging/outbox-d0c5bc0-v4 \
  --patched swing_trader.py.patched.v4 \
  --patch outbox.v4.patch \
  --tests test_outbox_artifact_v4.py \
  --manifest manifest.v4.json \
  --run-artifact-tests
```

Linux 預設要求五個 artifact 均為 owner-only `0600`。只有輸出
`"status": "valid"`、patched SHA 在 tests 前後不變，而且 repository
backend/frontend tests 亦通過，先可進入 production deploy review。

### 建立 portfolio

`PORTFOLIO_OPEN` 係不可變 master fact。Paper 已確認由
`2026-07-16T00:00:00Z` 起計，initial cash 為 USD 100,000。以下 command
使用 stable ID；第一次會建立 opening event，完全相同 command 重試會成為
idempotent no-op：

```bash
cd /opt/portfolio-tracker/backend
sudo -u portfolio /usr/bin/python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  open \
  --portfolio paper \
  --event-id paper-open-20260716 \
  --occurred-at 2026-07-16T00:00:00Z \
  --initial-cash 100000
```

真實倉 initial cash 及 effective UTC 尚未確認，**不要**以 `0` 或任何
placeholder 建立 opening event。Live tab 會保持 `NO_DATA`；等兩個真實值
一齊確認後，先用另一個 stable ID 建立：

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  open \
  --portfolio live \
  --event-id live-open-LIVE_EFFECTIVE_DATE \
  --occurred-at LIVE_EFFECTIVE_UTC \
  --initial-cash LIVE_INITIAL_CASH
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
  --source manual-import \
  --note "manual /trade"
```

模擬倉把 `--portfolio` 改成 `paper`，並可加入：

```text
--source swing-trader --reason "entry signal" --strategy "momentum"
```

同一次重試必須重用完全相同的 `event_id` 和 payload：

- 同 ID、同 payload：idempotent no-op
- 同 ID、不同 payload：拒絕並回報 conflict

來源如果有獨立記錄時間，可加 `--created-at <UTC-Z>`；source 必須將呢個
值同 event ID 一齊保存，retry 時原樣重用。省略時會使用
`occurred_at`，確保同一 command 重試仍然 deterministic。

Telegram handler 可以把原始指令直接交給同一個 parser；`event-id` 必須由
Telegram update ID 或另一個可重用 Import ID 產生：

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  telegram-trade \
  --portfolio live \
  --event-id live-telegram-UPDATE_ID \
  --occurred-at 2026-07-24T15:30:00Z \
  --text "/trade BUY AAPL 10 @ 180.50 fee:1.50 note:earnings play"
```

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

AMEND 只可改 `note`、`fee`、`reason`、`strategy`，並只可指向原始
BUY、SELL 或 CASH_FLOW。VOID 最好直接指向原始 economic event；亦可指向
AMEND，效果係取消該 AMEND 所屬嘅原始 economic event。VOID 永遠不可指向
另一個 VOID。

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  amend \
  --portfolio live \
  --event-id live-amend-IMPORT_ID \
  --occurred-at 2026-07-24T16:00:00Z \
  --target live-telegram-IMPORT_ID \
  --amend-reason "correct broker fee" \
  --fee 1.25
```

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  void \
  --portfolio live \
  --event-id live-void-IMPORT_ID \
  --occurred-at 2026-07-24T16:05:00Z \
  --target live-telegram-IMPORT_ID \
  --void-reason "duplicate broker fill"
```

### 報價

VPS 現有 bot 可以繼續用 yfinance 作內部研究／模擬用途，但 yfinance 文件
把 Yahoo Finance API 定位為 personal use，而 Yahoo Finance 明確禁止再分發
其資料。因此，未取得另外授權前，不要把 yfinance 衍生的 close／SPY benchmark
經 `quote-batch` 寫入會公開發布的 snapshot。

取得允許公開／再分發資料的 quote provider 後，正式 daily cron 必須把同一個
session 的所有持倉 close 及一個 SPY benchmark 組成單一 JSON batch，再交給
bridge：

```bash
cat <<'JSON' | python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  quote-batch \
  --file -
[
  {
    "event_id": "market-aapl-2026-07-24",
    "occurred_at": "2026-07-24T20:30:00Z",
    "session_date": "2026-07-24",
    "symbol": "AAPL",
    "close": "218.40"
  },
  {
    "event_id": "market-spy-quote-2026-07-24",
    "occurred_at": "2026-07-24T20:30:01Z",
    "session_date": "2026-07-24",
    "symbol": "SPY",
    "close": "620.10"
  },
  {
    "event_id": "market-spy-benchmark-2026-07-24",
    "occurred_at": "2026-07-24T20:30:02Z",
    "session_date": "2026-07-24",
    "symbol": "SPY",
    "close": "620.10",
    "benchmark": true
  }
]
JSON
```

`quote-batch` 會先驗證整批資料必須屬於同一個 session、每個
`(action, symbol)` 唯一，而且剛好有一個 SPY benchmark。全部通過後才會
在同一把 global ledger lock 下寫入，最後只 rebuild／request publish 一次。
同一批資料 retry 會按 stable event ID 成為 no-op。如果程序在 batch 中途
終止，`rebuild.pending` 會保留完整 event ID 清單；systemd 會拒絕由部分
batch 生成快照，直至原 batch 用相同 stable IDs retry 完成。

單筆 `quote` 只應用於人工補數：

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  quote \
  --event-id market-aapl-correction-2026-07-24 \
  --occurred-at 2026-07-24T20:30:00Z \
  --session-date 2026-07-24 \
  --symbol AAPL \
  --close 218.40
```

同一個 session 必須為所有未平倉 symbol 提供 close。任何一個缺失，該日整個
portfolio NAV 會標記為 `INSUFFICIENT_MARKET_DATA`。如果 SPY 本身亦是持倉，
batch 要同時包含 SPY `QUOTE` 及 `BENCHMARK_CLOSE`，兩者用途不同。

### Hermes 讀取

```bash
python3 integrations/hermes_bridge.py \
  --root /var/lib/portfolio-tracker \
  read \
  --portfolio live
```

`read` 會先比較 ledger source heads；如 snapshot 落後，會在 global lock 下
重建並建立 publish request，唔會靜默回傳舊資料。輸出只有 derived holdings、
trades、NAV 與 metrics，不需要 agent 自行重算 FIFO。

## GitHub Pages

1. 建立 public repository：`portfolio-tracker`。
2. 將本 repository 內容放在 `main` root。
3. Repository Settings → Pages → Deploy from branch → `main` / `(root)`。
4. 手動建立 `portfolio-data` branch；publisher 不會自動建立或 force push branch。
5. 建立 fine-grained PAT，只允許該 repository 的 **Contents: Read and write**。
6. PAT 只放 VPS root-owned、mode `0600` 的
   `/etc/portfolio-tracker/portfolio.env`，變數名必須為
   `PORTFOLIO_GITHUB_TOKEN`。不要改用 `/data/.hermes/.env` 的
   `GITHUB_TOKEN`，亦永遠不要放入 repository、JSON、systemd unit 或 log。
7. `js/config.js` 先讀取公開 data branch 的 raw endpoint，避免未認證
   GitHub Contents API rate limit；API endpoint 及 `main` 本地快照只作後備：

```js
snapshotUrls: [
  "https://raw.githubusercontent.com/cliffordfok/portfolio-tracker/portfolio-data/portfolio-snapshot.json",
  "https://api.github.com/repos/cliffordfok/portfolio-tracker/contents/portfolio-snapshot.json?ref=portfolio-data",
  "./data/portfolio-snapshot.json",
]
```

Frontend request 使用 cache-busting query、2 分鐘 TTL 及每小時共享 budget；
Contents API 後備 request 會使用 GitHub raw media `Accept` header，亦唔依賴
jsDelivr cache。

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

只有 `portfolio-publish.service` 會載入 secret-bearing
`/etc/portfolio-tracker/portfolio.env`。Rebuild 及 backup 使用固定 private
runtime path，不會接收 GitHub PAT；`verify-vps.sh` 會驗證呢個最小權限邊界。

Repo clone 到標準路徑後，可用 installer 建立專用 service user、0700 runtime
目錄、私有 environment file 及 systemd units。Installer **唔會**啟用或啟動
service，亦唔會覆寫已存在嘅 environment file：

```bash
sudo git clone https://github.com/cliffordfok/portfolio-tracker.git \
  /opt/portfolio-tracker
sudo /opt/portfolio-tracker/scripts/install-vps.sh
sudoedit /etc/portfolio-tracker/portfolio.env
sudo /opt/portfolio-tracker/scripts/verify-vps.sh
```

Rebuild path unit 會即時處理 `rebuild.pending`；安全 timer 每五分鐘只比較
JSONL source heads，資料無變更時不會重寫 snapshot 或製造 GitHub commit。
Publisher 寫入 `publication-attempt.json` 後才 PUT。若 PUT 成功但 response
timeout／process crash，下次會以 intended content hash 採納成功 commit；
未知 remote edit 會 fail closed。`publish.pending` 只係快速觸發提示；安全
timer 會比較 local snapshot hash 與 `published-state.json`。兩者相同就直接
回報 `idle`，不會向 GitHub 發出 request；兩者不同就算 pending marker 因
crash 遺失，亦會繼續 publication recovery。

如果新建 `portfolio-data` 時已經有一份 sample snapshot，而 VPS 尚未有
`published-state.json`，第一次覆寫必須由人明確確認：

```bash
python3 -m portfolio_tracker.cli \
  --root /var/lib/portfolio-tracker \
  publish \
  --repository cliffordfok/portfolio-tracker \
  --branch portfolio-data \
  --path portfolio-snapshot.json \
  --bootstrap
```

之後 systemd service 永遠唔會使用 `--bootstrap`；任何未知 remote edit
仍然會被拒絕。

完成兩個 `PORTFOLIO_OPEN`、首個 snapshot rebuild 同一次人工確認嘅
bootstrap publish 後，先建立一份已驗證 backup：

```bash
cd /opt/portfolio-tracker/backend
sudo -u portfolio /usr/bin/python3 -m portfolio_tracker.cli \
  --root /var/lib/portfolio-tracker \
  backup
sudo -u portfolio /usr/bin/python3 -m portfolio_tracker.cli \
  --root /var/lib/portfolio-tracker \
  doctor \
  --require-initialized \
  --require-current \
  --require-published \
  --require-backup
```

`doctor` 只會輸出 event counts、revision、hash 狀態及 backup ID；唔會輸出
持倉、交易內容或 token。全部通過後先啟用觸發器：

```bash
sudo systemctl enable --now \
  portfolio-rebuild.path \
  portfolio-rebuild.timer \
  portfolio-publish.path \
  portfolio-publish.timer \
  portfolio-backup.timer
sudo /opt/portfolio-tracker/scripts/verify-vps.sh --active
```

每日 ledger backup 使用同一把 global lock，輸出精確 bytes、SHA-256
manifest，而且只會寫入 VPS 私有 `backups/`：

```bash
python3 -m portfolio_tracker.cli \
  --root /var/lib/portfolio-tracker \
  backup
```

部署時一併安裝及啟用 `portfolio-backup.timer.example`。

### Docker／Hermes cron（目前 VPS）

目前 VPS repository 及 runtime 實際位於 `/data/portfolio-tracker` 和
`/data/portfolio`，container 內沒有 systemd。`scripts/portfolio_cron.py`
提供相同嘅 one-shot rebuild、publish、backup、doctor 操作，唔會安裝或修改
crontab：

```bash
/usr/local/bin/python3 \
  /data/portfolio-tracker/scripts/portfolio_cron.py rebuild
```

所有 child process 都會先移除 ambient `GITHUB_TOKEN`、`GH_TOKEN` 及
`PORTFOLIO_GITHUB_TOKEN`。只有 publisher child 會由一個非 symlink、
owner-only `0600`、單行 ASCII token file 取得 PAT。普通 `publish`／定時
`maintain` 永遠不會傳 `--bootstrap`。預設 token path：

```text
/data/portfolio/secrets/github-token
```

PAT 尚未建立時，只可安排 `rebuild`／`backup`，不可安排 `publish` 或
`maintain`。PAT 安裝完成後，首次發布使用明確的一次性命令：

```bash
/usr/local/bin/python3 \
  /data/portfolio-tracker/scripts/portfolio_cron.py bootstrap-publish
```

`bootstrap-publish` 是唯一會傳 `--bootstrap` 的 wrapper action，只可由
operator 人工執行，不能放入 crontab。它會先在完全沒有 PAT 的環境重建
snapshot；只有 rebuild 成功後才讀取 token 及啟動 bootstrap publisher。
成功並驗證 publication state 後，建議 cron：

```cron
*/5 * * * * /usr/local/bin/python3 /data/portfolio-tracker/scripts/portfolio_cron.py maintain
17 3 * * * /usr/local/bin/python3 /data/portfolio-tracker/scripts/portfolio_cron.py backup
```

`maintain` 會先以完全冇 PAT 嘅環境執行 `rebuild --if-needed`；成功後先讀取
token file，並只注入 publisher child。如果 rebuild 失敗，佢唔會讀 PAT 或
啟動 publisher。

單一 Unix user container 無法提供 systemd credential／獨立 service user
級別嘅檔案隔離；以上 wrapper 提供 process environment 最小權限。需要嚴格
OS-level PAT 隔離時，publisher 必須改由另一個 one-shot container/user 執行。

目前 Live initial cash/effective UTC 尚未提供，所以只可執行普通 `doctor`；
`doctor-active` 會要求 Paper、Live、publication 及 backup 全部已正式上線，
留待 Live 初始化後使用。

## 重新生成示範數據

必須使用一個全新的 runtime directory，seed script 不會刪除既有 ledger：

```powershell
cd backend
python seed_demo.py --runtime ../../work/new-demo-runtime --output ../data
```

## 已確認部署設定

- 模擬倉：effective UTC `2026-07-16T00:00:00Z`，initial cash USD 100,000
- VPS：由 Hermes 執行；沿用現有 cron infrastructure，不需要 SSH
- Hermes：paper cron 及 Telegram `/trade` 的唯一 writer
- yfinance：只限 VPS 內部用途，未獲准用於 public snapshot

## 上線前仍需決定

- 獲准公開／再分發資料的 quote provider
- 真實倉 `initial_cash` 及 effective UTC；確認前 Live 保持 `NO_DATA`
- fine-grained PAT 是否已按指定位置安全存放於 VPS
- 現有 paper state 的 immutable source trade ID；如果沒有，部署時需要訂立
  canonical hash Import ID 規則

GitHub repository、`main` Pages 及 `portfolio-data` branch 已建立。以上剩餘項目
全部是部署設定，不需要改動 ledger、FIFO 或 snapshot 架構。

## 安全與私隱

- Public dashboard 代表所有 snapshot fields 都會公開，包括交易日期、股數、價格和 notes。
- 不想公開的 note 必須在寫入前移除，不能依賴 frontend 隱藏。
- `.gitignore` 阻擋 master JSONL、runtime state、token 和 repair backups。
- Publisher 只接受環境變數 token，CLI error 不會輸出 token。
- 不要把真實 broker statement、account number、email、Telegram user data 或 PAT 放入 sample JSON。
