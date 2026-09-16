from __future__ import annotations
import json
import logging
import time
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss
from .features import PRICE_FEATURES, price_frame
from .data import INTERVALS, seconds

LOG = logging.getLogger(__name__)
SCANNER_EXTRA_FEATURES = ["log_liquidity", "age_hours", "turnover_h1", "buy_sell_ratio_h1",
                          "top10_ex_lp_fraction", "largest_funding_cluster_fraction", "creator_bad_rate"]


class LinearProbability:
    """Portable JSON coefficients only. Remote pickle/joblib models are never loaded."""
    def __init__(self, data):
        self.data = data

    def predict(self, values):
        x = np.asarray([values.get(k, np.nan) for k in self.data["features"]], dtype=float)
        known = np.isfinite(x)
        if known.mean() < 0.75:
            return None
        x = np.where(known, x, self.data["median"])
        z = ((x - np.array(self.data["mean"])) / np.array(self.data["scale"]))
        logit = float(z @ np.array(self.data["coef"]) + self.data["intercept"])
        a, b = self.data.get("calibration", [1.0, 0.0])
        return float(1 / (1 + np.exp(-np.clip(a * logit + b, -35, 35))))

    def save(self, path):
        p = Path(path)
        p.parent.mkdir(exist_ok=True, parents=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, allow_nan=False), encoding="utf-8")
        tmp.replace(p)

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))


def model_context(cfg, venue):
    if venue == "cex":
        return {"interval": cfg["cex"]["interval"], "symbols": sorted(cfg["cex"]["symbols"]),
                "fee_rate": cfg["cex"]["fee_rate"], "label_extra_cost": .003}
    return {"network": cfg["scanner"]["network"]}


def model_unavailable(data, max_age_s, context, now=None):
    now = time.time() if now is None else now
    if data.get("context") != context:
        return "model_configuration_changed"
    for name, value in (("model", data.get("created_at")),
                        ("history", data.get("metrics", {}).get("label_max_time"))):
        if not isinstance(value, (int, float)) or not np.isfinite(value) or not -5 <= now-value <= max_age_s:
            return "stale_" + name
    return None


def fit_temporal(frame, features, min_rows=300, min_positives=25, group_col=None, optional_features=None):
    """60/20/20 chronological train/calibration/test; purge overlapping labels."""
    f = frame.sort_values("asof").copy()
    f = f[(f["label_known_at"] <= time.time()) & (f["label_known_at"] > f["asof"])
          & np.isfinite(f["asof"]) & f["label"].isin([0, 1])]
    if len(f) < min_rows or f["label"].sum() < min_positives or (1-f["label"]).sum() < min_positives:
        return None, {"status": "insufficient_mature_labels", "rows": len(f)}
    times = np.sort(f["asof"].unique())
    t1, t2 = times[int(len(times)*0.6)], times[int(len(times)*0.8)]
    train = f[(f["asof"] < t1) & (f["label_known_at"] < t1)]
    cal = f[(f["asof"] >= t1) & (f["asof"] < t2) & (f["label_known_at"] < t2)]
    test = f[f["asof"] >= t2]
    if group_col:
        # Keep all snapshots of a token in a single chronological segment.
        cal = cal[~cal[group_col].isin(train[group_col])]
        test = test[~test[group_col].isin(pd.concat([train[group_col], cal[group_col]]))]
    for name, part in (("train", train), ("calibration", cal), ("test", test)):
        if len(part) < 30 or part["label"].nunique() < 2 or part["label"].sum() < 5 or (1-part["label"]).sum() < 5:
            return None, {"status": "insufficient_" + name, "rows": len(part)}
    # Feature availability is learned only on training rows, never from holdout.
    features = list(features) + [k for k in (optional_features or []) if k in train
                               and np.isfinite(train[k].to_numpy(float)).mean() >= .8]
    X = train[features].to_numpy(float)
    med = np.array([np.nanmedian(X[:,j]) if np.isfinite(X[:,j]).any() else 0 for j in range(X.shape[1])])
    X = np.where(np.isfinite(X), X, med)
    mean, scale = X.mean(axis=0), X.std(axis=0)
    scale[scale < 1e-8] = 1
    transform = lambda p: (np.where(np.isfinite(p[features].to_numpy(float)), p[features].to_numpy(float), med)-mean)/scale
    lr = LogisticRegression(C=0.2, max_iter=1000, random_state=43).fit((X-mean)/scale, train["label"])
    calibration = LogisticRegression(C=1, max_iter=500).fit(lr.decision_function(transform(cal)).reshape(-1,1), cal["label"])
    prediction = calibration.predict_proba(lr.decision_function(transform(test)).reshape(-1,1))[:,1]
    baseline = np.full(len(test), train["label"].mean())
    bins = []
    for low in (0, .1, .25, .5, .75, .9):
        high = {0:.1, .1:.25, .25:.5, .5:.75, .75:.9, .9:1.01}[low]
        mask = (prediction >= low) & (prediction < high)
        if mask.any():
            bins.append({"low": low, "high": high, "n": int(mask.sum()),
                         "predicted": float(prediction[mask].mean()),
                         "observed": float(test["label"].to_numpy()[mask].mean())})
    metrics = {"status": "trained_research_model", "n_train": len(train), "n_calibration": len(cal),
               "n_test": len(test), "test_positives": int(test["label"].sum()),
               "test_prevalence": float(test["label"].mean()), "brier": float(brier_score_loss(test["label"],prediction)),
               "baseline_brier": float(brier_score_loss(test["label"],baseline)),
               "average_precision": float(average_precision_score(test["label"], prediction)), "reliability_bins": bins,
               "test_from": float(t2), "test_through": float(test["asof"].max()),
               "label_max_time": float(test["label_known_at"].max()), "features": features,
               "train_label_through": float(train["label_known_at"].max()),
               "calibration_from": float(t1), "calibration_label_through": float(cal["label_known_at"].max()),
               "warning": "Temporal test, not proof of trading profit; samples remain correlated."}
    d = {"schema":1, "features":features, "median":med.tolist(), "mean":mean.tolist(), "scale":scale.tolist(),
         "coef":lr.coef_[0].tolist(), "intercept":float(lr.intercept_[0]),
         "calibration":[float(calibration.coef_[0,0]), float(calibration.intercept_[0])],
         "created_at":time.time(), "metrics":metrics}
    d["training_associations"] = sorted([
        {"feature":name,"calibrated_standardized_coefficient":float(lr.coef_[0,j]*calibration.coef_[0,0]),
         "positive_median":float(train.loc[train["label"]==1,name].median()) if train.loc[train["label"]==1,name].notna().any() else None,
         "negative_median":float(train.loc[train["label"]==0,name].median()) if train.loc[train["label"]==0,name].notna().any() else None}
        for j,name in enumerate(features)],key=lambda x:abs(x["calibrated_standardized_coefficient"]),reverse=True)
    return LinearProbability(d), metrics


def train_cex(cfg, store):
    frames = []
    step = INTERVALS[cfg["cex"]["interval"]]
    for symbol in cfg["cex"]["symbols"]:
        f = price_frame(store.candles("cex", symbol),step)
        if f.empty:
            continue
        # Target: positive six-bar close return after assumed round-trip costs.
        # It is a trade gate, not a strategy PnL forecast.
        f["label"] = ((f["c"].shift(-6) / f["c"] - 1) > 2*cfg["cex"]["fee_rate"] + .003).astype(int)
        f["asof"], f["label_known_at"] = f["close_ts"], f["close_ts"].shift(-6)
        valid = f["continuous"] & ((f["ts"].shift(-6)-f["ts"]) == 6*step)
        f = f[valid].dropna(subset=PRICE_FEATURES+["label_known_at"])
        frames.append(f)
    if not frames:
        return {"status":"no_history"}
    model, report = fit_temporal(pd.concat(frames), PRICE_FEATURES, cfg["cex"]["model_min_rows"])
    if model:
        model.data["purpose"] = "cex_6bar_positive_net_close_return"
        model.data["context"] = model_context(cfg, "cex")
        model.save(store.root / "models" / "cex_gate.json")
    store.set("model:cex", report)
    return report


def scanner_training_rows(cfg, store):
    frames = []
    for pool in store.rows("SELECT * FROM pools WHERE network=?", (cfg["scanner"]["network"],)):
        f = price_frame(store.candles("dex:"+pool["network"], pool["pool"]),300)
        if f.empty or len(f) < 60:
            continue
        for target in cfg["scanner"]["targets"]:
            steps = int(target["horizon_hours"]*3600/300)
            if len(f) < steps + 43:
                continue
            # Closing-price milestones, not peaks or executable exits.
            future = f["c"].shift(-1).iloc[::-1].rolling(steps, min_periods=steps).max().iloc[::-1]
            known = f["close_ts"].shift(-steps)
            g = f.copy()
            g["label"] = (future / f["c"] >= target["multiple"]).astype(int)
            g["asof"], g["label_known_at"] = f["close_ts"], known
            g["target"], g["group"] = target["name"], pool["network"]+":"+pool["token"]
            valid = (f["ts"].shift(-steps)-f["ts"] == steps*300) & f["continuous"]
            frames.append(g[valid].dropna(subset=PRICE_FEATURES+["label_known_at"]).iloc[::12])
    path = store.root / "downloads" / "scanner_dataset.csv"
    if path.exists():
        g = pd.read_csv(path)
        for k in PRICE_FEATURES:
            if k not in g:
                g[k] = np.nan
        g["asof"] = g["asof"].map(seconds)
        g["label_known_at"] = g["label_known_at"].map(seconds)
        g = g[(g["label_known_at"] > g["asof"]) & g["label"].isin([0,1]) & (g["network"]==cfg["scanner"]["network"])]
        for key in PRICE_FEATURES+SCANNER_EXTRA_FEATURES:
            if key in g:
                g[key] = pd.to_numeric(g[key],errors="coerce").replace([np.inf,-np.inf],np.nan)
        g["group"] = g["network"] + ":" + g["token"]
        # Strict shared semantics: only model input columns defined in DATA.md.
        frames.append(g)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames,ignore_index=True)
    key = ["group","asof","target"]
    conflicts = combined.groupby(key)["label"].transform("nunique")>1
    return combined[~conflicts].drop_duplicates(key,keep="last")


def train_scanner(cfg, store):
    f = scanner_training_rows(cfg, store)
    reports = {}
    for target in cfg["scanner"]["targets"]:
        name = target["name"]
        if f.empty:
            reports[name] = {"status":"no_mature_history_or_dataset"}
            continue
        g = f[f["target"] == name].copy()
        # Require a complete horizon even for early successful labels.
        g = g[g["label_known_at"] >= g["asof"] + target["horizon_hours"]*3600]
        model, report = fit_temporal(g, PRICE_FEATURES, cfg["scanner"]["model_min_rows"],
                                    cfg["scanner"]["model_min_positives"], "group", SCANNER_EXTRA_FEATURES)
        reports[name] = report
        if model:
            model.data["purpose"] = "chart_close_multiple_not_executable_return"
            model.data["target"] = target
            model.data["context"] = model_context(cfg, "scanner")
            model.save(store.root / "models" / (name + ".json"))
    store.set("model:scanner", reports)
    return reports
