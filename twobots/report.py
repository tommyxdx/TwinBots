from __future__ import annotations
import html
import json
import time
from pathlib import Path


def build_report(cfg,store):
    accounts = {}
    for venue in ("cex","dex","copy"):
        state = store.get("account:"+venue)
        if state is None:
            continue
        mark = store.rows("SELECT ts,equity,payload FROM marks WHERE venue=? ORDER BY id DESC LIMIT 1",(venue,))
        orders = store.rows("SELECT status,count(*) AS n FROM orders WHERE venue=? GROUP BY status",(venue,))
        path = store.rows("SELECT ts,equity FROM marks WHERE venue=? ORDER BY id", (venue,))
        peak, max_dd = state["initial_cash"], 0.0
        for point in path:
            peak = max(peak, point["equity"])
            max_dd = max(max_dd, 1-point["equity"]/peak)
        details = json.loads(mark[0]["payload"]) if mark else {}
        stale = details.get("stale_symbols", []) + details.get("unquotable_marked_zero", [])
        settled = [json.loads(x["payload"]) for x in store.rows("SELECT payload FROM orders WHERE venue=?", (venue,))]
        fills = sum(p.get("settled_status") in ("FILLED", "PARTIALLY_FILLED_CANCELED") for p in settled)
        performance = {"filled_orders": fills, "no_fills": fills == 0,
                       "realized_pnl": state["realized_pnl"], "fees": state["fees"],
                       "observed_max_drawdown": max_dd if path else None,
                       "net_return_at_last_mark": mark[0]["equity"]/state["initial_cash"]-1 if mark else None,
                       "mark_age_s": time.time()-mark[0]["ts"] if mark else None,
                       "stale_inventory": stale, "mark_count": len(path),
                       "warning": "Sampled paper marks, not validated profitability; subscriptions/hosting excluded and execution assumptions unverified"}
        accounts[venue] = {"account":state,"latest_mark":mark,"orders_by_status":orders,
                           "forward_metrics": performance,
                           "capital_scope":"Separate hypothetical account; do not add CEX and DEX returns as one $100 portfolio"}
    scans = store.rows("SELECT payload FROM scans ORDER BY id DESC LIMIT 20")
    builds = store.get("wallet:build") or {}
    usable = sum(1 for b in builds.values() if b.get("usable"))
    blocked = {}
    for entry in builds.values():
        if entry.get("usable"):
            continue
        reason = entry.get("blocked_by") or entry.get("error") or "pending"
        blocked[reason] = blocked.get(reason, 0) + 1
    ledger_builds = {"checked": len(builds), "usable": usable,
                     "rejection_rate": round(1 - usable / len(builds), 3) if builds else None,
                     "blocked_by": dict(sorted(blocked.items(), key=lambda kv: -kv[1])),
                     "rpc_calls_last_seen": max((b.get("rpc_calls", 0) for b in builds.values()),
                                                default=0)} if builds else None
    wallet_report = store.get("wallet:latest")
    if wallet_report is not None:
        wallet_report["report_age_s"] = time.time() - wallet_report["generated_at"]
        wallet_report["stale"] = wallet_report["report_age_s"] > cfg["wallets"]["max_age_s"] or any(
            time.time() - row["asof"] > cfg["wallets"]["max_age_s"] for row in wallet_report["ranking"])
    return {"generated_at":time.time(),"mode":"PAPER_ONLY","accounts":accounts,
            "scanner_kind":cfg["scanner"]["kind"], "wallet_scanner":wallet_report,
            "ledger_builds":ledger_builds,
            "scanner_latest":[json.loads(x["payload"]) for x in scans] if cfg["scanner"]["kind"] == "tokens" else [],
            "model_cex":store.get("model:cex"),"model_scanner":store.get("model:scanner"),
            "history_bootstrap":store.get("bootstrap:report"),"cohort":store.get("history:cohort_report"),
            "request_counts":store.rows("SELECT * FROM requests ORDER BY day DESC LIMIT 7"),
            "candle_counts":store.rows("SELECT market,count(*) AS n FROM candles GROUP BY market"),
            "notifications":store.rows("SELECT status,count(*) AS n FROM outbox GROUP BY status"),
            "recent_local_rejections":store.rows("SELECT ts,payload FROM events WHERE kind='cex_rejection' ORDER BY id DESC LIMIT 20"),
            "recent_errors":store.rows("SELECT ts,kind,payload FROM events WHERE kind LIKE '%error%' OR kind LIKE '%unavailable%' ORDER BY id DESC LIMIT 20"),
            "heartbeats":{k:store.get("heartbeat:"+k) for k in ("cex","dex","copy","scanner","maintenance")},
            "copy_leaders":store.get("copy:leaders"),
            "recent_copy_entries":store.rows("SELECT ts,payload FROM events WHERE kind='copy_entry_result' ORDER BY id DESC LIMIT 20"),
            "assumptions":{
                "cex_latency_ms":cfg["cex"]["latency_ms"],"visible_depth_fraction":cfg["cex"]["visible_liquidity_fraction"],
                "cex_fee_rate_fallback":cfg["cex"]["fee_rate"],"fee_currency":"USDT quote-equivalent",
                "cex_lost_ack_probability":cfg["cex"]["lost_ack_probability"],
                "dex_latency_ms":cfg["dex"]["latency_ms"],"dex_adverse_output_bps":cfg["dex"]["adverse_output_bps"],
                "dex_gas_usd":cfg["dex"]["gas_usd_per_tx"],"dex_revert_probability":cfg["dex"]["revert_probability"],
                "dex_dropped_probability":cfg["dex"]["dropped_probability"],
                "limits":["No proof of profitability or $100k attainment",
                    "L2 cannot reveal true order queue; only IOC is simulated",
                    "Historical candles do not reconstruct execution; PnL starts with live paper fills",
                    "Virtual orders do not change the public order book or AMM reserves",
                    "DEX quotes do not verify token transfer restrictions, MEV, slot landing or wallet execution",
                    "Missing failed-token history creates censoring/survivorship bias; no invented loss labels",
                    "Funding, borrowing and leverage excluded: spot inventory only",
                    "Copy entries fill at our own later quote, never the leader's price; lag, size and capacity differ",
                    "A leader's historical rank is not evidence that copying it is profitable",
                    "API/hosting/data subscription costs are not subtracted from account returns"]}}


def export_report(cfg,store):
    report = build_report(cfg,store)
    folder = store.root/"reports"
    folder.mkdir(exist_ok=True)
    raw = json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)
    (folder/"latest.json").write_text(raw,encoding="utf-8")
    cards = []
    wallets = report["wallet_scanner"] if cfg["scanner"]["kind"] == "wallets" else None
    if wallets is not None:
        def fmt(value, percent=False):
            return "—" if value is None else (f"{value:.1%}" if percent else f"{value:,.2f}")
        rows = []
        for wallet in wallets["ranking"]:
            m = wallet["windows"]["90"]
            values = [wallet["rank"] or "观察", wallet["address"], fmt(m["realized_pnl_usd"]),
                      fmt(m["cost_roi"], True), m["closed_cycles"], m["trade_fills"], fmt(m["win_rate"], True),
                      fmt(m["profit_factor"]), fmt(m["without_best_token_pnl_usd"]), fmt(wallet["open_loss_usd"]),
                      fmt(m["external_origin_pnl_usd"]), fmt(wallet["censored_cost_fraction"], True),
                      fmt(wallet["score"])]
            rows.append("<tr>" + "".join("<td>" + html.escape(str(v)) + "</td>" for v in values) + "</tr>")
        headings = ["排名", "钱包地址", "已实现净盈亏 $", "已售成本收益率", "完整平仓", "成交次数", "胜率",
                    "Profit Factor", "去掉最大盈利币后 $", "未平仓亏损 $", "转入币盈亏 $", "记录缺口",
                    "研究分数"]
        cards.append("<section><h2>钱包历史表现 · 90 天</h2><p>"
                     + ("数据已过期，请重新扫描。" if wallets["stale"] else "各钱包数据截止时间见审计明细。")
                     + "胜率按完整持仓周期计算，跨窗口周期不计入胜率；分数不是盈利概率。"
                     + "转入币盈亏来自空投或从其它地址转入的库存，不计入收益率和胜率；"
                     + "记录缺口是转出到其它地址的成本占比，越高说明这个钱包的实际去向越看不到。"
                     + f"数据不足的钱包：{len(wallets['unavailable'])} 个。</p><div style='overflow-x:auto'><table><thead><tr>"
                     + "".join("<th>" + h + "</th>" for h in headings) + "</tr></thead><tbody>"
                     + "".join(rows) + "</tbody></table></div></section>")
    builds = report["ledger_builds"]
    if builds:
        reasons = "、".join(f"{k} {v}" for k, v in builds["blocked_by"].items()) or "无"
        cards.append("<section><h2>账本还原</h2><p>已检查 "
                     + f"{builds['checked']} 个候选，可用 {builds['usable']} 个，拒绝率 "
                     + (f"{builds['rejection_rate']:.1%}" if builds["rejection_rate"] is not None else "—")
                     + "。</p><p>拒绝原因：" + html.escape(reasons)
                     + "。转入、转出、代币互换和批量卖出都已入账，不构成拒绝；"
                     + "剩下的主要是一笔买入多个代币和历史太短。</p></section>")
    leaders = report["copy_leaders"]
    if cfg["follow"]["enabled"] and leaders is not None:
        entries = [json.loads(row["payload"]) for row in report["recent_copy_entries"]]
        filled = [e for e in entries if e.get("settled_status") == "FILLED"]
        lags = [e["lag_s"] for e in filled if e.get("lag_s") is not None]
        cards.append("<section><h2>跟单来源</h2><p>当前跟随 "
                     + html.escape(str(len(leaders["addresses"]))) + " 个钱包："
                     + (html.escape("、".join(leaders["addresses"])) or "无合格钱包")
                     + f"。最近 {len(entries)} 次入场信号中成交 {len(filled)} 次"
                     + (f"，跟单延迟中位数 {sorted(lags)[len(lags)//2]:.1f} 秒" if lags else "")
                     + "。成交价是本程序自己的报价，不是被跟随钱包的成交价。</p></section>")
    for venue,item in report["accounts"].items():
        state = item["account"]
        marks = item["latest_mark"]
        eq = f"{marks[0]['equity']:.4f}" if marks else "尚未估值"
        cards.append(f"<section><h2>{venue.upper()} 独立模拟账户</h2><p>起始 ${state['initial_cash']:.2f} · "
                     f"现金 ${state['cash']:.4f} · 最近净值 {eq} · 已记费用 ${state['fees']:.4f}</p>"
                     f"<p>新仓暂停：{state['halted']}；持仓数：{len(state['positions'])}</p></section>")
    text = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<title>TwinCryptoBots 模拟报告</title><style>body{font:16px/1.6 system-ui;max-width:1050px;margin:40px auto;padding:0 20px;background:#101820;color:#edf2f7}"
            "section{background:#1b2b36;padding:16px;margin:16px 0;border-radius:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}summary{cursor:pointer}table{border-collapse:collapse;font-size:13px}th,td{padding:8px;border-bottom:1px solid #405360;text-align:right}td:nth-child(2){font-family:monospace;text-align:left}</style>"
            "<h1>TwinCryptoBots · PAPER</h1><p>这是模拟记录，未执行真实交易。两个账户各自使用虚拟本金，不能合并视为一笔 $100 的收益。</p>"
            +"".join(cards)+"<p>完整成交、模型检验、数据覆盖和假设见下方 JSON。此报告不会自动刷新，请重新运行 report。</p>"
            "<details open><summary>审计数据</summary><pre>"+html.escape(raw)+"</pre></details></html>")
    (folder/"latest.html").write_text(text,encoding="utf-8")
    return str(folder/"latest.html")
