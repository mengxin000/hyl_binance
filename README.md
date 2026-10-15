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
- 原始行情和样本仅放内存；重启后重新采样。持久化每条路线状态、模拟执行事件和网络诊断于 `data/`。一秒统计循环只计算 μ、σ、分位数与运行范围，不再统计开/平次数或写入 `opportunities.jsonl`；开平计数只来自实时模拟模块。

## 配置

- `config/market.json`：报价币、四项按交易所和市场类型区分的成交额下限、可选例外别名及 HL 特殊永续报价币覆盖。
- `config/basis_spot_spot.json`、`basis_spot_perp.json`、`basis_perp_perp.json`：各类统计窗口、分位数与阈值。
- `config/runtime.json`：采样周期、新鲜度、WebSocket 分片与持久化参数。
- `config/simulation.json`：固定下单金额、两段延迟、数据年龄、样本窗口和 maker/taker 费率。
- `config/score.json`：Profit指数与滑点风险权重、尾部风险权重和样本数量折减。

`quantile_refresh_seconds` 控制每条路线重算 P5/P95 分位数的间隔；修改运行配置后会热加载到已有路线。
`hyperliquid_subscriptions_per_connection` 是 HL 每个 WebSocket shard 的目标订阅数，范围为 1–10；程序按现货/永续分组，且单进程最多打开 10 条 HL WebSocket。若同一币种连续三次在订阅确认阶段断连，会暂时从当前市场集合隔离，避免它反复拖断同 shard 的其他行情；市场集合变化后会重新尝试。

无效 JSON 会保留上一份有效配置，并在终端错误栏显示原因。网络诊断 JSONL 有大小上限并滚动；若诊断队列过载，会丢弃新日志而不阻塞行情循环。

## 固定金额滑点观测

默认 `order_notional_usdt=10`、`exchange_delay_ms=20`、`deal_delay_ms=30`，单位分别为 USDT、ms、ms，以当前 `config/simulation.json` 中的配置为准。模拟在独立后台线程运行，不受一秒统计采样或 REST 刷新等待影响。统计模型通过不可变 `TriggerLevels` 和 `SignalPolicy` 发布每条路线的绝对开线、平线和复位中线；模拟器用逐条有效 BBO 检查阈值穿越。持续越界只触发一次，回到复位中线后重新允许同方向观测。每条路线只允许一个待处理模拟任务。无需达到列表的 2σ 门槛才能积累模拟样本。

在 `t0` 高于绝对开线时产生开方向观测：现货/永续路线的现货腿假定 maker 买入，永续腿 taker 卖出。同类路线默认 Binance maker 买入、HL taker 卖出。低于绝对平线时产生相反方向观测：原 maker 腿卖出、原 taker 腿买入。这是独立开/平方向成本观测，不管理账户库存、不配对完整持仓回合，不将净入场价差当作已实现利润；资金费率暂不计入模拟成本或 Score。

### 自适应触发线

在各自的 `config/basis_*.json` 中配置模式，三类页面可以不同。当前配置默认使用 `sigma`，旧配置省略新增字段时仍使用 `fixed`，保持原固定bps行为。参数均支持热加载。

| `threshold_mode` | 开方向触发条件 | 平方向触发条件 | 复位中线 |
| --- | --- | --- | --- |
| `fixed` | `B > μ + open_threshold_bps` | `B < μ + close_threshold_bps` | 所选窗口μ |
| `sigma` | `B > μ + open_threshold_sigma * σ` | `B < μ - close_threshold_sigma * σ` | 所选窗口μ |
| `quantile` | `B > Q(open_threshold_quantile)` | `B < Q(close_threshold_quantile)` | 所选窗口P50 |

这里 `B`、μ、σ和分位数结果的单位均为bps。分位数结果本身就是绝对基差，不再加μ。分位数模式用中位数复位，因为偏斜分布的μ可能落在选定的两个分位数之外。开方向在 `B <= 中线` 时重新允许触发，平方向在 `B >= 中线` 时重新允许触发。

```json
{
  "threshold_mode": "sigma",
  "threshold_window": "total",
  "open_threshold_sigma": 2.0,
  "close_threshold_sigma": 2.0,
  "open_threshold_quantile": 0.95,
  "close_threshold_quantile": 0.05,
  "threshold_min_samples": 120
}
```

以上字段合并到已有配置，不覆盖其余窗口及筛选参数。改为 `quantile` 即使用P95/P5；例如 `open_threshold_quantile=0.90`、`close_threshold_quantile=0.10` 使用P90/P10。分位数要求 `0 <= close < 0.5 < open <= 1`；两侧sigma倍数均为正，可分别设置为2和1.5。

`threshold_window` 可选 `total`、`long`、`short`，分别使用本路线对应窗口的有效价差样本；样本仍由原统计采样产生，实时触发使用最新已发布的阈值。σ触发线随每轮统计更新；分位数触发线按 `runtime.quantile_refresh_seconds` 刷新（默认15秒），首次就绪和修改相关配置时立即重算。触发分位数与列表展示的 `quantile_low/high` 独立，因此配置P90/P10不会把P5/P95展示列改掉。

自适应模式先积累 `threshold_min_samples=120` 个有效行情统计样本，约一秒采样时至少两分钟，有数据缺口时更久。固定模式保持两样本即可就绪。零σ、重复分位数导致 `平线 < 复位中线 < 开线` 不成立时暂停触发。CLI增加“开线/平线·bps”，显示实际绝对基差线，或“预热N/120”“分布零宽”；表中原μ±2σ仍是总窗统计。

修改模式、所选窗口、该模式的有效参数或预热样本数，会取消对应路线待处理模拟、清空其滑点/edge/Profit/Score样本，并记录 `threshold_rule_reset`，避免混合不同筛选条件。正常随行情移动的μ、σ、分位数不会重置结果；修改当前模式未使用的参数也不会重置。执行日志保存触发时完整阈值快照及 `rule_id`，便于核对信号。

`t1=t0+exchange_delay` 时，以 maker 买入 bid / 卖出 ask 假定全部成交。金额按该时刻的 USDT 价格转换成数量 q，taker 腿使用相同 q。`t2=t1+deal_delay` 时按卖出 bids / 买入 asks 扫单。maker 假定成交不限制在最优档显示数量；taker 数量足够时用 BBO，否则需一致的深度快照。成交数量为连续数量，本版不进行交易所最小下单金额、数量精度、借币或真实订单合法性验证。

只对模拟 taker 腿额外订阅深度：Binance `depth20@100ms` 与 HL `l2Book`，使用完整前 20 档快照，不将其当增量订单簿。HL 先完成所有 BBO 确认，再订阅深度；深度拒绝不会把下一条 BBO 币种误判为失败。新订阅复用已有 shard，不额外增加 HL 连接。输入依据：[Binance 现货快照文档](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams)、[HL 订阅文档](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)。

模拟独立记录 JSON 解析前的应用层接收 `monotonic_ns`，原 `BookQuote.received_mono` 与超时处理不变。查找 t1/t2 之前最后收到的状态；延迟执行的任务也不会读取目标时刻之后的价格或汇率。`resolution_grace_ms=5` 只是等输入解析与线程交接的结果汇集宽限，不改变 t1/t2。应用层接收戳仍包含此前网络、协议和操作系统缓冲的影响，不能代表撮合引擎的时间。

窗口保存最近 `window_samples=500` 次已完成模拟，包含有效观测和不可评估观测，待处理任务不进入窗口。开、平各至少 `min_samples=5` 个有效观测才评分，窗口容量须至少为 `2 * min_samples`。深度不足、深度与 BBO 不一致、数据超时、断连和模拟队列溢出导致的待处理任务取消记为不可评估；不作为零滑点或成交失败概率。重连会清除对应模拟盘口有效性。配置金额、延迟、费率等变化时取消待处理观测并清空模拟样本，不混合不同场景；只改 Score 权重会立即重排。

费用单位为 bps，按交易所、spot/perp、maker/taker 分别配置，支持负费率。默认值是无折扣费率场景，不查询真实账户等级、手续费币种折扣、质押或返佣；实用前可改为自己的费率假设。HL 默认基准依据：[官方费率表](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)。每个观测单独记录两腿费用，扫单 VWAP 已包含深度成本，净优势不重复扣滑点。

CLI 模拟列：模拟开/平/异常、平均延迟/深度滑点、总滑点均值/P95、费用、开edge、平edge、Profit指数和 Score。开/平次数是完成两腿估算的有效观测数，异常是同一窗口内不可评估次数；三者之和不超过 `window_samples`。异常不进入均值与分位数。所有价格损耗与费用以 10U（或配置金额）归一成 bps，滑点正值代表成本、负值代表改善。

### 净edge与Profit指数

每次观测的净edge为 `net_edge_bps = [s * q * (taker_vwap - maker_price) - maker_fee - taker_fee] / order_notional_usdt * 10000`；开方向 `s=+1`，平方向 `s=-1`。价格统一为 USDT，已经使用延迟后的 maker 成交价和 taker 扫单均价，因此不再扣一次滑点。

开edge、平edge分别是对应方向有效观测的 `net_edge_bps` 算术平均。每次下单名义金额相同，按等额次数平均等价于按名义金额加权，不按基础币数量加权。

```text
配对次数 = min(有效开次数, 有效平次数)
配对净edge = 平均开edge + 平均平edge
Profit指数 = 配对次数 * 配对净edge
```

例如开20次、平15次，开edge=12bps、平edge=-3bps，则 Profit指数=15*(12-3)=135。它是当前窗口的累计净edge指数，不是 USDT 利润、年化收益率或逐笔已实现利润；不按持仓先后顺序配对，也不计入资费。缺少任一方向时显示 `—`，真实的零值显示 `+0.00`。窗口滑动后次数、均值与指数均可变化。

### Score

Score 只使用模拟统计，在当前页满足原列表筛选条件的路线中计算。开平各须满足最低样本数，不按历史样本年龄暂停评分。样本不足或评分关闭时显示 `—`；合格但 Profit指数不大于0的路线 Score=0，避免只靠低滑点获得高分。滚动窗口移除旧样本或修改场景/触发规则重置结果后，仍可能重新不足最低样本数。模拟所用实时行情的新鲜度校验独立生效。

对其余合格且 Profit指数为正的路线，按本页进行相对排名：Profit指数越高越好，滑点风险越低越好。排名采用并列平均名次归一化到0~1；只有一个合格正收益路线时两个排名均为0.5。

```text
滑点风险 = (1-tail_weight)*平均总滑点 + tail_weight*P95总滑点
覆盖率 = 有效观测数 / 已完成模拟数                         # 同一滚动窗口
样本折减 = min(1, 配对次数/confidence_paired_samples)
Score = 100 * 覆盖率 * 样本折减
        * (profit_weight*Profit排名 + slippage_weight*风险排名)
        / (profit_weight + slippage_weight)
```

手续费已经进入净edge及Profit，滑点风险不再加入手续费。默认 Profit排名占60%、风险排名占40%，风险中的均值/P95各占50%；配对次数达到30时不再做样本折减。该折减是样本数量权重，不是统计置信区间。Score是相对排序指标，不是收益率或成交概率。

`config/score.json` 可热加载参数：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `enabled` | `true` | 是否使用Score排序 |
| `profit_weight` | `0.6` | Profit指数排名权重 |
| `slippage_weight` | `0.4` | 滑点风险排名权重 |
| `tail_weight` | `0.5` | 风险中P95所占比例，其余为平均滑点 |
| `confidence_paired_samples` | `30` | 样本折减达到1所需的配对次数 |

排序先使用Score，再以Profit指数、配对次数、有效样本总数打破并列；未评分路线排在后面，使用同样的Profit排序。关闭Score后直接按Profit指数排序；关闭模拟后按路线名称稳定排序。一秒开/平计数及旧的 `opportunity_weight` 配置已移除。

### 模拟文件记录

- `data/simulation/observations.jsonl`：逐行追加的模拟事件日志，每行一个JSON对象。有效事件 `execution_observation` 记录路线、开平方向、触发基差与阈值快照、两段目标时间、选用报价时间、模拟数量、参考价、maker成交价、taker扫单均价、两腿手续费、延迟/深度/总滑点、净edge和深度来源。`unavailable_observation` 记录无法评估的路线、方向及原因；`scenario_reset`、`threshold_rule_reset` 记录配置切换。
- `data/simulation/observations.1.jsonl`：上一份滚动日志。当前文件达到 `runtime.network_log_max_bytes`（默认20,000,000字节，约20MB）后转为此文件，只保留一个旧备份，后续轮转会覆盖。
- `data/simulation/state.json`：按 `runtime.persist_interval_seconds`（默认60秒，主循环繁忙时可延后）覆盖写入的最新汇总快照，包括模拟配置、Score配置、队列丢弃数，以及各路线的开平/异常数、滑点均值与P95、开平平均edge、Profit指数、实际触发线和待处理状态。Score数值在CLI按当前页面即时计算，快照保存它依赖的聚合指标及配置。

日志中 `ts` 是事件日志生成时的Unix秒时间；`signal_ns`、`maker_target_ns`、`taker_target_ns`、`maker_quote_ns`、`taker_quote_ns` 使用本机单调时钟纳秒，用于检查目标延迟和报价年龄，不应直接转换为日历时间。`scenario_id` 是模拟配置指纹，阈值快照中的 `rule_id` 是触发规则指纹，均不是唯一运行编号。日志可跨多次运行追加，历史版本可能缺少新字段。

这些文件用于追溯执行估算及排查数据异常；没有保存全量逐条BBO或完整盘口历史。重启后重新积累样本，不从 `state.json` 或旧日志恢复评分。

可用 `uv run python -m unittest discover -s tests -v` 运行离线模型与流适配测试。

## 注意

当前版本是候选路线筛选器，不是交易系统。公开 API 可用性、市场命名和交易所限频可能变化；启动后应先观察 `data/diagnostics/network_pipeline.jsonl` 与行情新鲜度。资金费率用于显示，不参与排序。
