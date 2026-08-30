# Binance Spot CVD Dashboard

Flask + TradingView Lightweight Charts 的本地 Spot order-flow dashboard。Klines 由 Binance REST API 取得；CVD 由本機 collector 收到的 raw `trade` tick-by-tick 資料聚合，不使用 `aggTrade`。

## 安裝與啟動

```powershell
python -m pip install -r requirements.txt
Copy-Item config.json.example config.json
python app.py
```

請先依本機需求修改 `config.json`；此檔案已由 Git 忽略。若檔案不存在，程式會使用 `config.json.example` 所列的預設值。相對路徑以專案根目錄為基準，因此可從任何工作目錄啟動。

瀏覽 `http://127.0.0.1:5000`。`app.py` 是 parent process，預設會啟動 collector：

- `collector.py`：訂閱 Binance 所有可交易 Spot symbols 的 `<symbol>@trade` raw trade stream，依 trade ID 將每一筆成交寫入 SQLite。
- `app.py`：Flask 頁面與 chart API。

cleanup 預設關閉，資料會持續累積。只有將 `cleanup.auto_start` 設為 `true` 時，server 才會啟動 `cleanup.py` 並依 retention 設定刪除舊資料。

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