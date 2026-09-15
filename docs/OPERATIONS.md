# 本地与 24 小时运行

## Telegram

1. 在 Telegram 找官方 `@BotFather`，执行 `/newbot` 创建你自己的 bot，将 token 填到本地 `.env` 的 `TELEGRAM_BOT_TOKEN`。
2. 你先向这个 bot 发送 `/start`。通过 Telegram 官方 `getUpdates` 方法读取自己消息的 `message.chat.id`，填入 `TELEGRAM_CHAT_ID`；不要把 token 发给第三方查 ID 网站。
3. 将 `telegram.enabled` 改为 `true`。通知只发送到这个固定 chat，不根据代币元数据中的地址转发。
4. 达到阈值才发送，不保证每轮有提醒。超时会记录 UNKNOWN，避免因不知道是否送达而自动重复刷屏；见报告 notifications 或 SQLite `outbox`。

## 配置运行顺序

先运行离线 demo、测试，再 `doctor --online`。让默认 CEX paper 和 scanner 运行；若只想看候选，运行 `scan`。默认钱包模式保持 `dex.enabled: false` 与 `follow.enabled: false`。钱包数据接入见 `docs/WALLET_DATA.md`，跟单接入见 `docs/COPY_TRADING.md`；只有明确切换旧 `scanner.kind: tokens`、补齐报价与安全数据后才可启用旧 DEX paper。

如使用本地 GET 字段适配器，复制 `adapter.example.json` 为自己的配置，填写两个 upstream URL、输入参数对应名、输出 JSON 字段路径，以及 key 环境变量。先独立启动 `python scripts/adapter_server.py --config YOUR_ADAPTER.json`，然后 bot 使用 `http://127.0.0.1:8787/features?network={network}&token={token}&pool={pool}` 和 `/quote`。桥接器不加载 `.env`；其 key 应由操作系统环境变量提供。缺失字段不补真值，复杂 API 不属于仅字段改名可解决的范围。

## 无人值守

程序自带断线重连、日预算、维护任务、数据库和单实例锁。要在电脑重启后自动运行，仍需要操作系统计划任务/服务；聊天窗口本身不是你的运行服务器。本包没有替你部署云主机。

- Windows：任务计划程序 → 登录或启动时运行本项目 `.venv\Scripts\python.exe`，参数 `-m twobots run`，起始目录为项目目录；启用失败重启，保持网络与电源。首次依赖安装先交互完成。
- Linux：修改附带 `scripts/twobots.service.example` 中的用户和绝对路径，再用 systemd 管理。示例默认包含扫描器与 CEX；DEX 按 config 控制。云服务器费用不在 paper 账户内。
- macOS：可使用 launchd；至少禁用运行时休眠。单独关上终端会结束进程，除非由服务管理器启动。

每次重启保留原 `data_dir` 就会恢复虚拟库存、现金、回撤暂停状态和订单。不要同时在多个机器上共享同一 SQLite 文件，也不要在进程运行中直接删除数据库。停止后备份整个 data 文件夹；活跃 SQLite 的 WAL 不能只复制主文件。

## 报告和故障

`python -m twobots report` 生成可本地打开的 HTML 和完整 JSON。检查最新时间，不要把 stale 估值当成能成交的价值。数据库中 `orders` 是本地 paper 订单；`events` 保存信号与失败；`requests` 是 UTC 日请求量。

| 现象 | 原因/处理 |
|---|---|
| 一直没有买单 | 可能是现金状态、无信号、模型不如基线、仓位/名义金额过小、安全数据缺失、超预算或行情断线；查报告与控制台 |
| 概率一直为空 | 没有足够成熟完整标签，或留出验证失败。需要合格历史集/更长观察，不是换 LLM |
| 数据源超时/403/451 | 检查本地可用性、账户和当地访问条件；不要绕过提供商限制。未通过连接检查的功能不会伪造数据 |
| HTTP 429 | 降低扫描候选数/频率、检查供应商配额和自定义接口；本程序退避并记入请求预算 |
| Daily budget exhausted | 暂停需要 HTTP 的操作至下一个 UTC 日；预算按请求计，供应商可能另按权重收费 |
| `Another process...` | 同目录相同服务已运行；不用删锁文件，正常退出旧进程即可 |
| 回撤暂停未恢复 | 设计如此。评估后用新的 `data_dir` 开始独立实验；不要在旧记录中删亏损伪造连续收益 |
| 尾仓无法卖出 | CEX 最小下单金额/步长或 DEX 无路由，库存保留；不是保证流动性可控 |
| 策略文件改了但结果难比较 | 每个实验使用不同配置和 data_dir，保留版本及参数；规则/模型实验的起止时间应一致 |

依赖使用主版本范围，未声称冻结所有平台环境。测试环境的实际依赖版本见 `docs/VALIDATION.md`。Python 3.11/3.12 更便于安装预编译依赖；Windows 启动脚本依赖 `python` 命令在 PATH 中。
