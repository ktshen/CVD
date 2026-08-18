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

chart.panes()[0].setStretchFactor(6);
chart.panes()[1].setStretchFactor(1.6);
chart.panes()[2].setStretchFactor(2.4);

function positionPaneLabels() {
    const chartBounds = chartElement.getBoundingClientRect();
    const labels = document.querySelectorAll('.chart-label');
    chart.panes().forEach((pane, index) => {
        const paneBounds = pane.getHTMLElement().getBoundingClientRect();
        labels[index].style.top = `${paneBounds.top - chartBounds.top + 8}px`;
    });
}

requestAnimationFrame(positionPaneLabels);

function formatNumber(value) {
    return new Intl.NumberFormat('en-US', { maximumFractionDigits: 4 }).format(value);
}

function setStatus(state, text) {
    feedStatus.className = `feed-status ${state}`;
    feedStatus.querySelector('span').textContent = text;
}

function setSeriesData(data) {
    candles.setData(data.candles.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    volume.setData(data.candles.map(item => ({
        time: item.time,
        value: item.volume,
        color: item.close >= item.open ? 'rgba(20, 132, 97, .42)' : 'rgba(212, 82, 82, .42)',
    })));
    sma30.setData(data.candles.filter(item => item.sma30 !== undefined).map(item => ({ time: item.time, value: item.sma30 })));
    sma45.setData(data.candles.filter(item => item.sma45 !== undefined).map(item => ({ time: item.time, value: item.sma45 })));
    sma60.setData(data.candles.filter(item => item.sma60 !== undefined).map(item => ({ time: item.time, value: item.sma60 })));
    delta.setData(data.cvd.map(item => ({
        time: item.time,
        value: item.delta,
        color: item.delta >= 0 ? 'rgba(20, 132, 97, .38)' : 'rgba(212, 82, 82, .38)',
    })));
    cvd.setData(data.cvd.map(item => ({ time: item.time, value: item.cvd })));
    chart.timeScale().fitContent();
}

async function loadChart() {
    const symbol = symbolInput.value.trim().toUpperCase();
    if (!symbol) return;
    symbolInput.value = symbol;
    loadButton.disabled = true;
    message.textContent = `${symbol} · ${intervalInput.value} 資料載入中`;
    setStatus('', '同步中');
    try {
        const params = new URLSearchParams({ symbol, interval: intervalInput.value, limit: '500' });
        const response = await fetch(`/api/chart?${params}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '無法載入圖表');
        setSeriesData(data);
        const last = data.candles[data.candles.length - 1];
        const lastCvd = data.cvd[data.cvd.length - 1];
        document.getElementById('market-symbol').textContent = symbol;
        document.getElementById('last-price').textContent = formatNumber(last.close);
        const tickCount = data.cvd.reduce((total, bucket) => total + bucket.trades, 0);
        document.getElementById('tick-count').textContent = formatNumber(tickCount);
        document.getElementById('net-cvd').textContent = lastCvd ? formatNumber(lastCvd.cvd) : '--';
        document.getElementById('net-cvd').style.color = lastCvd && lastCvd.cvd < 0 ? '#d45252' : '#148461';
        emptyState.hidden = data.cvd.length > 0;
        message.textContent = data.cvd.length
            ? 'Klines: Binance · CVD: local aggregate trades'
            : 'Klines 已載入；等待本地 collector 累積此交易對資料';
        setStatus('live', '資料已更新');
    } catch (error) {
        message.textContent = error.message;
        setStatus('error', '載入失敗');
    } finally {
        loadButton.disabled = false;
    }
}

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
    .then(response => response.json())
    .then(items => {
        if (!Array.isArray(items)) return;
        document.getElementById('symbols').replaceChildren(...items.map(item => {
            const option = document.createElement('option');
            option.value = item.symbol;
            option.label = `${item.baseAsset} / ${item.quoteAsset}`;
            return option;
        }));
    })
    .catch(() => {});

loadChart();