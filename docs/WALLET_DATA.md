# 钱包数据接入与运行（1.2.0）

## 已实现范围

`scanner.kind: wallets` 是新默认。扫描器从 Solana 近期池成交发送地址发现候选，也接受 `wallets.addresses` 名单和本地账本文件。只有成本、成交、费用、库存和覆盖范围通过校验的完整账本才进入评分。

账本可以自己准备，也可以用内置适配器从链上还原：`python -m adapters.ledger --address <公钥> --out wallet_ledgers`，数据源与成本见 [DATA_SOURCES.md](DATA_SOURCES.md)。未提供账本时可发现地址，但输出 `unavailable`，不会产生真实排行榜。不能将供应商 PnL 汇总 URL 直接填入 `wallets.url_template`。

钱包排名不训练模型，不使用 LLM，不签名、不广播。CEX 原模拟策略独立保留。钱包模式要求 `dex.enabled: false`（那是旧的按代币评分的 DEX paper，可通过明确设置 `scanner.kind: tokens` 使用）；跟单走 `follow.enabled`，需要另一份近实时成交流，见 [COPY_TRADING.md](COPY_TRADING.md)。

## 零 API 验证与本地运行

在仓库根目录执行 `python -m examples.wallet_demo`。它生成四份**合成**账本及 `wallet_demo_output/reports/latest.html`，测试单币暴赚、低胜率盈利、高胜率亏损、金额放大但成绩不变。没有网络请求，这些数字不是回测收益。

真实账本的运行步骤：

1. 执行 `python -m twobots init`。旧 `config.yaml` 不会被覆盖；合并示例中 `wallets` 整段，设置 `scanner.kind: wallets`。遗漏 `kind` 的旧配置也默认切换到钱包模式；若旧配置启用了 DEX，启动会要求关闭。
2. 只在现有配置相应位置修改：`wallets.source: local`、`wallets.discover_enabled: false`、`wallets.ledger_dir: wallet_ledgers`、`dex.enabled: false`。
3. 将每个完整账本存为 `wallet_ledgers/<Solana 公钥>.json`。路径相对配置文件目录；文件名必须与内容地址一致。程序自动收集有效文件名，也可配置地址名单。该目录已加入 Git 忽略。
4. 执行 `python -m twobots scan --once`，再执行 `python -m twobots report`。独立钱包 `scan` 不下载 Binance 历史、不跟踪池 K 线、不训练百倍币模型。
5. 默认每轮最多分析两个钱包，重复执行会轮换。本地重排不消耗 API；大量文件时可提高 `max_wallets_per_run`，总候选数由 `max_candidates` 控制。

## 候选地址发现：GeckoTerminal

开启 `wallets.discover_enabled: true`，保留 `scanner.gecko_base: https://api.geckoterminal.com/api/v2`。无需 Key、无需连接钱包。使用新池列表，然后读取 `/networks/solana/pools/{pool}/trades` 中的发送地址。[官方端点说明](https://api.geckoterminal.com/docs/index.html)。

默认每六小时一轮，一页新池、最多两个池的成交、每池十个不同地址；最多 50 个候选。正常情况下发现部分约 12 次 HTTP/天，失败重试另计。主机请求间隔为 6.5 秒。官方不同页面曾给出不同公共速率上限，遇 429 按返回情况退避，不能把本地预算视为供应商承诺。

发送者可能是路由、托管或关联地址；本版没有自动核验实际受益人。近期池样本会漏掉不交易新币的优秀钱包，应同时提供地址名单。

## 完整账本格式

可运行完整样例由 `python -m examples.wallet_demo` 生成。必填字段如下：

| 层级 | 字段与类型 | 含义 |
|---|---|---|
| 根对象 | `schema_version: 1`, `currency: USD`, `network: solana`, `address`, `source` | 格式、币种、钱包公钥、来源及处理版本 |
| 根对象 | `history_start`, `asof` | Unix 秒；历史从空交易库存起，至少覆盖扫描前 90 天及更早的成本 |
| `quality` 对象 | `complete`, `initial_inventory_empty`, `all_protocols`, `fees_included`, `transfers_included`, `failed_transactions_included` | 必须为真实布尔值；所有项确实为 true 才允许计算 |
| `transactions` 列表 | 每条含 `id`, `ts`, `side`, `token`, `quantity`, `notional_usd`, `fee_usd` | `side` 为 buy/sell；数量、USD 金额可用十进制字符串 |
| `marks` 列表 | 每条含 `token`, `quantity`, `value_usd`, `asof` | 所有未平仓资产的数量和 USD 估值；空仓显式给 `[]` |

事务 ID 应为交易签名与资产腿序号组合，不能重复使用多资产交易共享的签名。记录按真实执行顺序排列，同秒也不能颠倒。全部分页完成后才可声明 `complete: true`，最多 50,000 条、默认文件上限 10 MB；达到上限应输出缺失，不能静默截断。

`asof` 默认不得落后六小时，库存估值不得落后账本一小时。不能在第 90 天直接截断早期买入。`notional_usd` 是成交时 USD 对价，不能用今天价格回填；`fee_usd` 是该资产腿独占的全部成本。已体现在实际数量/净额中的池费用不能重复扣；同笔 gas/优先费只能分摊一次。交换两种研究资产通常应产生一卖一买。计价现金进出不算交易利润，不能伪造为低成本买入。

买入成本加费用，卖出按当时移动加权成本匹配，扣卖出费用。`side: fee` 配合 `id/ts/fee_usd` 记录失败交易 gas 等独立费用。完整清仓才结束持仓周期；部分卖出不增加胜场。跨窗口周期保留窗口内已实现盈亏，但不计入该窗口的完整周期胜率。

本版不支持转入/转出交易资产、空投、借贷、LP 和衍生品会计。发现此类活动、卖出超过已知库存、未知成本或遗漏协议时该钱包不排名。**不要为通过校验把转账改为零成本买入，或强行把质量声明改为 true。** 覆盖声明会出现在报告里，程序无法从一份 JSON 独立证明没有漏数据。

未平仓 token 必须逐项提供数量吻合的估值；无法估值应输出缺失。确认不可兑现的资产可以保守记零并保留数量，不能删除亏损库存。浮盈不能抵消未平仓亏损的排名扣分项。

## 只读适配器与供应商选择

已有完整账本服务时，将 `wallets.source` 改为 `adapter`，填该服务真实 HTTPS GET 地址，如 `https://<你的服务>/wallet-ledger?network={network}&address={address}`。它必须返回上面的单个完整 JSON，本程序不会自动跟随 `next` 分页。配置 `wallets.header` / `header_prefix` 匹配认证，在本机 `.env` 的 `WALLET_DATA_API_KEY` 填该**适配器**密钥。拒绝 HTTP 重定向及非 HTTPS（本机测试除外），不请求钱包签名。

尚无服务时，先确认供应商能导出：完整历史及失败交易、所需全部协议、关联 token account、成交时 USD 价格、费用、转移和库存。只有总收益和胜率的产品不满足本版输入格式。

- **Birdeye**：从官方数据 API 页面进入 Dashboard，检查账号 Wallet PnL 接口权限，再创建数据 API Key。官方教程使用 `POST https://public-api.birdeye.so/wallet/v2/pnl/details`，认证为 `X-API-KEY` 和 `x-chain: solana`，响应按 token 汇总且需要分页。它不能直接证明完整平仓周期、费用和成本覆盖，需补齐历史并转换。WAC 与 net_cash 口径不同，部分协议历史不回填。[官方钱包 PnL 教程](https://birdeye.so/data-api/blog/detail/wallet-pnl-tracker-solana-birdeye-data)、[成本口径说明](https://docs.birdeye.so/reference/get-wallet-v2-pnl)。
- **Helius**：在官方 Dashboard 创建项目并取得数据 API Key。Enhanced Transaction History 需关注关联 token account，并按游标完整分页；还需转换资产腿、匹配成本、补齐成交时 USD 价格和费用。过滤 SWAP 后的空页不一定表示历史终点。全历史回溯消耗 credits/CU，先按钱包交易量估算；不要为本版直接购买大额套餐。[官方历史接口说明](https://www.helius.dev/docs/enhanced-transactions/transaction-history)。

这些账号尚未在项目实测；未创建账号、购买套餐或调用付费接口。供应商 Key 不能自动把原始响应变成本程序账本。

## 缓存与费用上限

适配器默认六小时刷新一次，每轮最多两个钱包；失败也记录尝试时间，避免密集重试。50 个稳定候选正常约 200 次适配器读取/天，候选变动与调度会改变数量。所有 HTTP 含重试仍受共享日预算 1,000 次约束。这个数是请求次数，**不是 CU**，适配器背后的历史重建可能另行计费。

失效或失败的新响应移除旧排名；过期缓存也不排序。读取本地账本、报告、测试、修改排名公式均在本地完成。账本缓存在本地 SQLite，长期运行需按数据保留需求备份及管理；不要提交含密钥的配置或私有研究账本。
