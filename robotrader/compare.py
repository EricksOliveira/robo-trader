import argparse
import math

import numpy as np
import pandas as pd

from robotrader import config
from robotrader.binance_client import BinanceClient
from robotrader.strategy import atr, breakout_rules, sma_cross_rules, with_trend_filter

DAYS = {"1h": 365, "4h": 3 * 365, "1d": 6 * 365}
STOPS = {
    "sem stop": None,
    "stop 2N": ("fixed", 2.0),
    "stop móvel 3N": ("trailing", 3.0),
}


def strategies(df: pd.DataFrame, fast: int, slow: int) -> dict:
    base = {
        f"Médias {fast}x{slow}": sma_cross_rules(df, fast, slow),
        "Tartarugas 20/10": breakout_rules(df, 20, 10),
        "Tartarugas 55/20": breakout_rules(df, 55, 20),
    }
    result = {}
    for name, rules in base.items():
        result[name] = rules
        result[name + " + MM200"] = with_trend_filter(df, rules)
    return result


def simulate(df, buy, sell, n, stop, capital: float, fee: float, risk_pct: float) -> dict:
    opens, lows, closes = df["open"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    buy, sell, n = buy.to_numpy(), sell.to_numpy(), n.to_numpy()
    kind, k = stop if stop else (None, 2.0)

    cash, qty, entry_cost = capital, 0.0, 0.0
    stop_price, peak = None, 0.0
    results = []
    equity = np.empty(len(df))
    bars_in = 0

    def close_position(price: float) -> None:
        nonlocal cash, qty, stop_price
        proceeds = qty * price * (1 - fee)
        results.append(proceeds - entry_cost)
        cash += proceeds
        qty, stop_price = 0.0, None

    for i in range(len(df)):
        if i > 0:
            exited = False
            if qty > 0 and sell[i - 1]:
                close_position(opens[i])
                exited = True

            if qty == 0 and not exited and buy[i - 1] and not math.isnan(n[i - 1]):
                price = opens[i]
                spend = cash
                if risk_pct:
                    spend = min(cash, cash * risk_pct / 100 / (k * n[i - 1]) * price)
                qty = spend * (1 - fee) / price
                cash -= spend
                entry_cost = spend
                if kind:
                    stop_price, peak = price - k * n[i - 1], price

            if qty > 0 and stop_price is not None and lows[i] <= stop_price:
                close_position(min(opens[i], stop_price))
            elif qty > 0 and kind == "trailing":
                peak = max(peak, closes[i])
                stop_price = max(stop_price, peak - k * n[i])

        equity[i] = cash + qty * closes[i]
        bars_in += qty > 0

    eq = pd.Series(equity)
    gains = sum(r for r in results if r > 0)
    losses = -sum(r for r in results if r < 0)
    return {
        "return": (equity[-1] / capital - 1) * 100,
        "drawdown": (eq / eq.cummax() - 1).min() * 100,
        "trades": len(results),
        "win_rate": sum(r > 0 for r in results) / len(results) * 100 if results else 0.0,
        "profit_factor": gains / losses if losses else math.inf,
        "exposure": bars_in / len(df) * 100,
    }


def buy_and_hold(df: pd.DataFrame, fee: float) -> dict:
    closes = df["close"]
    return {
        "return": ((1 - fee) ** 2 * closes.iloc[-1] / df["open"].iloc[0] - 1) * 100,
        "drawdown": (closes / closes.cummax() - 1).min() * 100,
        "trades": 1,
        "win_rate": math.nan,
        "profit_factor": math.nan,
        "exposure": 100.0,
    }


def fmt(value: float, suffix: str = "", signed: bool = False) -> str:
    if math.isnan(value):
        return "-"
    if math.isinf(value):
        return "inf"
    return f"{value:+.1f}{suffix}" if signed else f"{value:.1f}{suffix}"


def print_table(rows: list[tuple[str, str, dict]]) -> None:
    header = ["Estratégia", "Stop", "Resultado", "Maior queda", "Operações", "Acertos", "Fator lucro", "Comprado"]
    lines = [header] + [
        [
            name,
            stop,
            fmt(r["return"], "%", signed=True),
            fmt(r["drawdown"], "%"),
            str(r["trades"]),
            fmt(r["win_rate"], "%"),
            fmt(r["profit_factor"]),
            fmt(r["exposure"], "%"),
        ]
        for name, stop, r in rows
    ]
    widths = [max(len(line[c]) for line in lines) for c in range(len(header))]
    for idx, line in enumerate(lines):
        cells = [line[0].ljust(widths[0]), line[1].ljust(widths[1])]
        cells += [cell.rjust(w) for cell, w in zip(line[2:], widths[2:])]
        print("  ".join(cells))
        if idx == 0:
            print("  ".join("-" * w for w in widths))


def main() -> None:
    parser = argparse.ArgumentParser(description="Compara estratégias de traders famosos no histórico real")
    parser.add_argument("--symbol", default=config.SYMBOL)
    parser.add_argument("--intervals", default="1h,4h,1d")
    parser.add_argument("--fast", type=int, default=config.FAST_SMA)
    parser.add_argument("--slow", type=int, default=config.SLOW_SMA)
    parser.add_argument("--capital", type=float, default=config.BACKTEST_CAPITAL)
    parser.add_argument("--risk", type=float, default=0.0, help="%% do capital arriscado por operação (0 = tudo)")
    args = parser.parse_args()

    client = BinanceClient(testnet=False)
    sizing = f"risco de {args.risk:g}% por operação" if args.risk else "todo o capital em cada compra"

    for interval in args.intervals.split(","):
        days = DAYS.get(interval, 365)
        df = client.history(args.symbol, interval, days)
        if len(df) < 300:
            print(f"\n{interval}: poucos candles ({len(df)}), pulando.")
            continue

        n = atr(df)
        rows = [("Comprar e segurar", "-", buy_and_hold(df, config.FEE_RATE))]
        for name, (buy, sell) in strategies(df, args.fast, args.slow).items():
            for stop_name, stop in STOPS.items():
                rows.append((name, stop_name, simulate(df, buy, sell, n, stop, args.capital, config.FEE_RATE, args.risk)))
        rows.sort(key=lambda row: row[2]["return"], reverse=True)

        start = pd.to_datetime(df["open_time"].iloc[0], unit="ms").date()
        end = pd.to_datetime(df["close_time"].iloc[-1], unit="ms").date()
        print(f"\n{args.symbol} {interval}: {start} a {end} ({len(df)} candles), "
              f"taxa {config.FEE_RATE * 100:.2f}% por ordem, {sizing}")
        print_table(rows)


if __name__ == "__main__":
    main()
