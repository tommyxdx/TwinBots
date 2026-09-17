from __future__ import annotations
import os
import math
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
    def finite_config(value):
        if isinstance(value, dict):
            for item in value.values():
                finite_config(item)
        elif isinstance(value, list):
            for item in value:
                finite_config(item)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Configuration numbers must be finite")
    finite_config(cfg)
    if cfg.get("mode") != "paper":
        raise ValueError("Only mode: paper is supported. Real trading is not implemented.")
    cfg["scanner"].setdefault("kind", "wallets")
    if cfg["scanner"]["kind"] not in ("wallets", "tokens"):
        raise ValueError("scanner.kind must be wallets or tokens")
    defaults = {"source": "chain", "ledger_dir": "wallet_ledgers", "addresses": [],
                "ledger_build_every_s": 600, "ledger_build_refresh_s": 14400,
                "ledger_reject_retry_s": 86400,
                "ledgers_per_cycle": 25, "ledger_calls_per_cycle": 20,
                "ledger_max_pages": 400, "ledger_retry_s": 3600,
                "ledger_max_transactions": 60000, "ledger_memory_ceiling_mb": 0,
                "url_template": "", "api_key_env": "WALLET_DATA_API_KEY",
                "header": "Authorization", "header_prefix": "Bearer ",
                "discover_enabled": True, "discovery_every_s": 21600,
                "discovery_pool_source": "trending", "discovery_sells_only": True,
                "discovery_min_trade_usd": 100.0,
                "discovery_max_pools": 6, "discovery_addresses_per_pool": 10,
                "max_candidates": 50, "max_wallets_per_run": 2, "refresh_s": 21600,
                "max_age_s": 21600, "max_ledger_mb": 10, "min_history_days": 30,
                "min_closed_cycles": 10, "min_closed_tokens": 3,
                "cycle_dust_fraction": 0.001,
                "ranking_snapshot_every_s": 86400,
                "max_censored_cost_fraction": 0.25, "max_unclassified_fraction": 0.5}
    w = cfg["wallets"] = {**defaults, **cfg.get("wallets", {})}
    # chain builds the ledgers itself from on-chain history; local reads files you
    # supply; adapter fetches them from a service you run.
    if (w["source"] not in ("chain", "local", "adapter") or type(w["discover_enabled"]) is not bool
            or type(w["discovery_sells_only"]) is not bool
            or w["discovery_pool_source"] not in ("trending", "top", "new")):
        raise ValueError("Invalid wallet source/discovery configuration")
    if w["discovery_min_trade_usd"] < 0:
        raise ValueError("wallets.discovery_min_trade_usd cannot be negative")
    from .wallets import BLOCKING_FLAGS, WALLET_FLAGS, valid_address
    if not isinstance(w["addresses"], list) or any(not valid_address(a) for a in w["addresses"]):
        raise ValueError("wallets.addresses must be a list of Solana public addresses")
    for key in ("discovery_every_s", "discovery_max_pools", "discovery_addresses_per_pool", "max_candidates",
                "max_wallets_per_run", "refresh_s", "max_age_s", "max_ledger_mb", "min_closed_cycles",
                "min_closed_tokens", "min_history_days", "ledger_build_every_s",
                "ledger_build_refresh_s", "ledgers_per_cycle", "ledger_max_pages",
                "ledger_retry_s", "ledger_calls_per_cycle", "ledger_reject_retry_s",
                "ledger_max_transactions", "ranking_snapshot_every_s"):
        if type(w[key]) is not int or w[key] <= 0:
            raise ValueError(f"wallets.{key} must be a positive integer")
    # Zero means derive it from the machine, which is what a small box wants.
    if type(w["ledger_memory_ceiling_mb"]) is not int or w["ledger_memory_ceiling_mb"] < 0:
        raise ValueError("wallets.ledger_memory_ceiling_mb must be 0 (automatic) or a positive integer")
    if not 0 < w["cycle_dust_fraction"] < 0.1:
        raise ValueError("wallets.cycle_dust_fraction must be a small positive share")
    if not 0 < w["max_censored_cost_fraction"] < 1:
        raise ValueError("wallets.max_censored_cost_fraction must be between 0 and 1")
    if not 0 < w["max_unclassified_fraction"] < 1:
        raise ValueError("wallets.max_unclassified_fraction must be between 0 and 1")
    # A ledger carries the time it was built, and the ranking refuses one older
    # than max_age_s. Rebuilding less often than that leaves every ledger dead
    # for the difference, which looks exactly like nothing being ranked at all.
    # The constraint is derived rather than chosen, so it is corrected, loudly.
    if w["ledger_build_refresh_s"] >= w["max_age_s"]:
        import logging
        logging.warning("wallets.ledger_build_refresh_s (%ds) is not below max_age_s (%ds); "
                        "ledgers would be stale for %dh out of every %dh. Using %ds.",
                        w["ledger_build_refresh_s"], w["max_age_s"],
                        (w["ledger_build_refresh_s"] - w["max_age_s"]) // 3600,
                        w["ledger_build_refresh_s"] // 3600, w["max_age_s"] * 2 // 3)
        w["ledger_build_refresh_s"] = w["max_age_s"] * 2 // 3
    if w["max_candidates"] > 1000 or len(w["addresses"]) > w["max_candidates"] or w["max_ledger_mb"] > 64:
        raise ValueError("Wallet candidate/ledger limits exceeded")
    w["ledger_dir"] = str((path.parent / w["ledger_dir"]).resolve())
    if cfg["scanner"]["kind"] == "wallets":
        if cfg["scanner"]["network"] != "solana":
            raise ValueError("Wallet ledger accounting currently supports Solana only")
        if cfg["dex"]["enabled"]:
            raise ValueError("Wallet mode uses follow.enabled for copy trading; keep dex.enabled false")
    # A YAML key with only comments under it parses as None, not an empty list.
    given = {k: v for k, v in (cfg.get("shortlist") or {}).items() if v is not None}
    short = cfg["shortlist"] = {"enabled": False, "refresh_s": 86400, "max_addresses": 500,
                                "sources": [], **given}
    if type(short["enabled"]) is not bool or not isinstance(short["sources"], list):
        raise ValueError("Invalid shortlist configuration")
    for key in ("refresh_s", "max_addresses"):
        if type(short[key]) is not int or short[key] <= 0:
            raise ValueError(f"shortlist.{key} must be a positive integer")
    if short["max_addresses"] > 5000:
        raise ValueError("shortlist.max_addresses exceeds what any budget can reconstruct")
    for source in short["sources"]:
        if not isinstance(source, dict) or source.get("kind") not in ("http", "file"):
            raise ValueError("Each shortlist source needs kind: http or file")
        if source["kind"] == "file":
            if not source.get("path"):
                raise ValueError("A file shortlist source needs a path")
            source["path"] = str((path.parent / source["path"]).resolve())
        else:
            if not str(source.get("url", "")).startswith("https://"):
                raise ValueError("A shortlist source URL must be HTTPS")
            if not source.get("address_path"):
                raise ValueError("A shortlist source needs address_path to locate addresses")
    follow_defaults = {"enabled": False, "source": "local", "activity_dir": "wallet_activity",
                       "url_template": "", "api_key_env": "WALLET_ACTIVITY_API_KEY",
                       "header": "Authorization", "header_prefix": "Bearer ",
                       "max_leaders": 3, "min_score": 10.0, "allowed_flags": [],
                       "activity_poll_s": 30, "activity_lookback_s": 900, "status_every_s": 300,
                       "ranking_max_age_s": 21600, "max_feed_age_s": 300, "max_signal_age_s": 120,
                       "platform_fee_fraction": 0.01,
                       "min_leader_notional_usd": 50.0, "seen_memory": 5000, "cooldown_s": 3600,
                       "mirror_exits": True, "max_activity_mb": 4,
                       "initial_cash": 100, "ticket_usd": 2, "max_positions": 3,
                       "max_drawdown": 0.10, "max_hold_hours": 24, "stop_fraction": 0.25,
                       "trail_fraction": 0.30, "poll_s": 60, "mark_every_s": 300}
    f = cfg["follow"] = {**follow_defaults, **cfg.get("follow", {})}
    if f["source"] not in ("local", "adapter") or type(f["enabled"]) is not bool or type(f["mirror_exits"]) is not bool:
        raise ValueError("Invalid follow source/enabled/mirror_exits configuration")
    if f["enabled"] and cfg["scanner"]["kind"] != "wallets":
        raise ValueError("follow.enabled requires scanner.kind: wallets to produce a ranking")
    for key in ("max_leaders", "ranking_max_age_s", "max_feed_age_s", "max_signal_age_s",
                "seen_memory", "cooldown_s", "max_activity_mb", "max_positions",
                "max_hold_hours", "poll_s", "mark_every_s", "activity_poll_s",
                "activity_lookback_s", "status_every_s"):
        if type(f[key]) is not int or f[key] <= 0:
            raise ValueError(f"follow.{key} must be a positive integer")
    if not isinstance(f["allowed_flags"], list) or not set(f["allowed_flags"]) <= set(WALLET_FLAGS):
        raise ValueError("follow.allowed_flags must be a subset of " + ", ".join(WALLET_FLAGS))
    if set(f["allowed_flags"]) & set(BLOCKING_FLAGS):
        raise ValueError("follow.allowed_flags cannot re-admit a flag that blocks ranking")
    if not 0 <= f["platform_fee_fraction"] < 0.1:
        raise ValueError("follow.platform_fee_fraction must be a small non-negative share")
    if f["initial_cash"] <= 0 or f["ticket_usd"] <= 0 or f["min_leader_notional_usd"] < 0:
        raise ValueError("Invalid follow capital/ticket/notional configuration")
    if not 0 < f["max_drawdown"] < 1 or not 0 < f["stop_fraction"] < 1 or not 0 < f["trail_fraction"] < 1:
        raise ValueError("Invalid follow drawdown/stop/trailing fraction")
    if f["max_activity_mb"] > 64 or f["max_leaders"] > 200:
        raise ValueError("Follow activity/leader limits exceeded")
    # Every leader is polled or pushed separately, and each concurrent position
    # needs its own ticket, so a wide leader set is not free in either currency.
    if f["max_leaders"] > f["max_positions"] * 20:
        raise ValueError("follow.max_leaders far exceeds max_positions; most signals "
                         "would be dropped for lack of a free slot")
    if f["max_signal_age_s"] > f["max_feed_age_s"]:
        raise ValueError("follow.max_signal_age_s cannot exceed follow.max_feed_age_s")
    f["activity_dir"] = str((path.parent / f["activity_dir"]).resolve())
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
    if not 0 < c["min_stop_fraction"] <= c["max_stop_fraction"] < 1:
        raise ValueError("Invalid CEX stop range")
    if not 0 < c["model_threshold"] < 1 or c["stop_atr"] <= 0 or c["trail_atr"] <= 0:
        raise ValueError("Invalid CEX model or ATR threshold")
    if not 0 < cfg["dex"]["stop_fraction"] < 1 or not 0 < cfg["dex"]["trail_fraction"] < 1:
        raise ValueError("Invalid DEX stop/trailing fraction")
    return cfg
