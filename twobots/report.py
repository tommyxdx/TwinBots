from __future__ import annotations
import html
import json
import time
from pathlib import Path


def build_report(cfg,store):
    accounts = {}
    for venue in ("cex","dex"):
        state = store.get("account:"+venue)
        if state is None:
            continue
        mark = store.rows("SELECT ts,equity,payload FROM marks WHERE venue=? ORDER BY id DESC LIMIT 1",(venue,))
        orders = store.rows("SELECT status,count(*) AS n FROM orders WHERE venue=? GROUP BY status",(venue,))
        accounts[venue] = {"account":state,"latest_mark":mark,"orders_by_status":orders,
                           "capital_scope":"Separate hypothetical account; do not add CEX and DEX returns as one $100 portfolio"}
    scans = store.rows("SELECT payload FROM scans ORDER BY id DESC LIMIT 20")
    return {"generated_at":time.time(),"mode":"PAPER_ONLY","accounts":accounts,
            "scanner_latest":[json.loads(x["payload"]) for x in scans],
            "model_cex":store.get("model:cex"),"model_scanner":store.get("model:scanner"),
            "history_bootstrap":store.get("bootstrap:report"),"cohort":store.get("history:cohort_report"),
            "request_counts":store.rows("SELECT * FROM requests ORDER BY day DESC LIMIT 7"),
            "candle_counts":store.rows("SELECT market,count(*) AS n FROM candles GROUP BY market"),
            "notifications":store.rows("SELECT status,count(*) AS n FROM outbox GROUP BY status"),
            "recent_local_rejections":store.rows("SELECT ts,payload FROM events WHERE kind='cex_rejection' ORDER BY id DESC LIMIT 20"),
            "recent_errors":store.rows("SELECT ts,kind,payload FROM events WHERE kind LIKE '%error%' OR kind LIKE '%unavailable%' ORDER BY id DESC LIMIT 20"),
            "heartbeats":{k:store.get("heartbeat:"+k) for k in ("cex","dex","scanner","maintenance")},
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
                    "API/hosting/data subscription costs are not subtracted from account returns"]}}


def export_report(cfg,store):
    report = build_report(cfg,store)
    folder = store.root/"reports"
    folder.mkdir(exist_ok=True)
    raw = json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)
    (folder/"latest.json").write_text(raw,encoding="utf-8")
    cards = []
    for venue,item in report["accounts"].items():
        state = item["account"]
        marks = item["latest_mark"]
        eq = f"{marks[0]['equity']:.4f}" if marks else "尚未估值"
        cards.append(f"<section><h2>{venue.upper()} 独立模拟账户</h2><p>起始 ${state['initial_cash']:.2f} · "
                     f"现金 ${state['cash']:.4f} · 最近净值 {eq} · 已记费用 ${state['fees']:.4f}</p>"
                     f"<p>新仓暂停：{state['halted']}；持仓数：{len(state['positions'])}</p></section>")
    text = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<title>TwinCryptoBots 模拟报告</title><style>body{font:16px/1.6 system-ui;max-width:1050px;margin:40px auto;padding:0 20px;background:#101820;color:#edf2f7}"
            "section{background:#1b2b36;padding:16px;margin:16px 0;border-radius:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}summary{cursor:pointer}</style>"
            "<h1>TwinCryptoBots · PAPER</h1><p>这是模拟记录，未执行真实交易。两个账户各自使用虚拟本金，不能合并视为一笔 $100 的收益。</p>"
            +"".join(cards)+"<p>完整成交、模型检验、数据覆盖和假设见下方 JSON。此报告不会自动刷新，请重新运行 report。</p>"
            "<details open><summary>审计数据</summary><pre>"+html.escape(raw)+"</pre></details></html>")
    (folder/"latest.html").write_text(text,encoding="utf-8")
    return str(folder/"latest.html")
