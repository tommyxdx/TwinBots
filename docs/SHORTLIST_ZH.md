# 候选粗筛：接多个平台

## 为什么要这一层

Solana 每天活跃地址数百万，而你每月能还原的账本是几百个——差四到五个数量级。链上自动发现只能给你一个很窄的样本，靠它凑不出好的候选池。

这些平台已经把全链索引好了。让它们做**粗筛**（百万 → 几百），你的程序做**精验**（严格还原 + 排名）。

**粗筛必然不准**，这没关系。它们的 PnL 用自己的成本口径，不区分转入库存、不计转出缺口、不做完整持仓周期。它们只需要回答"这几百个地址值不值得花 RPC 调用去查"，准确性由后面那一级补。

## 配置形状

一个源就是一次 HTTP 请求加一个取值路径，不管哪家平台都是这个形状。所以源写在配置里而不是为每家写一个客户端——这些 API 变得比代码该变的频率高。

```yaml
shortlist:
  enabled: true
  refresh_s: 86400      # 一天刷新一次够了
  max_addresses: 500
  sources:
    - name: dune
      kind: http
      url: https://api.dune.com/api/v1/query/你的QueryID/results
      headers: {X-DUNE-API-KEY: "env:DUNE_API_KEY"}
      params: {limit: 500}
      address_path: result.rows[].trader
```

- `env:NAME` 从环境变量取值。**某个源的 Key 没配就跳过它，不会让整轮失败**，所以可以一个一个接。
- `address_path` 用 `.` 走字典、`[]` 走列表。`result.rows[].trader` 和 `data.items[].address` 覆盖了绝大多数返回形状。
- `kind: file` 读本地 CSV/JSON/纯文本，适合手动导出的结果。

先单独跑一次验证配置，不用启动整个 bot：

```bash
python -m twobots shortlist
```

输出每个源返回了几条、几条是合法地址、几条被丢弃、哪个源因为缺 Key 被跳过。

## 各平台怎么接

### Dune（推荐先接这个）

免费档可用。**不要用"提交查询 + 轮询"那套**——`GET /api/v1/query/{id}/results` 直接读最近一次缓存结果，不触发执行，更便宜也更简单。

做法：在 Dune 网页上写一条返回**单列地址**的查询（Solana DEX trades 表已经索引好了），手动跑一次，然后把 query id 填进配置。之后程序每天读一次缓存结果。

查询该怎么写，往这个方向：90 天内交易笔数 ≥ N、**买入和卖出都有**、按已实现盈亏或者交易笔数排序。买卖都有这一条很重要——只买不卖的地址是分发钱包，喂进来也会被拒。

`address_path` 填 `result.rows[].你的列名`。

### 不知道 address_path 填什么？让程序自己找

**不用查文档、不用猜字段名。** `--probe` 打一次接口，扫描返回体里所有长得像 Solana 地址的值，告诉你它们在哪个路径：

```bash
python -m twobots shortlist --probe "https://public-api.birdeye.so/<接口路径>" --header "X-API-KEY:env:BIRDEYE_API_KEY" --header "x-chain:solana" --param "limit=10"
```

输出形如：

```json
{
  "top_level_keys": ["data", "success"],
  "wallet_path_candidates": {"data.items[].owner": 5},
  "not_wallets": {
    "data.items[].quote.address": 5,
    "data.items[].base.address": 5,
    "data.items[].poolId": 5
  },
  "next": "Use a wallet_path_candidates entry; confirm the field means a trader, not a token or pool"
}
```

`--header NAME:env:VAR` 从 `.env` 读 Key，不用把密钥打在命令行里。

**注意 `not_wallets` 那一栏。** 代币 mint、池子地址和钱包地址都是 32 字节 base58，光看形状分不出来。比如 Birdeye 的成交流水接口会同时返回 `owner`（钱包）、`base.address` / `quote.address`（代币）和 `poolId`（池子）——**把代币 mint 当钱包喂进排名器不会报错，只会安静地算出一堆没有意义的结果**。所以按字段名分成了两栏，只从 `wallet_path_candidates` 里选，并确认那个字段确实指交易者。

### Birdeye

接口路径在 [docs.birdeye.so](https://docs.birdeye.so/) 的 reference 章节。**但文档站改过版、链接会失效，而且能不能调取决于你的套餐——直接用 `--probe` 打一次比查文档准。**

2026-09 用真实 Key 实测通过的三个（全部 `X-API-KEY` + `x-chain: solana`）：

| 路径 | 是什么 | 钱包字段 |
|---|---|---|
| `/trader/gainers-losers` | 全局盈亏榜 | `data.items[].address` |
| `/defi/v2/tokens/top_traders` | 某个代币的顶级交易者 | `data.items[].owner` |
| `/defi/txs/token` | 某个代币的成交流水 | `data.items[].owner` |

**做粗筛用第一个**——它直接就是按盈亏排的交易者榜，不用先选代币。

`type` 合法值是 **`today` / `yesterday` / `1W` / `30d` / `90d`**（填别的会 400 并回显合法列表）。**一个 Key 可以声明多个源**，每个档位一条，实测五个档位各返回 100 个、并集 **247 个独立地址**——单用 `1W` 只有 100。

| 档位 | 与 1W 的重叠 |
|---|---|
| today | 31 |
| yesterday | 43 |
| 30d | 93 |
| 90d | 82 |

`today` / `yesterday` 带来的新地址最多，但**优先用长窗口**：按 90 天盈亏排出来的钱包，比按当天排出来的更可能有排名需要的完整平仓周期。

```yaml
    - name: birdeye_90d
      kind: http
      url: https://public-api.birdeye.so/trader/gainers-losers
      headers: {X-API-KEY: "env:BIRDEYE_API_KEY", x-chain: solana}
      params: {type: 90d, sort_by: PnL, sort_type: desc, limit: 100}
      address_path: data.items[].address
    - name: birdeye_1w
      kind: http
      url: https://public-api.birdeye.so/trader/gainers-losers
      headers: {X-API-KEY: "env:BIRDEYE_API_KEY", x-chain: solana}
      params: {type: 1W, sort_by: PnL, sort_type: desc, limit: 100}
      address_path: data.items[].address
```

注意它的 PnL 口径和本程序的排名完全不同，这里只用它来决定"哪些地址值得花 RPC 去查"。

### Solscan Pro：免费 Key 不够用

header 是 `token`（用 `Authorization: Bearer` 会回 "Token is missing"，可以确认格式）。

但 2026-09 实测：**免费档 Key 对 `/v2.0/` 下所有接口都返回 401 "Unauthorized: Please upgrade your api key level"**——`account/detail`、`token/defi/activities`、`token/holders`、`token/trending`、`token/top` 无一例外。Key 本身有效，是套餐等级不够。

要用 Solscan 得先付费升级。在那之前，多接几个 Birdeye 档位是免费的替代方案。

### Flipside

两个问题：它的查询 API 是**异步的**（提交 → 轮询 → 取结果），一次请求拿不到结果；而且 2026-09 实测时它文档里那个 API 主机名**根本无法解析 DNS**。

所以只有一条路：在 Flipside 网页跑完查询、导出 CSV，用 `kind: file` 读：

```yaml
    - name: flipside
      kind: file
      path: flipside_export.csv
      column: address
```

### 加别的平台

不用改代码。任何"一次请求返回一批地址"的 REST 接口，照上面的形状加一条 source 就行。

## 合并规则

- **按地址去重**，记录每个地址被哪几个源提到过
- **多个源都提到的排在前面**——这只是个便宜的排序依据，不是能力证据（这些平台共用数据源和口径，一致不代表正确）
- 不是合法 Solana 地址的直接丢弃并计数
- 单个源最多取 1000 条，总量由 `max_addresses` 限制

## 候选优先级

还原是稀缺资源，所以顺序决定了 RPC 调用花在谁身上：

1. `wallets.addresses` —— 你手填的
2. 粗筛结果 —— 平台给的
3. 链上自动发现 —— 最宽最杂的一层

## 安全

- 源地址必须 HTTPS，拒绝重定向（不会把 Key 转发到别处）
- 请求受共享日预算和主机限速约束
- **provider 自己的报错消息不记录**，只留异常类型——它可能回显带 Key 的查询串。本程序自己构造的报错（只含状态码和主机名）会原样保留，因为那里面没有密钥
- Key 只从环境变量读，`.env` 已在 `.gitignore` 里

## 可达性实测（2026-09，德国 Vodafone 家宽）

| 平台 | 结果 |
|---|---|
| Dune API | HTTP 401「需要 Key」— 通 |
| Birdeye API | HTTP 401「需要 Key」— 通 |
| Solscan Pro API | HTTP 401「需要 Key」— 通 |
| Flipside API | DNS 解析失败，主机名不存在 |

**Dune 没有封锁德国**：`dune.com`、`docs.dune.com`、`api.dune.com` 从这条线路全部正常。如果你的浏览器打开 Dune 显示 blocked，那是浏览器那一侧的问题（VPN/加速器的出口 IP 被 Cloudflare 标记、拦截类插件、或者缓存的 Cloudflare challenge），不是地区限制。关掉 VPN、用无痕窗口、停用插件再试。

排查时看 `twobots shortlist` 的报错：**401 是 Key 不对，403 才是真被拦，404 是路径写错**。这三个码不含密钥，会原样保留在报告里。

## 我实测到什么程度

**已用真实 Key 实测**：Birdeye 的三个接口（上表）全部返回 200，`--probe` 在真实响应上正确分出了钱包字段和代币/池子字段。

**按官方文档实现但未实测**：Dune 的缓存结果端点（URL、header、`result.rows` 结构已核对，但我没有 Dune 账号）。Solscan Pro 同理。

取值路径、钱包/代币分栏、合并去重、缺 Key 跳过、报错隔离、Key 不泄露、文件源的四种格式，都有离线测试覆盖。

**每接一个新源，先 `--probe` 一次再写进配置**——文档会过时，套餐权限也因账号而异，打一次接口是唯一可靠的确认方式。
