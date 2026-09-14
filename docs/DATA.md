# 历史、实时数据和接口约定

## 数据来源与初次启动

| 数据 | 默认来源/方式 | 自动化范围 | 无法替代的内容 |
|---|---|---|---|
| Binance 历史 | 官方 `data.binance.vision` 月度 ZIP + SHA256 | 12 月 × BTC/ETH/SOL/DOGE × 4h；自动补 REST 近期 | 历史 L2 订单簿、账户成交 |
| Binance 实时 | 官方 REST + 增量深度 WebSocket | 自动快照、缓冲、序列校验、断线重建 | 自己的真实排队位置、隐形流动性 |
| 新池和聚合 OHLCV | GeckoTerminal 公共 API | 每轮发现、轮换候选、历史抽样跟踪 | 全部首发、完整失败币清单、早期持仓 |
| 链上安全和钱包关系 | 你提供的标准化 GET JSON 接口 | 填 URL/Key 后自动读取和验证时间 | 未接入时明确未知，不调用 RPC 猜测 |
| 百倍训练集 | 你提供的 CSV 下载 URL | 下载、可选 SHA256、解析、训练 | 不内置声称完整的免费百倍数据库 |

Binance 自 2025 年开始部分现货历史时间戳为微秒，读取器同时处理秒、毫秒、微秒。只收已结束 K 线；重复导入按主键覆盖，ZIP 文件不执行解压到任意路径。缓存每个下载的哈希；损坏会明确报错，不静默训练。

默认 `.sqlite3` 只保存聚合 K 线、池元数据、扫描、订单、模型指标和账户状态。价格训练在本机 CPU 上每日最多尝试一次，约万行的线性模型无需 GPU；实际耗时依机器而定。扫描/错误/净值记录保留 35 天；订单和聚合 K 线持续保留。缓存和 SQLite 长期仍会增长，不是零本地存储。需要更长期净值审计，请定期导出报告与备份。

`history.cohort_*` 控制一个小型历史研究样本。它不是既有全量失败币数据库；初次启动无法凭空得到那些已经消失的池。在发行后 48 小时内被发现的池可入组，超过该年龄仅接受显式历史清单。抽样按池地址哈希，不按事后收益挑选；到容量上限仍会漏样。失败币没有数据时，不把它的最后价格当成成功退出，也不武断标记跌幅为 100%。

## 标准化安全/持仓接口

在 `scanner.feature_url_template` 填入，例如：

```yaml
feature_url_template: "https://YOUR-SERVICE.example/features?network={network}&token={token}&pool={pool}"
feature_api_key_env: FEATURE_API_KEY
feature_header: Authorization
feature_header_prefix: "Bearer "
```

返回如下 JSON；字段值为示意，**不是任何真实币的分析**：

```json
{
  "network": "solana",
  "token": "REQUESTED_TOKEN_ADDRESS",
  "pool": "REQUESTED_POOL_ADDRESS",
  "asof": "2026-09-13T12:00:00Z",
  "source": "YOUR_INDEXER_AND_CHECK_METHOD",
  "features": {
    "can_sell": true,
    "mint_revoked": true,
    "freeze_revoked": true,
    "top10_ex_lp_fraction": 0.24,
    "largest_funding_cluster_fraction": 0.12,
    "creator_bad_rate": 0.05
  }
}
```

`network/token/pool` 必须匹配请求。`asof` 必须是数据实际时点，默认不超过 300 秒，不能用当前时间覆盖过期数据。缺失检查请直接省略键，不要填 `true`；JSON 字符串 `"true"` 不被接受。`can_sell` 应来自真实检查方法，报价存在不能证明可以卖出。撤销权限也不能证明没有骗局、集中抛售或其它程序限制。前十持仓必须排除已验证池账户并处理托管账户归集；资金簇和创建者历史应由索引器提供，而不是仅凭地址数量推断。

如果现有供应商只差字段名，可用 `scripts/adapter_server.py` 加 `adapter.example.json` 的声明式映射转换，无需接入 LLM。该桥接器只支持 GET JSON；复杂授权/POST/推断逻辑仍需供应商或你自己的标准化服务。它不自己创造链上安全分析。

## 远程池清单

`history.pool_catalog_url` 返回 UTF-8 CSV：

```csv
network,pool,token,created_at
solana,POOL_ADDRESS,TOKEN_ADDRESS,2026-09-01T10:00:00Z
```

应包含你在同一发现规则下观察的失败与成功项目，并注明覆盖范围。当前自动跟踪仅处理配置网络、仍在 `cohort_follow_days` 内的抽样池；更早的完整历史请通过训练 CSV 输入。仅提供赢家清单不会产生可信成功率。

## 标准化训练 CSV

最低表头：

```csv
network,token,asof,label_known_at,target,label,ret1,ret6,ret42,ema_gap,atr_pct,volatility,volume_ratio,drawdown42
```

| 字段 | 严格语义 |
|---|---|
| `asof` | 决策时点，UTC ISO 字符串或 Unix 时间；所有特征必须在此时已经可获取 |
| `label_known_at` | 完整未来窗口可知的时间，不早于 `asof + horizon`；尚未成熟者不参与训练 |
| `target` | `x2_24h` / `x10_7d` / `x100_30d`，与配置名称一致 |
| `label` | 完整未来窗口内任一 5m 收盘价相对 asof 收盘价达到目标为 1，否则 0；观察缺失/无法确定请不提交标签 |
| `ret1/6/42` | 5m 收盘价分别相对 1/6/42 根之前的简单收益率，例如 0.1 表示 10% |
| `ema_gap` | close/EMA20−1；EMA span=20、adjust=False、至少 20 根 |
| `atr_pct` | EWM 真波幅，alpha=1/14、adjust=False、至少 14 根，除以 close |
| `volatility` | 20 根 ret1 的样本标准差，ddof=1 |
| `volume_ratio` | 当前美元量/之前 20 根美元量的中位数，截断为 [0,50] |
| `drawdown42` | close/最近 42 根最高 high−1，包含当前已结束 bar |

可选增强字段：`log_liquidity` 为 ln(1+美元流动性)，`age_hours`，`turnover_h1` 为小时美元量/流动性，`buy_sell_ratio_h1` 为 buys/max(sells,1)，`top10_ex_lp_fraction`，`largest_funding_cluster_fraction`，`creator_bad_rate`。某增强列在目标样本中有至少 80% 覆盖才纳入该模型；预测时所需特征至少 75% 已知，否则不输出。其余按训练段中位数填充。

重复 `(network:token,asof,target)` 去重；标签冲突则丢弃冲突样本。模型限定配置网络。特征是否真正 point-in-time 不能仅靠 CSV 格式验证，需要数据提供方保证；不接受远程 pickle/joblib，避免执行不受信任的序列化对象。需指定合法的数据使用授权，代码不绕过登录或数据许可。

## 标准化 DEX 报价接口

`dex.provider: generic` 时，程序 GET `dex.generic_quote_url`，参数为 `network,input_token,output_token,amount,slippage_bps`。`amount` 是原始单位整数串，不是人类可读 token 数量。

```json
{
  "input_token": "REQUESTED_INPUT_ADDRESS",
  "output_token": "REQUESTED_OUTPUT_ADDRESS",
  "in_amount": "2000000",
  "out_amount": "123456789",
  "min_out": "122222221",
  "price_impact_fraction": 0.004,
  "asof": "2026-09-13T12:00:00Z",
  "context_slot": 12345678
}
```

金额须为正整数串/整数；0.004 表示 0.4% 价格冲击。输出金额应已含 AMM/平台费，额外网络成本由配置扣除。缺路由、不可交易、接口异常应返回非 2xx；不能给 0 输出伪装正常报价。报价币地址、精度、估值必须和网络一致。这个接口只报价，不得暗中发送交易。

## CU 与费用

默认不调用链上 RPC，因此程序本身不消耗你的 RPC CU；外部索引商可能按 credits/CU/请求计费。`http.max_requests_per_day` 统计应用 HTTP 次数（含重试），不能替代各供应商按权重计费。WebSocket 连接/消息未算 HTTP 次数。默认扫描最多约 1,152 次/天，若每候选额外查一次安全接口再约 864 次/天；DEX 两仓每 5 分钟反查可额外约 576 次/天，再加历史与 CEX。实际扫描耗时会降低频率，入场/失败重试也会增加请求。达到预算后停止新增 HTTP 请求，已有 WebSocket 可能仍在线；报告会提示数据缺失，报价驱动退出也可能暂停。

你的 $100 是投资本金，基础设施成本应单独预算。优先先用现有常开电脑、公共行情和小规模 paper 观察；本包不要求购买某种套餐。
