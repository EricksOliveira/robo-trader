import numpy as np
import pandas as pd

BUY = 1
SELL = -1

BREAKOUTS = {"turtle_20_10": (20, 10), "turtle_55_20": (55, 20)}
CATALOG = "catalog"
STRATEGIES = ["sma", *BREAKOUTS, CATALOG]
TREND_PERIOD = 200
ATR_PERIOD = 20
DAY_MS = 86_400_000


def add_signals(df: pd.DataFrame, fast: int, slow: int) -> pd.DataFrame:
    df = df.copy()
    df["sma_fast"] = df["close"].rolling(fast).mean()
    df["sma_slow"] = df["close"].rolling(slow).mean()

    above = df["sma_fast"] > df["sma_slow"]
    was_above = above.shift(1, fill_value=False)
    ready = df["sma_slow"].notna() & df["sma_slow"].shift(1).notna()

    df["signal"] = 0
    df.loc[ready & above & ~was_above, "signal"] = BUY
    df.loc[ready & ~above & was_above, "signal"] = SELL
    return df


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def sma_cross_rules(df: pd.DataFrame, fast: int, slow: int) -> tuple[pd.Series, pd.Series]:
    signals = add_signals(df, fast, slow)["signal"]
    return signals == BUY, signals == SELL


def breakout_rules(df: pd.DataFrame, entry: int, exit: int) -> tuple[pd.Series, pd.Series]:
    highest = df["high"].rolling(entry).max().shift(1)
    lowest = df["low"].rolling(exit).min().shift(1)
    return df["close"] > highest, df["close"] < lowest


def with_trend_filter(df: pd.DataFrame, rules: tuple[pd.Series, pd.Series], period: int = TREND_PERIOD):
    trend = df["close"].rolling(period).mean()
    buy, sell = rules
    return buy & (df["close"] > trend), sell | (df["close"] < trend)


def rules_for(df: pd.DataFrame, s: dict) -> tuple[pd.Series, pd.Series]:
    if s["strategy"] in BREAKOUTS:
        rules = breakout_rules(df, *BREAKOUTS[s["strategy"]])
    else:
        rules = sma_cross_rules(df, s["fast_sma"], s["slow_sma"])
    if s["trend_filter"]:
        rules = with_trend_filter(df, rules)
    return rules


def candles_needed(s: dict) -> int:
    lookback = BREAKOUTS[s["strategy"]][0] if s["strategy"] in BREAKOUTS else s["slow_sma"]
    if s["trend_filter"]:
        lookback = max(lookback, TREND_PERIOD)
    return min(1000, max(lookback, ATR_PERIOD) + 100)


def catalog(df: pd.DataFrame, interval_ms: int, days: int, hold: int) -> pd.DataFrame:
    # rows are UTC days, columns the candle start within the day (ms); each cell is the up rate
    # over the previous `days` days only, so a day's list never peeks at that day
    open_time = df["open_time"]
    exit_time = open_time.shift(-(hold - 1))
    day = open_time // DAY_MS
    won = (df["close"].shift(-(hold - 1)) > df["open"]).astype(float)
    # a window that ends after midnight is only known the next day, too late for that day's list
    known = (exit_time - open_time == (hold - 1) * interval_ms) & (exit_time // DAY_MS == day)
    won[~known] = np.nan
    table = pd.DataFrame({"day": day, "slot": open_time % DAY_MS, "won": won})
    table = table.pivot(index="day", columns="slot", values="won")
    table = table.reindex(range(int(table.index.min()), int(table.index.max()) + 2))
    return table.rolling(days, min_periods=days).mean().shift(1)


def catalog_rate_at(open_time: pd.Series, rates: pd.DataFrame) -> np.ndarray:
    rows = rates.index.get_indexer(open_time // DAY_MS)
    cols = rates.columns.get_indexer(open_time % DAY_MS)
    found = (rows >= 0) & (cols >= 0)
    result = np.full(len(open_time), np.nan)
    result[found] = rates.to_numpy()[rows[found], cols[found]]
    return result
