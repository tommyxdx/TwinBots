# 跟单交易接入与运行

跟单 bot 消费钱包排名的结果，把入选钱包的新成交镜像到一个**独立的模拟账户**。仍然是 paper 模式：只读报价，不签名、不广播、不接私钥。

## 与扫描器的关系

扫描器每六小时用 90 天完整账本给钱包打分（见 [WALLET_RANKING.md](WALLET_RANKING.md)）。跟单 bot 只从 `status: ranked` 且无标记的钱包里挑领投，再单独订阅这些钱包的**近实时成交流**。两条数据管线不同：排名是历史批量，跟单是实时增量，必须分别接入。

历史排名高不等于跟单能赚。领投的入场价、仓位规模、资金容量和你都不一样，而你永远比它晚。

## 领投筛选

`follow.max_leaders` 个钱包，按排名顺序取，且必须同时满足：

| 条件 | 配置项 | 说明 |
|---|---|---|
| 已进入正式排序 | — | `score` 为 `null` 的观察名单不跟 |
| 分数下限 | `min_score` | 默认 10 |
| 标记为空 | `allowed_flags` | 默认 `[]`，即单币集中、去掉最大盈利币即亏损的钱包都不跟 |
| 排名与账本新鲜 | `ranking_max_age_s` | 默认 6 小时，过期则暂停开新仓 |

排名阶段已经硬性拒绝样本不足、以及**已实现盈利盖不住未平仓亏损**的钱包。后者是最常见的假业绩形态：只卖赢家、把烂币永远挂在账上，账面已实现收益很好看，真实净值是负的。这类钱包不会拿到分数，因此也进不了领投名单；`allowed_flags` 不允许重新放行这两个标记。

## 成交流格式

`follow.source: local` 时放在 `<activity_dir>/<Solana 公钥>.json`；`adapter` 时由 `url_template` 的 GET 返回同一结构。文件名/`address` 必须与领投地址一致。

```json
{
  "schema_version": 1,
  "network": "solana",
  "address": "<领投钱包公钥>",
  "asof": 1757900000,
  "source": "<来源与处理版本>",
  "fills": [
    {"id": "<交易签名:资产腿序号>", "ts": 1757899990, "side": "buy",
     "token": "<mint 地址>", "notional_usd": "250"}
  ]
}
```

`asof` 落后超过 `max_feed_age_s`（默认 300 秒）整份数据作废。单条 `fills` 最多 500 条，`id` 在一份数据内不可重复，`side` 只接受 `buy`/`sell`。`notional_usd` 是领投该笔的美元对价，用于 `min_leader_notional_usd` 过滤灰尘单。

这里**不需要**完整成本历史——那是排名阶段的事。跟单只需要"谁、什么时候、买卖了哪个币、多大"。

内置适配器可以直接产出这份数据，轮询和 Webhook 两种模式（后者便宜约 100 倍）：

```bash
python -m adapters.activity poll --data-dir data --out wallet_activity --watch
python -m adapters.activity webhook --out wallet_activity --port 8788
```

数据源选型、延迟与费用对比见 [DATA_SOURCES.md](DATA_SOURCES.md)。

## 延迟闸门

跟单失败最主要的原因是延迟。程序有两道闸：

1. 读到成交时，`now - fill.ts > max_signal_age_s`（默认 120 秒）直接丢弃，**并永久标记为已处理**——同一笔成交之后即使重新出现在数据里也不会补单。
2. 报价往返本身耗时，下单前再查一次延迟，超限返回 `COPY_LAG_EXCEEDED` 不成交。

每笔成功入场都会把 `copy_lag_s` 写进持仓和事件日志，报告里给出延迟中位数。这个数字如果接近闸门上限，说明数据源太慢，跟单结果不可信。

## 出场

`mirror_exits: true`（默认）时领投卖出即全额跟卖。除此之外本地风控独立生效，与领投无关：

- `stop_fraction` 止损、`trail_fraction` 移动止盈（`take_profit_activate_multiple` 触发后）
- `max_hold_hours` 超时平仓
- `max_drawdown` 触发后停止开新仓
- 连续 `stale_mark_s` 拿不到可执行报价的持仓按 0 计入净值，但仓位数量保留不删

领投可能在你看不到的地方用你没有的信息出场。本地风控是兜底，不是复制。

## 账户与成本

跟单使用 `account:copy`，与 CEX、DEX 三个账户各自独立计算，**不能相加当成一笔本金的收益**。仓位规模是固定票面 `ticket_usd`，不按领投仓位比例缩放——领投的资金量和风险偏好不可观测。

滑点、价格冲击上限、gas、往返成本筛选沿用 `dex` 段的参数，因为这些是场地成本而不是策略参数。入场前会做一次买卖双向报价的往返成本测算，超过 `max_roundtrip_cost_fraction` 不进场。

## 运行

```bash
python -m twobots scan --once     # 先产出排名
python -m twobots trade --venue copy
python -m twobots report
```

`run` 命令会在 `follow.enabled: true` 时自动带上跟单 bot。`doctor` 输出 `copy_trading_enabled` 与数据源配置。

同一份成交流重复投喂不会重复开仓（`id` 去重，记忆 `seen_memory` 条）；同一代币 `cooldown_s` 内不再入场。领投数据缺失或格式错误记录事件后跳过，不影响其它领投，也不会因此清仓。

## 本版没有做的事

不按领投仓位比例调整规模；不识别领投之间的关联地址或对敲；不检测领投是否在跟单者进场后卖给跟单者；不做 MEV、抢跑或私有交易池；不验证 `notional_usd` 与链上实际金额一致。成交价是本程序自己的报价，不是领投的成交价，因此模拟结果与"如果当时跟了会怎样"不是同一回事。
