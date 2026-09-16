from __future__ import annotations
import json
import logging
import math
import time
from urllib.parse import quote
from .data import number, seconds
from .features import price_frame
from .models import LinearProbability, model_context, model_unavailable
from .net import auth_headers
from .storage import dumps

LOG = logging.getLogger(__name__)


def finite_features(row):
    return {k: float(v) for k, v in row.items()
            if isinstance(v, (int, float)) and math.isfinite(v)}


def assess(pool, row, security, cfg, now=None):
    """Transparent hypotheses, NOT a learned 100x classifier."""
    now = now or time.time()
    a = pool.get("attributes", {})
    liq = number(a.get("reserve_in_usd"))
    vol = number(a.get("volume_usd", {}).get("h1"))
    tx = a.get("transactions", {}).get("h1", {})
    buys, sells = number(tx.get("buys")), number(tx.get("sells"))
    r = finite_features(row or {})
    checks = []

    def add(name, weight, value, rule, evidence):
        passed = None if value is None else bool(rule(value))
        checks.append({"name": name, "weight": weight, "pass": passed, "evidence": evidence})

    add("流动性门槛", 15, liq, lambda x: x >= cfg["min_liquidity_usd"], liq)
    age = (now - pool["created"]) / 3600
    add("池龄 0.5–48 小时", 5, age, lambda x: .5 <= x <= cfg["max_age_hours"], round(age, 2))
    turnover = vol / liq if vol is not None and liq and liq > 0 else None
    add("小时换手不过热", 10, turnover, lambda x: .05 <= x <= 3, turnover)
    ratio = buys / max(sells, 1) if buys is not None and sells is not None else None
    add("有双向成交且买卖比适中", 10, ratio,
        lambda x: sells >= 5 and 1.1 <= x <= 4, {"buys": buys, "sells": sells})
    add("最近 30 分钟正动量但未翻倍", 10, r.get("ret6"), lambda x: 0 < x < .5, r.get("ret6"))
    add("成交量增长不过热", 10, r.get("volume_ratio"), lambda x: 1.2 <= x <= 5, r.get("volume_ratio"))
    add("距近期高点回撤有限", 5, r.get("drawdown42"), lambda x: -.25 < x <= 0, r.get("drawdown42"))
    add("可卖出检查", 10, security.get("can_sell"), lambda x: x is True, security.get("can_sell"))
    auth = None if any(k not in security for k in ("mint_revoked", "freeze_revoked")) else (
        security["mint_revoked"] is True and security["freeze_revoked"] is True)
    add("增发与冻结权限已撤销", 10, auth, lambda x: x is True, auth)
    top = number(security.get("top10_ex_lp_fraction"))
    add("前十持仓占比低于 35%（剔除池）", 5, top, lambda x: 0 <= x < .35, top)
    cluster = number(security.get("largest_funding_cluster_fraction"))
    add("最大关联资金簇低于 20%", 5, cluster, lambda x: 0 <= x < .2, cluster)
    bad = number(security.get("creator_bad_rate"))
    add("创建者历史坏币率低于 20%", 5, bad, lambda x: 0 <= x < .2, bad)
    blocked = [k for k in ("can_sell", "mint_revoked", "freeze_revoked") if security.get(k) is False]
    concentrations = (("top10_ex_lp_fraction", top, .35),
                      ("largest_funding_cluster_fraction", cluster, .2),
                      ("creator_bad_rate", bad, .2))
    blocked.extend(k for k, value, maximum in concentrations
                   if value is not None and not 0 <= value < maximum)
    risk_verified = (all(security.get(k) is True for k in ("can_sell", "mint_revoked", "freeze_revoked"))
                     and all(value is not None and 0 <= value < maximum
                             for _, value, maximum in concentrations))
    score = sum(c["weight"] for c in checks if c["pass"] is True)
    return {"score": score, "score_denominator": 100, "passed": sum(c["pass"] is True for c in checks),
            "known": sum(c["pass"] is not None for c in checks), "total_checks": len(checks),
            "checks": checks, "blocked": blocked,
            "risk_verified": risk_verified,
            "market_eligible": checks[0]["pass"] is True and checks[1]["pass"] is True,
            "security_verified": all(security.get(k) is True for k in ("can_sell", "mint_revoked", "freeze_revoked")),
            "score_meaning": "Unvalidated heuristic priority score; not probability or proven edge"}


class Scanner:
    def __init__(self, cfg, store, http, fetcher, telegram):
        self.cfg, self.c, self.store, self.http = cfg, cfg["scanner"], store, http
        self.fetcher, self.telegram = fetcher, telegram

    def security(self, pool):
        template = self.c.get("feature_url_template")
        if not template:
            return {}, {"status": "not_configured"}
        url = template.format(**{k: quote(str(pool[k]), safe="") for k in ("network", "token", "pool")})
        p = self.http.json(url, headers=auth_headers(self.c, "feature_"))
        if any(p.get(k) != pool[k] for k in ("network", "token", "pool")):
            raise ValueError("Feature API identity mismatch")
        at = seconds(p["asof"])
        if not -5 <= time.time()-at <= self.c["feature_max_age_s"]:
            raise ValueError("Feature API snapshot is stale or in the future")
        features = p.get("features", {})
        for k in ("can_sell", "mint_revoked", "freeze_revoked"):
            if k in features and not isinstance(features[k], bool):
                raise ValueError("Security values must be JSON booleans, not strings")
        for k in ("top10_ex_lp_fraction", "largest_funding_cluster_fraction", "creator_bad_rate"):
            if k in features and (isinstance(features[k], bool) or number(features[k]) is None
                                  or not 0 <= number(features[k]) <= 1):
                raise ValueError("Security fractions must be finite values in [0,1]")
        return features, {"status": "external_snapshot", "asof": at, "source": p.get("source", "configured_adapter")}

    def probabilities(self, row):
        results = {}
        for target in self.c["targets"]:
            name = target["name"]
            p = self.store.root / "models" / (name + ".json")
            result = {"probability": None, "status": "insufficient_mature_labels", "target": target,
                      "meaning": "future closing-price milestone, not executable investment return"}
            if p.exists():
                try:
                    model = LinearProbability.load(p)
                except (OSError, ValueError):
                    result["status"] = "invalid_model"
                    results[name] = result
                    continue
                m = model.data["metrics"]
                result["validation"] = m
                unavailable = model_unavailable(model.data, 7*86400, model_context(self.cfg, "scanner"))
                if model.data.get("target") != target:
                    result["status"] = "model_target_changed"
                elif unavailable:
                    result["status"] = unavailable
                elif m["brier"] >= m["baseline_brier"] or m["average_precision"] <= m["test_prevalence"]:
                    result["status"] = "no_holdout_advantage"
                else:
                    result["probability"] = model.predict(row)
                    result["status"] = "research_estimate" if result["probability"] is not None else "missing_inputs"
                result["training_associations"] = model.data.get("training_associations",[])[:5]
            results[name] = result
        return results

    def run_once(self):
        pools = self.fetcher.discover()
        # Rotate eligible candidates; don't select only historical winners for collection.
        eligible = [p for p in pools if 0 <= time.time()-p["created"] <= self.c["max_age_hours"]*3600]
        eligible.sort(key=lambda p: self.store.get("scan:last:" + p["pool"], 0))
        results = []
        for p in eligible[:self.c["max_candidates"]]:
            errors = []
            try:
                self.fetcher.pool_history(p["network"], p["pool"], refresh=True)
            except Exception as exc:
                errors.append(str(exc))
            f = price_frame(self.store.candles("dex:"+p["network"], p["pool"], 1000),300)
            fresh = bool(not f.empty and 0 <= time.time()-f.iloc[-1]["close_ts"] <= 900
                         and f.iloc[-1]["continuous"])
            row = f.iloc[-1].to_dict() if fresh else {}
            try:
                sec, provenance = self.security(p)
            except Exception as exc:
                sec, provenance = {}, {"status": "unavailable"}
                errors.append(str(exc))
            a = p.get("attributes",{})
            liq,vol = number(a.get("reserve_in_usd")),number(a.get("volume_usd",{}).get("h1"))
            tx = a.get("transactions",{}).get("h1",{})
            buys,sells = number(tx.get("buys")),number(tx.get("sells"))
            extra = {"log_liquidity":math.log1p(liq) if liq is not None and liq>=0 else None,
                     "age_hours":(time.time()-p["created"])/3600,
                     "turnover_h1":vol/liq if vol is not None and liq and liq>0 else None,
                     "buy_sell_ratio_h1":buys/max(sells,1) if buys is not None and sells is not None else None,
                     **{k:number(sec.get(k)) for k in ("top10_ex_lp_fraction","largest_funding_cluster_fraction","creator_bad_rate")}}
            row.update(finite_features(extra))
            result = {"at": time.time(), "network": p["network"], "token": p["token"], "pool": p["pool"],
                      "symbol": p.get("token_attributes", {}).get("symbol", "?"),
                      **assess(p, row, sec, self.c), "features": finite_features(row),
                      "history_fresh": fresh,
                      "history_asof": float(f.iloc[-1]["close_ts"]) if not f.empty else None,
                      "security_source": provenance,
                      "probabilities": self.probabilities(row), "errors": errors}
            with self.store.transaction() as db:
                db.execute("INSERT INTO scans(ts,network,token,pool,score,payload) VALUES(?,?,?,?,?,?)",
                           (result["at"], p["network"], p["token"], p["pool"], result["score"], dumps(result)))
            self.store.set("scan:last:"+p["pool"], result["at"])
            last = self.store.get("scan:alert:"+p["token"], {"at":0, "score":0})
            due = time.time()-last["at"] >= self.c["cooldown_hours"]*3600 or (
                result["score"]-last["score"] >= self.c["notify_min_score_change"])
            if (result["score"] >= self.c["min_score"] and result["known"] >= self.c["min_known_checks"]
                    and not result["blocked"] and due):
                self.telegram.enqueue(f"scan:{p['network']}:{p['token']}:{int(result['at'])}", self.message(result))
                self.store.set("scan:alert:"+p["token"], {"at":result["at"], "score":result["score"]})
            LOG.info("Scan %s score=%s known=%s/%s blocked=%s", p["token"], result["score"],
                     result["known"], result["total_checks"], result["blocked"])
            results.append(result)
        self.telegram.flush()
        self.store.set("heartbeat:scanner", time.time())
        return results

    @staticmethod
    def message(r):
        yes = "、".join(c["name"] for c in r["checks"] if c["pass"] is True)
        unknown = "、".join(c["name"] for c in r["checks"] if c["pass"] is None)
        lines = [f"观察候选 {r['symbol']} | {r['network']}", f"合约：{r['token']}", f"池：{r['pool']}",
                 f"启发式评分 {r['score']}/100（不是成功概率）",
                 f"特征符合 {r['passed']}/{r['total_checks']}；有数据 {r['known']}/{r['total_checks']}",
                 "符合："+yes, "未知："+(unknown or "无")]
        for name, p in r["probabilities"].items():
            value = "无法估计："+p["status"] if p["probability"] is None else f"研究估计 {p['probability']:.1%}"
            lines.append(name + "：" + value)
        lines.append("目标是图表收盘价倍数；不代表能以该价格卖出。由你决定是否交易。")
        return "\n".join(lines)
