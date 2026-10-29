import pandas as pd

BUY = 1
SELL = -1

BREAKOUTS = {"turtle_20_10": (20, 10), "turtle_55_20": (55, 20)}
STRATEGIES = ["sma", *BREAKOUTS]
TREND_PERIOD = 200
ATR_PERIOD = 20


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
