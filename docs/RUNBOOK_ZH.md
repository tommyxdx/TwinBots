# 从零跑通模拟跟单

全程 paper 模式：不签名、不广播、不下真实单，不需要也不接受任何钱包私钥。

## 一、要用到哪些 API

| 用途 | 服务 | 要钱吗 | Key 填哪里 |
|---|---|---|---|
| 钱包历史 + 实时成交 | **Helius** | 免费档 100 万 credits/月 | `.env` 的 `HELIUS_API_KEY` |
| SOL 美元价 | Binance 公开归档 | 免费，**无需注册** | 不用填 |
| 未平仓估值 | Jupiter 报价 | 免费，**无需注册** | 不用填 |
| 候选地址发现 | GeckoTerminal | 免费，**无需注册** | 不用填 |
| CEX 模拟（可选） | Binance 公开行情 | 免费，**无需注册** | 不用填 |
| 推送通知（可选） | Telegram | 免费 | `.env` 的 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` |

**只有 Helius 一个 Key 是必须的。** `.env.example` 里其它 Key 全是旧代币扫描模式或可选功能用的，跟单不需要。

### 拿 Helius Key

1. 打开 [helius.dev](https://www.helius.dev/) 注册，免费档不需要付款方式。
2. Dashboard 新建一个 project，复制它的 API Key。
3. 这个 Key 只读历史数据，**没有签名或发送交易的能力**。即便如此也不要提交进 Git——`.env` 已在 `.gitignore` 里。

## 二、填写位置

```bash
cd <项目目录>
python -m twobots init          # 生成 config.yaml 和 .env，不覆盖已有文件
```

`.env` 里只改这一行：

```
HELIUS_API_KEY=你的key
```

`config.yaml` 里确认/修改这几项（其余保持默认）：

```yaml
scanner:
  kind: wallets          # 默认就是
  network: solana

wallets:
  source: chain          # 程序自己从链上还原账本，不用单独跑适配器
  ledger_dir: wallet_ledgers
  discover_enabled: true # 自动从新池成交发现候选地址
  max_candidates: 50
  ledgers_per_cycle: 3   # 每轮还原几个，先小后大

follow:
  enabled: true          # 默认 false，跟单要打开
  source: local
  activity_dir: wallet_activity
  max_leaders: 3
  min_score: 10          # 见第五节，这个值需要按实际分数分布校准
  allowed_flags: []      # 空 = 只跟完全无标记的钱包
  max_signal_age_s: 120
  ticket_usd: 2
  initial_cash: 100

dex:
  enabled: false         # 必须保持 false，那是旧的按代币评分的 bot

cex:
  enabled: false         # 想专心看跟单就关掉，省下 Binance 历史下载
```

## 三、先离线验证，不花一分钱

```bash
python -m pytest tests/ -q
python -m examples.copy_demo
```

`copy_demo` 用合成数据跑通"排名 → 选领投 → 跟单"全链路，输出四个钱包的排名、被选中的领投和开仓延迟。确认这步能跑通再往下，否则出问题分不清是数据还是程序。

## 四、接真实数据：一条命令

```bash
python -m twobots run
```

只要 `.env` 里有 `HELIUS_API_KEY`、`config.yaml` 里 `wallets.source: chain` 且 `follow.enabled: true`，这一条命令就会持续做完整循环：

1. 从 GeckoTerminal 新池成交发现候选地址
2. 逐批从链上还原候选钱包的完整账本（每轮 `ledgers_per_cycle` 个，默认 3）
3. 排名，把不合格的标为观察
4. 从合格钱包里选领投，拉它们的实时成交流
5. 跟单到独立模拟账户，止损/移动止盈/超时/回撤熔断照常生效
6. 每小时导出一次报告

没有 Helius Key 时，账本还原和成交流会每轮记一条警告后跳过，其余部分照常运行——不会整个崩掉。

### 候选地址从哪来：这是最关键的一步

三个来源可以混用：

- **手填**：`wallets.addresses: [地址1, 地址2]`，优先级最高，最先还原。
- **平台粗筛**：接 Dune / Birdeye / Solscan 等，让已经索引过全链的平台把候选从百万缩到几百，见 [SHORTLIST_ZH.md](SHORTLIST_ZH.md)。**这是扩大候选池的正确做法。**
- **自动发现**：`wallets.discover_enabled: true`，从热门池的大额卖出方取样。免费，优先级最低。
- **现成账本**：直接把 JSON 放进 `wallet_ledgers/`，文件名等于内容里的 `address`。

**自动发现的实测结果：14 个候选，0 个可排名。** 成功还原的三个长这样：

| 买入 | 卖出 | 转出 | 完整平仓 | 记录缺口 |
|---|---|---|---|---|
| 3,799 | 161 | 3,415 | 0 | 96.6% |
| 4,041 | 150 | 3,679 | 0 | 96.8% |
| 1,012 | 16 | 868 | 0 | 98.7% |

买进去几千次、几乎不卖，全部转走——这是**分发钱包/打包器**，买完就转给下游地址，盈亏根本不发生在这个地址上。剩下的候选里两个是当天新建的钱包、一个是交易量超过预算上限的做市商。

原因很直接：GeckoTerminal 新池成交的 `tx_from_address` 是**签名者**，新币池子里的签名者绝大多数是狙击机器人和 bundler，不是你想跟的人。

所以**自动发现只适合当对照组的随机样本**（见第五节），正式候选要自己供。从 GMGN、Kolscan、Solscan、Dune 这类地方挑你认为值得研究的地址，填进 `wallets.addresses`。程序会优先还原它们。

适配器现在会用一页采样先判断形态：一个地址如果绝大多数仓位是转走而不是卖出，直接以 `buys_and_forwards_rather_than_trades` 拒绝，**不会再花几十次调用去拉它的完整历史**。

### 拒绝率直接看报告

报告里"账本还原"一节给出已检查数、可用数、拒绝率和拒绝原因分布，不需要单独跑诊断。想在开跑前先摸底几个地址，仍可以单独调：

```bash
python -m adapters.ledger --diagnose --address <钱包1> --address <钱包2>
```

### 费用先摸底再放量

每个钱包的账本还原要走完它的全部历史，老钱包单个可能上万 credits。`ledgers_per_cycle` 默认 3、`ledger_build_refresh_s` 默认一天，就是为了让你先看清一个钱包实际花多少。报告里有 `rpc_calls`，看清楚再往上调。

领投的成交流按 `follow.activity_poll_s`（默认 30 秒）轮询，**每个领投每轮一次调用**。跟 3 个领投约 260 万 credits/月，已经超免费档；跟更多就得用 webhook（每次推送 1 credit），见 [DATA_SOURCES.md](DATA_SOURCES.md)。

### 想跟更多钱包

`follow.max_leaders` 最高 200，但**真正的瓶颈是 `max_positions`**：领投再多，同时只能持有 `max_positions` 个仓位，其余信号直接丢弃。结果不是"跟前 100 名"，而是"跟恰好在我有空位时开仓的那个"——这会偏向高频钱包，而不是排名高的钱包。

所以两个要一起调。配置校验会在 `max_leaders > max_positions × 20` 时直接报错，就是为了挡住这个误配。模拟盘本金是虚拟的，为了更快攒够样本，可以把 `initial_cash`、`max_positions`、`max_leaders` 按比例一起放大。

## 五、怎么判断有没有 edge

这一节比前面都重要。**模拟账户赚钱本身不是 edge 的证据。**

### 先看延迟

报告里的跟单延迟中位数如果接近 `max_signal_age_s`（默认 120 秒），说明数据源太慢，成交价和领投差太远，后面所有结论都不可信。先把延迟压下来再谈别的。轮询 30 秒的延迟中位数大概在 15~45 秒，webhook 能到个位数秒。

### 必须有对照组

只跑一组没法回答"排名有没有用"。至少要两组并行，同时间段、同参数、只改候选池：

- **A 组**：你精选的候选钱包
- **B 组**：随机候选。把 `wallets.discover_enabled: true` 的自动发现结果直接拿来用就是一个现成的随机组

做法：复制整个项目目录，B 组用不同的 `data_dir` 和不同的 `wallet_ledgers/`，两边 `follow` 参数完全一致，同时开跑。

**如果 A 组不明显好过 B 组，排名没有在筛选出任何东西。** 这是最关键的一条，也是最容易被跳过的一条。

再加一条基准：同期直接拿着 SOL 不动的收益。跟单跑赢了随机组但跑输躺着拿 SOL，那也没有意义。

### 样本量和时间

- `max_positions: 3`、`ticket_usd: 2`，一天可能只有几笔成交。**十几笔成交什么都证明不了。**
- 目标至少 50~100 笔完整平仓，按这个配置大概需要几周。
- **参数在观察期内必须冻结。** 中途调 `min_score` 或 `max_leaders` 会让整段结果作废——那就变成在结果上拟合参数了。

### 领投会换人

排名每 6 小时刷新，领投名单会变。这本身制造选择效应：表现变差的钱包被换掉，看起来像是策略在止损，其实是幸存者偏差。记录每次换人的时间，分析时要把这件事算进去。

### 这套东西证明不了什么

- 不能证明实盘能复制。模拟成交用的是**本程序自己的报价**，不是领投的成交价，也没有 MEV、抢跑、落块失败、真实滑点。
- 不能证明领投是靠能力而不是运气，排名只有 90 天窗口。
- 不能识别领投之间的关联地址、对敲，或者领投在跟单者进场后反手卖给跟单者。
- 成交量一旦放大，`ticket_usd` 从 2 变成 200，价格冲击会完全改变结果。

## 六、每天的常规循环

```bash
python -m twobots run      # 一直开着，其余都是它自己做
python -m twobots report   # 想看的时候单独跑，不影响上面那个
```

Ctrl+C 正常退出：先停各个 loop，再导出一次最终报告，**持仓默认保留**。

想在退出时清仓加 `--close-positions`。**不建议在观察期内这么做**——每次重启都强制平仓会加上一次本不该有的往返成本，并且实现了策略没有要求的出场，等于往实验里注入噪声。这个标志是给"观察期结束、收尾"用的，不是给日常重启用的。

`follow.enabled: true` 但没有合格领投时，跟单 bot 什么都不做，报告里"跟单来源"显示"当前跟随 0 个钱包：无合格钱包"——这是正常的。常见原因是候选还没还原出账本、全被排名拒绝了，或者分数都低于 `min_score`。先看报告里"账本还原"一节的拒绝原因分布。
