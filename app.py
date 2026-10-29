import csv
import math
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal

import requests
import webview

import config
import news
from binance_client import INTERVAL_MS, BinanceError
from bot import (
    BOT_TAG,
    INTERVALS,
    LOCK_FILE,
    LOG_FILE,
    NEWS_CACHE_FILE,
    RESOURCE_DIR,
    TRADES_FILE,
    SYMBOLS,
    Bot,
    InstanceLock,
    Market,
    check_position_symbol,
    fee_in,
    load_settings,
    load_state,
    make_client,
    market_data_client,
    save_settings,
    validate_settings,
)
from strategy import BREAKOUTS, TREND_PERIOD

TAKE_PROFIT_TYPES = {"LIMIT", "LIMIT_MAKER", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"}
STOP_LOSS_TYPES = {"STOP_LOSS", "STOP_LOSS_LIMIT"}


def utc_offset_seconds() -> int:
    return int(datetime.now().astimezone().utcoffset().total_seconds())


def line(df, values, offset: int, digits: int) -> list[dict]:
    return [
        {"time": int(t) // 1000 + offset, "value": round(v, digits)}
        for t, v in zip(df["open_time"], values)
        if not math.isnan(v)
    ]


def indicator_lines(df, s: dict) -> list[tuple[str, object, str]]:
    close = df["close"]
    if s["strategy"] in BREAKOUTS:
        entry, exit = BREAKOUTS[s["strategy"]]
        lines = [
            (f"Máx {entry}", df["high"].rolling(entry).max().shift(1), "#4dd0e1"),
            (f"Mín {exit}", df["low"].rolling(exit).min().shift(1), "#ff9800"),
        ]
    else:
        lines = [
            (f"SMA {s['fast_sma']}", close.rolling(s["fast_sma"]).mean(), "#f5c518"),
            (f"SMA {s['slow_sma']}", close.rolling(s["slow_sma"]).mean(), "#2962ff"),
        ]
    if s["trend_filter"]:
        lines.append((f"MM{TREND_PERIOD}", close.rolling(TREND_PERIOD).mean(), "#ce93d8"))
    return lines


def group_fills(fills: list[dict]) -> list[dict]:
    orders = {}
    for f in fills:
        o = orders.setdefault(f["orderId"], {"time": f["time"], "buy": f["isBuyer"], "qty": 0.0, "quote": 0.0})
        o["qty"] += float(f["qty"])
        o["quote"] += float(f["quoteQty"])
    result = []
    for o in sorted(orders.values(), key=lambda o: o["time"]):
        o["price"] = o["quote"] / o["qty"] if o["qty"] else 0.0
        o["label"] = datetime.fromtimestamp(o["time"] / 1000).strftime("%d/%m %H:%M")
        result.append(o)
    return result


def group_open_orders(orders: list[dict]) -> list[dict]:
    groups = {}
    for o in orders:
        key = o["orderListId"] if o["orderListId"] != -1 else f"order-{o['orderId']}"
        g = groups.setdefault(key, {
            "list_id": o["orderListId"],
            "order_id": o["orderId"],
            "side": o["side"],
            "bot": o["clientOrderId"].startswith(BOT_TAG + "-"),
            "qty": float(o["origQty"]) - float(o["executedQty"]),
            "take_profit": None,
            "stop_loss": None,
        })
        if o["type"] in TAKE_PROFIT_TYPES:
            g["take_profit"] = float(o["price"]) or float(o["stopPrice"])
        elif o["type"] in STOP_LOSS_TYPES:
            g["stop_loss"] = float(o["stopPrice"])
    return list(groups.values())


def trade_results() -> dict:
    sales = []
    if TRADES_FILE.exists():
        with TRADES_FILE.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    if r.get("lucro_usdt"):
                        sales.append((datetime.fromisoformat(r["data"]), float(r["lucro_usdt"])))
                except ValueError:
                    continue
    now = datetime.now()
    starts = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "7d": now - timedelta(days=7),
        "30d": now - timedelta(days=30),
        "all": datetime.min,
    }
    results = {}
    for period, start in starts.items():
        pnls = [pnl for when, pnl in sales if when >= start]
        results[period] = {
            "gain": round(sum(p for p in pnls if p > 0), 2) + 0.0,
            "loss": round(sum(p for p in pnls if p < 0), 2) + 0.0,
            "count": len(pnls),
            "wins": sum(1 for p in pnls if p > 0),
        }
    return results


def percent(value, name: str, limit: float) -> float:
    if value in (None, ""):
        return 0.0
    value = float(value)
    if not 0 < value < limit:
        raise ValueError(f"{name} precisa estar entre 0 e {limit:g}%")
    return value


class Api:
    def __init__(self):
        self._client = None
        self._markets = {}
        self._data = None
        self._lock = threading.Lock()
        self._history = []
        self._history_symbol = None
        self._bot_thread = None
        self._bot_stop = threading.Event()
        self._bot_lock = InstanceLock(LOCK_FILE)

    def _connect(self):
        with self._lock:
            if self._client is None:
                client = make_client()
                self._data = market_data_client(client)
                self._markets = {}
                self._client = client
            symbol = load_settings()["symbol"]
            if symbol not in self._markets:
                self._markets[symbol] = Market(self._client.symbol_info(symbol))
            return self._client, symbol, self._markets[symbol]

    def _bot_running(self) -> bool:
        return self._bot_thread is not None and self._bot_thread.is_alive()

    def _safe(self, fn):
        try:
            return fn()
        except ValueError as e:
            return {"error": str(e)}
        except (BinanceError, requests.RequestException) as e:
            self._client = None
            return {"error": str(e)}

    def get_config(self) -> dict:
        settings = load_settings()
        return {
            "symbol": settings["symbol"],
            "symbols": SYMBOLS,
            "interval": settings["interval"],
            "intervals": INTERVALS,
            "testnet": config.USE_TESTNET,
            "trade_usdt": settings["trade_usdt"],
        }

    def get_bot(self) -> dict:
        lines = []
        if LOG_FILE.exists():
            with LOG_FILE.open(encoding="utf-8", errors="replace") as f:
                lines = [line.rstrip("\n") for line in f.readlines()[-40:]]
        running = self._bot_running()
        return {
            "running": running,
            "elsewhere": not running and self._bot_lock.held_elsewhere(),
            "settings": load_settings(),
            "log": lines,
            "results": trade_results(),
        }

    def get_news(self) -> dict:
        s = load_settings()
        events, fetched_at = news.load_events(NEWS_CACHE_FILE)
        events = news.relevant(events, news.parse_countries(s["news_countries"]), s["news_impact"])
        now = time.time()
        upcoming = [e for e in events if e["time"] + s["news_after_min"] * 60 >= now][:8]
        for e in upcoming:
            e["label"] = datetime.fromtimestamp(e["time"]).strftime("%d/%m %H:%M")
        return {
            "events": upcoming,
            "active": news.blocking_event(events, now, s["news_before_min"], s["news_after_min"]),
            "filter": s["news_filter"],
            "fetched": datetime.fromtimestamp(fetched_at).strftime("%d/%m %H:%M") if fetched_at else None,
        }

    def set_symbol(self, symbol: str) -> dict:
        return self._safe(lambda: self._set_symbol(symbol))

    def _set_symbol(self, symbol: str) -> dict:
        if self._bot_running() or self._bot_lock.held_elsewhere():
            raise ValueError("Desligue o robô antes de trocar de par")
        check_position_symbol(symbol)
        save_settings({**load_settings(), "symbol": symbol})
        return {"ok": f"Par trocado para {symbol}"}

    def start_bot(self, values: dict) -> dict:
        return self._safe(lambda: self._start_bot(values))

    def _start_bot(self, values: dict) -> dict:
        if self._bot_running():
            raise ValueError("O robô já está ligado")
        settings = validate_settings(values)
        check_position_symbol(settings["symbol"])
        save_settings(settings)
        if not self._bot_lock.acquire():
            raise ValueError("O robô já está rodando em outra janela. Feche a outra antes.")
        try:
            client = make_client()
            market = Market(client.symbol_info(settings["symbol"]))
        except Exception:
            self._bot_lock.release()
            raise

        self._bot_stop.clear()
        bot = Bot(client, market, settings, self._bot_stop)

        def run():
            try:
                bot.run()
            finally:
                self._bot_lock.release()

        self._bot_thread = threading.Thread(target=run, daemon=True)
        self._bot_thread.start()
        return {"ok": "Robô ligado"}

    def stop_bot(self) -> dict:
        self._bot_stop.set()
        if self._bot_thread is not None:
            self._bot_thread.join(timeout=20)
        return {"ok": "Robô desligado"}

    def get_chart(self, interval: str) -> dict:
        return self._safe(lambda: self._chart(interval))

    def _chart(self, interval: str) -> dict:
        if interval not in INTERVAL_MS:
            raise ValueError(f"Tempo gráfico inválido: {interval}")
        _, symbol, market = self._connect()
        offset = utc_offset_seconds()
        settings = load_settings()
        df = self._data.klines(symbol, interval, limit=1000 if settings["trend_filter"] else 500)

        candles = [
            {"time": int(r.open_time) // 1000 + offset, "open": r.open, "high": r.high, "low": r.low, "close": r.close}
            for r in df.itertuples()
        ]

        period = INTERVAL_MS[interval] // 1000
        first = candles[0]["time"] if candles else 0
        markers = []
        for o in self._history if self._history_symbol == symbol else []:
            ts = o["time"] // 1000
            ts = ts - ts % period + offset
            if ts < first:
                continue
            markers.append({
                "time": ts,
                "position": "belowBar" if o["buy"] else "aboveBar",
                "shape": "arrowUp" if o["buy"] else "arrowDown",
                "color": "#26a69a" if o["buy"] else "#ef5350",
                "text": "C" if o["buy"] else "V",
            })

        return {
            "symbol": symbol,
            "digits": market.digits,
            "tick": float(market.tick),
            "candles": candles,
            "lines": [
                {"name": name, "color": color, "data": line(df, values, offset, market.digits)}
                for name, values, color in indicator_lines(df, settings)
            ],
            "markers": markers,
        }

    def get_account(self) -> dict:
        return self._safe(self._account)

    def _account(self) -> dict:
        client, symbol, market = self._connect()
        balances = client.full_balances()
        price = float(client.klines(symbol, "1m", limit=1)["close"].iloc[-1])
        history = group_fills(client.my_trades(symbol))
        self._history, self._history_symbol = history, symbol
        state = load_state()

        qty = float(Decimal(state["position_qty"])) if state["symbol"] == symbol else 0.0
        entry_cost = state["entry_cost"]
        position = None
        if qty > 0:
            position = {
                "qty": qty,
                "entry_price": entry_cost / qty,
                "entry_cost": entry_cost,
                "open_pnl": qty * price - entry_cost,
            }

        empty = {"free": 0.0, "locked": 0.0}
        return {
            "symbol": symbol,
            "digits": market.digits,
            "base": market.base,
            "quote": market.quote,
            "base_balance": balances.get(market.base, empty),
            "quote_balance": balances.get(market.quote, empty),
            "price": price,
            "position": position,
            "orders": group_open_orders(client.open_orders(symbol)),
            "history": history[-20:][::-1],
            "updated": datetime.now().strftime("%H:%M:%S"),
        }

    def buy(self, amount, stop_loss_pct, take_profit_pct) -> dict:
        return self._safe(lambda: self._buy(amount, stop_loss_pct, take_profit_pct))

    def _buy(self, amount, stop_loss_pct, take_profit_pct) -> dict:
        client, symbol, market = self._connect()
        amount = float(amount)
        stop_loss = percent(stop_loss_pct, "Stop loss", 50)
        take_profit = percent(take_profit_pct, "Stop gain", 100)
        if amount < market.min_notional:
            raise ValueError(f"Valor mínimo por ordem: {market.min_notional:g} {market.quote}")

        order = client.market_buy(symbol, amount)
        executed = Decimal(order["executedQty"])
        qty = market.floor_qty(executed - fee_in(order, market.base))
        avg = float(order["cummulativeQuoteQty"]) / float(executed)
        done = f"Comprado {qty} {market.base} a {avg:,.{market.digits}f}"
        if not stop_loss and not take_profit:
            return {"ok": done}

        quantity = format(qty, "f")
        sl_price = market.round_price(avg * (1 - stop_loss / 100))
        tp_price = market.round_price(avg * (1 + take_profit / 100))
        try:
            if stop_loss and take_profit:
                client.oco_sell(symbol, quantity, tp_price, sl_price)
                stops = f"stop loss {sl_price} e stop gain {tp_price}"
            elif stop_loss:
                client.stop_loss_sell(symbol, quantity, sl_price)
                stops = f"stop loss {sl_price}"
            else:
                client.take_profit_sell(symbol, quantity, tp_price)
                stops = f"stop gain {tp_price}"
        except BinanceError as e:
            return {"error": f"{done}, mas o stop NÃO foi criado: {e}"}
        return {"ok": f"{done}, com {stops}"}

    def sell(self, amount) -> dict:
        return self._safe(lambda: self._sell(amount))

    def _sell(self, amount) -> dict:
        client, symbol, market = self._connect()
        amount = float(amount)
        if amount < market.min_notional:
            raise ValueError(f"Valor mínimo por ordem: {market.min_notional:g} {market.quote}")
        order = client.market_sell_quote(symbol, amount)
        avg = float(order["cummulativeQuoteQty"]) / float(order["executedQty"])
        return {"ok": f"Vendido {order['executedQty']} {market.base} a {avg:,.{market.digits}f}"}

    def cancel(self, list_id: int, order_id: int) -> dict:
        return self._safe(lambda: self._cancel(list_id, order_id) or {"ok": "Ordem cancelada"})

    def _cancel(self, list_id: int, order_id: int) -> None:
        client, symbol, _ = self._connect()
        if list_id != -1:
            client.cancel_order_list(symbol, list_id)
        else:
            client.cancel_order(symbol, order_id)

    def sell_now(self, list_id: int, order_id: int, qty: float) -> dict:
        return self._safe(lambda: self._sell_now(list_id, order_id, qty))

    def _sell_now(self, list_id: int, order_id: int, qty: float) -> dict:
        client, symbol, market = self._connect()
        self._cancel(list_id, order_id)
        quantity = market.floor_qty(Decimal(str(qty)))
        order = client.market_sell(symbol, format(quantity, "f"))
        avg = float(order["cummulativeQuoteQty"]) / float(order["executedQty"])
        return {"ok": f"Stops cancelados e vendido {quantity} {market.base} a {avg:,.{market.digits}f}"}


def main() -> None:
    api = Api()
    webview.create_window(
        "Robô Trader",
        str(RESOURCE_DIR / "ui" / "index.html"),
        js_api=api,
        width=1280,
        height=860,
        min_size=(900, 600),
        background_color="#131722",
    )
    webview.start()
    api.stop_bot()


if __name__ == "__main__":
    main()
