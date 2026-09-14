from __future__ import annotations
import os
from pathlib import Path
import yaml


def load_env(path: Path):
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config(path="config.yaml"):
    path = Path(path).resolve()
    load_env(path.parent / ".env")
    if not path.exists():
        raise ValueError("Copy config.example.yaml to config.yaml first.")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if cfg.get("mode") != "paper":
        raise ValueError("Only mode: paper is supported. Real trading is not implemented.")
    root = Path(cfg["data_dir"])
    cfg["data_dir"] = str((path.parent / root).resolve())
    Path(cfg["data_dir"]).mkdir(parents=True, exist_ok=True)
    for section in ("cex", "dex"):
        c = cfg[section]
        for key in ("initial_cash",):
            if c[key] <= 0:
                raise ValueError(f"{section}.{key} must be positive")
        if not 0 < c["max_drawdown"] < 1:
            raise ValueError("max_drawdown must be between 0 and 1")
        if len(c["latency_ms"]) != 2 or not 0 <= c["latency_ms"][0] <= c["latency_ms"][1]:
            raise ValueError("Invalid latency range")
    c = cfg["cex"]
    if not 0 < c["visible_liquidity_fraction"] <= 1 or c["fee_rate"] < 0:
        raise ValueError("Invalid execution cost/depth parameters")
    if not 0 < c["risk_fraction"] <= c["max_position_fraction"] <= 1:
        raise ValueError("Invalid position/risk fractions")
    for k in ("dropped_probability", "revert_probability"):
        if not 0 <= cfg["dex"][k] <= 1:
            raise ValueError(k)
    if not 0<=c["lost_ack_probability"]<=1 or c["strategy"] not in ("adaptive","rules"):
        raise ValueError("Invalid CEX acknowledgment/strategy configuration")
    from .data import INTERVALS
    if c["interval"] not in INTERVALS or c["interval"]!=cfg["history"]["interval"]:
        raise ValueError("CEX/history intervals must match and be a supported interval")
    if not set(c["symbols"]).issubset(cfg["history"]["symbols"]):
        raise ValueError("history.symbols must contain every cex.symbol")
    if c["snapshot_limit"] not in (100,500,1000,5000):
        raise ValueError("Use a supported depth snapshot limit")
    for section,keys in {"cex":["max_positions","depth_stale_ms","model_refresh_days"],
                         "scanner":["poll_s","max_candidates","pages","history_limit"],
                         "history":["months","cohort_max_pools","cohort_follow_days","cohort_refresh_hours"],
                         "dex":["quote_decimals","quote_usd","ticket_usd","mark_every_s","poll_s","quote_max_age_s"],
                         "http":["max_requests_per_day","max_download_mb","timeout_s"]}.items():
        if any(cfg[section][k]<=0 for k in keys):
            raise ValueError(f"{section}: expected positive configuration values")
    if not 0<cfg["history"]["cohort_sample_fraction"]<=1:
        raise ValueError("cohort_sample_fraction must be in (0,1]")
    for section,keys in {"cex":["slippage_limit_bps","adverse_fill_bps"],
                         "dex":["slippage_bps","adverse_output_bps","gas_usd_per_tx","extra_fee_usd"]}.items():
        if any(cfg[section][k]<0 for k in keys):
            raise ValueError("Costs/slippage assumptions cannot be negative")
    if cfg["dex"]["adverse_output_bps"]>=10000 or not 0<cfg["dex"]["slippage_bps"]<10000:
        raise ValueError("Invalid DEX output/slippage range")
    if cfg["scanner"]["network"] != "solana" and cfg["dex"]["enabled"] and cfg["dex"]["provider"] == "jupiter":
        raise ValueError("Jupiter is Solana-only. Configure generic provider for other chains.")
    return cfg
