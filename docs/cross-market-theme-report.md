# 跨市场主线跟踪报告

该报告独立于逐股盘前、盘中、盘后报告，用于把“美股已发生的产业催化”与“A 股、港股关注标的当天是否真正响应”拆成两次可审计观察。它不覆盖个股估值、持仓成本、买卖点、止损或逐股最终动作，因此“主线强化”与某只股票“观望/减仓”可以同时成立，不构成观点冲突。

## 两轮报告

| 北京时间 | 报告 | 必需输入 | 输出目的 |
| --- | --- | --- | --- |
| 工作日 09:25 | 美股收盘映射 | 新鲜美股盘后报告、美国代理行情；A/HK 盘前报告为补充 | 提出当天待验证的产业主线假设，并映射到当前关注池 |
| 工作日 16:50 | A/HK 收盘验证 | 同日上午 JSON 快照，以及至少一份新鲜 A 股或港股盘后报告 | 用 A/HK 实际收盘涨跌、逐股观点、日内资讯和港股官方披露确认或证伪上午假设 |

两个任务与现有 A/HK/US 逐股任务共用 `/run/lock/daily-stock-analysis-qqbot-active.lock`。主线任务使用 `flock -w 1200`，最多等待现有任务 20 分钟，不会像非阻塞锁那样在冲突时静默跳过；报告自身继续记录 1200 秒交付 SLO，数据字段不会为满足 SLO 被裁剪。

## 数据与判定边界

- 美股代理、A/HK 标的行情使用现有 `DataFetcherManager` fallback，报告保留来源、provider 时间、抓取时间和时间可核验状态。
- A 股和港股盘后阶段同时读取现有大盘复盘中的行业/概念 Top/Bottom 榜单。榜单命中与关注标的同向时作为板块扩散证据；两者反向时最终验证降为“分化”，不会用少数个股替代行业结论。
- 事件证据来自现有本地精选 RSS / NewsNow 资讯池，按主题关键词匹配，并保留原始链接、来源及 `published_at` 或 `fetched_at` 时间口径。未命中不等同于“没有事件”。
- 每条主线指定至少一个 SEC 官方发行人检查标的；映射到港股关注池的发行人同时查询公共 HKEXnews。官方接口失败会明确标为 `failed`，并禁止该主题显示“证据完整”，不会以新闻聚合源替代官方检查。
- 上午只有代理覆盖率至少三分之二时才判断方向。代理均值至少 `+1%` 且至少三分之二上涨为“强化”；均值至多 `-1%` 且至少三分之二下跌为“走弱”；均值绝对值小于 `0.5%` 为“无明显方向”，其他情况为“分化”。
- 下午 A/HK 行情覆盖不足 60% 时直接显示“数据不足”。上午方向与 A/HK 均值至少 `0.5%`、多数标的同向时记为“已验证”；达到反向阈值时记为“已证伪”；其余为“分化 / 待观察”。
- 周一上午允许美股盘后报告与资讯窗口放宽至 72 小时，以覆盖上周五已完成交易时段；其他工作日上午要求美股盘后报告不超过 18 小时。来源超过时限时任务跳过，不推送旧结论。

## 当前主题目录

默认目录为 [`config/cross_market_themes.json`](../config/cross_market_themes.json)，第一版围绕当前关注池配置八条主线：光通信/CPO、存储芯片/高阶封装、半导体设备与材料、AI 云与应用、算力基础设施/液冷散热、电力/核能/电网、锂资源/电池储能、工业自动化/工程机械。

每条配置包含：

- `keywords`：精选资讯匹配词；
- `board_keywords`：现有行业/概念榜单与主线之间的可审计映射词；未配置时沿用 `keywords`；
- `us_proxies`：美股已完成交易时段的产业观察代理，不是推荐买入列表；
- `official_symbols`：需要执行 SEC 官方检查的代表发行人；
- `target_symbols`：与该主线相关、且只有进入“Futu 持仓 + STOCK_LIST”并集后才会出现在报告中的 A/HK 标的。

需要调整关注行业时，复制该 JSON 后设置 `CROSS_MARKET_THEME_CONFIG_PATH`。目录版本和必填字段会在运行前校验，非法配置会直接失败而不是退回不可见默认值。

可选参数：

```dotenv
CROSS_MARKET_THEME_CONFIG_PATH=
CROSS_MARKET_THEME_NEWS_WINDOW_HOURS=36
CROSS_MARKET_THEME_PROXY_REQUEST_INTERVAL_SEC=0.25
CROSS_MARKET_THEME_MAX_NEWS_PER_THEME=2
```

## 手动验证与产物

先使用不推送模式验证数据与报告结构：

```bash
python scripts/run_cross_market_theme_report.py --phase morning --no-push
python scripts/run_cross_market_theme_report.py --phase close --no-push
```

上午产物保存为：

```text
reports/cross_market_theme/morning/theme_YYYYMMDD.md
reports/cross_market_theme/morning/theme_YYYYMMDD.json
```

下午产物保存在对应的 `close/` 目录。JSON 是下午验证使用的证据快照；Markdown 是 QQ 推送正文。上午缺少新鲜美股盘后报告、下午缺少同日上午快照，或下午两地盘后报告均不可用时，脚本返回 `skipped=true` 且不推送。

## systemd 安装

```bash
sudo install -m 0644 scripts/daily-stock-analysis-cross-market-theme@.service /etc/systemd/system/
sudo install -m 0644 scripts/daily-stock-analysis-cross-market-theme-morning.timer /etc/systemd/system/
sudo install -m 0644 scripts/daily-stock-analysis-cross-market-theme-close.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  daily-stock-analysis-cross-market-theme-morning.timer \
  daily-stock-analysis-cross-market-theme-close.timer
```

部署后检查：

```bash
systemctl list-timers --all 'daily-stock-analysis-cross-market-theme-*'
systemctl status daily-stock-analysis-cross-market-theme@morning.service
journalctl -u daily-stock-analysis-cross-market-theme@morning.service -n 100 --no-pager
```

回滚只需禁用两个 timer 并移除对应 unit；现有逐股报告、历史报告和关注池不受影响：

```bash
sudo systemctl disable --now \
  daily-stock-analysis-cross-market-theme-morning.timer \
  daily-stock-analysis-cross-market-theme-close.timer
```
