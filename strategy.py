import pandas as pd

BUY = 1
SELL = -1


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
