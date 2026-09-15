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
  source: local          # 适配器写文件到本地，不需要 adapter 模式
  ledger_dir: wallet_ledgers
  discover_enabled: true # 自动从新池成交发现候选地址
  max_candidates: 50

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
python -m pytest tests/ -q      # 128 项
python -m examples.copy_demo    # 合成数据跑通"排名 → 选领投 → 跟单"全链路
```

`copy_demo` 会输出四个合成钱包的排名、被选中的领投、开仓时的跟单延迟，以及一个 HTML 报告路径。确认这一步能跑通再往下，否则后面出问题分不清是数据还是程序。

## 四、接真实数据

### 4.1 先诊断，不要一上来就批量拉

```bash
python -m adapters.ledger --diagnose \
  --address <钱包1> --address <钱包2> --address <钱包3>
```

`--diagnose` 不写文件、不调 Jupiter，只告诉你每个钱包能不能用、被什么挡住、消耗了多少 `rpc_calls`。输出里的 `rejection_rate` 就是你这批候选的实际拒绝率。

**先用 3~5 个钱包试，看清单次 `rpc_calls` 再放量。** 很老的钱包要从第一笔交易拉起，单个可能消耗上万 credits。

### 4.2 候选地址从哪来

三个来源，可以混用：

- **自动发现**：`wallets.discover_enabled: true` 时，扫描器每 6 小时从 GeckoTerminal 新池成交里取发送地址。免费，但**样本偏向新币狙击者**，正是你想避开的那类。
- **手填**：`wallets.addresses: [地址1, 地址2]`，适合你已经从 Solscan、GMGN、Dune 之类看好的钱包。
- **本地文件**：直接把账本 JSON 放进 `wallet_ledgers/`，文件名必须等于内容里的 `address`。

### 4.3 生成账本

```bash
python -m adapters.ledger --address <钱包> --out wallet_ledgers
```

能用就写文件并打印统计，不能用就打印原因、**不写文件**。批量就重复 `--address`。

### 4.4 产出排名

```bash
python -m twobots scan --once
python -m twobots report
```

报告里会列出每个钱包的已实现盈亏、已售成本收益率、完整平仓数、胜率、Profit Factor、去掉最大盈利币后的盈亏、未平仓亏损和研究分数。`status: observation` 的是被拒绝的，不会被跟单。

**看一眼分数分布再回头调 `follow.min_score`。** 默认的 10 是拍的，真实分数分布未知。

### 4.5 起实时成交流

轮询模式最简单：

```bash
python -m adapters.activity poll --data-dir data --out wallet_activity --watch --interval 30
```

领投名单直接从 bot 的 `state.sqlite3` 读，所以要先跑过一次 `scan` 和跟单 bot。这个进程要一直开着。

**30 秒轮询 3 个领投约 260 万 credits/月，超免费档。** 认真跑就用 webhook（每次推送 1 credit，约 9 千/月），见 [DATA_SOURCES.md](DATA_SOURCES.md)。

### 4.6 跑跟单

另开一个终端：

```bash
python -m twobots trade --venue copy
```

或者 `python -m twobots run`，会按 `enabled` 自动带上扫描器和所有开启的 venue。

### 4.7 看结果

```bash
python -m twobots report
```

HTML 报告里"跟单来源"一节给出当前跟随的钱包、入场信号数、成交数和**跟单延迟中位数**。`COPY 独立模拟账户`一节给出现金、净值和持仓数。

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
# 终端 1：实时成交流，一直开着
python -m adapters.activity poll --data-dir data --out wallet_activity --watch

# 终端 2：扫描器 + 跟单，一直开着
python -m twobots run

# 终端 3：定期看
python -m twobots report
```

账本每周重新生成一次就够——重新拉链上数据是适配器的事，扫描器只负责读文件：

```bash
python -m adapters.ledger --address <钱包1> --address <钱包2> --out wallet_ledgers
```

`source: local` 时扫描器每轮都会重读文件，所以新账本很快生效。但每轮只处理 `wallets.max_wallets_per_run` 个（默认 2），配合 `scanner.poll_s: 600`，50 个钱包要轮 4 个多小时才转一圈。本地读文件不消耗 API，账本多就把 `max_wallets_per_run` 调大。

注意 `follow.enabled: true` 但没有合格领投时，跟单 bot 什么都不做，报告里"跟单来源"会显示"当前跟随 0 个钱包：无合格钱包"——这是正常的，不是故障。常见原因是所有候选都被排名拒绝了，或者分数都低于 `min_score`。
