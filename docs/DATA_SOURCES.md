# 数据从哪里来

两条管线，数据源和成本结构完全不同。

| 管线 | 用途 | 数据源 | 更新频率 | 实测状态 |
|---|---|---|---|---|
| 历史账本 | 钱包排名 | Helius `getTransactionsForAddress` | 6 小时 | 逻辑已测，RPC 未实测（需 Key） |
| SOL 计价 | 成交时美元对价 | Binance Vision 月度 K 线归档 | 按需 | **已实测通过** |
| 未平仓估值 | `marks` | Jupiter 报价 | 每次生成账本 | **已实测通过** |
| 实时成交流 | 跟单 | Helius 轮询或 Webhook | 30 秒 ~ 推送 | 逻辑已测，RPC 未实测 |

## 为什么用余额增减而不是 SWAP 事件

适配器不解析各家 DEX 的 swap 事件，而是从 `preTokenBalances`/`postTokenBalances` 算这个钱包自己的净变化。原因：

- 路由聚合器（Jupiter、OKX、各种 bot）会把一笔交易拆成多跳，解析事件容易漏腿或重复计。余额增减是最终结果，不受路由方式影响。
- 新协议不需要等解析器支持。
- **漏数据会变成显式拒绝而不是静默少算一笔。** 这对排名是必须的——少算一笔亏损就会让钱包看起来更好。

代价是：不是干净两边互换的交易一律标为无法还原，整个钱包不进榜。这是有意的，见下面"哪些钱包会被拒"。

## 一、历史账本：Helius

`getTransactionsForAddress` 是 Helius 自有方法，一次调用同时拿到签名和完整交易，并且**会带上钱包关联 token account 的活动**——这点很关键，标准 `getSignaturesForAddress` 不会返回 ATA 活动，用它会漏掉大部分交易。

旧的 Enhanced Transactions API（`/v0/addresses/{address}/transactions`）官方已标为 legacy maintenance mode，不要在新项目上用。

```bash
export HELIUS_API_KEY=<你的 key>
python -m adapters.ledger --address <钱包公钥> --out wallet_ledgers
```

适配器调用参数：`transactionDetails: full`、`filters.tokenAccounts: balanceChanged`、`status: any`（失败交易要算 gas）、`sortOrder: asc`（中断后得到的是连续前缀而不是中间断档）、`paginationToken` 翻页。

成功写出 `wallet_ledgers/<公钥>.json`；失败打印原因、**不写文件**，因为一份成本基准不完整的账本会直接污染排名。

### 每种链上行为怎么处理

一个活跃 agent 钱包会收空投、把仓位转到自己另一个地址、直接换币、一笔交易清掉好几个仓。这些都是正常操作，所以不再一律拒绝，而是分别处理——关键是**既不拒绝它，也不把无法证明的东西算成交易能力**。

| 情况 | 处理 | 理由 |
|---|---|---|
| USDC/SOL 充值提现 | **跳过** | 现金进出不进入代币成本会计 |
| 创建 ATA 扣的 SOL 租金 | 计入成本 | 每条计价腿各自折美元再相加 |
| 失败交易 | 记为独立 gas 费用 | 抢跑失败也是策略成本 |
| 代币转入 / 空投 | 零成本入账，**标记为外部来源** | 卖出后的盈亏进 `external_origin_pnl_usd`，不计入收益率、胜率和完整周期 |
| 代币换代币（BONK→WIF） | **成本结转**，不实现盈亏 | 不需要价格源。总盈亏在最终卖出时分毫不差，只是少一个完整周期 |
| 转出到其它地址 | 按成本移出，**不算胜也不算负**，累计记录缺口 | 去向不可观测。`censored_cost_fraction` 超过 `max_censored_cost_fraction`（默认 25%）才拒绝排名 |
| 一笔交易卖出多个代币 | 按成本基准比例分摊收入，**打标记** | 总额对，但每个代币的分布是估算的，而集中度指标依赖分布 |
| 一笔交易买入多个代币 | **仍然拒绝** | 成本在几个买入腿之间没有可辩护的分摊依据 |
| 历史不足 30 天 | 拒绝 | 低于 `wallets.min_history_days` |
| 历史 30~90 天 | **接受但降权** | `confidence` 里的活跃周数项会自动压低分数，不需要再加硬门槛 |

成本结转可以验算：买 A 花 500，换成 B，最后卖 900 → 总盈亏 400；用真实中间价拆成两段（假设换的时候 A 值 700）→ 200+200 = 400。**完全一致**，只是周期数从 2 变 1，`confidence` 略降，方向偏保守。

剩下的拒绝理由只有"一笔交易买入多个代币"和历史太短两条，比之前窄得多。真实拒绝率用 `--diagnose` 自己量。

### 成本

Helius 免费档 100 万 credits/月、10 请求/秒。`getTransactionsForAddress` 官方口径在"10 credits 起、按返回结果计量"和"每次 100 credits"之间有出入，按上限估算：

- 一个 90 天内有 2,000 笔交易的钱包，每页 500 条 → 约 4 次调用 → 400~4,000 credits
- 50 个候选全量回填 → 2 万~20 万 credits，免费档够用
- 但**很老的钱包要从第一笔交易开始拉**（`initial_inventory_empty` 的要求），几万笔交易的钱包单个就可能上万 credits

先拿 3~5 个钱包试，看实际 `rpc_calls` 输出再放量。

## 二、SOL 美元价：Binance 公开归档

**不需要任何 Key，已实测。** 研究代币本身从不定价——代币的美元价值一律由实际付出的计价资产推导，这样一个流动性极差或被操纵的 memecoin 预言机价格无法抬高钱包成绩。只有 SOL 需要真实价格序列。

用 `https://data.binance.vision` 的 SOLUSDT 月度 1 分钟 K 线归档，每个文件都有官方 SHA256，适配器会校验后缓存在本地。实测加载 2026-06 全月得到 43,200 个分钟收盘价。

局限：成交按**落块那一分钟的收盘价**定价，不是真实成交价。对 90 天排名够用，对单笔精确损益不够。USDC/USDT 一律按 $1，不建模脱锚。

## 三、未平仓估值：Jupiter

**不需要 Key，已实测**（BONK 100 万 → $2.68，WIF 100 → $18.03）。

用路由报价而不是预言机价格，因为排名关心的是这个仓位**实际能卖出多少**，而不是名义市值。报不出路由的代币按 0 计价但**保留数量**——删掉卖不掉的烂仓等于抹掉亏损，正是排名要抓的行为。

## 四、实时成交流：轮询 vs Webhook

这是成本差异最大的地方，**相差约 100 倍**。

| 方式 | 计费 | 3 个领投的月消耗 | 延迟 | 需要公网地址 |
|---|---|---|---|---|
| 轮询 30 秒 | 每次调用 10~100 credits | 约 260 万 credits，**超免费档** | 0~30 秒 + 出块 | 否 |
| 轮询 120 秒 | 同上 | 约 65 万 credits，勉强在免费档内 | 0~120 秒 | 否 |
| **Webhook** | **每次推送 1 credit** | 约 9 千 credits | 秒级 | **是** |

Webhook 是明显更优解：每天 100 笔成交也才 300 credits。代价是 Helius 需要能推到你的地址。

```bash
# 轮询：领投名单直接从 bot 的 state.sqlite3 读，适配器自己不做排名
python -m adapters.activity poll --data-dir data --out wallet_activity --watch --interval 30

# Webhook：在它前面放 HTTPS 和共享密钥
export WALLET_WEBHOOK_SECRET=<随机字符串>
python -m adapters.activity webhook --out wallet_activity --port 8788
```

两种模式都写 `wallet_activity/<公钥>.json`，对应 `follow.source: local`。写入用临时文件加原子替换，bot 不会读到写了一半的文件。

Webhook 模式需要在 Helius Dashboard 建一个 webhook，地址填你的公网入口，监听领投地址。注意**领投名单会随排名变化**，换人时要同步更新 webhook 的地址列表——本版没有自动同步这一步。

更低延迟的还有 LaserStream（Yellowstone gRPC），但从 $499/月的 Business 档起，对跟单这个量级不必要。

### 延迟决定跟单成败

`follow.max_signal_age_s` 默认 120 秒。轮询 120 秒会让实际延迟刚好顶到闸门上限，大量信号会被丢弃或踩线成交。**如果要认真跑跟单，用 webhook。** 报告里的延迟中位数如果接近闸门值，说明数据源太慢，结果不可信。

## 实测到什么程度

已用真实网络验证：Binance 价格序列、Jupiter 估值。已用 18 项离线测试验证：余额增减还原、租金/失败交易/转账/代币互换的分类、同秒执行顺序、以及**适配器产出的账本能被 bot 的 `analyze_ledger` 接受并算出正确的完整平仓数与已实现盈亏**。

**没有实测**：Helius RPC 调用和 webhook 推送的真实返回体（需要账号和 Key）。这两处按官方文档的字段名实现，第一次接真实 Key 时请先用单个钱包跑 `python -m adapters.ledger`，核对输出的 `transactions` 条数和 `rpc_calls`，再放量。

本项目没有注册任何账号、没有购买套餐、没有调用任何付费接口。
