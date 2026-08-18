const chartElement = document.getElementById('chart');
const form = document.getElementById('search-form');
const symbolInput = document.getElementById('symbol');
const intervalInput = document.getElementById('interval');
const loadButton = document.getElementById('load-button');
const message = document.getElementById('message');
const feedStatus = document.getElementById('feed-status');
const emptyState = document.getElementById('empty-state');

const chart = LightweightCharts.createChart(chartElement, {
    layout: {
        background: { type: LightweightCharts.ColorType.Solid, color: '#fbfcf9' },
        textColor: '#6f7974',
        fontFamily: 'IBM Plex Mono, monospace',
        fontSize: 11,
        panes: { separatorColor: '#d9ddd7', separatorHoverColor: '#148461', enableResize: true },
    },
    grid: { vertLines: { color: '#edf0eb' }, horzLines: { color: '#edf0eb' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#d9ddd7' },
    timeScale: { borderColor: '#d9ddd7', timeVisible: true, secondsVisible: false, rightOffset: 5 },
});

const candles = chart.addSeries(LightweightCharts.CandlestickSeries, {
    upColor: '#148461', downColor: '#d45252', borderVisible: false,
    wickUpColor: '#148461', wickDownColor: '#d45252',
}, 0);
const sma30 = chart.addSeries(LightweightCharts.LineSeries, { color: '#d59c26', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, 0);
const sma45 = chart.addSeries(LightweightCharts.LineSeries, { color: '#377e9d', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, 0);
const sma60 = chart.addSeries(LightweightCharts.LineSeries, { color: '#7f5f9b', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, 0);
const volume = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false }, 1);
const delta = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false }, 2);
const cvd = chart.addSeries(LightweightCharts.LineSeries, { color: '#17211d', lineWidth: 2, priceLineVisible: false }, 2);
let marketState = null;
let marketSocket = null;
let socketGeneration = 0;

chart.panes()[0].setStretchFactor(6);
chart.panes()[1].setStretchFactor(1.6);
chart.panes()[2].setStretchFactor(2.4);

function positionPaneLabels() {
    const chartBounds = chartElement.getBoundingClientRect();
    const labels = document.querySelectorAll('.chart-label');
    chart.panes().slice(1).forEach((pane, index) => {
        const paneBounds = pane.getHTMLElement().getBoundingClientRect();
        labels[index].style.top = `${paneBounds.top - chartBounds.top + 8}px`;
    });
}

requestAnimationFrame(positionPaneLabels);

function formatNumber(value) {
    return new Intl.NumberFormat('en-US', { maximumFractionDigits: 8 }).format(value);
}

function setStatus(state, text) {
    feedStatus.className = `feed-status ${state}`;
    feedStatus.querySelector('span').textContent = text;
}

function calculateSma(items, period) {
    let total = 0;
    return items.flatMap((item, index) => {
        total += item.close;
        if (index >= period) total -= items[index - period].close;
        return index >= period - 1 ? [{ time: item.time, value: total / period }] : [];
    });
}

function renderSeries(fitContent = false) {
    const candleData = marketState.candles;
    candles.setData(candleData.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    volume.setData(candleData.map(item => ({
        time: item.time,
        value: item.volume,
        color: item.close >= item.open ? 'rgba(20, 132, 97, .42)' : 'rgba(212, 82, 82, .42)',
    })));
    sma30.setData(calculateSma(candleData, 30));
    sma45.setData(calculateSma(candleData, 45));
    sma60.setData(calculateSma(candleData, 60));
    delta.setData(marketState.cvd.map(item => ({
        time: item.time,
        value: item.delta,
        color: item.delta >= 0 ? 'rgba(20, 132, 97, .38)' : 'rgba(212, 82, 82, .38)',
    })));
    cvd.setData(marketState.cvd.map(item => ({ time: item.time, value: item.cvd })));
    if (fitContent) chart.timeScale().fitContent();
}

function updateSummary() {
    const last = marketState.candles.at(-1);
    const lastCvd = marketState.cvd.at(-1);
    document.getElementById('market-symbol').textContent = marketState.symbol;
    document.getElementById('last-price').textContent = last ? formatNumber(last.close) : '--';
    document.getElementById('tick-count').textContent = formatNumber(marketState.tickCount);
    document.getElementById('net-cvd').textContent = lastCvd ? formatNumber(lastCvd.cvd) : '--';
    document.getElementById('net-cvd').style.color = lastCvd && lastCvd.cvd < 0 ? '#d45252' : '#148461';
    emptyState.hidden = marketState.cvd.length > 0;
}

function updateHover(candle) {
    if (!candle) return;
    const change = candle.open ? ((candle.close - candle.open) / candle.open) * 100 : 0;
    const range = candle.open ? ((candle.high - candle.low) / candle.open) * 100 : 0;
    document.getElementById('hover-symbol').textContent = marketState?.symbol || symbolInput.value;
    document.getElementById('hover-time').textContent = new Date(candle.time * 1000).toLocaleString();
    document.getElementById('hover-open').textContent = formatNumber(candle.open);
    document.getElementById('hover-high').textContent = formatNumber(candle.high);
    document.getElementById('hover-low').textContent = formatNumber(candle.low);
    document.getElementById('hover-close').textContent = formatNumber(candle.close);
    const changeElement = document.getElementById('hover-change');
    changeElement.textContent = `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
    changeElement.className = change >= 0 ? 'positive' : 'negative';
    document.getElementById('hover-range').textContent = `${range.toFixed(2)}%`;
}

function applyTrades(trades) {
    for (const trade of trades) {
        const bucket = Math.floor(trade.time / marketState.intervalMs) * marketState.intervalMs / 1000;
        let candle = marketState.candles.at(-1);
        if (!candle || candle.time < bucket) {
            candle = { time: bucket, open: trade.price, high: trade.price, low: trade.price, close: trade.price, volume: 0 };
            marketState.candles.push(candle);
            if (marketState.candles.length > 500) marketState.candles.shift();
        }
        if (candle.time === bucket) {
            candle.high = Math.max(candle.high, trade.price);
            candle.low = Math.min(candle.low, trade.price);
            candle.close = trade.price;
            candle.volume += trade.quantity;
        }

        let cvdBucket = marketState.cvd.at(-1);
        if (!cvdBucket || cvdBucket.time < bucket) {
            cvdBucket = { time: bucket, trades: 0, buyVolume: 0, sellVolume: 0, delta: 0, cvd: cvdBucket?.cvd || 0 };
            marketState.cvd.push(cvdBucket);
        }
        if (cvdBucket.time === bucket) {
            cvdBucket.trades += 1;
            if (trade.buyerIsMaker) cvdBucket.sellVolume += trade.quantity;
            else cvdBucket.buyVolume += trade.quantity;
            cvdBucket.delta = cvdBucket.buyVolume - cvdBucket.sellVolume;
            const previousCvd = marketState.cvd.length > 1 ? marketState.cvd.at(-2).cvd : 0;
            cvdBucket.cvd = previousCvd + cvdBucket.delta;
        }
        marketState.tickCount += 1;
        marketState.lastTradeId = trade.tradeId;
    }
    renderSeries();
    updateSummary();
    updateHover(marketState.candles.at(-1));
}

function connectMarketSocket() {
    const generation = ++socketGeneration;
    if (marketSocket) marketSocket.close();
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const params = new URLSearchParams({ symbol: marketState.symbol, after: marketState.lastTradeId });
    marketSocket = new WebSocket(`${protocol}//${location.host}/ws/market?${params}`);
    marketSocket.addEventListener('open', () => setStatus('live', 'WebSocket 即時連線'));
    marketSocket.addEventListener('message', event => {
        const payload = JSON.parse(event.data);
        if (payload.error) {
            console.error(payload.error);
            message.textContent = payload.error.split('\n').at(-2) || payload.error;
            return;
        }
        if (payload.type === 'trades' && payload.symbol === marketState.symbol) applyTrades(payload.trades);
    });
    marketSocket.addEventListener('close', () => {
        if (generation !== socketGeneration) return;
        setStatus('error', 'WebSocket 重連中');
        setTimeout(connectMarketSocket, 2000);
    });
    marketSocket.addEventListener('error', () => marketSocket.close());
}

async function loadChart() {
    const symbol = symbolInput.value.trim().toUpperCase();
    if (!symbol) return;
    symbolInput.value = symbol;
    socketGeneration += 1;
    if (marketSocket) marketSocket.close();
    loadButton.disabled = true;
    message.textContent = `${symbol} · ${intervalInput.value} 資料載入中`;
    setStatus('', '同步中');
    try {
        const params = new URLSearchParams({ symbol, interval: intervalInput.value, limit: '500' });
        const response = await fetch(`/api/chart?${params}`);
        const data = await response.json();
        if (!response.ok) throw new Error([data.error, data.details].filter(Boolean).join('\n'));
        marketState = { ...data, tickCount: data.cvd.reduce((total, bucket) => total + bucket.trades, 0) };
        renderSeries(true);
        updateSummary();
        updateHover(marketState.candles.at(-1));
        message.textContent = data.cvd.length
            ? 'Klines: Binance REST · 即時更新: local raw-trade WebSocket'
            : 'Klines 已載入；等待本地 collector 累積此交易對資料';
        connectMarketSocket();
    } catch (error) {
        console.error(error);
        message.textContent = error.message;
        setStatus('error', '載入失敗');
    } finally {
        loadButton.disabled = false;
    }
}

chart.subscribeCrosshairMove(param => {
    const candle = param.seriesData.get(candles);
    updateHover(candle || marketState?.candles.at(-1));
});

form.addEventListener('submit', event => {
    event.preventDefault();
    loadChart();
});

new ResizeObserver(entries => {
    const { width, height } = entries[0].contentRect;
    chart.resize(width, height);
    requestAnimationFrame(positionPaneLabels);
}).observe(chartElement);

fetch('/api/symbols')
    .then(async response => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.error);
        return data;
    })
    .then(items => {
        if (!Array.isArray(items)) return;
        document.getElementById('symbols').replaceChildren(...items.map(item => {
            const option = document.createElement('option');
            option.value = item.symbol;
            option.label = `${item.baseAsset} / USDT`;
            return option;
        }));
    })
    .catch(error => {
        console.error(error);
        message.textContent = `Symbols 載入失敗: ${error.message}`;
    });

loadChart();