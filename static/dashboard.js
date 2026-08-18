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
const sma30 = chart.addSeries(LightweightCharts.LineSeries, { color: '#f28e2b', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, 0);
const sma45 = chart.addSeries(LightweightCharts.LineSeries, { color: '#8b5cf6', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, 0);
const sma60 = chart.addSeries(LightweightCharts.LineSeries, { color: '#ec4899', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, 0);
const sma120 = chart.addSeries(LightweightCharts.LineSeries, { color: '#2f80b7', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, 0);
const volume = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false }, 1);
const delta = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false }, 2);
const cvd = chart.addSeries(LightweightCharts.LineSeries, { color: '#d59c26', lineWidth: 1, priceLineVisible: false }, 2);
const deltaZ = chart.addSeries(LightweightCharts.HistogramSeries, { priceLineVisible: false, lastValueVisible: true }, 3);
const oiChange = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: 'percent' }, priceLineVisible: false }, 4);
deltaZ.createPriceLine({ price: 2, color: '#148461', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: '+2' });
deltaZ.createPriceLine({ price: -2, color: '#d45252', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: '-2' });
const absorptionMarkers = LightweightCharts.createSeriesMarkers(candles, []);
let marketState = null;
let marketSocket = null;
let orderBookSocket = null;
let socketGeneration = 0;
let bookGeneration = 0;
let oiTimer = null;

chart.panes()[0].setStretchFactor(5);
chart.panes()[1].setStretchFactor(1.2);
chart.panes()[2].setStretchFactor(1.5);
chart.panes()[3].setStretchFactor(1.2);
chart.panes()[4].setStretchFactor(1.4);

function positionPaneLabels() {
    const chartBounds = chartElement.getBoundingClientRect();
    const labels = document.querySelectorAll('.pane-legend');
    chart.panes().slice(1).forEach((pane, index) => {
        const paneBounds = pane.getHTMLElement().getBoundingClientRect();
        labels[index].style.top = `${paneBounds.top - chartBounds.top + 8}px`;
    });
}

requestAnimationFrame(positionPaneLabels);

function formatNumber(value) {
    return new Intl.NumberFormat('en-US', { maximumFractionDigits: 8 }).format(value);
}

function formatPrice(value) {
    const absolute = Math.abs(value);
    const digits = absolute >= 100 ? 2 : absolute >= 1 ? 4 : 8;
    return new Intl.NumberFormat('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
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

function rollingZScore(values, length = 100) {
    return values.map((value, index) => {
        if (value === null || index + 1 < length) return null;
        const window = values.slice(index - length + 1, index + 1);
        if (window.some(item => item === null)) return null;
        const mean = window.reduce((total, item) => total + item, 0) / length;
        const variance = window.reduce((total, item) => total + (item - mean) ** 2, 0) / length;
        const std = Math.sqrt(variance);
        return std === 0 ? 0 : (value - mean) / std;
    });
}

function recomputeIndicators() {
    const previous = new Map((marketState.indicators || []).map(item => [item.time, item]));
    const cvdByTime = new Map(marketState.cvd.map(item => [item.time, item]));
    let cumulative = 0;
    let cumulativeVolume = 0;
    let cumulativeNotional = 0;
    marketState.indicators = marketState.candles.map(candle => {
        const cvdBucket = cvdByTime.get(candle.time);
        const deltaValue = cvdBucket?.delta || 0;
        cumulative += deltaValue;
        cumulativeVolume += candle.volume;
        cumulativeNotional += ((candle.high + candle.low + candle.close) / 3) * candle.volume;
        const old = previous.get(candle.time);
        const span = candle.high - candle.low;
        return {
            ...candle,
            delta: deltaValue,
            cvd: cumulative,
            closePosition: span === 0 ? 0.5 : (candle.close - candle.low) / span,
            openInterest: old?.openInterest ?? null,
            vwap: cumulativeVolume === 0 ? null : cumulativeNotional / cumulativeVolume,
        };
    });
    const deltaScores = rollingZScore(marketState.indicators.map(item => item.delta));
    const oiChanges = marketState.indicators.map((item, index, items) => {
        const previousOi = index >= marketState.oiChangeLength ? items[index - marketState.oiChangeLength].openInterest : null;
        return item.openInterest === null || previousOi === null || previousOi === 0
            ? null
            : (item.openInterest - previousOi) / previousOi;
    });
    const oiScores = rollingZScore(oiChanges);
    marketState.indicators.forEach((item, index, items) => {
        item.deltaZ = deltaScores[index];
        item.oiChange = oiChanges[index];
        item.oiChangeZ = oiScores[index];
        item.bullishAbsorption = item.deltaZ !== null && item.deltaZ < -2 && item.closePosition > 0.55;
        item.bearishAbsorption = item.deltaZ !== null && item.deltaZ > 2 && item.closePosition < 0.45;
        const previousClose = index >= marketState.oiChangeLength ? items[index - marketState.oiChangeLength].close : null;
        item.price_up_oi_up = previousClose !== null && item.close > previousClose && item.oiChange > 0;
        item.price_up_oi_down = previousClose !== null && item.close > previousClose && item.oiChange < 0;
        item.price_down_oi_up = previousClose !== null && item.close < previousClose && item.oiChange > 0;
        item.price_down_oi_down = previousClose !== null && item.close < previousClose && item.oiChange < 0;
        item.oiContext = ['price_up_oi_up', 'price_up_oi_down', 'price_down_oi_up', 'price_down_oi_down']
            .find(name => item[name]) || 'unavailable';
        const cvdSlope = index === 0 ? 0 : item.cvd - items[index - 1].cvd;
        item.longTrend = item.vwap !== null && item.close > item.vwap && cvdSlope > 0 && item.deltaZ > 1 && item.oiChange !== null && item.oiChange >= 0;
        item.shortTrend = item.vwap !== null && item.close < item.vwap && cvdSlope < 0 && item.deltaZ < -1 && item.oiChange !== null && item.oiChange >= 0;
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
    sma120.setData(calculateSma(candleData, 120));
    delta.setData(marketState.cvd.map(item => ({
        time: item.time,
        value: item.delta,
        color: item.delta >= 0 ? 'rgba(20, 132, 97, .38)' : 'rgba(212, 82, 82, .38)',
    })));
    cvd.setData(marketState.cvd.map(item => ({ time: item.time, value: item.cvd })));
    deltaZ.setData(marketState.indicators.flatMap(item => item.deltaZ === null ? [] : [{
        time: item.time,
        value: item.deltaZ,
        color: item.deltaZ > 2 ? '#148461' : item.deltaZ < -2 ? '#d45252' : 'rgba(111, 121, 116, .42)',
    }]));
    oiChange.setData(marketState.indicators.flatMap(item => item.oiChange === null ? [] : [{
        time: item.time,
        value: item.oiChange,
        color: item.oiChange >= 0
            ? (item.oiChangeZ > 2 ? '#5b4ad1' : 'rgba(91, 74, 209, .48)')
            : (item.oiChangeZ < -2 ? '#343b38' : 'rgba(111, 121, 116, .48)'),
    }]));
    absorptionMarkers.setMarkers(marketState.indicators.flatMap(item => {
        if (item.bullishAbsorption) return [{ time: item.time, position: 'belowBar', color: '#148461', shape: 'arrowUp', text: 'ABS' }];
        if (item.bearishAbsorption) return [{ time: item.time, position: 'aboveBar', color: '#d45252', shape: 'arrowDown', text: 'ABS' }];
        return [];
    }));
    if (fitContent) chart.timeScale().fitContent();
}

function updateSummary() {
    const last = marketState.candles.at(-1);
    document.getElementById('market-symbol').textContent = marketState.symbol;
    document.getElementById('last-price').textContent = last ? `$${formatNumber(last.close)}` : '--';
    document.getElementById('tick-count').textContent = formatNumber(marketState.tickCount);
    emptyState.hidden = marketState.cvd.length > 0;
}

function smaAt(index, period) {
    if (index < period - 1) return null;
    const window = marketState.candles.slice(index - period + 1, index + 1);
    return window.reduce((total, item) => total + item.close, 0) / period;
}

function updateHover(candle) {
    if (!candle) return;
    const change = candle.open ? ((candle.close - candle.open) / candle.open) * 100 : 0;
    const candleIndex = marketState?.candles.findIndex(item => item.time === candle.time) ?? -1;
    document.getElementById('hover-symbol').textContent = marketState?.symbol || symbolInput.value;
    document.getElementById('hover-time').textContent = new Date(candle.time * 1000).toLocaleString();
    document.getElementById('hover-open').textContent = formatNumber(candle.open);
    document.getElementById('hover-high').textContent = formatNumber(candle.high);
    document.getElementById('hover-low').textContent = formatNumber(candle.low);
    document.getElementById('hover-close').textContent = formatNumber(candle.close);
    const changeElement = document.getElementById('hover-change');
    changeElement.textContent = `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
    changeElement.className = change >= 0 ? 'positive' : 'negative';
    for (const period of [30, 45, 60, 120]) {
        const value = smaAt(candleIndex, period);
        document.getElementById(`hover-sma${period}`).textContent = value === null ? '--' : formatPrice(value);
    }
    const indicator = marketState?.indicators.find(item => item.time === candle.time);
    document.getElementById('hover-volume').textContent = formatNumber(candle.volume);
    document.getElementById('hover-delta').textContent = indicator ? formatNumber(indicator.delta) : '--';
    document.getElementById('hover-cvd').textContent = indicator ? formatNumber(indicator.cvd) : '--';
    document.getElementById('hover-delta-z').textContent = indicator?.deltaZ === null || indicator?.deltaZ === undefined ? '--' : indicator.deltaZ.toFixed(3);
    document.getElementById('hover-absorption').textContent = indicator?.bullishAbsorption
        ? 'BULLISH ABSORPTION'
        : indicator?.bearishAbsorption ? 'BEARISH ABSORPTION' : 'NORMAL';
    document.getElementById('hover-oi').textContent = indicator?.openInterest === null || indicator?.openInterest === undefined ? '--' : formatNumber(indicator.openInterest);
    document.getElementById('hover-oi-change').textContent = indicator?.oiChange === null || indicator?.oiChange === undefined ? '--' : `${(indicator.oiChange * 100).toFixed(3)}%`;
    document.getElementById('hover-oi-z').textContent = indicator?.oiChangeZ === null || indicator?.oiChangeZ === undefined ? '--' : indicator.oiChangeZ.toFixed(3);
    updateOiContext(indicator);
}

const oiContextText = {
    price_up_oi_up: '價格上漲且 OI 上升：新 futures 倉位進場；Spot CVD 同升時，偏多延續較健康。',
    price_up_oi_down: '價格上漲但 OI 下降：可能是空單回補，趨勢延續性需要小心。',
    price_down_oi_up: '價格下跌且 OI 上升：新 futures 倉位進場；Spot CVD 同降時，偏空延續較健康。',
    price_down_oi_down: '價格下跌但 OI 下降：可能是多單平倉或去槓桿；若有 bullish absorption，注意反彈。',
    unavailable: 'OI 是 futures positioning filter，不代表多空方向。',
};

function updateOiContext(indicator) {
    const context = document.getElementById('hover-oi-context');
    const signal = indicator?.longTrend ? ' · LONG FILTER CONFIRMED' : indicator?.shortTrend ? ' · SHORT FILTER CONFIRMED' : '';
    context.textContent = marketState?.oiError
        ? `OI unavailable: ${marketState.oiError}`
        : `${oiContextText[indicator?.oiContext || 'unavailable']}${signal}`;
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
    recomputeIndicators();
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

function renderBookSide(elementId, levels) {
    const parsed = levels.slice(0, 10).map(([price, quantity]) => ({ price: Number(price), quantity: Number(quantity) }));
    const maxTotal = Math.max(...parsed.map(level => level.price * level.quantity), 1);
    document.getElementById(elementId).replaceChildren(...parsed.map(level => {
        const row = document.createElement('div');
        row.className = 'book-row';
        row.style.setProperty('--depth', `${Math.min((level.price * level.quantity / maxTotal) * 100, 100)}%`);
        for (const value of [formatNumber(level.price), formatNumber(level.quantity), formatNumber(level.price * level.quantity)]) {
            const cell = document.createElement('span');
            cell.textContent = value;
            row.appendChild(cell);
        }
        return row;
    }));
}

function connectOrderBook() {
    const generation = ++bookGeneration;
    if (orderBookSocket) orderBookSocket.close();
    const status = document.getElementById('book-status');
    const stream = `${marketState.depthWebSocketUrl}/${marketState.symbol.toLowerCase()}@depth10@100ms`;
    orderBookSocket = new WebSocket(stream);
    orderBookSocket.addEventListener('open', () => {
        status.className = 'book-status live';
        status.querySelector('span').textContent = '100ms LIVE';
    });
    orderBookSocket.addEventListener('message', event => {
        const payload = JSON.parse(event.data);
        renderBookSide('book-bids', payload.bids || payload.b || []);
        renderBookSide('book-asks', payload.asks || payload.a || []);
    });
    orderBookSocket.addEventListener('close', () => {
        if (generation !== bookGeneration) return;
        status.className = 'book-status';
        status.querySelector('span').textContent = '重連中';
        setTimeout(() => marketState && connectOrderBook(), 1000);
    });
    orderBookSocket.addEventListener('error', () => orderBookSocket.close());
    document.getElementById('book-symbol').textContent = `${marketState.symbol} · 10 LEVELS`;
}

async function refreshOpenInterest() {
    if (!marketState || marketState.oiError) return;
    try {
        const response = await fetch(`/api/open-interest?symbol=${encodeURIComponent(marketState.symbol)}`);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error);
        const latest = marketState.indicators.at(-1);
        if (latest) latest.openInterest = payload.openInterest;
        recomputeIndicators();
        renderSeries();
        updateHover(marketState.candles.at(-1));
    } catch (error) {
        marketState.oiError = error.message;
        updateOiContext(null);
    }
}

async function loadChart() {
    const symbol = symbolInput.value.trim().toUpperCase();
    if (!symbol) return;
    symbolInput.value = symbol;
    socketGeneration += 1;
    bookGeneration += 1;
    if (marketSocket) marketSocket.close();
    if (orderBookSocket) orderBookSocket.close();
    if (oiTimer) clearInterval(oiTimer);
    loadButton.disabled = true;
    message.textContent = `${symbol} · ${intervalInput.value} 資料載入中`;
    setStatus('', '同步中');
    try {
        const params = new URLSearchParams({ symbol, interval: intervalInput.value, limit: '500' });
        const response = await fetch(`/api/chart?${params}`);
        const data = await response.json();
        if (!response.ok) throw new Error([data.error, data.details].filter(Boolean).join('\n'));
        marketState = { ...data, tickCount: data.cvd.reduce((total, bucket) => total + bucket.trades, 0) };
        recomputeIndicators();
        renderSeries(true);
        updateSummary();
        updateHover(marketState.candles.at(-1));
        message.textContent = data.cvd.length
            ? 'Klines: Binance REST · 即時更新: local raw-trade WebSocket'
            : 'Klines 已載入；等待本地 collector 累積此交易對資料';
        connectMarketSocket();
        connectOrderBook();
        if (!marketState.oiError) {
            refreshOpenInterest();
            oiTimer = setInterval(refreshOpenInterest, 5000);
        }
    } catch (error) {
        console.error(error);
        message.textContent = error.message;
        setStatus('error', '載入失敗');
    } finally {
        loadButton.disabled = false;
    }
}

chart.subscribeCrosshairMove(param => {
    const plottedCandle = param.seriesData.get(candles);
    const candle = plottedCandle && marketState?.candles.find(item => item.time === plottedCandle.time);
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