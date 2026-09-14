# API 获取与配置指引（审查修订版）

适用：TwinCryptoBots 1.1.1，基于 GitHub 提交 `b9a740ca6dfe9d96ec60eeb400f61d54a83dcb47` 的 1.1.0 导入。官方文档核对日期：2026-09-14。仓库仅有示例配置，没有已填好的供应商账号或真实密钥；下文对应实际代码字段，不代表已验证你的供应商连接。

## 先使用不需要密钥的配置

在项目目录执行 `python -m twobots init`，仅在本机编辑生成的 `config.yaml` 和 `.env`。两者已被 Git 忽略。保持：

```yaml
mode: paper
# 以下字段分属 config.yaml 已有的对应章节，不要重复创建同名章节。
cex:
  enabled: true
  fetch_account_fees: false
dex:
  enabled: false
telegram:
  enabled: false
```

这是现有配置的局部说明，不能用它覆盖完整配置文件。程序没有实盘订单、钱包签名或广播实现；不需要 OpenAI、Claude、钱包私钥或助记词。逻辑回归在本地训练，不是大语言模型。

| 数据/功能 | 获取位置 | 本地字段 | 是否必须 |
|---|---|---|---|
| Binance 历史与实时公共行情 | 官方公开服务 | `history.archive_base`、`cex.rest_base`、`cex.ws_base` | CEX paper 必须可访问，免 Key |
| GeckoTerminal 新池/OHLCV | 公共 API | `scanner.gecko_base` | 扫描必须可访问，免 Key |
| 账户实际手续费 | Binance API 管理 | `BINANCE_API_KEY`、`BINANCE_API_SECRET` | 可选 |
| Jupiter 只读报价 | Jupiter Portal | `JUPITER_API_KEY`、`dex.quote_url` | 启用此 DEX provider 时使用 |
| 安全、持仓、资金簇数据 | 你选定的索引/安全数据供应商 | `FEATURE_API_KEY`、`scanner.feature_url_template` | 默认 DEX 入场必须有合格证据 |
| 历史研究 CSV | 你拥有使用权的数据源 | `DATA_API_KEY`、`history.scanner_dataset_url` | 可选 |
| Telegram 自己的通知 | BotFather | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | 可选 |
| 其它链报价 | 自定义只读适配器 | `DEX_QUOTE_API_KEY`、`dex.generic_quote_url` | 可选 |

## Binance 公共数据与可选只读费率

默认保留 `https://api.binance.com`、`wss://stream.binance.com:9443` 和 `https://data.binance.vision`。程序下载月度 K 线与校验文件，使用公共 exchangeInfo、klines、depth、avgPrice，订阅增量深度；交易模拟在本机账本完成。[官方市场数据与 WebSocket 文档](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/ws-streams/~)。

不需要为了运行 paper 开通交易权限或创建交易所测试网账户。`doctor --online` 仅测试 Binance `/time` 与 GeckoTerminal 新池 HTTP，不验证 WebSocket 同步、实际报价、安全供应商或长期稳定性。当地访问失败应按该服务的可用范围处理；不要把密钥发给“行情代理”。

确实需要读取自己的手续费时：

1. 登录 Binance 官网，进入账户的 API 管理并创建 API。
2. 当前代码使用 HMAC-SHA256；选择系统生成的 HMAC Key。仅支持 RSA/Ed25519 的账号不能直接把其私钥填入 `BINANCE_API_SECRET`。
3. 保留读取权限，关闭现货/合约交易、提币和转账权限；可限制为运行电脑的固定出口 IP。
4. 在本机 `.env` 填入 `BINANCE_API_KEY` 和 `BINANCE_API_SECRET`，将 `cex.fetch_account_fees` 改为 `true`。
5. 本实现仅对官方 `api.binance.com`、`api1` 至 `api4.binance.com` 发送签名的 `GET /api/v3/account/commission`。这是账户请求签名，不是交易签名。

程序使用 taker 与 buyer/seller，加上返回的 tax/special commission；不假设 BNB 折扣。认证失败或畸形费率不能自动当成零费用。若不需要账户费率，恢复 `fetch_account_fees: false`，明确使用每边 `fee_rate: 0.001` 的实验假设。[创建 Key](https://www.binance.com/en/support/faq/detail/360002502072)，[账户费率接口](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account)。

## GeckoTerminal

无需注册 Key，保留 `scanner.gecko_base: https://api.geckoterminal.com/api/v2` 与 `network: solana`。官方公共接口当前为每分钟 30 次；程序默认对该主机至少间隔 3 秒。遇到 429 应减频，不应提高重试次数。付费 CoinGecko 接口不应仅替换域名：认证、路径与套餐需要按供应商文档单独适配。[入门](https://apiguide.geckoterminal.com/getting-started)，[限速说明](https://apiguide.geckoterminal.com/faq)。

它提供近期池与 OHLCV，不提供本项目全部安全/资金簇证据，也不保证覆盖已消失的失败币。

## Jupiter 只读报价

1. 打开 [Jupiter Portal](https://portal.jup.ag/)，登录并生成 API Key。先使用免费额度，勿为本项目提前购买套餐。
2. 在 `.env` 填 `JUPITER_API_KEY=你的密钥`。程序通过 `x-api-key` 请求头发送；`quote_header_prefix` 保持空字符串。
3. 当前实现对应 `dex.provider: jupiter` 与 `https://api.jup.ag/swap/v1/quote`，使用 `inputMint/outputMint/amount/slippageBps/swapMode=ExactIn`。
4. 保留 Solana USDC 地址 `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`、`quote_decimals: 6`。默认 $2 对应 2,000,000 原始单位，`quote_usd: 1.0` 是未模拟脱锚的假设。
5. 安全接口尚未具备下节证据时，保持 `dex.enabled: false`。有报价不等于可以卖出。

官方文档目前明确 V1 Metis 已停止积极维护、由 Swap V2 取代。本次保留已有只读 V1 协议，没有猜测新版字段；若账号无法使用 V1，需开发/验证 `generic` 适配器，不能只改 URL。禁止接入 `/execute` 或发送交易端点。[V1 报价格式](https://developers.jup.ag/docs/swap/v1/get-quote)。

Portal 当前说明无 Key 为 0.5 RPS、免费 Key 为 1 RPS，修订配置统一至少间隔 2 秒；新 Key 激活可能需要 2–5 分钟。套餐和计费以账号控制台为准。[Portal 设置](https://developers.jup.ag/docs/portal/setup)。

## 安全、持仓、资金簇接口

仓库没有指定真实供应商，`FEATURE_API_KEY` 本身不是某一家的通用密钥。应先向供应商确认网络覆盖、实际返回字段、证据时间、用量计费及数据许可，再获取该供应商控制台的只读 Key。

填 `scanner.feature_url_template`（可代入 `{network}`、`{token}`、`{pool}`）；默认从 `.env` 的 `FEATURE_API_KEY` 生成 `Authorization: Bearer ...`。如果供应商要求 `x-api-key`，修改 `feature_header: x-api-key` 和 `feature_header_prefix: ""`。

响应规范见 [DATA.md](DATA.md)。必须匹配 network/token/pool，`asof` 是实际证据时点，默认 300 秒内；缺失字段省略，不能填写假的 `true` 或用请求完成时间刷新旧证据。

修订版 DEX 默认要求全部满足：

| 字段 | 通过条件 | 不能代替它的证据 |
|---|---|---|
| `can_sell` | JSON 布尔 `true` | 报价存在、网页“低风险”标签 |
| `mint_revoked`、`freeze_revoked` | 均为 JSON 布尔 `true` | 不核实 mint 权限的普通余额查询 |
| `top10_ex_lp_fraction` | 有效比例且小于 0.35 | 未剔除池/托管地址的原始 top10 |
| `largest_funding_cluster_fraction` | 有效比例且小于 0.20 | 钱包数量或简单地址去重 |
| `creator_bad_rate` | 有效比例且小于 0.20 | 未覆盖历史的“没有发现坏币” |

安全字段齐备也不能证明不会 rug 或必定能卖出。Solana 代币还可能有 Token-2022 转账钩子、永久委托等机制；供应商需要说明检查方法及未覆盖风险。[权限基础](https://solana.com/docs/tokens/basics)，[永久委托](https://solana.com/docs/tokens/extensions/permanent-delegate)。本程序没有实现完整链上分叉执行模拟。

已有 GET JSON 接口只需改字段名时，可复制 `adapter.example.json` 为本机 `adapter.local.json`，填 `features.upstream_url` 与映射，设置进程环境变量 `FEATURE_UPSTREAM_KEY`，执行 `python scripts/adapter_server.py --config adapter.local.json`。适配器不自动加载 `.env`，主程序才会加载；它默认只监听 `127.0.0.1:8787`。主程序可使用 `http://127.0.0.1:8787/features?network={network}&token={token}&pool={pool}`。

映射器不会推导资金簇、转换百分数、合并供应商或制造安全结论。缺失证据时 DEX 不开仓；复杂接口必须另写规范化服务。HTTP 客户端和适配器现在都拒绝重定向，请填写最终可信 URL。

## 历史 CSV 和其它链

`history.scanner_dataset_url`、`pool_catalog_url` 均为可选；默认 `DATA_API_KEY` 使用 Bearer 头。训练 CSV 要有 `network,token,asof,label_known_at,target,label` 和已定义的特征；按 [DATA.md](DATA.md) 保留完整未来窗口、失败币与缺失标签的区分。建议填写供应商通过可信渠道给出的 `scanner_dataset_sha256`，它验证字节完整性，不证明样本无偏。

`dex.provider: generic` 请求 `generic_quote_url`，`quote_api_key_env` 设为 `DEX_QUOTE_API_KEY`，同时配置实际 `quote_header/quote_header_prefix`。返回的是整数输入/输出、最小接受量、比例形式的价格冲击和证据时间。跨链还必须同步修改 `scanner.network`、报价币地址、精度、gas、延迟。`.env` 中的 `CHAIN_RPC_URL` 当前没有调用代码，填写它不会补齐安全分析，也不会启动 RPC 采集。

## Telegram（可选）

通过 Telegram 官方 [BotFather](https://t.me/BotFather) 创建自己的 bot，保存 token，然后向自己的 bot 发送 `/start`。在本机调用 Telegram `getUpdates`，取这条消息的 `message.chat.id`；仅使用自己控制的私聊 ID。将 token 与 ID 分别填到 `.env`，最后设 `telegram.enabled: true`。Token 会出现在 Telegram API URL 中，勿发到聊天或提交仓库。[官方教程](https://core.telegram.org/bots/tutorial)。本次未创建 bot、未发送通知。

## 用量与检查顺序

修订默认：每 600 秒扫描，最多 2 个候选；20 个 cohort、每 12 小时刷新；HTTP 每日 1,000 次、最多重试一次。正常扫描约 `144 × (1 + 2) = 432` 请求/天，比原 1,152 下降约 62.5%；配置安全接口再约 288，cohort 最多约 40，另加 CEX/初始下载/重试。首次 12 月 × 4 币 ZIP 与校验约 96 请求，再补近期数据。以上是估算，不是供应商 CU。

若启用 DEX，两仓每 300 秒反查理论上再约 576 次/天，实际循环 120 秒会影响触发时刻；1,000 的全局预算可能不够。先明确报价额度，可将 `dex.mark_every_s` 设 600 并把 HTTP 预算设 1,500，代价是退出检查更慢。达到预算会停止 HTTP，包括报价退出；故不能仅降低总预算却忽略已持仓退出所需请求。WebSocket、适配器上游计费和 RPC CU 不包含在 HTTP 次数里。

顺序执行：

```text
python -m twobots init
python -m twobots doctor
python -m unittest discover -s tests -v
python -m twobots doctor --online
python -m twobots fetch
python -m twobots train --target cex
python -m twobots run
python -m twobots report
```

前 3 步可以离线；后续数据命令才消耗外部额度。`demo` 是合成行为测试，不是收益验证。旧模型缺少新 context 字段时会拒绝使用，需重新训练。旧 `config.yaml` 不会自动变成新的低用量配置，需手动合并所需字段；新实验使用独立 `data_dir`，避免混合策略、虚拟本金和历史收益。
