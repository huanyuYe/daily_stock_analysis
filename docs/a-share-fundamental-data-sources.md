# A 股财报、事件与策略数据源决策

本文记录 A 股 MVP 在分析与策略阶段实际消费的数据、免费 fallback、已知缺口，以及后续付费数据源的积分门槛和接入状态。目标是避免把“项目存在某个 Token 配置”误解为“对应财务接口已经进入分析链路”。

> 基线日期：2026-07-29。免费网页接口和 Tushare 权限规则均可能调整；升级前应重新核对官方页面和线上样本。

## 当前结论

- 默认无需付费 Token，即可完成 A 股核心财报、估值、个股资金流、龙虎榜、板块榜、官方公告、个股新闻和未来 30 日限售解禁的分析输入。
- 核心财报优先使用 AkShare 封装的东方财富单股财务指标，失败后回退同花顺财务摘要；资金流优先使用项目已安装的 efinance 历史资金流，避免东财 AkShare 资金流接口连接失败时拖垮总预算。
- 财报与资金流均携带 `source_id`、`as_of` 和 `retrieved_at`。当前免费财报只有单源校验，因此明确标记 `verification_status=single_source`，不宣称双源核验。
- 8 秒基本面总预算内，核心财报和当日策略信号优先；业绩预告、业绩快报和机构持仓是短预算 best-effort 补充项，超时不会丢弃已成功的财报、资金流、龙虎榜或板块数据。
- 当基本面子块覆盖质量低于 50 分时，系统不再保留基本面信号权重，并对剩余信号重新归一化，避免“数据失败但基本面仍贡献 10%–20%”。
- 当前 `TUSHARE_TOKEN` 已用于项目其他行情能力，但下表中的 Tushare 财务、资金流、股东和质押接口尚未接入这条基本面聚合链路。购买积分后仍需完成接口适配与双源核验，不能仅配置 Token 就视为升级完成。

## 免费链路

| 策略输入 | 首选免费接口 | 免费 fallback | 当前用途与边界 |
| --- | --- | --- | --- |
| 估值 | 已获取的实时行情 PE/PB/总市值/流通市值 | 现有实时行情源顺序 | 复用同一份实时报价，不再重复请求 |
| 核心财报 | AkShare `stock_financial_analysis_indicator_em` | `stock_financial_abstract_new_ths`、旧版财务摘要 | 营收、归母净利润、营收同比、利润同比、ROE、毛利率、负债率、流动/速动比率、每股经营现金流及同比 |
| 个股资金流 | efinance `stock.get_history_bill` | AkShare `stock_individual_fund_flow`、`stock_main_fund_flow` | 最新日主力净流入及 5/10 日累计；沪深市场路由分开处理 |
| 龙虎榜 | AkShare 东方财富龙虎榜统计 | 东方财富龙虎榜明细/机构买卖统计 | 近 20 日是否上榜、次数和最近日期 |
| 板块表现 | 现有 DataFetcherManager 板块榜 fallback | AkShare、Tushare、efinance 等既有源 | 行业/概念强弱与个股关联板块共同参与策略 |
| 官方公告 | AkShare 巨潮资讯公告 | 无；失败时保留其他事件源 | 官方来源优先，按策略新闻窗口做时间硬过滤 |
| 个股新闻 | AkShare 东方财富个股新闻 | 既有通用新闻搜索 | 结构化事件分类、方向、重要性和事件分 |
| 限售解禁 | AkShare `stock_restricted_release_queue_sina` | 公告/新闻中出现的解禁事件 | 未来 30 日事件；解禁流通市值达到 5 亿元时标记“大额”风险 |
| 业绩预告/快报 | AkShare 东方财富季度全市场接口 | 官方公告事件中的财报线索 | 请求时只给短预算；接口慢或限流时 fail-open |
| 机构/股东 | AkShare 机构持股、十大股东和股东户数 | 无 | 请求时短预算补充；当前免费全市场接口可能超时 |

AkShare 的股票数据接口与字段以其[官方股票数据文档](https://akshare.akfamily.xyz/data/stock/stock.html)为准。免费接口依赖公开网页，可能受限流、字段变更和网络区域影响，不构成稳定性承诺。

## 当前策略实际消费

### 财报和成长质量

分析 Prompt 会区分以下口径：

- `operating_cash_flow`：经营活动现金流净额，只有上游返回绝对额时才填写；
- `operating_cash_flow_per_share`：每股经营现金流，不能冒充绝对额；
- `operating_cash_flow_yoy`：经营现金流同比，用于判断利润与现金流方向是否背离；
- 营收/归母净利润及同比、ROE、毛利率、资产负债率参与成长质量和风险判断；
- 报告期、来源和核验状态随数据进入 Prompt，模型不得把旧报告期或单源结果写成已确认事实。

### 新闻和事件

普通分析、事件驱动策略和 RiskAgent 均优先消费结构化 A 股事件，再使用通用网页搜索补充。事件包含：

- 官方公告、媒体新闻和结构化未来事件的来源层级；
- 发布时间与未来事件日期；
- `earnings`、`ownership_change`、`regulatory_risk` 等事件类型；
- `positive`、`negative`、`uncertain`、`neutral` 方向；
- 重要性、事件分和高重要性负面事件计数。

### 数据质量和信号权重

`valuation/growth/earnings/institution/capital_flow/dragon_tiger/boards` 按各自真实状态计分，不再因为父级结构存在就统一记 75 分。基本面质量低于 50 分或整体不可用时：

1. `signal_attribution.fundamentals` 设为 0；
2. 其余有效信号按原比例重新归一化；
3. 报告数据限制明确显示 `fundamentals: partial` 或对应失败状态。

## 仍未完全覆盖的字段

| 缺口 | 当前降级行为 | 对策略的影响 | 免费阶段建议 |
| --- | --- | --- | --- |
| 经营现金流绝对额 | 保留 `null`，使用每股值和同比，不做伪换算 | 无法直接计算净利润/经营现金流绝对额比值 | 后台低频拉完整现金流量表并缓存；不得放入 8 秒在线关键路径 |
| 财报第二独立来源核验 | 标记 `single_source` | 单源字段错误时只能降低置信度 | 保留 EM→THS fallback；需要双源时再接 Tushare |
| 机构持仓与十大流通股东稳定快照 | 超时标记 `failed/not_supported` | 机构增减仓信号可能缺失 | 改为盘后后台抓取并持久化，在线分析读取最近成功快照 |
| 业绩预告/快报稳定快照 | 超时后仍保留正式财报和公告事件 | 预期差判断可能少一条结构化数字来源 | 官方公告事件作为免费兜底；后台按报告季更新 |
| 主营业务分部 | 当前不进入基本面 Prompt | 无法精确拆分收入/利润暴露 | 有明确需求时接入 `fina_mainbz` 并增加单位/币种契约 |
| 股权质押统计 | 仅在公告/新闻命中时进入风险 | 无法形成统一质押率阈值 | 高杠杆/控制权风险策略启用前接 `pledge_stat` |
| 行业财务分位数 | 当前只有个股财报和板块涨跌 | 估值和 ROE 缺少同行业分位 | 先建立行业分类映射与本地横截面缓存，再考虑付费源 |

## Tushare 积分与付费升级决策

以下门槛来自 Tushare 官方接口文档，表示接口积分权限，不等同于人民币价格。积分获取规则和付费报价可能变化，购买前必须在官方页面复核。

| 候选接口 | 官方积分门槛 | 可补能力 | 当前代码接入 | 建议优先级 |
| --- | ---: | --- | --- | --- |
| `share_float` | 120；更高频权限通常要求 5000 | 限售解禁日历的独立来源 | 未接入基本面链路 | P3；免费新浪接口已能工作 |
| `forecast` | 2000；季度全量 VIP 约 5000 | 业绩预告结构化数字 | 未接入 | P1；预期差策略的重要补强 |
| `cashflow` | 2000；VIP 约 5000 | 经营现金流绝对额、完整现金流量表 | 未接入 | P1；解决当前最明确财报字段缺口 |
| `fina_indicator` | 2000；VIP 约 5000 | 财务指标第二独立来源 | 未接入 | P1；用于双源核验 |
| `moneyflow` | 2000 | 个股资金流第二来源 | 未接入 | P2；efinance 免费链路已可用 |
| `top10_floatholders` | 2000；5000 可提高频率 | 十大流通股东与机构变动 | 未接入 | P2；需配合后台快照 |
| `pledge_stat` | 2000 | 股权质押比例与控制权风险 | 未接入 | P2；风险策略补强 |
| `fina_mainbz` | 2000；VIP 约 5000 | 主营业务分部收入和利润 | 未接入 | P3；先定义分部字段契约 |
| `daily_basic` | 2000；5000 可放宽总量限制 | 估值、换手率和市值稳定源 | 项目其他路径已有部分能力，未用于本次双源财报核验 | P3；现有实时估值已可用 |

官方依据：

- [注册与积分说明](https://tushare.pro/document/1?doc_id=450)
- [业绩预告 `forecast`](https://tushare.pro/document/2?doc_id=45)
- [现金流量表 `cashflow`](https://tushare.pro/document/2?doc_id=44)
- [财务指标 `fina_indicator`](https://tushare.pro/document/2?doc_id=79)
- [个股资金流 `moneyflow`](https://tushare.pro/document/2?doc_id=170)
- [十大流通股东 `top10_floatholders`](https://tushare.pro/document/2?doc_id=62)
- [限售解禁 `share_float`](https://tushare.pro/document/2?doc_id=160)
- [股权质押 `pledge_stat`](https://tushare.pro/document/2?doc_id=110)
- [主营业务构成 `fina_mainbz`](https://tushare.pro/document/2?doc_id=81)
- [每日指标 `daily_basic`](https://tushare.pro/document/2?doc_id=32)

## 推荐升级顺序

1. 免费版本先持续运行，观察 20–50 个真实分析批次中 `institution`、`earnings` 和 `cashflow` 缺失率。
2. 若经营现金流绝对额和双源核验成为决策阻塞，优先评估 2000 积分档的 `cashflow + fina_indicator + forecast`。
3. 接入付费接口时使用 `TUSHARE_TOKEN` 环境变量，不把 Token、账号或响应原文写入仓库。
4. 新适配必须保留 `source_id/as_of/retrieved_at/verification_status`，并保存双源一致、冲突或仅单源可用的状态；冲突时 fail closed，不用 fallback 覆盖差异。
5. 完成线上双源样本、超时降级和回归测试后，才能把 `verification_status` 从 `single_source` 升级为 `verified`。

## 成本决策所需的下一批证据

在决定购买积分前，建议从生产分析记录按周统计：

- 各基本面子块 `ok/partial/failed/not_supported` 占比；
- 财报报告期落后当前最近报告季的比例；
- 业绩预告、机构持仓和绝对现金流缺失是否真正改变最终动作；
- 免费接口的 P50/P95 耗时、错误类型和 fallback 命中率；
- 结构化事件对风险覆盖和后验结果的实际贡献。

只有当缺失数据真实影响策略动作或风险边界时，再按上表购买和接入，避免为“字段看起来更全”承担长期成本。
