from __future__ import annotations
import numpy as np
import pandas as pd

PRICE_FEATURES = ["ret1", "ret6", "ret42", "ema_gap", "atr_pct", "volatility",
                  "volume_ratio", "drawdown42"]


def price_frame(rows, expected_interval=None):
    if not rows:
        return pd.DataFrame()
    f = pd.DataFrame(rows).sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    close, high, low = f["c"], f["h"], f["l"]
    f["ret1"] = close.pct_change(fill_method=None)
    f["ret6"] = close.pct_change(6, fill_method=None)
    f["ret42"] = close.pct_change(42, fill_method=None)
    f["ema20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    f["ema200"] = close.ewm(span=200, adjust=False, min_periods=200).mean()
    f["ema_gap"] = close / f["ema20"] - 1
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    f["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    f["atr_pct"] = f["atr"] / close
    f["volatility"] = f["ret1"].rolling(20, min_periods=20).std()
    denom = f["qv"].shift(1).rolling(20, min_periods=20).median().replace(0, np.nan)
    f["volume_ratio"] = (f["qv"] / denom).clip(0, 50)
    f["drawdown42"] = close / high.rolling(42, min_periods=42).max() - 1
    f["breakout"] = close > high.shift(1).rolling(20).max()
    f["range6"] = high.shift(1).rolling(6).max() - low.shift(1).rolling(6).min()
    f["prior_range6"] = high.shift(7).rolling(6).max() - low.shift(7).rolling(6).min()
    # Do not treat observations separated by a data gap as consecutive bars.
    if len(f) >= 3:
        step = expected_interval or f["ts"].diff().dropna().median()
        bad = f["ts"].diff().gt(step * 1.5)
        f["continuous"] = ~bad.rolling(201, min_periods=1).max().astype(bool)
    else:
        f["continuous"] = False
    return f.replace([np.inf, -np.inf], np.nan)


def regime(row):
    if not bool(row.get("continuous", False)) or not np.isfinite(row.get("ema200", np.nan)):
        return "unknown"
    if row["atr_pct"] > 0.12 or row["ret6"] < -0.12:
        return "stress"
    if row["c"] > row["ema200"] and row["ret42"] > 0:
        return "trend"
    return "range"


def signal(row, state):
    if state != "trend":
        return None
    if bool(row["breakout"]) and row["range6"] < row["prior_range6"]:
        return "breakout"
    if 0 < row["ret1"] < 0.025 and -0.025 < row["ema_gap"] < 0.015 and row["ret6"] < 0:
        return "pullback"
    return None
