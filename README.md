# Hyperliquid × Binance 公共行情价差筛选器

独立 Python 终端程序，读取公开市场元数据、24 小时成交额、盘口 BBO、前 20 档快照与永续资金费率，进行价差筛选和模拟滑点观测。不会读取账户私有信息，也不下单。

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
├── simulation/      # 独立延迟、盘口、成交、费用、期望与 Score 模型
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
- Hyperliquid 永续元数据只给出币种标识、不提供明确报价币；程序默认按 USDC 标准化，只有经过确认的例外才通过 `hyperliquid_perp_quote_overrides` 覆盖。价格统计会用 Binance 最新 USDC/USDT 现货 BBO 中间价折算为 USDT。
- Binance 现货与 USDⓈ-M 永续；仅筛选 USDT、USDC 报价市场，不取币本位交割合约。
- 基础资产先按完全相同的名称配对；没有精确匹配时，才尝试名称包含关系（例如 `USOL` 与 `SOL`）。包含关系只生成候选，必须等两边新鲜 BBO 到达并按 Binance 最新 USDC/USDT 汇率换算后，价格偏差不超过 1% 才进入统计；超过 1% 记为 `base_price_mismatch`，不计为有效路线。`base_aliases` 仅保留给少数无法通过包含关系识别的例外，不需要逐个维护常见 `U*` 资产。
- 24h quote turnover 门槛按交易所和市场类型分别配置：默认 HL 现货/永续各 500,000 USDT 等值，Binance 现货/永续各 5,000,000 USDT 等值。USDC 成交额暂按 1 USDT 估算用于市场初筛；价差计算统一使用 Binance 最新且新鲜的 USDC/USDT 现货 BBO 中间价换算，HL 与 Binance 的 USDC 报价共用该汇率。
- `config/market.json` 的 `binance_price_min_usdt` 可热加载设置 Binance 标的最低价格；比较值使用 Binance 行情折算后的 USDT mid，低于下限的路线不参与统计和列表。`null` 表示不限制；只修改价格下限不会触发市场 REST 元数据重拉。
- 统计价差：同类市场按 HL/Binance - 1；现货/永续按永续/现货 - 1。领域快照另外计算两种方向的 BBO 可成交价差（HL 买/BN 卖、BN 买/HL 卖）。这些是最优档毛价差，未扣手续费，也不代表大额可成交价差。两边 BBO 分别须满足 `runtime.quote_max_age_ms`；两条消息的接收时间差仅用于诊断，不作为路线硬过滤条件。
- μ、σ、分位数继续使用原 BBO 和统计采样逻辑。独立模拟引擎接收逐条行情与数量，按本机接收时间计算延迟和 taker 扫单；不模拟排队及 maker 成交概率。统计窗口在 `config/basis_*.json` 中调整，JSON 修改会热加载。
- 原始行情和样本仅放内存；重启后重新采样。持久化每条路线状态、机会事件和网络诊断于 `data/`。

## 配置

- `config/market.json`：报价币、四项按交易所和市场类型区分的成交额下限、可选例外别名及 HL 特殊永续报价币覆盖。
- `config/basis_spot_spot.json`、`basis_spot_perp.json`、`basis_perp_perp.json`：各类统计窗口、分位数与阈值。
- `config/runtime.json`：采样周期、新鲜度、WebSocket 分片与持久化参数。
- `config/simulation.json`：固定下单金额、两段延迟、数据年龄、样本窗口和 maker/taker 费率。
- `config/score.json`：滑点成本与开平次数权重、尾部成本权重、评分样本有效期。

`quantile_refresh_seconds` 控制每条路线重算 P5/P95 分位数的间隔；修改运行配置后会热加载到已有路线。
`hyperliquid_subscriptions_per_connection` 是 HL 每个 WebSocket shard 的目标订阅数，范围为 1–10；程序按现货/永续分组，且单进程最多打开 10 条 HL WebSocket。若同一币种连续三次在订阅确认阶段断连，会暂时从当前市场集合隔离，避免它反复拖断同 shard 的其他行情；市场集合变化后会重新尝试。

无效 JSON 会保留上一份有效配置，并在终端错误栏显示原因。网络诊断 JSONL 有大小上限并滚动；若诊断队列过载，会丢弃新日志而不阻塞行情循环。

## 固定金额滑点观测

默认 `order_notional_usdt=10`、`exchange_delay_ms=20`、`deal_delay_ms=30`，单位分别为 USDT、ms、ms。模拟在独立后台线程运行，不受一秒统计采样或 REST 刷新等待影响。现有统计模型通过 `SignalPolicy` 提供最新 μ 与开平阈值；模拟器用逐条有效 BBO 检查阈值穿越。持续越界只触发一次，回到 μ 后重新允许同方向观测。无需达到列表的 2σ 门槛才能积累模拟样本。

在 `t0` 高于 `μ + open_threshold_bps` 时产生开方向观测：现货/永续路线的现货腿假定 maker 买入，永续腿 taker 卖出。同类路线默认 Binance maker 买入、HL taker 卖出。低于 `μ + close_threshold_bps` 时产生相反方向观测：原 maker 腿卖出、原 taker 腿买入。这是独立开/平方向成本观测，不管理账户库存、不配对完整持仓回合，不将净入场价差当作已实现利润；资金费率暂不计入模拟成本或 Score。

`t1=t0+exchange_delay` 时，以 maker 买入 bid / 卖出 ask 假定全部成交。金额按该时刻的 USDT 价格转换成数量 q，taker 腿使用相同 q。`t2=t1+deal_delay` 时按卖出 bids / 买入 asks 扫单。maker 假定成交不限制在最优档显示数量；taker 数量足够时用 BBO，否则需一致的深度快照。成交数量为连续数量，本版不进行交易所最小下单金额、数量精度、借币或真实订单合法性验证。

只对模拟 taker 腿额外订阅深度：Binance `depth20@100ms` 与 HL `l2Book`，使用完整前 20 档快照，不将其当增量订单簿。HL 先完成所有 BBO 确认，再订阅深度；深度拒绝不会把下一条 BBO 币种误判为失败。新订阅复用已有 shard，不额外增加 HL 连接。输入依据：[Binance 现货快照文档](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams)、[HL 订阅文档](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)。

模拟独立记录 JSON 解析前的应用层接收 `monotonic_ns`，原 `BookQuote.received_mono` 与超时处理不变。查找 t1/t2 之前最后收到的状态；延迟执行的任务也不会读取目标时刻之后的价格或汇率。`resolution_grace_ms=5` 只是等输入解析与线程交接的结果汇集宽限，不改变 t1/t2。应用层接收戳仍包含此前网络、协议和操作系统缓冲的影响，不能代表撮合引擎的时间。

窗口保存最近 `window_samples=500` 个有效观测，至少 `min_samples=5` 个才评分。深度不足、深度与 BBO 不一致、数据超时、断连和模拟队列溢出记为不可评估；不作为零滑点或成交失败概率。重连会清除对应模拟盘口有效性。配置金额、延迟、费率等变化时取消待处理观测并清空模拟样本，不混合不同场景；只改 Score 权重会立即重排。

费用单位为 bps，按交易所、spot/perp、maker/taker 分别配置，支持负费率。默认值是无折扣费率场景，不查询真实账户等级、手续费币种折扣、质押或返佣；实用前可改为自己的费率假设。HL 默认基准依据：[官方费率表](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)。每个观测单独记录两腿费用，扫单 VWAP 已包含深度成本，净优势不重复扣滑点。

CLI 新列：模拟 N/异常、平均延迟/深度滑点、总滑点均值/P95、费用和 Score。所有价格损耗与费用以 10U（或配置金额）归一成 bps，滑点正值代表成本、负值代表改善。

Score 在当前页满足原列表条件且样本有效的路线中计算相对分位排名。默认 80% 执行成本、20% `min(开仓次数, 平仓次数)`；执行成本为滑点加费用，均值和 P95 各占 50% (`tail_weight`)。成本越低越好；已触发观测的可评估覆盖率还会乘入评分，避免大量数据异常的路线靠少量样本排名靠前。300 秒无新有效模拟样本不再评分。Score 是相对排序指标，不是收益百分比或成交概率。样本不足显示 `—`，排在已评分路线后，沿用原开平次数排序；`score.enabled=false` 恢复原排序。

`data/simulation/observations.jsonl` 保存有场景 ID 的逐次观测和异常原因，按网络日志上限滚动。`data/simulation/state.json` 保存当前配置与各路线聚合结果。重启后重新积累，不从旧场景恢复评分。

可用 `uv run python -m unittest discover -s tests -v` 运行离线模型与流适配测试。

## 注意

当前版本是候选路线筛选器，不是交易系统。公开 API 可用性、市场命名和交易所限频可能变化；启动后应先观察 `data/diagnostics/network_pipeline.jsonl` 与行情新鲜度。资金费率用于显示，不参与排序。
