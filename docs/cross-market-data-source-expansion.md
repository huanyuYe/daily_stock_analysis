# Vibe-Research 跨市场数据源对比与扩展决策

本文核验 X 帖子所介绍的 [Vibe-Research](https://github.com/simonlin1212/Vibe-Research)，对比本项目现有 A 股、港股、美股行情与资讯链路，并记录本轮可落地扩展、暂缓项和后续优先级。

> 核验基线：2026-08-03。Vibe-Research 源码基线为 `3eae02d64462bab04460e210077df614e75e99bb`。免费网页接口、授权条款和字段均可能变化，生产接入前必须重新做线上样本与条款复核。

## 先区分“产品已接入”与“仓库附带工具”

Vibe-Research 的 README 同时描述产品后端和两个随仓库附带的数据工具箱，不能把工具箱中存在的示例代码直接视为产品运行时已消费：

- 产品后端的 A 股数据主要位于 `backend/astock.py`：腾讯行情、东方财富研报/公告/资金与榜单、AkShare 新闻/一致预期、mootdx K 线/F10 等。
- 产品后端的港股/美股数据位于 `backend/gstock.py`，实际只接入东方财富 `push2/push2delay` 行情、搜索和 datacenter 关键财务指标；源码明确说明 Yahoo、SEC 等国外源未并入。
- SEC EDGAR、FINRA、CBOE、Yahoo 财务/期权等能力主要存在于 `global-stock-data/SKILL.md` 工具说明中，不是 Vibe-Research 页面和分析链路的默认运行时能力。
- “12 赛道 108 源”来自 `backend/news_sources.json` 的 RSS/Atom 列表，内容丰富，但其中混合官方机构、公司博客、行业媒体、商业媒体、个人/第三方源和 RSS 转换服务，不能统一赋予同一真实性等级。

## 当前项目与目标项目对比

| 能力 | Vibe-Research 实际产品链路 | 当前项目链路 | 真实性 / 时效性 / 丰富度结论 |
| --- | --- | --- | --- |
| A 股日线与行情 | 腾讯、mootdx；部分页面接口走东方财富 | Efinance、AkShare、Tushare、TickFlow、PyTDX、Baostock、YFinance、腾讯的可配置 fallback；实时行情另有腾讯/Sina/Efinance/AkShare/Tushare 顺序 | 当前项目来源数量、路由、熔断、诊断更完整。两项目的多个“不同库”最终可能同源于东财，不能按库数量当作独立交叉验证。 |
| 港股/美股日线与行情 | 产品后端主要是东财 `push2`，失败转 `push2delay` | YFinance 为历史主路径，AkShare/Longbridge 等补实时和字段；Longbridge 有明确认证与行情权限 | 当前项目更适合生产 fallback。东财 `push2delay` 可作为低置信度研究备份，但不应替换有权限语义的 Longbridge。 |
| 财务与估值 | A 股 AkShare/mootdx/东财；港美股产品后端为东财 datacenter 关键指标 | A 股财报、估值、分红、机构、资金块带来源/时间/状态；港美股有 YFinance/Alpha Vantage 等适配 | 当前项目的证据状态更完整。Vibe 的港美股东财指标可作为字段补充，不构成独立官方核验。 |
| 公司公告 | A 股巨潮/东财；港美股产品后端未接官方申报 | A 股结构化事件优先巨潮；通用搜索把 HKEX/SEC 域名列为官方来源 | A 股已大体覆盖。美股应直接接 SEC EDGAR，港股应直接接 HKEXnews 发行人披露，而不是只靠搜索命中。 |
| 券商研报/一致预期 | A 股东财 reportapi、同花顺一致预期；产品页面已消费研报列表 | 过去只有通用搜索关键词，没有按代码返回的结构化研报元数据 | 信息丰富度存在明确缺口；适合新增，但必须标为卖方观点，不能当公司事实。 |
| A 股资金与交易信号 | 融资融券、大宗交易、股东户数、资金流、龙虎榜、解禁、板块、热度和互动易 | 已有资金流、龙虎榜、板块、解禁和基本面；部分细分项仍不稳定或未进入关键路径 | 可按需求补大宗交易/互动易等，但优先后台缓存，避免挤占在线分析预算。 |
| 产业与宏观资讯 | 108 个 RSS/Atom，近 7 日并发抓取 | 可配置 RSS/Atom/NewsNow 情报池 + 搜索 provider + 来源链接持久化 | Vibe 的源列表可作为候选目录，但需逐源测试、分级、去重和许可复核，不宜整体复制并默认启用。 |
| 美股监管/衍生品 | 工具箱含 SEC、FINRA、CBOE，产品后端未接 | 尚无 SEC 结构化申报、FINRA short-volume、CBOE 期权正式链路 | SEC 是高价值官方扩展；FINRA/CBOE 受使用与再分发边界约束，需先完成合规评估。 |

## 真实性与时效性分级

建议后续统一使用以下证据层级，而不是把“接口能返回 JSON”当作真实性保证：

1. **监管机构 / 交易所 / 公司正式披露**：巨潮、上交所/深交所、HKEXnews、SEC EDGAR。用于确认事实，仍须保留报告期、发布日期和原文链接。
2. **有权限语义的专业数据服务**：Longbridge、Tushare 等。需记录账号权限、行情级别、延迟口径和抓取时间。
3. **门户聚合与公开前端接口**：东方财富、腾讯、新浪、Yahoo 等。适合行情/研究补充，但通常没有面向本项目的 SLA；多个封装库可能高度同源。
4. **媒体、RSS 与卖方观点**：用于发现事件、行业趋势和预期差；必须与正式披露分层，不能单独确认财务事实。

SEC 官方说明其 submissions 与 XBRL JSON API 无需 API Key，申报历史在披露时实时更新，典型延迟分别低于 1 秒和 1 分钟，适合作为美股正式申报主源：[EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)。HKEXnews 提供发行人公告、财务报告和结果公告检索，适合作为港股正式披露主源：[HKEXnews 搜索说明](https://www2.hkexnews.hk/-/media/HKEXnews/Homepage/Listed-Company-Publications/Search-Guide/PredefinedSearchGuide_e.pdf)。Longbridge 的 OpenAPI 行情权限与 App/Web 权限分开，实时等级取决于订阅，不能把“接口成功”直接等同于同一深度和实时性：[Longbridge 行情定价与权限](https://open.longbridge.com/pricing)。

## 本轮已落地：A 股结构化券商研报元数据

新增 `AShareResearchReportService`，按精确六位 A 股代码读取东方财富 reportapi 最近 180 天元数据：

- 只返回标题、发布机构、发布日期、行业、评级变化、EPS 预测和原始 PDF 链接；
- 不下载、不解析、不缓存 PDF 正文，避免把第三方研报内容复制进本地数据层；
- 每条记录固定标记 `source_id=eastmoney_reportapi`、`source_tier=sell_side_aggregator`、`verification_status=single_source_opinion`；
- 未来日期、窗口外记录和重复记录会被拒绝；网络或字段异常返回 `missing`，不阻断原分析；
- 多维情报搜索会优先把该精确代码源用于“机构分析”，失败或无覆盖时继续使用原搜索 provider；
- IntelAgent 新增 `get_research_reports` 只读工具，并明确要求评级与 EPS 预测不得冒充公告或已实现业绩。

2026-08-03 的只读样本检查中，`600519` 返回 35 条记录；最近样本发布日期为 2026-07-23，包含机构、评级和未来三年 EPS 字段。该检查只证明当时接口与字段可用，不构成长期 SLA 或第二来源核验。

## 本轮继续落地：SEC-A、SEC-B、公共 HKEXnews 与精选 RSS

美股新增统一监管披露服务：

- **SEC-A**：通过 SEC 官方 ticker/CIK 映射和 `submissions/CIK##########.json` 获取申报表单、申报/接收时间、报告期、accession number 和原始文件链接。
- **SEC-B**：通过 `companyfacts/CIK##########.json` 提取营收、净利润、资产、负债、股东权益、经营利润、经营现金流和稀释 EPS；每个指标按 `filed_at` 选择截至分析时点已申报的最新值，不使用未来申报，也不跨 concept 求和。
- SEC 请求使用可配置 `SEC_EDGAR_USER_AGENT`，单请求超时受 `REGULATORY_FETCH_TIMEOUT_SEC` 约束；成功缓存 30 分钟，失败短缓存 60 秒。

港股新增公共 HKEXnews Title Search 适配器：先用公开代码前缀检索解析内部 `stockId`，再按日期区间查询发行人披露，规范化发布日期、标题、发行人、文档编号和原文链接。该网页接口没有公开稳定 API 承诺，因此明确记录为 `hkexnews_public_title_search`，不下载/解析 PDF 正文；页面结构变化时返回来源失败状态并让主分析 fail-open。

两市场的记录均标记 `source_tier=official_regulator` 和 `verification_status=official_primary`，同时保留 `as_of`、各来源状态和 warning。传统分析会把结构化证据追加到 `news_context`；Agent 分析会预取到 `regulatory_disclosures`，IntelAgent 也可按需调用 `get_regulatory_disclosures`。

精选 RSS 第一批共 15 个模板，覆盖宏观（Federal Reserve、ECB）、AI/软件（OpenAI、Google Research、DeepMind、Hugging Face、GitHub）、半导体/新能源（Semiconductor Engineering、CnEVPost、pv magazine、Energy-Storage.news）及医药/安全/航天（STAT、Fierce Biotech、Microsoft Security Blog、SpaceNews）。模板默认 `pilot=true`、`auto_enable=false`，不会随自动刷新隐式开启；生产试点设置 `NEWS_INTEL_AUTO_BOOTSTRAP_DEFAULTS=false` 后，只拉取显式启用的这 15 个源，不会额外补齐 8 个基础源。启用后同市场证据不足时可补充 `global` 行业证据，仍限制为最多 6 条上下文。

RSS 接入没有新增数据库迁移或新进程，代码与部署成本低；主要运行成本来自串行 HTTP 请求。按当前 8 秒上限，全部 15 源最坏会显著拉长单次自动刷新，因此生产建议首周只显式启用 4–6 个与关注标的行业直接相关的源，利用现有 60 分钟冷却观察成功率、重复率和延迟后再扩大。信息增益主要是更早捕捉产业政策、技术发布、供应链和临床/安全事件，不提升发行人财务事实的真实性等级。

## 更新后的后续优先级

| 优先级 | 扩展 | 决策 |
| --- | --- | --- |
| P0 | A 股结构化券商研报元数据 | 本轮完成；仅作观点证据。 |
| P1 | SEC EDGAR submissions + XBRL companyfacts | 本轮完成 SEC-A/SEC-B；下一步可补历史 submissions 分片、表单重要性分级和币种/单位冲突诊断。 |
| P1 | HKEXnews 发行人公告 | 本轮完成公共 Title Search 元数据；下一步做中英双版本/联合公告去重、类别编码映射和页面契约监控。 |
| P2 | 精选产业 RSS 模板 | 本轮完成 15 源默认禁用试点；下一步依据线上成功率、重复率和行业命中率决定扩容或淘汰。 |
| P2 | A 股互动易、大宗交易、股东户数后台快照 | 信息丰富，但不进入短预算在线关键路径；先做持久化和时效标记。 |
| 暂缓 | CBOE 期权、FINRA 批量做空数据 | 在商业使用、自动化下载与再分发条款复核前不并入生产默认链路。 |
| 不采用 | 直接复制 Vibe 全量 SKILL 或 108 源并默认启用 | 会放大同源误判、限流、许可与噪声风险，也绕过当前项目已有诊断和证据契约。 |

## 风险与回滚

- 东方财富 reportapi 是门户聚合接口，不是公司、交易所或券商向本项目承诺的官方 API；字段和访问策略可能变化。
- 研报评级天然有利益关系、更新滞后和样本偏差；本功能只改善“信息丰富度”，不提升基础事实的真实性等级。
- SEC 的 ticker/CIK 映射文件由 SEC 明确提示不保证完整或准确；映射失败时不会回退到名称猜测。
- 公共 HKEXnews 是网页检索契约，页面参数或 HTML 结构变化会使适配器降级；它不等同于 HKEX 付费 IIS 实时信息服务。
- RSS 真实性随源而异，试点只用于事件发现和行业上下文，不能替代公司/交易所/监管披露。
- 回滚时关闭 `REGULATORY_DISCLOSURES_ENABLED`、停用试点 RSS 源即可完成运行时降级；代码级回滚可移除监管服务、Agent 工具和 RSS 模板，不涉及数据库迁移、模型路由或既有历史报告。
