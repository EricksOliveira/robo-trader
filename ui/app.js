const COLORS = { fast: "#f5c518", slow: "#2962ff", up: "#26a69a", down: "#ef5350" };

let cfg;
let interval;
let chart;
let candles;
let fastLine;
let slowLine;
let lastTime = 0;
let last = { candle: null, fast: null, slow: null };

const $ = (id) => document.getElementById(id);

function num(value, digits = 2) {
  return Number(value).toLocaleString("pt-BR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function signed(el, value) {
  el.textContent = (value > 0 ? "+" : "") + num(value);
  el.className = value > 0 ? "pos" : value < 0 ? "neg" : "";
}

function setStatus(text, isError = false) {
  $("status").textContent = text;
  $("status").className = "status" + (isError ? " error" : "");
}

function createChart() {
  chart = LightweightCharts.createChart($("chart"), {
    autoSize: true,
    layout: { background: { color: "#131722" }, textColor: "#d1d4dc" },
    grid: { vertLines: { color: "#1e222d" }, horzLines: { color: "#1e222d" } },
    rightPriceScale: { borderColor: "#2a2e39" },
    timeScale: { borderColor: "#2a2e39", timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });

  candles = chart.addCandlestickSeries({
    upColor: COLORS.up, downColor: COLORS.down,
    wickUpColor: COLORS.up, wickDownColor: COLORS.down,
    borderVisible: false,
  });
  fastLine = chart.addLineSeries({ color: COLORS.fast, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
  slowLine = chart.addLineSeries({ color: COLORS.slow, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });

  chart.subscribeCrosshairMove((param) => {
    if (!param.time) return renderLegend(last.candle, last.fast, last.slow);
    renderLegend(param.seriesData.get(candles), param.seriesData.get(fastLine)?.value, param.seriesData.get(slowLine)?.value);
  });
}

function renderLegend(candle, fast, slow) {
  const legend = $("legend");
  legend.replaceChildren();
  const add = (text, color) => {
    const span = document.createElement("span");
    span.textContent = text;
    if (color) span.style.color = color;
    legend.appendChild(span);
  };
  if (candle) {
    const color = candle.close >= candle.open ? COLORS.up : COLORS.down;
    add(`Abe ${num(candle.open)}  Máx ${num(candle.high)}  Mín ${num(candle.low)}  Fec ${num(candle.close)}`, color);
  }
  if (fast != null) add(`SMA ${cfg.fast}: ${num(fast)}`, COLORS.fast);
  if (slow != null) add(`SMA ${cfg.slow}: ${num(slow)}`, COLORS.slow);
}

function updateTail(series, points) {
  for (const p of points.slice(-2)) {
    if (p.time >= lastTime) series.update(p);
  }
}

async function refreshChart(reset) {
  const requested = interval;
  const data = await pywebview.api.get_chart(requested);
  if (requested !== interval) return;
  if (data.error) return setStatus("Erro no gráfico: " + data.error, true);
  if (!data.candles.length) return;

  if (reset) {
    candles.setData(data.candles);
    fastLine.setData(data.fast);
    slowLine.setData(data.slow);
    chart.timeScale().scrollToRealTime();
  } else {
    updateTail(candles, data.candles);
    updateTail(fastLine, data.fast);
    updateTail(slowLine, data.slow);
  }
  candles.setMarkers(data.markers);

  lastTime = data.candles.at(-1).time;
  last = { candle: data.candles.at(-1), fast: data.fast.at(-1)?.value, slow: data.slow.at(-1)?.value };
  renderLegend(last.candle, last.fast, last.slow);
}

async function refreshAccount() {
  const data = await pywebview.api.get_account();
  if (data.error) return setStatus("Erro na conta: " + data.error, true);

  $("quote-name").textContent = data.quote;
  $("base-name").textContent = data.base;
  $("quote-free").textContent = num(data.quote_free);
  $("base-free").textContent = num(data.base_free, 8);
  $("price").textContent = num(data.price);
  signed($("day-pnl"), data.day_pnl);

  const position = $("position");
  position.replaceChildren();
  if (data.position) {
    position.className = "";
    const p = data.position;
    const rows = [
      ["Quantidade", `${num(p.qty, 8)} ${data.base}`],
      ["Preço de compra", num(p.entry_price)],
      ["Valor investido", `${num(p.entry_cost)} ${data.quote}`],
    ];
    for (const [label, value] of rows) position.appendChild(row(label, value));
    const pnl = row("Resultado aberto", "");
    signed(pnl.lastChild, p.open_pnl);
    position.appendChild(pnl);
  } else {
    position.className = "muted";
    position.textContent = "Sem compra aberta";
  }

  const body = $("trades");
  body.replaceChildren();
  for (const t of data.trades) {
    const tr = document.createElement("tr");
    const cells = [
      t.data.slice(11, 16),
      t.lado === "COMPRA" ? "Compra" : "Venda",
      num(t.preco_medio),
      t.lucro_usdt === "" ? "" : t.lucro_usdt,
    ];
    for (const text of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    }
    tr.children[1].className = t.lado === "COMPRA" ? "pos" : "neg";
    if (t.lucro_usdt !== "") signed(tr.children[3], Number(t.lucro_usdt));
    body.appendChild(tr);
  }
  $("no-trades").style.display = data.trades.length ? "none" : "";

  setStatus("Atualizado às " + data.updated);
}

function row(label, value) {
  const div = document.createElement("div");
  div.className = "row";
  const span = document.createElement("span");
  span.textContent = label;
  const b = document.createElement("b");
  b.textContent = value;
  div.append(span, b);
  return div;
}

function buildIntervals() {
  const box = $("intervals");
  for (const value of cfg.intervals) {
    const button = document.createElement("button");
    button.textContent = value;
    button.classList.toggle("active", value === interval);
    button.onclick = () => {
      interval = value;
      for (const b of box.children) b.classList.toggle("active", b === button);
      lastTime = 0;
      refreshChart(true);
    };
    box.appendChild(button);
  }
}

async function init() {
  cfg = await pywebview.api.get_config();
  interval = cfg.interval;

  $("symbol").textContent = cfg.symbol;
  $("mode").textContent = cfg.testnet ? "TESTNET" : "REAL";
  $("mode").classList.add(cfg.testnet ? "testnet" : "real");

  buildIntervals();
  createChart();
  await refreshChart(true);
  await refreshAccount();
  setInterval(() => refreshChart(false), 3000);
  setInterval(refreshAccount, 10000);
}

window.addEventListener("pywebviewready", init);
