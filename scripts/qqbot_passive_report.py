#!/usr/bin/env python3
"""Serve cached A-share reports to a passive QQ group command.

The command prints one compact, coverage-checked report to stdout. Hermes may
split that output into multiple passive replies, so this module never calls an
LLM or a messaging API itself.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional

from src.schemas.decision_action import normalize_decision_action


DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_CHARS = 12000
DEFAULT_ANALYSIS_SERVICE = "daily-stock-analysis.service"
MAX_QQ_PASSIVE_REPLIES = 5
QQ_MESSAGE_CHARS = 4000

_STOCK_CODE_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,19}"

_SUMMARY_RE = re.compile(
    rf"^\s*\S*\s*\*\*(?P<name>.+?)\((?P<code>{_STOCK_CODE_PATTERN})\)\*\*:\s*"
    r"(?P<action>[^|]+)\|\s*(?:评分|信心)\s*(?P<score>[^|]+)\|\s*"
    r"(?P<trend>.+?)\s*$",
    re.MULTILINE,
)
_DETAIL_HEADING_RE = re.compile(
    rf"^##\s+\S*\s*(?P<name>.+?)\s+\((?P<code>{_STOCK_CODE_PATTERN})\)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class StockDigest:
    name: str
    code: str
    action: str
    score: str
    trend: str
    conclusion: str
    sentiment: str
    earnings: str
    latest: str
    empty_position: str
    held_position: str
    quote: str
    technical: str
    guardrail: str
    watch: str
    plan: str
    position: str
    risk: str
    bullish: str
    bearish: str
    sectors: str


def _report_files(reports_dir: Path) -> Iterable[Path]:
    if not reports_dir.is_dir():
        return ()
    return (
        path
        for path in reports_dir.glob("*.md")
        if path.is_file() and not path.is_symlink()
    )


def cleanup_reports(
    reports_dir: Path,
    *,
    retention_days: int,
    now: Optional[datetime] = None,
) -> list[Path]:
    """Delete Markdown reports whose mtime is older than the retention window."""
    current = now or datetime.now()
    cutoff = current - timedelta(days=retention_days)
    removed: list[Path] = []
    for path in _report_files(reports_dir):
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        if modified < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def find_latest_report(
    reports_dir: Path,
    *,
    retention_days: int,
    now: Optional[datetime] = None,
) -> Optional[Path]:
    """Return the newest aggregate report still inside the retention window."""
    current = now or datetime.now()
    cutoff = current - timedelta(days=retention_days)
    candidates = [
        path
        for path in _report_files(reports_dir)
        if path.name.startswith("report_")
        and datetime.fromtimestamp(path.stat().st_mtime) >= cutoff
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def _clean_markdown(value: str) -> str:
    value = re.sub(r"[*_`>#]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _clip(value: str, limit: int) -> str:
    value = _clean_markdown(value)
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip("，。；; ") + "…"


def _extract_field(section: str, label: str) -> str:
    match = re.search(
        rf"^\s*\*\*[^*\n]*{re.escape(label)}\*\*:\s*(.+?)\s*$",
        section,
        re.MULTILINE,
    )
    return _clean_markdown(match.group(1)) if match else "未提供"


def _extract_conclusion(section: str) -> str:
    match = re.search(
        r"^\s*>\s*\*\*一句话决策\*\*:\s*(.+?)\s*$",
        section,
        re.MULTILINE,
    )
    return _clean_markdown(match.group(1)) if match else "未提供"


def _extract_risk(section: str) -> str:
    match = re.search(
        r"\*\*数据限制\*\*:\s*\n(?P<body>.*?)(?=\n###|\n---|\Z)",
        section,
        re.DOTALL,
    )
    if not match:
        return "未提供"
    bullets = re.findall(r"^\s*-\s+(.+?)\s*$", match.group("body"), re.MULTILINE)
    useful = [
        _clean_markdown(item)
        for item in bullets
        if not re.match(r"^(quote|technical)\s*:", _clean_markdown(item))
    ]
    return "；".join(useful[:2]) if useful else "未提供"


def _extract_table_row(section: str, key: str) -> str:
    lines = section.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|") or key not in line:
            continue
        cells = [_clean_markdown(cell) for cell in line.strip().strip("|").split("|")]
        if len(cells) == 2 and key in cells[0]:
            return cells[1]
        column = next(
            (position for position, cell in enumerate(cells) if key in cell),
            None,
        )
        if column is None:
            continue
        for candidate in lines[index + 1:]:
            if not candidate.lstrip().startswith("|"):
                break
            candidate_cells = [
                _clean_markdown(cell)
                for cell in candidate.strip().strip("|").split("|")
            ]
            if all(not (set(cell) - {"-", ":"}) for cell in candidate_cells):
                continue
            if column < len(candidate_cells):
                return candidate_cells[column]
    return "未提供"


def _extract_bullets(section: str, heading: str, limit: int = 2) -> str:
    match = re.search(
        rf"\*\*{re.escape(heading)}\*\*:\s*\n(?P<body>.*?)(?=\n\*\*|\n###|\n---|\Z)",
        section,
        re.DOTALL,
    )
    if not match:
        return "未提供"
    bullets = re.findall(r"^\s*-\s+(.+?)\s*$", match.group("body"), re.MULTILINE)
    return "；".join(_clean_markdown(item) for item in bullets[:limit]) or "未提供"


def _extract_subsection(section: str, heading: str) -> str:
    match = re.search(
        rf"^###\s+\S*\s*{re.escape(heading)}\s*$"
        r"(?P<body>.*?)(?=^###\s|\n---|\Z)",
        section,
        re.MULTILINE | re.DOTALL,
    )
    return match.group("body") if match else section


def _extract_market_quote(section: str) -> str:
    quote_section = _extract_subsection(section, "当日行情")
    fields = []
    for label in (
        "当前价",
        "涨跌幅",
        "最高",
        "最低",
        "量比",
        "换手率",
        "行情来源",
    ):
        value = _extract_table_row(quote_section, label)
        if value != "未提供":
            fields.append(f"{label}{value}")
    return "｜".join(fields) if fields else "未提供"


def _extract_technical(section: str) -> str:
    technical_section = _extract_subsection(section, "数据透视")
    metrics = []
    for label in ("MA5", "MA10", "MA20", "乖离率(MA5)", "支撑位", "压力位"):
        value = _extract_table_row(technical_section, label)
        if value != "未提供":
            metrics.append(f"{label} {value}")
    arrangement = _extract_field(technical_section, "均线排列")
    volume = _extract_field(technical_section, "成交量")
    structure = "未提供"
    if "弱势多头修复" in arrangement:
        structure = "弱势多头修复"
    elif "强势空头排列" in arrangement:
        structure = "强势空头排列"
    elif "空头排列" in arrangement:
        structure = "空头排列"
    elif "多头排列" in arrangement:
        structure = "未形成完整多头排列"
    trend_match = re.search(r"趋势强度:\s*([^|；\n]+)", arrangement)
    if trend_match:
        structure += f"，趋势强度 {_clean_markdown(trend_match.group(1))}"
    parts = []
    if metrics:
        parts.append("，".join(metrics))
    if structure != "未提供":
        parts.append(f"均线结构 {structure}")
    if volume != "未提供":
        parts.append(volume)
    return "；".join(parts) if parts else "未提供"


def _extract_guardrail(section: str) -> str:
    match = re.search(
        r"\|\s*行动窗口\s*\|.*?\n\|[-|:\s]+\|\s*\n"
        r"\|\s*(?P<window>.*?)\s*\|\s*(?P<action>.*?)\s*\|"
        r"\s*(?P<next>.*?)\s*\|",
        section,
        re.DOTALL,
    )
    if not match:
        return "未提供"
    return (
        f"{_clean_markdown(match.group('window'))}："
        f"{_clean_markdown(match.group('action'))}；"
        f"下次检查 {_clean_markdown(match.group('next'))}"
    )


def _action_bucket(action: str) -> str:
    normalized = normalize_decision_action(action)
    if normalized in {"buy", "add"}:
        return "buy"
    if normalized in {"reduce", "sell"}:
        return "sell"
    return "watch"


def _action_icon(action: str) -> str:
    normalized = normalize_decision_action(action)
    if normalized in {"buy", "add"}:
        return "🟢"
    if normalized in {"reduce", "sell"}:
        return "🔴"
    if normalized in {"avoid", "alert"}:
        return "⚠️"
    if normalized == "hold":
        return "🟡"
    return "⚪"


def _extract_plan(section: str, *, action: str) -> str:
    plan_section = _extract_subsection(section, "作战计划")
    points = []
    bucket = _action_bucket(action)
    if bucket == "buy":
        labels = (
            ("理想买入点", "理想"),
            ("次优买入点", "次优"),
            ("止损位", "止损"),
            ("目标位", "目标"),
        )
    elif bucket == "sell":
        labels = (
            ("止损位", "风险线"),
            ("目标位", "退出参考"),
        )
    else:
        labels = (
            ("理想买入点", "条件入场"),
            ("次优买入点", "次级条件"),
            ("止损位", "持仓风控"),
            ("目标位", "持仓目标"),
        )
    for source_label, short_label in labels:
        value = _extract_table_row(plan_section, source_label)
        if value != "未提供":
            prices = re.findall(r"-?\d+(?:\.\d+)?元", value)
            concise = " → ".join(dict.fromkeys(prices[:2]))
            points.append(f"{short_label}：{concise or value}")
    return "；".join(points) if points else "未提供"


def _extract_sectors(section: str) -> str:
    match = re.search(
        r"^###\s+\S*\s*关联板块\s*$\s*(?P<body>.*?)(?=\n---|\Z)",
        section,
        re.MULTILINE | re.DOTALL,
    )
    return _clean_markdown(match.group("body")) if match else "未提供"


def _parse_digests(content: str) -> list[StockDigest]:
    summaries = list(_SUMMARY_RE.finditer(content))
    if not summaries:
        raise ValueError("报告摘要中未找到任何股票代码")

    detail_matches = list(_DETAIL_HEADING_RE.finditer(content))
    sections: dict[str, str] = {}
    for index, match in enumerate(detail_matches):
        end = (
            detail_matches[index + 1].start()
            if index + 1 < len(detail_matches)
            else len(content)
        )
        sections[match.group("code")] = content[match.start():end]

    expected_codes = [match.group("code") for match in summaries]
    missing = [code for code in expected_codes if code not in sections]
    if missing:
        raise ValueError("报告缺少标的详情：" + "、".join(missing))

    return [
        StockDigest(
            name=_clean_markdown(match.group("name")),
            code=match.group("code"),
            action=_clean_markdown(match.group("action")),
            score=_clean_markdown(match.group("score")),
            trend=_clean_markdown(match.group("trend")),
            conclusion=_extract_conclusion(sections[match.group("code")]),
            sentiment=_extract_field(sections[match.group("code")], "舆情情绪"),
            earnings=_extract_field(sections[match.group("code")], "业绩预期"),
            latest=_extract_field(sections[match.group("code")], "最新动态"),
            empty_position=_extract_table_row(
                sections[match.group("code")],
                "空仓者",
            ),
            held_position=_extract_table_row(
                sections[match.group("code")],
                "持仓者",
            ),
            quote=_extract_market_quote(sections[match.group("code")]),
            technical=_extract_technical(sections[match.group("code")]),
            guardrail=_extract_guardrail(sections[match.group("code")]),
            watch=_extract_bullets(
                sections[match.group("code")],
                "观察条件",
            ),
            plan=_extract_plan(
                sections[match.group("code")],
                action=_clean_markdown(match.group("action")),
            ),
            position=_extract_field(
                sections[match.group("code")],
                "仓位建议",
            ),
            risk=_extract_risk(sections[match.group("code")]),
            bullish=_extract_field(
                sections[match.group("code")],
                "最强看多信号",
            ),
            bearish=_extract_field(
                sections[match.group("code")],
                "最强看空信号",
            ),
            sectors=_extract_sectors(sections[match.group("code")]),
        )
        for match in summaries
    ]


def _render_digests(
    digests: list[StockDigest],
    *,
    generated_at: str,
    field_limit: int,
    report_date: str,
    market_status: str,
) -> str:
    buckets = [_action_bucket(item.action) for item in digests]
    buy_count = buckets.count("buy")
    wait_count = buckets.count("watch")
    sell_count = buckets.count("sell")
    lines = [
        f"# 🎯 {report_date} 决策仪表盘",
        "",
        (
            f"> 共分析 **{len(digests)}** 只股票 | "
            f"🟢买入:{buy_count} 🟡观望:{wait_count} 🔴卖出:{sell_count}"
        ),
        f"市场状态：{market_status}",
        "",
        "## 📊 分析结果摘要",
        "",
    ]
    for item in digests:
        icon = _action_icon(item.action)
        lines.append(
            f"{icon} **{item.name}({item.code})**: {item.action}"
            f" | 评分 {item.score} | {item.trend}"
        )
    lines.extend(["", "---"])

    for index, item in enumerate(digests, start=1):
        icon = _action_icon(item.action)
        lines.extend(
            [
                "",
                f"## {icon} {item.name} ({item.code}) [{index}/{len(digests)}]",
                "",
                "### 📰 重要信息速览",
                f"舆情情绪：{_clip(item.sentiment, field_limit)}",
                f"业绩预期：{_clip(item.earnings, field_limit)}",
                f"最新动态：{_clip(item.latest, field_limit)}",
                "",
                "### 📌 核心结论",
                f"**{item.action} | {item.trend} | 评分 {item.score}**",
                f"一句话决策：{_clip(item.conclusion, field_limit)}",
                f"空仓者：{_clip(item.empty_position, field_limit)}",
                f"持仓者：{_clip(item.held_position, field_limit)}",
                "",
                "### 📈 行情与技术",
                f"行情：{_clip(item.quote, field_limit)}",
                f"技术：{item.technical}",
                "",
                "### 🛡️ 决策护栏与作战计划",
                f"护栏：{_clip(item.guardrail, field_limit)}",
                f"观察：{_clip(item.watch, field_limit)}",
                f"点位：{item.plan}",
                f"仓位：{_clip(item.position, field_limit)}",
                f"数据限制：{_clip(item.risk, field_limit)}",
                "",
                "### 🎯 信号归因",
                f"最强看多：{_clip(item.bullish, field_limit)}",
                f"最强看空：{_clip(item.bearish, field_limit)}",
                f"关联板块：{_clip(item.sectors, field_limit)}",
            ]
        )
    lines.extend(
        [
            "",
            "---",
            f"生成时间：{generated_at}｜完整报告在服务器保留 7 天。",
        ]
    )
    return "\n".join(lines)


def _validate_coverage(message: str, digests: list[StockDigest]) -> None:
    missing = [item.code for item in digests if item.code not in message]
    if missing:
        raise ValueError("QQ 消息缺少标的：" + "、".join(missing))
    if len(message) > MAX_QQ_PASSIVE_REPLIES * QQ_MESSAGE_CHARS:
        raise ValueError("QQ 消息超过 5 条被动回复容量")


def build_qq_summary(report_path: Path, *, max_chars: int) -> str:
    """Build a compact report that contains every analyzed stock."""
    content = report_path.read_text(encoding="utf-8").strip()
    digests = _parse_digests(content)
    generated_at = datetime.fromtimestamp(report_path.stat().st_mtime).strftime(
        "%Y-%m-%d %H:%M"
    )
    date_match = re.search(r"^#\s+\S*\s*(\d{4}-\d{2}-\d{2})", content)
    report_date = date_match.group(1) if date_match else generated_at[:10]
    status_match = re.search(r"^市场状态：\s*(.+?)\s*$", content, re.MULTILINE)
    market_status = (
        _clean_markdown(status_match.group(1))
        if status_match
        else "市场状态未提供"
    )
    hard_limit = min(
        max_chars,
        MAX_QQ_PASSIVE_REPLIES * QQ_MESSAGE_CHARS,
    )
    for field_limit in (180, 140, 100, 70, 40):
        message = _render_digests(
            digests,
            generated_at=generated_at,
            field_limit=field_limit,
            report_date=report_date,
            market_status=market_status,
        )
        if len(message) <= hard_limit:
            _validate_coverage(message, digests)
            return message
    raise ValueError(
        f"紧凑报告仍超过消息上限（{hard_limit} 字），未发送部分结果"
    )


def _service_is_active(service_name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", service_name],
        check=False,
        timeout=10,
    )
    return result.returncode == 0


def _start_service(service_name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "start", "--no-block", service_name],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0


def handle_report_request(
    reports_dir: Path,
    *,
    retention_days: int,
    max_chars: int,
    service_name: str,
    service_is_active: Callable[[str], bool] = _service_is_active,
    start_service: Callable[[str], bool] = _start_service,
) -> str:
    """Return a cached report or start one background analysis run."""
    cleanup_reports(reports_dir, retention_days=retention_days)
    latest = find_latest_report(reports_dir, retention_days=retention_days)
    if latest is not None:
        try:
            return build_qq_summary(latest, max_chars=max_chars)
        except ValueError as exc:
            return f"A股报告完整性校验失败，未发送部分结果：{exc}"

    if service_is_active(service_name):
        return "A股报告正在生成中，请稍后再次发送“@机器人 a股报告”获取结果。"

    if start_service(service_name):
        return (
            "当前没有 7 天内的 A股报告，已触发一轮后台分析。"
            "生成通常需要约 15 分钟，请稍后再次发送“@机器人 a股报告”获取结果。"
        )
    return "当前没有可用的 A股报告，后台分析启动失败，请联系管理员检查服务状态。"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Serve and retain passive QQ A-share reports"
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path(
            os.getenv("QQBOT_PASSIVE_REPORT_DIR", str(project_root / "reports"))
        ),
    )
    parser.add_argument(
        "--retention-days",
        type=_positive_int,
        default=int(
            os.getenv(
                "QQBOT_PASSIVE_REPORT_RETENTION_DAYS",
                str(DEFAULT_RETENTION_DAYS),
            )
        ),
    )
    parser.add_argument(
        "--max-chars",
        type=_positive_int,
        default=int(
            os.getenv("QQBOT_PASSIVE_REPORT_MAX_CHARS", str(DEFAULT_MAX_CHARS))
        ),
    )
    parser.add_argument(
        "--service-name",
        default=os.getenv(
            "QQBOT_PASSIVE_REPORT_SERVICE",
            DEFAULT_ANALYSIS_SERVICE,
        ),
    )
    parser.add_argument("action", choices=("handle", "cleanup"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.action == "cleanup":
        removed = cleanup_reports(
            args.reports_dir,
            retention_days=args.retention_days,
        )
        print(f"removed={len(removed)}")
        return 0

    print(
        handle_report_request(
            args.reports_dir,
            retention_days=args.retention_days,
            max_chars=args.max_chars,
            service_name=args.service_name,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
