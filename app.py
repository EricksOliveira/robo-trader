import math
import threading
from datetime import datetime
from decimal import Decimal

import requests
import webview

import config
from binance_client import INTERVAL_MS, BinanceError
from bot import (
    BOT_TAG,
    INTERVALS,
    LOCK_FILE,
    LOG_FILE,
    Bot,
    InstanceLock,
    RESOURCE_DIR,
    Market,
    fee_in,
    load_settings,
    load_state,
    make_client,
    save_settings,
)
from strategy import add_signals
TAKE_PROFIT_TYPES = {"LIMIT", "LIMIT_MAKER", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"}
STOP_LOSS_TYPES = {"STOP_LOSS", "STOP_LOSS_LIMIT"}


def utc_offset_seconds() -> int:
    return int(datetime.now().astimezone().utcoffset().total_seconds())


def line(df, column: str, offset: int) -> list[dict]:
    return [
        {"time": int(t) // 1000 + offset, "value": round(v, 2)}
        for t, v in zip(df["open_time"], df[column])
        if not math.isnan(v)
    ]


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
        self._market = None
        self._lock = threading.Lock()
        self._history = []
        self._bot_thread = None
        self._bot_stop = threading.Event()
        self._bot_lock = InstanceLock(LOCK_FILE)

    def _connect(self):
        with self._lock:
            if self._client is None:
                client = make_client()
                self._market = Market(client.symbol_info(config.SYMBOL))
                self._client = client
            return self._client, self._market

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
            "symbol": config.SYMBOL,
            "interval": settings["interval"],
            "intervals": INTERVALS,
            "fast": settings["fast_sma"],
            "slow": settings["slow_sma"],
            "testnet": config.USE_TESTNET,
            "trade_usdt": settings["trade_usdt"],
        }

    def get_bot(self) -> dict:
        lines = []
        if LOG_FILE.exists():
            with LOG_FILE.open(encoding="utf-8", errors="replace") as f:
                lines = [line.rstrip("\n") for line in f.readlines()[-40:]]
        running = self._bot_thread is not None and self._bot_thread.is_alive()
        return {
            "running": running,
            "elsewhere": not running and self._bot_lock.held_elsewhere(),
            "settings": load_settings(),
            "log": lines,
        }

    def start_bot(self, values: dict) -> dict:
        return self._safe(lambda: self._start_bot(values))

    def _start_bot(self, values: dict) -> dict:
        if self._bot_thread is not None and self._bot_thread.is_alive():
            raise ValueError("O robô já está ligado")
        settings = save_settings(values)
        if not self._bot_lock.acquire():
            raise ValueError("O robô já está rodando em outra janela. Feche a outra antes.")
        try:
            client = make_client()
            market = Market(client.symbol_info(config.SYMBOL))
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
        client, _ = self._connect()
        offset = utc_offset_seconds()
        settings = load_settings()
        df = add_signals(client.klines(config.SYMBOL, interval, limit=500), settings["fast_sma"], settings["slow_sma"])

        candles = [
            {"time": int(r.open_time) // 1000 + offset, "open": r.open, "high": r.high, "low": r.low, "close": r.close}
            for r in df.itertuples()
        ]

        period = INTERVAL_MS[interval] // 1000
        first = candles[0]["time"] if candles else 0
        markers = []
        for o in self._history:
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
            "candles": candles,
            "fast": line(df, "sma_fast", offset),
            "slow": line(df, "sma_slow", offset),
            "markers": markers,
        }

    def get_account(self) -> dict:
        return self._safe(self._account)

    def _account(self) -> dict:
        client, market = self._connect()
        balances = client.full_balances()
        price = float(client.klines(config.SYMBOL, "1m", limit=1)["close"].iloc[-1])
        self._history = group_fills(client.my_trades(config.SYMBOL))
        state = load_state()

        qty = float(Decimal(state["position_qty"]))
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
            "base": market.base,
            "quote": market.quote,
            "base_balance": balances.get(market.base, empty),
            "quote_balance": balances.get(market.quote, empty),
            "price": price,
            "position": position,
            "day_pnl": state["day_pnl"],
            "orders": group_open_orders(client.open_orders(config.SYMBOL)),
            "history": self._history[-20:][::-1],
            "updated": datetime.now().strftime("%H:%M:%S"),
        }

    def buy(self, amount, stop_loss_pct, take_profit_pct) -> dict:
        return self._safe(lambda: self._buy(amount, stop_loss_pct, take_profit_pct))

    def _buy(self, amount, stop_loss_pct, take_profit_pct) -> dict:
        client, market = self._connect()
        amount = float(amount)
        stop_loss = percent(stop_loss_pct, "Stop loss", 50)
        take_profit = percent(take_profit_pct, "Stop gain", 100)
        if amount < market.min_notional:
            raise ValueError(f"Valor mínimo por ordem: {market.min_notional:g} {market.quote}")

        order = client.market_buy(config.SYMBOL, amount)
        executed = Decimal(order["executedQty"])
        qty = market.floor_qty(executed - fee_in(order, market.base))
        avg = float(order["cummulativeQuoteQty"]) / float(executed)
        done = f"Comprado {qty} {market.base} a {avg:,.2f}"
        if not stop_loss and not take_profit:
            return {"ok": done}

        quantity = format(qty, "f")
        sl_price = market.round_price(avg * (1 - stop_loss / 100))
        tp_price = market.round_price(avg * (1 + take_profit / 100))
        try:
            if stop_loss and take_profit:
                client.oco_sell(config.SYMBOL, quantity, tp_price, sl_price)
                stops = f"stop loss {sl_price} e stop gain {tp_price}"
            elif stop_loss:
                client.stop_loss_sell(config.SYMBOL, quantity, sl_price)
                stops = f"stop loss {sl_price}"
            else:
                client.take_profit_sell(config.SYMBOL, quantity, tp_price)
                stops = f"stop gain {tp_price}"
        except BinanceError as e:
            return {"error": f"{done}, mas o stop NÃO foi criado: {e}"}
        return {"ok": f"{done}, com {stops}"}

    def sell(self, amount) -> dict:
        return self._safe(lambda: self._sell(amount))

    def _sell(self, amount) -> dict:
        client, market = self._connect()
        amount = float(amount)
        if amount < market.min_notional:
            raise ValueError(f"Valor mínimo por ordem: {market.min_notional:g} {market.quote}")
        order = client.market_sell_quote(config.SYMBOL, amount)
        avg = float(order["cummulativeQuoteQty"]) / float(order["executedQty"])
        return {"ok": f"Vendido {order['executedQty']} {market.base} a {avg:,.2f}"}

    def cancel(self, list_id: int, order_id: int) -> dict:
        return self._safe(lambda: self._cancel(list_id, order_id) or {"ok": "Ordem cancelada"})

    def _cancel(self, list_id: int, order_id: int) -> None:
        client, _ = self._connect()
        if list_id != -1:
            client.cancel_order_list(config.SYMBOL, list_id)
        else:
            client.cancel_order(config.SYMBOL, order_id)

    def sell_now(self, list_id: int, order_id: int, qty: float) -> dict:
        return self._safe(lambda: self._sell_now(list_id, order_id, qty))

    def _sell_now(self, list_id: int, order_id: int, qty: float) -> dict:
        client, market = self._connect()
        self._cancel(list_id, order_id)
        quantity = market.floor_qty(Decimal(str(qty)))
        order = client.market_sell(config.SYMBOL, format(quantity, "f"))
        avg = float(order["cummulativeQuoteQty"]) / float(order["executedQty"])
        return {"ok": f"Stops cancelados e vendido {quantity} {market.base} a {avg:,.2f}"}


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
