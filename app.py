import csv
import math
import threading
from datetime import datetime
from decimal import Decimal

import requests
import webview

import config
from binance_client import INTERVAL_MS, BinanceError
from bot import BASE_DIR, TRADES_FILE, Market, load_state, make_client
from strategy import add_signals

INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"]


def utc_offset_seconds() -> int:
    return int(datetime.now().astimezone().utcoffset().total_seconds())


def line(df, column: str, offset: int) -> list[dict]:
    return [
        {"time": int(t) // 1000 + offset, "value": round(v, 2)}
        for t, v in zip(df["open_time"], df[column])
        if not math.isnan(v)
    ]


def read_trades() -> list[dict]:
    if not TRADES_FILE.exists():
        return []
    with TRADES_FILE.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


class Api:
    def __init__(self):
        self._client = None
        self._market = None
        self._lock = threading.Lock()

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
        except (BinanceError, requests.RequestException) as e:
            self._client = None
            return {"error": str(e)}

    def get_config(self) -> dict:
        return {
            "symbol": config.SYMBOL,
            "interval": config.INTERVAL,
            "intervals": INTERVALS,
            "fast": config.FAST_SMA,
            "slow": config.SLOW_SMA,
            "testnet": config.USE_TESTNET,
        }

    def get_chart(self, interval: str) -> dict:
        return self._safe(lambda: self._chart(interval))

    def _chart(self, interval: str) -> dict:
        if interval not in INTERVAL_MS:
            return {"error": f"Tempo gráfico inválido: {interval}"}
        client, _ = self._connect()
        offset = utc_offset_seconds()
        df = add_signals(client.klines(config.SYMBOL, interval, limit=500), config.FAST_SMA, config.SLOW_SMA)

        candles = [
            {"time": int(r.open_time) // 1000 + offset, "open": r.open, "high": r.high, "low": r.low, "close": r.close}
            for r in df.itertuples()
        ]

        period = INTERVAL_MS[interval] // 1000
        first = candles[0]["time"] if candles else 0
        markers = []
        for t in read_trades():
            ts = int(datetime.fromisoformat(t["data"]).timestamp())
            ts = ts - ts % period + offset
            if ts < first:
                continue
            is_buy = t["lado"] == "COMPRA"
            markers.append({
                "time": ts,
                "position": "belowBar" if is_buy else "aboveBar",
                "shape": "arrowUp" if is_buy else "arrowDown",
                "color": "#26a69a" if is_buy else "#ef5350",
                "text": "C" if is_buy else "V",
            })
        markers.sort(key=lambda m: m["time"])

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
        balances = client.balances()
        price = float(client.klines(config.SYMBOL, "1m", limit=1)["close"].iloc[-1])
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

        return {
            "base": market.base,
            "quote": market.quote,
            "base_free": balances.get(market.base, 0.0),
            "quote_free": balances.get(market.quote, 0.0),
            "price": price,
            "position": position,
            "day_pnl": state["day_pnl"],
            "trades": read_trades()[-20:][::-1],
            "updated": datetime.now().strftime("%H:%M:%S"),
        }


def main() -> None:
    webview.create_window(
        "Robô Trader",
        str(BASE_DIR / "ui" / "index.html"),
        js_api=Api(),
        width=1280,
        height=800,
        min_size=(900, 600),
        background_color="#131722",
    )
    webview.start()


if __name__ == "__main__":
    main()
