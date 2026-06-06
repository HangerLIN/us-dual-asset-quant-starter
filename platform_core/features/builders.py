from __future__ import annotations

import pandas as pd


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    typical = (out["high"] + out["low"] + out["close"]) / 3
    out["vwap"] = (typical * out["volume"]).cumsum() / out["volume"].replace(0, pd.NA).cumsum()
    return out


def add_opening_range(df: pd.DataFrame, *, minutes: int = 5) -> pd.DataFrame:
    out = df.copy()
    opening = out.head(minutes)
    out["opening_range_high"] = opening["high"].max()
    out["opening_range_low"] = opening["low"].min()
    return out
