import argparse
import csv
import json
import os
import sys
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


def log(message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state() -> dict:
    state = {"position_qty": "0", "entry_cost": 0.0, "day": today(), "day_pnl": 0.0, "last_candle": 0}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    if state["day"] != today():
        state["day"] = today()
        state["day_pnl"] = 0.0
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


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
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        self.min_notional = float(notional.get("minNotional", 0))

    def floor_qty(self, qty: Decimal) -> Decimal:
        return qty.quantize(self.step, rounding=ROUND_DOWN)


def buy(client: BinanceClient, market: Market, state: dict) -> None:
    if config.TRADE_USDT < market.min_notional:
        log(f"TRADE_USDT ({config.TRADE_USDT}) abaixo do mínimo da corretora ({market.min_notional}).")
        return
    order = client.market_buy(config.SYMBOL, config.TRADE_USDT)
    qty = Decimal(order["executedQty"]) - fee_in(order, market.base)
    cost = float(order["cummulativeQuoteQty"]) + float(fee_in(order, market.quote))
    state["position_qty"] = str(qty)
    state["entry_cost"] = cost
    save_state(state)
    record_trade("COMPRA", qty, cost, None)
    log(f"COMPRA: {qty} {market.base} por {cost:.2f} {market.quote}")


def sell(client: BinanceClient, market: Market, state: dict) -> None:
    qty = market.floor_qty(Decimal(state["position_qty"]))
    if qty < market.min_qty:
        log(f"Quantidade {qty} abaixo do mínimo para vender. Posição zerada no registro.")
    else:
        order = client.market_sell(config.SYMBOL, format(qty, "f"))
        proceeds = float(order["cummulativeQuoteQty"]) - float(fee_in(order, market.quote))
        pnl = proceeds - state["entry_cost"]
        state["day_pnl"] += pnl
        record_trade("VENDA", qty, proceeds, pnl)
        log(f"VENDA: {qty} {market.base} por {proceeds:.2f} {market.quote} (resultado {pnl:+.2f})")
    state["position_qty"] = "0"
    state["entry_cost"] = 0.0
    save_state(state)


def tick(client: BinanceClient, market: Market) -> None:
    state = load_state()
    df = client.klines(config.SYMBOL, config.INTERVAL, limit=config.SLOW_SMA + 50)
    df = df[df["close_time"] < client.now_ms()]
    df = add_signals(df, config.FAST_SMA, config.SLOW_SMA)
    last = df.iloc[-1]

    if int(last["open_time"]) == state["last_candle"]:
        return
    state["last_candle"] = int(last["open_time"])
    save_state(state)

    holding = Decimal(state["position_qty"]) > 0
    log(
        f"Preço {last['close']:.2f} | SMA{config.FAST_SMA} {last['sma_fast']:.2f} | "
        f"SMA{config.SLOW_SMA} {last['sma_slow']:.2f} | "
        f"{'comprado' if holding else 'fora'} | resultado do dia {state['day_pnl']:+.2f}"
    )

    if last["signal"] == BUY and not holding:
        if state["day_pnl"] <= -config.MAX_DAILY_LOSS_USDT:
            log("Sinal de compra ignorado: limite de perda diária atingido.")
        else:
            buy(client, market, state)
    elif last["signal"] == SELL and holding:
        sell(client, market, state)


def sleep_until_next_candle() -> None:
    period = INTERVAL_MS[config.INTERVAL] / 1000
    now = time.time()
    time.sleep((now // period + 1) * period - now + 5)


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

    log(f"Robô iniciado: {config.SYMBOL} {config.INTERVAL}, SMA {config.FAST_SMA} x {config.SLOW_SMA}, "
        f"{config.TRADE_USDT} USDT por compra. Ctrl+C para parar.")
    try:
        while True:
            try:
                tick(client, market)
            except (BinanceError, requests.RequestException) as e:
                log(f"Erro, tentando de novo no próximo candle: {e}")
            sleep_until_next_candle()
    except KeyboardInterrupt:
        log("Robô parado.")


if __name__ == "__main__":
    main()
