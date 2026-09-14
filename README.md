# TwinCryptoBots 1.1.1

两个 bot 和一个独立数据获取器：**meme 候选扫描 + Binance/DEX 模拟交易**。不使用大语言模型、不需要 OpenAI Key，不签署交易、不广播、不真实下单。默认 Solana 扫描、Binance USDT 现货 paper，虚拟起始资金 $100。DEX paper 需补齐接口后启用。

这是一套可运行的研究与模拟系统，不是已证实盈利的策略。$100 到 $100,000 是 1,000 倍，程序没有收益承诺，也没有内置“百倍成功率”。

审查修订：见 [审查与修复记录](docs/AUDIT_2026-09-14.md) 和 [逐项 API 获取指引](docs/API_SETUP_ZH.md)。旧模型需重新训练；旧配置请手动合并低用量字段。

## 最快启动

安装 Python 3.11 或 3.12，解压后进入本目录。Windows 双击 `start_windows.cmd`，首次会创建虚拟环境、安装依赖、复制配置并启动。Linux/macOS 执行 `bash start.sh`。运行机器需要保持联网和唤醒；关机后 bot 不运行。

也可以手动执行：

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m twobots init
python -m twobots doctor --online
python -m twobots run
```

`run` 首次启动会下载最近 12 个完整月份、4 个币种的 Binance 4h K 线，校验官方 SHA256，再补近期 K 线并训练轻量模型。初次下载可能需要几分钟；网络受限时记录缺失、继续启动能连接的服务，不把断线视为有效行情。重启利用缓存。配置文件中 `bootstrap_on_start: false` 可关闭启动下载，先用独立 `fetch` 获取数据。

**不填任何密钥也可以运行：**公开历史下载、GeckoTerminal 扫描、规则评分、Binance 公共订单簿 paper、离线 demo。是否能连接取决于当地网络及数据源访问政策。行情连接成功也可能没有符合条件的交易；模型无样本或未超过留出集基线时，默认 CEX 会保持现金。

## 需要填写的位置

| 功能 | `.env` | `config.yaml` |
|---|---|---|
| Binance 公共行情 paper | 不需要 Key | `cex.rest_base`、`cex.ws_base` 默认官方域名 |
| 读取实际账户费率，可选 | `BINANCE_API_KEY`、`BINANCE_API_SECRET` | `cex.fetch_account_fees: true`；仅调用只读账户费率接口 |
| Telegram 通知自己 | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | `telegram.enabled: true` |
| Solana DEX 报价 paper | `JUPITER_API_KEY` | `dex.enabled: true`、`dex.quote_url` |
| 链上安全/持仓/资金簇数据 | `FEATURE_API_KEY`（若服务需要） | `scanner.feature_url_template`，规范见 `docs/DATA.md` |
| 远程训练数据，可选 | `DATA_API_KEY`（若服务需要） | `history.scanner_dataset_url`，建议同时填 SHA256 |
| 其它链/其它报价商 | `DEX_QUOTE_API_KEY` | `dex.provider: generic`、`dex.generic_quote_url`、`dex.quote_api_key_env: DEX_QUOTE_API_KEY`、报价币及网络 |

`CHAIN_RPC_URL` 仅是预留字段，当前实现不调用 RPC。普通 Binance 交易 API 无法替代链上历史、持仓、安全检查和 Jupiter 报价接口。**不要填写钱包私钥：程序不需要也不接受钱包签名。**

默认 Jupiter 使用配置化的 Swap V1 只读 quote 路径。官方已标记该接口为旧版；若你的账户仅提供新版 API，需使用 `generic` 适配器转换新版响应，不能只改 URL 而保留不同请求格式。接口规范和一个可运行的本地适配器示例已附上。

## 三个组成部分

### 1. 扫描 bot

每 10 分钟从 GeckoTerminal 的指定网络新池列表发现候选，轮换分析最多 2 个池，避免每次都选最热的赢家。默认只能覆盖提供商当前返回的近期池，**不是全链新币发现器，也不是抢首个区块的 sniper**。同一 token 多个池的 Telegram 通知会去重。

12 项特征总权重 100：流动性、池龄、小时换手、双向成交、短期动量、量能、回撤、可卖出检查、增发/冻结权限、前十持仓、资金簇、创建者历史。每项显示符合/不符合/未知、原始证据和数据来源；未知不得当作通过。权重是可审计的初始假设，不是从百倍币中证明的规律，阈值可在 `scanner.py:assess` 调整。

额外尝试训练 2 倍/24h、10 倍/7 天、100 倍/30 天的分类模型；预测目标是**未来完整窗口内某根 5m K 线收盘价相对当前收盘价的倍数**，不是买入后可兑现回报。样本不足、时间分割失败、模型过期或留出集不如基线时，显示无法估计。合格模型提供校准后的研究概率、验证样本量及特征关联。特征数/100 分绝不转换成成功概率。

外部历史数据具备足够覆盖时，模型也会使用流动性、交易结构、持仓、资金簇等历史特征；没有这些字段时只训练 8 项价格/成交量特征，不用今天的持仓数据回填过去。无 LLM。

### 2. 交易 bot

**Binance：**实时 WebSocket 增量深度 + REST 快照，校验更新序列。使用当前可见深度的保守比例模拟限价 IOC，支持未成交、部分成交后撤销、延迟期间价格移走、过期数据拒绝成交、费用、数量步长和名义金额过滤、未知回执和重启恢复。不会用历史 K 线制造“真实回测成交”。只做现货，多头或现金。

策略按行情切换：趋势中寻找收缩后突破或回调恢复，震荡/压力期不新开仓，已持仓按行情退出；校准逻辑回归模型决定是否放行信号。仓位按 ATR 距离和费用预算缩放，初始单笔风险预算为净值 0.5%，单仓上限 25%，最多 2 仓。止损和跟踪止损在实时行情上检查，但价格跳空、深度不足仍可能导致更大损失。净值回撤 10% 后持久暂停新仓，继续处理退出，不自动恢复追涨。

**DEX：**候选评分 + 新鲜安全检查 + 动量/波动状态筛选；先查买入和反向卖出报价过滤往返成本，再等待设定延迟、重新报价。若输出低于最初最低接受量，模拟整笔回滚，仅扣设定 gas；未上链丢弃不扣 gas；取不到到达报价则记录不可验证、不虚构成交。成功 swap 是整笔原子成交，不伪造部分成交。止损、翻倍后追踪退出、最长持有 24h 都基于反向报价。无法卖出的持仓不会凭空消失，超时无报价按 0 估值并保留库存。

DEX 当前默认 $2/笔，最多 2 笔，和 CEX 各自有独立 $100 虚拟账户，用于对照实验；不是一份 $100 同时投入两处。若要模拟总共 $100，请在第一次运行前自行分配两边 `initial_cash`。已有账户不随配置改本金，重新实验请使用新的 `data_dir`。

### 3. 独立数据获取器

```bash
python -m twobots fetch
python -m twobots fetch --watch
python -m twobots fetch --pool-pages 12
python -m twobots train --target all
```

`fetch --watch` 每小时运行下载/发现/历史跟踪；训练单独执行或由 `run` 的每日维护触发。`--pool-pages` 是每个被抽样跟踪池的回溯页数，不会遍历全链；多页仅在该池达到刷新间隔时使用。默认近期池按地址哈希抽样，最多同时跟踪 20 个、跟踪至池龄 35 天，每 12 小时请求历史。抽样独立于评分及后续涨幅，但仍受提供商漏收录、容量和历史可用性影响。

下载官方 ZIP 和 API 聚合 K 线，不从创世区块拉交易，不落盘原始订单簿。日 HTTP 请求预算默认 1,000（并非供应商的 CU/加权请求计价），限速、缓存和重试有上限。多个本地进程共享 SQLite 预算。默认维护小规模数据即可；大量池/完整资金簇数据会需要外部索引器，不能靠这个预算免费完整重建。

## 常用命令

```bash
python -m twobots demo                       # 全离线，合成场景，结果不是策略收益
python -m unittest discover -s tests -v      # 软件行为测试
python -m twobots scan --once                # 扫描一轮并按配置发通知
python -m twobots scan                       # 持续扫描
python -m twobots trade --venue cex          # 独立运行 CEX paper
python -m twobots trade --venue dex          # DEX paper，需先启用和配置
python -m twobots run                        # 扫描 + 已启用 venue + 自动维护
python -m twobots report                     # 生成 data/reports/latest.html 和 JSON
python -m twobots --config another.yaml run  # 使用另一组实验配置
```

不要同时运行同一数据目录的 `run` 和 `scan`/相同 venue 的 `trade`，进程锁会阻止重复引擎。独立 `fetch` 可以共享数据目录；若与维护刚好冲突会提示锁占用，稍后重试即可。退出按 Ctrl+C。系统重启后需要重新启动脚本；无人值守部署见 `docs/OPERATIONS.md`。

## 交付边界

核心策略、数据源客户端、纸上账本、模型训练、Telegram 发信、报价适配器、异常处理、报告和启动脚本均提供源码。外部服务的账户/套餐/密钥、完整失败币历史库、付费持仓/资金簇索引器由你后续选择并填写；不是包内已经存在的服务。

免费公开历史接口足以启动 Binance 实验，却不能可靠给出一个完整的“所有新币，包括归零/不可卖者”的训练集。没有这样的数据时，100 倍模型可能长期不产生概率，这比只用成功案例训练后报高信心更符合实际。已有研究数据也有授权、抽样与标签差异，不能当作已验证的交易 edge。详见 `docs/DATA.md`、`docs/STRATEGY.md`、`docs/EXECUTION.md`、`docs/SOURCES.md` 和 `docs/VALIDATION.md`。
