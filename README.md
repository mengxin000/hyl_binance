# Hyperliquid × Binance 公共行情价差筛选器

独立 Python 终端程序，仅读取公开市场元数据、24 小时成交额、盘口 BBO 和永续资金费率。不会读取账户私有信息，也不下单。

## 开发与运行

```powershell
uv sync
uv run hpl-binance
```

`uv sync` 会根据 `pyproject.toml` 和 `uv.lock` 建立一致的虚拟环境。`uv run hpl-binance` 使用守护入口，主程序异常退出时会自动重启；只启动主程序时运行 `uv run hyperl-binance`。正常退出或 Ctrl+C 会停止程序。终端按 `K` 在现货/现货、现货/永续、永续/永续三页之间切换。

维护代码时可用 `uv run ruff format src tools main.py guardian.py` 格式化，并用 `uv run ruff check src tools main.py guardian.py` 做静态检查。依赖只在 `pyproject.toml` 中声明；提交代码时一并保留 `uv.lock`。

也保留了项目目录下的兼容入口：`python main.py` 和 `python guardian.py`。配置文件仍放在 `config/`，运行状态、历史数据与诊断日志仍写入 `data/`。从其他工作目录启动时，可用 `HYPERL_BINANCE_HOME` 指定项目目录。

## 代码结构

```text
src/hyperl_binance/
├── domain/          # 标准化行情模型、路线筛选、价差统计
├── exchanges/       # Hyperliquid / Binance 元数据适配与归一化
├── market_data/     # 各交易所 WS 流和统一 BBO 行情缓存
├── config/          # JSON 配置、校验与热加载
├── persistence/     # 状态和诊断日志持久化
├── presentation/    # 终端界面
├── app.py           # 应用编排
├── cli.py           # 主程序入口
└── guardian.py      # 异常退出守护入口
```

交易所数据先由各自 adapter 转成统一的 `Instrument` 和 `BookQuote` 模型，再交给领域层计算；领域层不依赖 REST/WS 响应格式。新增交易所时，在对应 adapter 中实现映射，并接入 discovery 或 feed 编排。

结构参考 [Hyperliquid 官方 Python SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) 的模块化组织；本程序只接入公开行情 API，不引入账户签名或交易功能。

## 市场范围与规则

- Hyperliquid 现货与默认 perp DEX；排除 HIP-3 Builder DEX。
- Hyperliquid 默认永续价格按 USDT 计价、以 USDC 作保证金；保证金币不作为报价币。`HYPE-USD`、`PURR-USD` 按 USDC 计价并在配置中显式覆盖。
- Binance 现货与 USDⓈ-M 永续；仅筛选 USDT、USDC 报价市场，不取币本位交割合约。
- 基础资产先按完全相同的名称配对；没有精确匹配时，才尝试名称包含关系（例如 `USOL` 与 `SOL`）。包含关系只生成候选，必须等两边新鲜 BBO 到达并按 Binance 最新 USDC/USDT 汇率换算后，价格偏差不超过 1% 才进入统计；超过 1% 记为 `base_price_mismatch`，不计为有效路线。`base_aliases` 仅保留给少数无法通过包含关系识别的例外，不需要逐个维护常见 `U*` 资产。
- 24h quote turnover 门槛按交易所和市场类型分别配置：默认 HL 现货/永续各 500,000 USDT 等值，Binance 现货/永续各 5,000,000 USDT 等值。USDC 成交额暂按 1 USDT 估算用于市场初筛；价差计算统一使用 Binance 最新且新鲜的 USDC/USDT 现货 BBO 中间价换算，HL 与 Binance 的 USDC 报价共用该汇率。
- 统计价差：同类市场按 HL/Binance - 1；现货/永续按永续/现货 - 1。终端另外显示两种方向的 BBO 可成交价差（HL 买/BN 卖、BN 买/HL 卖）。这些是最优档毛价差，未扣手续费，也不代表大额可成交价差。两边 BBO 分别须在 1 秒内更新；两条消息的接收时间差仅用于诊断，不作为路线硬过滤条件。
- 只用 BBO；暂不重建 L2 深度、不做扫单、排队位置或延迟模拟。统计采样与窗口在 `config/basis_*.json` 中调整，JSON 修改会热加载。
- 原始行情和样本仅放内存；重启后重新采样。持久化每条路线状态、机会事件和网络诊断于 `data/`。

## 配置

- `config/market.json`：报价币、四项按交易所和市场类型区分的成交额下限、可选例外别名及 HL 特殊永续报价币覆盖。
- `config/basis_spot_spot.json`、`basis_spot_perp.json`、`basis_perp_perp.json`：各类统计窗口、分位数与阈值。
- `config/runtime.json`：采样周期、新鲜度、WebSocket 分片与持久化参数。

`quantile_refresh_seconds` 控制每条路线重算 P5/P95 分位数的间隔；修改运行配置后会热加载到已有路线。
`hyperliquid_subscriptions_per_connection` 是 HL 每个 WebSocket shard 的目标订阅数，范围为 1–10；程序按现货/永续分组，且单进程最多打开 10 条 HL WebSocket。若同一币种连续三次在订阅确认阶段断连，会暂时从当前市场集合隔离，避免它反复拖断同 shard 的其他行情；市场集合变化后会重新尝试。

无效 JSON 会保留上一份有效配置，并在终端错误栏显示原因。网络诊断 JSONL 有大小上限并滚动；若诊断队列过载，会丢弃新日志而不阻塞行情循环。

## 注意

当前版本是候选路线筛选器，不是交易系统。公开 API 可用性、市场命名和交易所限频可能变化；启动后应先观察 `data/diagnostics/network_pipeline.jsonl` 与行情新鲜度。资金费率用于显示，不参与排序。
