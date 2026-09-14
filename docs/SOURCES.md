# 官方接口与研究参考

开发核对日期：2026-09-13。API 路径、配额、认证及可用性可能变化；`doctor --online` 验证当前网络连接，以下页面不是对你账户套餐的保证。项目源码没有从第三方交易 bot 仓库复制策略实现。

1. [Binance Public Data 官方仓库](https://github.com/binance/binance-public-data)：月度/日度 ZIP、校验文件、现货微秒时间戳。作为自动历史下载源。
2. [Binance Spot WebSocket Streams](https://developers.binance.com/en/docs/binance-spot-api-docs/web-socket-streams)：diff depth 的 U/u 序列及本地订单簿同步流程。
3. [Binance Spot REST API](https://developers.binance.com/en/docs/binance-spot-api-docs/rest-api)：公开行情、交易规则及超时可能带来未知执行状态。
4. [Binance Filters](https://developers.binance.com/en/docs/binance-spot-api-docs/filters)：LOT_SIZE、价格与名义金额约束。
5. [Binance Commission FAQ](https://developers.binance.com/en/docs/binance-spot-api-docs/faqs/commission_faq)：standard/tax/special commission 与账户折扣。paper 使用报价币等值保守费用。
6. [GeckoTerminal API 指南](https://apiguide.geckoterminal.com/) 与 [API 参考](https://api.geckoterminal.com/docs/index.html)：近期池及池 OHLCV。最近新池列表不等于完整发行历史。
7. [DEX Screener API](https://docs.dexscreener.com/api/reference)：可作为其它元数据来源的参考，本版未调用它。
8. [Jupiter Swap V1 Quote](https://developers.jup.ag/docs/api-reference/swap/v1/quote)：input/output mint、原始整数数量、min out、价格冲击与 routePlan。旧版接口可用性须以账户和官方最新文档为准；不调用 swap 执行。
9. [Solana simulateTransaction](https://solana.com/docs/rpc/http/simulatetransaction) 与 [Transactions](https://solana.com/docs/core/transactions)：不广播的模拟和交易原子性；不等于为纸上账户保留虚拟链上库存。
10. [HftBacktest Order Fill](https://hftbacktest.readthedocs.io/en/latest/order_fill.html)：市场回放不受虚拟订单影响、L2 排队假设及部分成交建模局限。这里采纳这些限制，不宣称只看 L2 能还原真实执行。
11. [Telegram Bot 教程](https://core.telegram.org/bots/tutorial)：BotFather、令牌及 Bot API；只在本地填好后启用通知。
12. [MELT / MemeTrans 项目](https://github.com/git-disl/MELT) 与 [研究论文](https://arxiv.org/html/2602.13480v1)：可借鉴创建者/持仓/关联特征与时点研究设计；数据授权、样本范围和风险标签不同于“买入后可卖出的百倍回报”。研究报告减少损失不等于证明正收益。其大规模原始数据/序列化文件不默认下载，也不执行远程 pickle；如转换成 CSV，须先确认许可和标签适用性。

本版默认阈值（例如评分权重、延迟、深度折扣、手续费兜底、失败概率）是明确参数化的初始实验假设，没有把网络分享中的“百倍案例”写成统计上已证实的优势。
