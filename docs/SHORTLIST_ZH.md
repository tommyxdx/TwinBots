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
  "address_path_candidates": {"data.items[].address": 10},
  "next": "Put the path with the most addresses in address_path"
}
```

**把地址最多的那条路径填进 `address_path` 即可。** `--header NAME:env:VAR` 从 `.env` 读 Key，不用把密钥打在命令行里。

### Birdeye

top traders 类接口，`X-API-KEY` 加 `x-chain: solana`。用上面的 `--probe` 确定 `address_path`。

### Solscan Pro

普通 REST，header 是 `token`。同样用 `--probe`。

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

Dune 的缓存结果端点按官方文档实现（URL、header、`result.rows` 结构已核对）。取值路径、合并去重、缺 Key 跳过、报错隔离、Key 不泄露、文件源的四种格式，都有离线测试覆盖，并用真实配置跑通了 `twobots shortlist`。

**没有实测**：Dune / Birdeye / Solscan 的真实响应（我没有这些平台的账号和 Key）。所以每接一个源，先 `python -m twobots shortlist` 看一次输出再开着跑。
