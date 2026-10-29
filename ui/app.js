const COLORS = { fast: "#f5c518", slow: "#2962ff", up: "#26a69a", down: "#ef5350" };

let cfg;
let interval;
let chart;
let candles;
let fastLine;
let slowLine;
let lastTime = 0;
let lastPrice = 0;
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

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function row(label, value) {
  const div = el("div", null, "row");
  div.append(el("span", label), el("b", value));
  return div;
}

function confirmReal(message) {
  return cfg.testnet || confirm("DINHEIRO REAL\n\n" + message);
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
    const span = el("span", text);
    span.style.color = color;
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
  lastPrice = last.candle.close;
  renderLegend(last.candle, last.fast, last.slow);
  updateStopHints();
}

async function refreshAccount() {
  const data = await pywebview.api.get_account();
  if (data.error) return setStatus("Erro na conta: " + data.error, true);

  lastPrice = data.price;
  $("quote-name").textContent = data.quote;
  $("base-name").textContent = data.base;
  $("quote-free").textContent = num(data.quote_balance.free);
  $("base-free").textContent = num(data.base_balance.free, 8);
  $("locked").textContent = `${num(data.base_balance.locked, 8)} ${data.base}`;
  $("price").textContent = num(data.price);
  signed($("day-pnl"), data.day_pnl);

  renderPosition(data);
  renderOrders(data);
  renderHistory(data);
  updateStopHints();
  setStatus("Atualizado às " + data.updated);
}

function renderPosition(data) {
  const position = $("position");
  position.replaceChildren();
  if (!data.position) {
    position.className = "muted";
    position.textContent = "Sem compra aberta";
    return;
  }
  position.className = "";
  const p = data.position;
  position.append(
    row("Quantidade", `${num(p.qty, 8)} ${data.base}`),
    row("Preço de compra", num(p.entry_price)),
    row("Valor investido", `${num(p.entry_cost)} ${data.quote}`),
  );
  const pnl = row("Resultado aberto", "");
  signed(pnl.lastChild, p.open_pnl);
  position.appendChild(pnl);
}

function renderOrders(data) {
  const box = $("orders");
  box.replaceChildren();
  for (const o of data.orders) {
    const item = el("div", null, "order");
    const head = row(o.side === "SELL" ? "Venda" : "Compra", `${num(o.qty, 5)} ${data.base}`);
    if (o.bot) head.firstChild.append(" ", el("span", "robô", "tag"));
    item.appendChild(head);
    if (o.take_profit != null) {
      const r = row("Stop gain", num(o.take_profit));
      r.lastChild.className = "pos";
      item.appendChild(r);
    }
    if (o.stop_loss != null) {
      const r = row("Stop loss", num(o.stop_loss));
      r.lastChild.className = "neg";
      item.appendChild(r);
    }

    if (o.bot) {
      item.appendChild(el("div", "Controlada pelo robô", "hint"));
      box.appendChild(item);
      continue;
    }

    const buttons = el("div", null, "buttons");
    const cancel = el("button", "Cancelar");
    cancel.onclick = () => runOrder(() => pywebview.api.cancel(o.list_id, o.order_id));
    buttons.appendChild(cancel);
    if (o.side === "SELL") {
      const sellNow = el("button", "Vender agora");
      sellNow.onclick = () => {
        if (confirmReal(`Cancelar os stops e vender ${o.qty} ${data.base} a mercado?`)) {
          runOrder(() => pywebview.api.sell_now(o.list_id, o.order_id, o.qty));
        }
      };
      buttons.appendChild(sellNow);
    }
    item.appendChild(buttons);
    box.appendChild(item);
  }
  $("no-orders").style.display = data.orders.length ? "none" : "";
}

function renderHistory(data) {
  const body = $("history");
  body.replaceChildren();
  for (const h of data.history) {
    const tr = el("tr");
    tr.append(
      el("td", h.label),
      el("td", h.buy ? "Compra" : "Venda", h.buy ? "pos" : "neg"),
      el("td", num(h.price)),
      el("td", num(h.quote)),
    );
    body.appendChild(tr);
  }
  $("no-history").style.display = data.history.length ? "none" : "";
}

function updateStopHints() {
  const sl = parseFloat($("sl").value);
  const tp = parseFloat($("tp").value);
  $("sl-price").textContent = sl > 0 && lastPrice ? `vende se cair a ~${num(lastPrice * (1 - sl / 100))}` : "";
  $("tp-price").textContent = tp > 0 && lastPrice ? `vende se subir a ~${num(lastPrice * (1 + tp / 100))}` : "";
}

function showMessage(text, ok) {
  $("order-msg").textContent = text;
  $("order-msg").className = "msg " + (ok ? "ok" : "error");
}

async function runOrder(action) {
  const buttons = document.querySelectorAll("#tab-manual button");
  buttons.forEach((b) => (b.disabled = true));
  try {
    const result = await action();
    showMessage(result.error || result.ok, !result.error);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
  await refreshAccount();
  await refreshChart(false);
}

function readAmount() {
  const amount = parseFloat($("amount").value);
  if (!(amount > 0)) {
    showMessage("Digite o valor em USDT", false);
    return null;
  }
  return amount;
}

function setupOrderForm() {
  $("amount").value = cfg.trade_usdt;
  $("sl").oninput = updateStopHints;
  $("tp").oninput = updateStopHints;

  $("buy").onclick = () => {
    const amount = readAmount();
    if (amount == null) return;
    const sl = $("sl").value || null;
    const tp = $("tp").value || null;
    if (confirmReal(`Comprar ${amount} USDT de ${cfg.symbol}?`)) {
      runOrder(() => pywebview.api.buy(amount, sl, tp));
    }
  };

  $("sell").onclick = () => {
    const amount = readAmount();
    if (amount == null) return;
    if (confirmReal(`Vender ${amount} USDT de ${cfg.symbol}?`)) {
      runOrder(() => pywebview.api.sell(amount));
    }
  };
}

const BOT_FIELDS = {
  interval: "bot-interval",
  fast_sma: "bot-fast",
  slow_sma: "bot-slow",
  trade_usdt: "bot-amount",
  max_daily_loss_usdt: "bot-max-loss",
  stop_loss_pct: "bot-sl",
  take_profit_pct: "bot-tp",
  news_filter: "bot-news",
  news_before_min: "bot-news-before",
  news_after_min: "bot-news-after",
  news_countries: "bot-news-countries",
  news_impact: "bot-news-impact",
};

function fillBotForm(settings) {
  for (const [key, id] of Object.entries(BOT_FIELDS)) {
    if ($(id).type === "checkbox") $(id).checked = settings[key];
    else $(id).value = settings[key];
  }
}

function readBotForm() {
  const values = {};
  for (const [key, id] of Object.entries(BOT_FIELDS)) {
    values[key] = $(id).type === "checkbox" ? $(id).checked : $(id).value;
  }
  return values;
}

function countdown(seconds) {
  const minutes = Math.round(Math.abs(seconds) / 60);
  const h = Math.floor(minutes / 60);
  const text = h >= 24 ? `${Math.floor(h / 24)}d ${h % 24}h` : h ? `${h}h${String(minutes % 60).padStart(2, "0")}` : `${minutes} min`;
  if (minutes === 0) return "agora";
  return seconds > 0 ? `em ${text}` : `há ${text}`;
}

async function refreshNews() {
  const data = await pywebview.api.get_news();
  const now = Date.now() / 1000;
  const box = $("news");
  box.replaceChildren();

  for (const e of data.events) {
    const item = el("div", null, "news-item" + (data.active && data.active.time === e.time && data.active.title === e.title ? " active" : ""));
    const top = el("div", null, "news-top");
    const left = el("span");
    left.append(el("span", null, "impact " + e.impact), `${e.label}  ${e.country}`);
    top.append(left, el("span", countdown(e.time - now)));
    item.append(top, el("div", e.title, "news-title"));
    if (e.forecast || e.previous) {
      item.appendChild(el("div", `Previsão ${e.forecast || "-"}  |  Anterior ${e.previous || "-"}`, "hint"));
    }
    box.appendChild(item);
  }
  if (!data.events.length) box.appendChild(el("div", "Nenhuma notícia relevante nesta semana", "muted"));
  $("news-info").textContent = data.fetched
    ? `Fonte: ForexFactory, atualizado em ${data.fetched}`
    : "Calendário indisponível no momento";

  const alert = $("news-alert");
  if (data.active) {
    const when = countdown(data.active.time - now);
    alert.textContent = (data.filter ? "Compras do robô pausadas: " : "Notícia: ") + `${data.active.country} ${data.active.title} (${when})`;
    alert.classList.add("show");
  } else {
    alert.classList.remove("show");
  }
}

function showBotMessage(text, ok) {
  $("bot-msg").textContent = text;
  $("bot-msg").className = "msg " + (ok ? "ok" : "error");
}

async function refreshBot() {
  const data = await pywebview.api.get_bot();
  const busy = data.running || data.elsewhere;
  $("bot-dot").className = "dot" + (data.running ? " on" : data.elsewhere ? " away" : "");
  $("bot-state").textContent = data.running ? "Ligado" : data.elsewhere ? "Rodando em outra janela" : "Desligado";
  $("bot-form").disabled = busy;
  $("bot-start").style.display = busy ? "none" : "";
  $("bot-stop").style.display = data.running ? "" : "none";

  const log = $("bot-log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.textContent = data.log.join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;
  return data;
}

function applySettingsToChart(settings) {
  cfg.fast = settings.fast_sma;
  cfg.slow = settings.slow_sma;
  if (cfg.intervals.includes(settings.interval)) selectInterval(settings.interval);
  else refreshChart(true);
}

function setupBot(settings) {
  const select = $("bot-interval");
  for (const value of cfg.intervals) select.appendChild(el("option", value));
  fillBotForm(settings);

  $("bot-start").onclick = async () => {
    if (!confirmReal("Ligar o robô automático?")) return;
    $("bot-start").disabled = true;
    showBotMessage("Ligando...", true);
    const result = await pywebview.api.start_bot(readBotForm());
    $("bot-start").disabled = false;
    showBotMessage(result.error || result.ok, !result.error);
    const data = await refreshBot();
    if (!result.error) {
      applySettingsToChart(data.settings);
      refreshNews();
    }
  };

  $("bot-stop").onclick = async () => {
    $("bot-stop").disabled = true;
    showBotMessage("Desligando...", true);
    const result = await pywebview.api.stop_bot();
    $("bot-stop").disabled = false;
    showBotMessage(result.error || result.ok, !result.error);
    await refreshBot();
  };
}

function setupTabs() {
  const buttons = document.querySelectorAll(".tabs button");
  for (const button of buttons) {
    button.onclick = () => {
      for (const b of buttons) b.classList.toggle("active", b === button);
      for (const tab of document.querySelectorAll(".tab")) tab.classList.toggle("active", tab.id === button.dataset.tab);
    };
  }
}

function selectInterval(value) {
  interval = value;
  for (const b of $("intervals").children) b.classList.toggle("active", b.textContent === value);
  lastTime = 0;
  refreshChart(true);
}

function buildIntervals() {
  const box = $("intervals");
  for (const value of cfg.intervals) {
    const button = el("button", value);
    button.classList.toggle("active", value === interval);
    button.onclick = () => selectInterval(value);
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
  setupTabs();
  setupOrderForm();
  createChart();
  const bot = await refreshBot();
  setupBot(bot.settings);
  await refreshChart(true);
  await refreshAccount();
  await refreshNews();
  setInterval(() => refreshChart(false), 3000);
  setInterval(refreshAccount, 10000);
  setInterval(refreshBot, 3000);
  setInterval(refreshNews, 30000);
}

window.addEventListener("pywebviewready", init);
