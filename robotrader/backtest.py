import argparse

import pandas as pd

from robotrader import config
from robotrader.binance_client import INTERVAL_MS, BinanceClient
from robotrader.strategy import BUY, DAY_MS, SELL, add_signals, catalog, catalog_rate_at


def run_backtest(df: pd.DataFrame, capital: float, fee_rate: float) -> dict:
    cash = capital
    qty = 0.0
    entry_cost = 0.0
    results = []
    equity = []

    signals = df["signal"].to_numpy()
    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()

    for i in range(len(df)):
        if i > 0:
            price = opens[i]
            if signals[i - 1] == BUY and qty == 0:
                entry_cost = cash
                qty = cash * (1 - fee_rate) / price
                cash = 0.0
            elif signals[i - 1] == SELL and qty > 0:
                cash = qty * price * (1 - fee_rate)
                results.append(cash - entry_cost)
                qty = 0.0
        equity.append(cash + qty * closes[i])

    equity = pd.Series(equity)
    drawdown = (equity / equity.cummax() - 1).min()
    final = equity.iloc[-1]
    hold = capital * (1 - fee_rate) ** 2 * closes[-1] / opens[0]

    return {
        "final": final,
        "return_pct": (final / capital - 1) * 100,
        "hold_pct": (hold / capital - 1) * 100,
        "trades": len(results),
        "wins": sum(1 for r in results if r > 0),
        "max_drawdown_pct": drawdown * 100,
        "open_position": qty > 0,
    }


def run_catalog(df: pd.DataFrame, starts, hold: int, trade: float, gales: int, capital: float, fee: float,
                stop_win: float, stop_loss: float) -> dict:
    opens, closes = df["open"].to_numpy(), df["close"].to_numpy()
    days = (df["open_time"] // DAY_MS).to_numpy()
    cash = capital
    results, day_pnl, equity = [], {}, [capital]
    gale, due, busted, broke, ups = 0, False, 0, 0, 0

    i = 0
    while i + hold <= len(df):
        if not (due or starts[i]):
            i += 1
            continue
        today = day_pnl.get(days[i], 0.0)
        spend = trade * config.MARTINGALE_FACTOR ** gale
        if today <= -stop_loss or (stop_win and today >= stop_win) or spend > cash:
            broke += spend > cash
            gale, due = 0, False
            i += 1
            continue

        ups += closes[i + hold - 1] > opens[i]
        pnl = spend * (1 - fee) ** 2 * closes[i + hold - 1] / opens[i] - spend
        cash += pnl
        results.append(pnl)
        equity.append(cash)
        day_pnl[days[i]] = today + pnl
        if pnl < 0 and gale < gales:
            gale, due = gale + 1, True
        else:
            busted += pnl < 0 and gales > 0
            gale, due = 0, False
        i += hold

    eq = pd.Series(equity)
    return {
        "final": cash,
        "pnl": cash - capital,
        "trades": len(results),
        "wins": sum(1 for r in results if r > 0),
        "ups": ups,
        "busted": busted,
        "broke": broke,
        "worst_day": min(day_pnl.values(), default=0.0),
        "max_drawdown": (eq - eq.cummax()).min(),
    }


def main_catalog(args) -> None:
    days = args.days or 90
    print(f"Baixando {days + args.catalog_days} dias de {args.symbol} ({args.interval})...")
    df = BinanceClient(testnet=False).history(args.symbol, args.interval, days + args.catalog_days)
    df = df[df["close_time"] < df["close_time"].max()].reset_index(drop=True)
    rates = catalog(df, INTERVAL_MS[args.interval], args.catalog_days, args.hold)
    keep = (df["open_time"] >= df["open_time"].iloc[-1] - days * DAY_MS).to_numpy()
    df = df[keep].reset_index(drop=True)
    rate_at = catalog_rate_at(df["open_time"], rates)
    starts = rate_at >= args.rate / 100
    promised = rate_at[starts].mean() * 100 if starts.any() else 0.0

    start = pd.to_datetime(df["open_time"].iloc[0], unit="ms").date()
    end = pd.to_datetime(df["close_time"].iloc[-1], unit="ms").date()
    print()
    print(f"Período:      {start} a {end} ({len(df)} candles)")
    print(f"Lista:        horários com acerto de {args.rate:g}% ou mais nos {args.catalog_days} dias anteriores, "
          f"expiração {args.hold} candle(s)")
    print(f"Sinais:       {starts.sum()} ({starts.sum() / days:.1f} por dia), acerto médio prometido pela lista "
          f"{promised:.0f}%")
    print(f"Operação:     {args.trade:g} USDT por entrada, gale de {config.MARTINGALE_FACTOR:g}x, "
          f"taxa {config.FEE_RATE * 100:.2f}% por ordem, capital {args.capital:,.0f} USDT")
    stops = f"stop loss do dia {args.stop_loss:g} USDT" + (f", stop win {args.stop_win:g} USDT" if args.stop_win else "")
    print(f"Stops:        {stops}")
    print()

    header = ["Gales", "Resultado", "Operações", "Subiu", "Acertos", "Perdeu o gale", "Sem saldo", "Pior dia", "Maior queda"]
    lines = [header]
    for gales in range(args.gales + 1):
        r = run_catalog(df, starts, args.hold, args.trade, gales, args.capital, config.FEE_RATE,
                        args.stop_win, args.stop_loss)
        lines.append([
            str(gales),
            f"{r['pnl']:+,.2f} ({r['pnl'] / args.capital * 100:+.1f}%)",
            str(r["trades"]),
            f"{r['ups'] / r['trades'] * 100:.0f}%" if r["trades"] else "-",
            f"{r['wins'] / r['trades'] * 100:.0f}%" if r["trades"] else "-",
            str(r["busted"]) if gales else "-",
            str(r["broke"]),
            f"{r['worst_day']:+,.2f}",
            f"{r['max_drawdown']:,.2f}",
        ])
    widths = [max(len(line[c]) for line in lines) for c in range(len(header))]
    for idx, line in enumerate(lines):
        print("  ".join(cell.rjust(w) for cell, w in zip(line, widths)))
        if idx == 0:
            print("  ".join("-" * w for w in widths))
    print()
    print("Subiu: entradas em que o preço fechou acima da compra (o acerto da lista). "
          "Acertos: entradas com lucro depois das taxas. Perdeu o gale: sequências que perderam até no "
          "último gale. Sem saldo: entradas puladas por falta de capital. Valores em USDT.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest das estratégias com dados reais da Binance")
    parser.add_argument("--symbol", default=config.SYMBOL)
    parser.add_argument("--interval", default=config.INTERVAL)
    parser.add_argument("--days", type=int, help=f"padrão: {config.BACKTEST_DAYS} (90 na catalogação)")
    parser.add_argument("--fast", type=int, default=config.FAST_SMA)
    parser.add_argument("--slow", type=int, default=config.SLOW_SMA)
    parser.add_argument("--capital", type=float, default=config.BACKTEST_CAPITAL)
    parser.add_argument("--catalog", action="store_true", help="testa a catalogação (lista de horários)")
    parser.add_argument("--catalog-days", type=int, default=config.CATALOG_DAYS)
    parser.add_argument("--rate", type=float, default=config.CATALOG_RATE, help="acerto mínimo %%")
    parser.add_argument("--hold", type=int, default=config.HOLD_CANDLES, help="expiração em candles")
    parser.add_argument("--gales", type=int, default=2, help="testa de 0 até esse número de gales")
    parser.add_argument("--trade", type=float, default=config.TRADE_USDT, help="USDT por entrada")
    parser.add_argument("--stop-win", type=float, default=config.DAILY_TARGET_USDT)
    parser.add_argument("--stop-loss", type=float, default=config.MAX_DAILY_LOSS_USDT)
    args = parser.parse_args()
    if args.catalog:
        main_catalog(args)
        return
    args.days = args.days or config.BACKTEST_DAYS

    print(f"Baixando {args.days} dias de {args.symbol} ({args.interval})...")
    df = BinanceClient(testnet=False).history(args.symbol, args.interval, args.days)
    if len(df) <= args.slow:
        print("Poucos candles para calcular as médias. Aumente --days.")
        return

    df = add_signals(df, args.fast, args.slow)
    r = run_backtest(df, args.capital, config.FEE_RATE)

    start = pd.to_datetime(df["open_time"].iloc[0], unit="ms").date()
    end = pd.to_datetime(df["close_time"].iloc[-1], unit="ms").date()
    win_rate = r["wins"] / r["trades"] * 100 if r["trades"] else 0.0

    print()
    print(f"Período:            {start} a {end} ({len(df)} candles)")
    print(f"Estratégia:         SMA {args.fast} x SMA {args.slow}, taxa {config.FEE_RATE * 100:.2f}% por ordem")
    print(f"Capital inicial:    {args.capital:,.2f} USDT")
    print(f"Capital final:      {r['final']:,.2f} USDT")
    print(f"Resultado robô:     {r['return_pct']:+.2f}%")
    print(f"Comprar e segurar:  {r['hold_pct']:+.2f}%")
    print(f"Operações fechadas: {r['trades']} (acertos: {r['wins']}, {win_rate:.0f}%)")
    print(f"Maior queda:        {r['max_drawdown_pct']:.2f}%")
    if r["open_position"]:
        print("Obs.: terminou com uma compra ainda aberta, avaliada pelo último preço.")


if __name__ == "__main__":
    main()
