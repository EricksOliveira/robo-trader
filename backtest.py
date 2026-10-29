import argparse

import pandas as pd

import config
from binance_client import BinanceClient
from strategy import BUY, SELL, add_signals


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest do cruzamento de médias móveis com dados reais da Binance")
    parser.add_argument("--symbol", default=config.SYMBOL)
    parser.add_argument("--interval", default=config.INTERVAL)
    parser.add_argument("--days", type=int, default=config.BACKTEST_DAYS)
    parser.add_argument("--fast", type=int, default=config.FAST_SMA)
    parser.add_argument("--slow", type=int, default=config.SLOW_SMA)
    parser.add_argument("--capital", type=float, default=config.BACKTEST_CAPITAL)
    args = parser.parse_args()

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
