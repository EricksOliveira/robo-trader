import argparse
import csv
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv

import config
from binance_client import INTERVAL_MS, BinanceClient, BinanceError
from strategy import BUY, SELL, add_signals

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
TRADES_FILE = BASE_DIR / "trades.csv"
LOG_FILE = BASE_DIR / "bot.log"
SETTINGS_FILE = BASE_DIR / "settings.json"
LOCK_FILE = BASE_DIR / "bot.lock"

BOT_TAG = "bot"
INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
DONE_STATUSES = {"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}


def log(message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state() -> dict:
    state = {
        "position_qty": "0",
        "entry_cost": 0.0,
        "day": today(),
        "day_pnl": 0.0,
        "last_candle": 0,
        "protection": None,
    }
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    if state["day"] != today():
        state["day"] = today()
        state["day_pnl"] = 0.0
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def default_settings() -> dict:
    return {
        "interval": config.INTERVAL,
        "fast_sma": config.FAST_SMA,
        "slow_sma": config.SLOW_SMA,
        "trade_usdt": config.TRADE_USDT,
        "max_daily_loss_usdt": config.MAX_DAILY_LOSS_USDT,
        "stop_loss_pct": config.STOP_LOSS_PCT,
        "take_profit_pct": config.TAKE_PROFIT_PCT,
    }


def load_settings() -> dict:
    settings = default_settings()
    if SETTINGS_FILE.exists():
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings.update({k: v for k, v in saved.items() if k in settings})
    return settings


def validate_settings(values: dict) -> dict:
    try:
        s = {
            "interval": str(values["interval"]),
            "fast_sma": int(values["fast_sma"]),
            "slow_sma": int(values["slow_sma"]),
            "trade_usdt": float(values["trade_usdt"]),
            "max_daily_loss_usdt": float(values["max_daily_loss_usdt"]),
            "stop_loss_pct": float(values.get("stop_loss_pct") or 0),
            "take_profit_pct": float(values.get("take_profit_pct") or 0),
        }
    except (KeyError, TypeError, ValueError):
        raise ValueError("Preencha todos os campos com números válidos")

    if s["interval"] not in INTERVALS:
        raise ValueError(f"Tempo gráfico inválido: {s['interval']}")
    if not 2 <= s["fast_sma"] < s["slow_sma"] <= 200:
        raise ValueError("A média rápida precisa ser menor que a lenta (entre 2 e 200)")
    if s["trade_usdt"] < 5:
        raise ValueError("O valor por compra precisa ser de pelo menos 5 USDT")
    if s["max_daily_loss_usdt"] <= 0:
        raise ValueError("A perda máxima por dia precisa ser maior que zero")
    if not 0 <= s["stop_loss_pct"] < 50:
        raise ValueError("Stop loss precisa estar entre 0 e 50% (0 desliga)")
    if not 0 <= s["take_profit_pct"] < 100:
        raise ValueError("Stop gain precisa estar entre 0 e 100% (0 desliga)")
    return s


def save_settings(values: dict) -> dict:
    s = validate_settings(values)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


def record_trade(side: str, qty: Decimal, quote: float, pnl: float | None) -> None:
    new_file = not TRADES_FILE.exists()
    with TRADES_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["data", "lado", "quantidade", "preco_medio", "valor_usdt", "lucro_usdt"])
        price = quote / float(qty) if qty else 0.0
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            side,
            str(qty),
            f"{price:.2f}",
            f"{quote:.2f}",
            "" if pnl is None else f"{pnl:.2f}",
        ])


def fee_in(order: dict, asset: str) -> Decimal:
    return sum((Decimal(f["commission"]) for f in order.get("fills", []) if f["commissionAsset"] == asset), Decimal(0))


class Market:
    def __init__(self, info: dict):
        self.base = info["baseAsset"]
        self.quote = info["quoteAsset"]
        filters = {f["filterType"]: f for f in info["filters"]}
        self.step = Decimal(filters["LOT_SIZE"]["stepSize"]).normalize()
        self.min_qty = Decimal(filters["LOT_SIZE"]["minQty"])
        self.tick = Decimal(filters["PRICE_FILTER"]["tickSize"]).normalize()
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        self.min_notional = float(notional.get("minNotional", 0))

    def floor_qty(self, qty: Decimal) -> Decimal:
        return qty.quantize(self.step, rounding=ROUND_DOWN)

    def round_price(self, price: float) -> str:
        return format(Decimal(str(price)).quantize(self.tick), "f")


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._file = f
        return True

    def release(self) -> None:
        if self._file is None:
            return
        if os.name == "nt":
            import msvcrt
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        self._file.close()
        self._file = None

    def held_elsewhere(self) -> bool:
        if self._file is not None:
            return False
        if self.acquire():
            self.release()
            return False
        return True


class Bot:
    def __init__(self, client: BinanceClient, market: Market, settings: dict, stop_event: threading.Event | None = None):
        self.client = client
        self.market = market
        self.s = settings
        self.stop_event = stop_event or threading.Event()

    def run(self) -> None:
        s = self.s
        stops = []
        if s["stop_loss_pct"]:
            stops.append(f"stop loss {s['stop_loss_pct']:g}%")
        if s["take_profit_pct"]:
            stops.append(f"stop gain {s['take_profit_pct']:g}%")
        log(f"Robô iniciado: {config.SYMBOL} {s['interval']}, SMA {s['fast_sma']} x {s['slow_sma']}, "
            f"{s['trade_usdt']:g} USDT por compra" + (", " + " e ".join(stops) if stops else ""))

        while not self.stop_event.is_set():
            try:
                self.tick()
            except (BinanceError, requests.RequestException) as e:
                log(f"Erro, tentando de novo no próximo candle: {e}")
            self._sleep_until_next_candle()
        log("Robô parado.")

    def _sleep_until_next_candle(self) -> None:
        period = INTERVAL_MS[self.s["interval"]] / 1000
        end = (time.time() // period + 1) * period + 5
        while not self.stop_event.is_set() and time.time() < end:
            self.stop_event.wait(min(1.0, end - time.time()))

    def tick(self) -> None:
        s = self.s
        state = load_state()
        self._check_protection(state)

        df = self.client.klines(config.SYMBOL, s["interval"], limit=s["slow_sma"] + 50)
        df = df[df["close_time"] < self.client.now_ms()]
        df = add_signals(df, s["fast_sma"], s["slow_sma"])
        last = df.iloc[-1]

        if int(last["open_time"]) == state["last_candle"]:
            return
        # saved before acting, so a crash mid-order never repeats the order on restart
        state["last_candle"] = int(last["open_time"])
        save_state(state)

        holding = Decimal(state["position_qty"]) > 0
        log(
            f"Preço {last['close']:.2f} | SMA{s['fast_sma']} {last['sma_fast']:.2f} | "
            f"SMA{s['slow_sma']} {last['sma_slow']:.2f} | "
            f"{'comprado' if holding else 'fora'} | resultado do dia {state['day_pnl']:+.2f}"
        )

        if last["signal"] == BUY and not holding:
            if state["day_pnl"] <= -s["max_daily_loss_usdt"]:
                log("Sinal de compra ignorado: limite de perda diária atingido.")
            else:
                self.buy(state)
        elif last["signal"] == SELL and holding:
            self.sell(state)

    def buy(self, state: dict) -> None:
        market = self.market
        order = self.client.market_buy(config.SYMBOL, self.s["trade_usdt"])
        qty = Decimal(order["executedQty"]) - fee_in(order, market.base)
        cost = float(order["cummulativeQuoteQty"]) + float(fee_in(order, market.quote))
        state["position_qty"] = str(qty)
        state["entry_cost"] = cost
        state["protection"] = None
        save_state(state)
        record_trade("COMPRA", qty, cost, None)
        log(f"COMPRA: {qty} {market.base} por {cost:.2f} {market.quote}")

        if self.s["stop_loss_pct"] or self.s["take_profit_pct"]:
            try:
                state["protection"] = self._protect(market.floor_qty(qty), cost / float(qty))
                save_state(state)
            except BinanceError as e:
                log(f"Compra feita, mas os stops NÃO foram criados: {e}")

    def _protect(self, qty: Decimal, avg_price: float) -> dict:
        sl, tp = self.s["stop_loss_pct"], self.s["take_profit_pct"]
        quantity = format(qty, "f")
        sl_price = self.market.round_price(avg_price * (1 - sl / 100))
        tp_price = self.market.round_price(avg_price * (1 + tp / 100))

        if sl and tp:
            r = self.client.oco_sell(config.SYMBOL, quantity, tp_price, sl_price, tag=BOT_TAG)
            protection = {"list_id": r["orderListId"], "order_ids": [o["orderId"] for o in r["orders"]]}
            log(f"Stops criados: stop loss {sl_price}, stop gain {tp_price}")
        elif sl:
            r = self.client.stop_loss_sell(config.SYMBOL, quantity, sl_price, tag=BOT_TAG)
            protection = {"list_id": -1, "order_ids": [r["orderId"]]}
            log(f"Stop criado: stop loss {sl_price}")
        else:
            r = self.client.take_profit_sell(config.SYMBOL, quantity, tp_price, tag=BOT_TAG)
            protection = {"list_id": -1, "order_ids": [r["orderId"]]}
            log(f"Stop criado: stop gain {tp_price}")
        return protection

    def _check_protection(self, state: dict) -> bool:
        protection = state.get("protection")
        if not protection or Decimal(state["position_qty"]) <= 0:
            return False

        orders = [self.client.get_order(config.SYMBOL, oid) for oid in protection["order_ids"]]
        filled = [o for o in orders if o["status"] == "FILLED"]
        if filled:
            o = filled[0]
            kind = "STOP LOSS" if o["type"].startswith("STOP_LOSS") else "STOP GAIN"
            qty = Decimal(o["executedQty"])
            proceeds = float(o["cummulativeQuoteQty"])
            pnl = proceeds - state["entry_cost"]
            state["day_pnl"] += pnl
            record_trade(kind, qty, proceeds, pnl)
            log(f"{kind} executado: {qty} {self.market.base} por {proceeds:.2f} {self.market.quote} "
                f"(resultado {pnl:+.2f})")
            self._clear_position(state)
            return True

        if all(o["status"] in DONE_STATUSES for o in orders):
            log("Os stops do robô foram cancelados fora dele. A posição continua aberta, sem stop.")
            state["protection"] = None
            save_state(state)
        return False

    def _cancel_protection(self, state: dict) -> None:
        protection = state.get("protection")
        if not protection:
            return
        try:
            if protection["list_id"] != -1:
                self.client.cancel_order_list(config.SYMBOL, protection["list_id"])
            else:
                self.client.cancel_order(config.SYMBOL, protection["order_ids"][0])
        except BinanceError:
            pass
        state["protection"] = None

    def sell(self, state: dict) -> None:
        if self._check_protection(state):
            return
        self._cancel_protection(state)

        market = self.market
        qty = market.floor_qty(Decimal(state["position_qty"]))
        if qty < market.min_qty:
            log(f"Quantidade {qty} abaixo do mínimo para vender. Posição zerada no registro.")
        else:
            order = self.client.market_sell(config.SYMBOL, format(qty, "f"))
            proceeds = float(order["cummulativeQuoteQty"]) - float(fee_in(order, market.quote))
            pnl = proceeds - state["entry_cost"]
            state["day_pnl"] += pnl
            record_trade("VENDA", qty, proceeds, pnl)
            log(f"VENDA: {qty} {market.base} por {proceeds:.2f} {market.quote} (resultado {pnl:+.2f})")
        self._clear_position(state)

    def _clear_position(self, state: dict) -> None:
        state["position_qty"] = "0"
        state["entry_cost"] = 0.0
        state["protection"] = None
        save_state(state)


def make_client() -> BinanceClient:
    load_dotenv(BASE_DIR / ".env")
    client = BinanceClient(
        os.getenv("BINANCE_API_KEY", ""),
        os.getenv("BINANCE_API_SECRET", ""),
        testnet=config.USE_TESTNET,
    )
    client.sync_time()
    return client


def check(client: BinanceClient, market: Market) -> None:
    balances = client.balances()
    log(f"Conexão OK. Saldo livre: {balances.get(market.quote, 0):.2f} {market.quote}, "
        f"{balances.get(market.base, 0):.8f} {market.base}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Robô de cruzamento de médias móveis")
    parser.add_argument("--check", action="store_true", help="só testa a conexão e mostra o saldo")
    args = parser.parse_args()

    if config.USE_TESTNET:
        log("Modo TESTNET: dinheiro fictício.")
    else:
        print("ATENÇÃO: modo REAL. As ordens vão usar dinheiro de verdade.")
        if input('Digite "DINHEIRO REAL" para continuar: ').strip() != "DINHEIRO REAL":
            sys.exit("Cancelado.")

    try:
        client = make_client()
        market = Market(client.symbol_info(config.SYMBOL))
        check(client, market)
    except (BinanceError, requests.RequestException) as e:
        sys.exit(f"Falha ao conectar: {e}")

    if args.check:
        return

    lock = InstanceLock(LOCK_FILE)
    if not lock.acquire():
        sys.exit("O robô já está rodando (no painel ou em outra janela).")
    print("Ctrl+C para parar.")
    try:
        Bot(client, market, load_settings()).run()
    except KeyboardInterrupt:
        log("Robô parado.")
    finally:
        lock.release()


if __name__ == "__main__":
    main()
