# Binance Spot CVD Dashboard

Flask + TradingView Lightweight Charts 的本地 Spot order-flow dashboard。Klines 由 Binance REST API 取得；CVD 由本機 collector 收到的 raw `trade` tick-by-tick 資料聚合，不使用 `aggTrade`。

## 安裝與啟動

```powershell
python -m pip install -r requirements.txt
python app.py
```

瀏覽 `http://127.0.0.1:5000`。`app.py` 是 parent process，會同時啟動：

- `collector.py`：訂閱 Binance 所有可交易 Spot symbols 的 `<symbol>@trade` raw trade stream，依 trade ID 將每一筆成交寫入 SQLite。
- `cleanup.py`：每小時刪除五天前的成交資料。
- `app.py`：Flask 頁面與 chart API。

初始 Klines 由 Binance REST 載入；之後頁面會連線 Flask `/ws/market`。Flask 從 SQLite 依 trade ID 增量推送 collector 已寫入的 raw ticks，瀏覽器即時更新當根 OHLC、Volume、SMA30/45/60 與 CVD，不需要手動刷新。WebSocket 斷線會每兩秒自動重連。

collector 每 60 秒會在 terminal 顯示一次：

```text
Collector minute summary: 123456 new rows inserted; queue depth 0
```

下拉清單與預設 collector 僅包含 `*/USDT` Spot pairs。若 API 發生未預期的 500，terminal 會輸出完整 traceback，頁面訊息與 API JSON 也會包含錯誤類型及 details。

停止 Flask（例如按 `Ctrl+C`）時，parent 會 terminate 並等待 collector 與 cleanup 結束，不會留下背景程序。`run.py` 是相同入口的 alias。只清理一次可執行：

```powershell
python cleanup.py --once
```

## CVD 定義

Binance raw `trade` event 的 `m` 表示 buyer 是否為 maker：

- `m=false`：buyer 是 taker，quantity 計為正 delta。
- `m=true`：seller 是 taker，quantity 計為負 delta。

每個 timeframe bucket 的 delta 為 `taker buy quantity - taker sell quantity`，CVD 是畫面查詢區間內 delta 的累加。每筆 row 對應 Binance 的單一 raw trade ID，不會把多筆成交先聚合。collector 啟動前的逐筆成交不會由 Klines 還原，因此沒有本地 tick 的歷史區段不會顯示 CVD。

## 容量控制

預設 collector 會收所有 Spot symbols。全市場逐筆資料即使只保留五天，仍可能使用大量磁碟與 I/O。可在啟動前設定環境變數：

```powershell
$env:CVD_QUOTE_ASSETS = "USDT"
$env:CVD_SYMBOLS = "BTCUSDT,ETHUSDT"
python run.py
```

其他設定請參考 `.env.example`；此專案不會自動讀取 `.env`，請以系統或 terminal 環境變數設定。預設使用 Binance 官方唯讀市場資料 endpoints `data-api.binance.vision` 與 `data-stream.binance.vision`，不需要 API key。