# Binance Spot CVD Dashboard

Flask + TradingView Lightweight Charts 的本地 Spot order-flow dashboard。Klines 由 Binance REST API 取得；CVD 由本機 collector 收到的 raw `trade` tick-by-tick 資料聚合，不使用 `aggTrade`。

## 安裝與啟動

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item config.json.example config.json
python app.py
```

請先依本機需求修改 `config.json`；此檔案已由 Git 忽略。若檔案不存在，程式會使用 `config.json.example` 所列的預設值。相對路徑以專案根目錄為基準，因此可從任何工作目錄啟動。

本機瀏覽 `http://127.0.0.1:5000`；Linux 遠端部署請瀏覽 `http://伺服器IP:5000`。預設 bind `0.0.0.0`，需確認主機 firewall / cloud security group 允許 TCP 5000；公開網路部署應使用反向代理、TLS 與存取控制。`app.py` 是 parent process，預設會啟動 collector：

- `collector.py`：訂閱 Binance 所有可交易 Spot symbols 的 `<symbol>@trade` raw trade stream，依 trade ID 將每一筆成交寫入 SQLite。
- `app.py`：Flask 頁面與 chart API。
- `notifier.py`：每 2 秒檢查有新成交的 symbols；1m/5m bullish ABS 符合 USD volume 門檻時擷取手機版 chart 並透過 Telegram Bot API 發送。

`python app.py` 的啟動順序如下，terminal 會逐步顯示狀態：

1. Read-only 檢查 SQLite schema。正常重啟不執行 DDL，也不取得 database write lock。
2. 僅在首次建庫或 schema 升級時執行一次 migration，並顯示 raw rows、完成筆數與耗時。
3. 啟動受管理的 collector；Telegram 設定完整時再啟動 notifier，兩者都會顯示 PID。
4. Flask 開始 listen。關閉 `app.py` 時，只有上述由它啟動的 child processes 會一起停止。

持續運行時，collector 每分鐘回報寫入 rows 與 queue depth；notifier 每分鐘回報追蹤 symbols、實際 evaluations、alerts、errors 與最新掃描耗時。這些摘要可用來判斷 CPU 消耗來源。

## Telegram ABS 即時通知

BotFather 只負責建立 bot；實際發送使用 Telegram Bot API。先在 Telegram 開啟 bot、按 Start 並傳一則訊息。在私人 `config.json` 的 `notifications` 區塊填入：

```json
{
	"notifications": {
		"auto_start": true,
		"bot_token": "由 BotFather 取得的新 token",
		"chat_id": "接收通知的 chat ID"
	}
}
```

`chat_id` 不是 bot username。私人聊天通常是數字 ID；不可填 `kt_volume_bot` 或 `@kt_volume_bot`。不知道 chat ID 時，先在手機開啟 bot、按 Start 並傳一則訊息，再執行：

```bash
python notifier.py --discover-chat-id
```

把顯示的數字填入 `config.json` 後，立即測試而不等待 ABS：

```bash
python notifier.py --test-telegram
```

檢查 remote machine 的 tick 新鮮度、1m/5m 歷史覆蓋、當前 ABS 與已發送紀錄：

```bash
python notifier.py --diagnose
```

填妥兩個欄位後執行 `python app.py`，server 會自動啟動 notifier；server 關閉時也會一併停止 notifier。notifier 啟動時會先驗證 Telegram destination，錯誤時直接退出並顯示 Telegram 原因。`config.json` 已由 Git 忽略，仍應限制檔案存取權限。已公開的 token 應先用 BotFather `/revoke` 撤銷並重發。

通知目前只發送 bullish absorption，監看規則如下：

- `1m`：該根 tick-by-tick `price × quantity` USD volume 必須大於 20,000 美金。
- `5m`：該根 tick-by-tick `price × quantity` USD volume 必須大於 50,000 美金。

每個有新 tick 的 symbol 都會即時重新判斷；同一 symbol、timeframe、candle 與方向只發送一次，重啟後也不重複。圖片以 390×844 mobile viewport 擷取對應 timeframe 的 chart，caption 包含 symbol、price、USD volume、USD volume threshold、USD volume MA20 與 Delta Z-score。

ABS 需要 100 根 Delta Z-score 歷史，因此新的資料庫需先累積至少 100 根連續 5m raw ticks（約 8 小時 20 分鐘）才會通知。這項限制避免用缺失資料製造假訊號。

cleanup 預設關閉，資料會持續累積。只有將 `cleanup.auto_start` 設為 `true` 時，server 才會啟動 `cleanup.py` 並依 retention 設定刪除舊資料。

正常重啟不會重建資料庫：主程序、collector 與 notifier 只做 read-only schema readiness check。只有全新資料庫或 schema 版本升級才執行 migration；terminal 會顯示 raw trade 數量、開始/完成及耗時。migration 完成前 web port 尚未開始 listen；若偵測到外部 collector 正在寫入，程式會要求先停止 collector，避免 SQLite write-lock 衝突。

Linux 若已有 `config.json`，請確認 `server.host` 是 `"0.0.0.0"`；舊檔案中的 `"127.0.0.1"` 仍會覆蓋新預設，導致遠端無法連線。

collector 也可獨立從 terminal 啟動：

```powershell
python collector.py
```

啟動 server 時會先檢查 `collector.lock_file`。若獨立 collector 已在執行，server 會沿用它而不再啟動第二份；server 關閉時也不會停止這個外部 collector。

初始 Klines 由 Binance REST 載入；之後頁面會連線 Flask `/ws/market`。Flask 從 SQLite 依 trade ID 增量推送 collector 已寫入的 raw ticks，瀏覽器即時更新當根 OHLC、Volume、SMA30/45/60 與 CVD，不需要手動刷新。WebSocket 斷線會每兩秒自動重連。

圖表另包含：

- 100-bar Delta Z-score 與 ±2 異常區間。
- 符合 Delta Z 與 close position 條件的 bullish/bearish absorption markers。
- Binance USD-M Futures Open Interest Change、100-bar OI change Z-score、5-bar price/OI context 與 filter 狀態。
- Binance Spot 10 檔 Order Book，瀏覽器直接連線 `@depth10@100ms`，降低轉送延遲。

OI 僅作 futures positioning filter，不代表多空方向。方向仍由 price、Spot CVD、Delta Z-score 與 VWAP 決定。Binance Futures 歷史 OI 最低粒度為 5m；1m/3m 圖會使用最近已知的 5m OI。若所在地區對 `fapi.binance.com` 回 HTTP 451，OI pane 會顯示 unavailable，Spot chart、CVD 與 Order Book 仍會運作。可透過 `BINANCE_FUTURES_REST_URL` 指向所在地區合法可用的 Binance Futures market-data endpoint。

collector 每 60 秒會在 terminal 顯示一次：

```text
Collector minute summary: 123456 new rows inserted; queue depth 0
```

下拉清單與預設 collector 僅包含 `*/USDT` Spot pairs。若 API 發生未預期的 500，terminal 會輸出完整 traceback，頁面訊息與 API JSON 也會包含錯誤類型及 details。

停止 Flask（例如按 `Ctrl+C`）時，parent 只會停止由自己啟動的 workers。`run.py` 是相同入口的 alias。手動清理一次可執行：

```powershell
python cleanup.py --once
```

## CVD 定義

Binance raw `trade` event 的 `m` 表示 buyer 是否為 maker：

- `m=false`：buyer 是 taker，quantity 計為正 delta。
- `m=true`：seller 是 taker，quantity 計為負 delta。

每個 timeframe bucket 的 delta 為 `taker buy quantity - taker sell quantity`，CVD 是畫面查詢區間內 delta 的累加。每筆 row 對應 Binance 的單一 raw trade ID，不會把多筆成交先聚合。collector 啟動前的逐筆成交不會由 Klines 還原，因此沒有本地 tick 的歷史區段不會顯示 CVD。

## 效能與正式部署

collector 會用 SQLite trigger 同步維護 1 分鐘 rollup；chart 的 CVD 查詢再由 minute rows 聚合成目標 timeframe，不會在每次載圖時重掃 raw ticks。既有資料庫首次升級會自動 backfill 一次。實測 161 萬 raw rows 的 500-day 查詢由約 191 ms 降到 0.12 ms；notifier 的 100×5m 查詢由約 49 ms 降到 0.30 ms。

Binance Klines、OI 會並行讀取，並使用 bounded in-memory TTL cache 與 per-key request coalescing。dashboard 使用 compact JSON 與 gzip；實測 5m response 由 372.6 KB 降到 13.8 KB（傳輸大小）。即時 ticks 會在瀏覽器每 100ms 合併重繪，資料不會被抽樣或丟棄。

48 CPU / 64 GB 主機建議從 8 個 web workers、每個 32 threads 開始壓測，而不是直接開 48 workers；更多 process 會複製 cache 並增加 Binance API 與 SQLite 連線。Linux 可將 worker 分開啟動：

```bash
python collector.py
python notifier.py
gunicorn --worker-class gthread --workers 8 --threads 32 --timeout 0 --bind 0.0.0.0:5000 app:app
```

Gunicorn 模式不會自動啟動 collector/notifier，應由 systemd、Docker Compose 或其他 supervisor 分別管理。`server.market_data_threads` 控制每個 web process 的 Binance I/O pool；`server.websocket_poll_seconds` 預設 0.1 秒，可依同時在線圖表數調高到 0.2–0.5 秒以降低 SQLite polling。正式值應以實際 concurrent users 壓測後決定。

## 容量控制

預設 collector 會收所有 USDT Spot symbols。全市場逐筆資料會持續使用磁碟與 I/O，可在 `config.json` 限制 `collector.symbols`，或修改 `database.path` 將 SQLite 存到其他磁碟，例如：

```json
{
	"database": {"path": "D:/market-data/spot.db"},
	"collector": {
		"quote_assets": ["USDT"],
		"symbols": ["BTCUSDT", "ETHUSDT"]
	}
}
```

所有可設定項目及精簡英文說明都在 `config.json.example`。預設使用 Binance 官方唯讀市場資料 endpoints，不需要 API key。

## SQLite 資料格式

原始資料位於 `spot_trades`，一列就是 Binance `trade` stream 的一筆成交：

| Column | SQLite type | Meaning |
| --- | --- | --- |
| `symbol` | TEXT | Uppercase Spot symbol, such as `BTCUSDT`. |
| `trade_id` | INTEGER | Binance raw trade ID; unique within a symbol. |
| `trade_time` | INTEGER | Binance event trade time in Unix milliseconds (UTC). |
| `price` | REAL | Executed price in quote asset units. |
| `quantity` | REAL | Executed quantity in base asset units. |
| `buyer_is_maker` | INTEGER | `0`: taker buy; `1`: taker sell. |

複合 primary key 是 `(symbol, trade_id)`，所以重連後收到重複成交也不會重複儲存。`spot_trades_readable` view 另外提供 `trade_time_ms`、ISO UTC `trade_time_utc`、`quote_quantity` 與文字形式的 `taker_side`，方便其他 scripts 直接查詢：

```sql
SELECT *
FROM spot_trades_readable
WHERE symbol = 'BTCUSDT'
ORDER BY trade_id DESC
LIMIT 100;
```