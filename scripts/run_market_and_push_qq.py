#!/usr/bin/env python3
"""Run one read-only Futu-portfolio market analysis and push its fresh QQ report."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.trading_calendar import infer_market_phase

try:
    from scripts.qqbot_active_report import push_content
    from scripts.qqbot_passive_report import (
        DEFAULT_MAX_CHARS,
        DEFAULT_RETENTION_DAYS,
        build_qq_summary,
        find_latest_report,
    )
except ModuleNotFoundError:
    from qqbot_active_report import push_content
    from qqbot_passive_report import (
        DEFAULT_MAX_CHARS,
        DEFAULT_RETENTION_DAYS,
        build_qq_summary,
        find_latest_report,
    )


@dataclass(frozen=True)
class MarketAnalysisProfile:
    market: str
    phase: str
    market_label: str
    phase_label: str

    @property
    def name(self) -> str:
        return f"{self.market}-{self.phase}"


_MARKET_LABELS = {"hk": "港股", "us": "美股"}
_PHASE_LABELS = {
    "premarket": "盘前",
    "intraday": "盘中",
    "postmarket": "盘后",
}


def parse_profile(value: str) -> MarketAnalysisProfile:
    """Parse the six explicitly supported HK/US scheduled-analysis profiles."""
    normalized = (value or "").strip().lower()
    market, separator, phase = normalized.partition("-")
    if (
        not separator
        or market not in _MARKET_LABELS
        or phase not in _PHASE_LABELS
    ):
        raise ValueError(
            "profile must be one of hk-premarket, hk-intraday, hk-postmarket, "
            "us-premarket, us-intraday, us-postmarket"
        )
    return MarketAnalysisProfile(
        market=market,
        phase=phase,
        market_label=_MARKET_LABELS[market],
        phase_label=_PHASE_LABELS[phase],
    )


def filter_market_stock_codes(
    stock_codes: Sequence[str],
    market: str,
) -> list[str]:
    """Keep only target-market symbols while preserving portfolio order."""
    from src.core.trading_calendar import get_market_for_stock

    result: list[str] = []
    for raw_code in stock_codes:
        code = str(raw_code or "").strip().upper()
        if code and get_market_for_stock(code) == market and code not in result:
            result.append(code)
    return result


def _default_reports_dir(
    project_root: Path,
    profile: MarketAnalysisProfile,
) -> Path:
    configured = (os.getenv("REPORTS_DIR") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else project_root / path
    return project_root / "reports" / profile.market / profile.phase


def _analysis_environment(
    reports_dir: Path,
    profile: MarketAnalysisProfile,
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "REPORTS_DIR": str(reports_dir),
            "RUN_IMMEDIATELY": "true",
            "SCHEDULE_ENABLED": "false",
            "MARKET_REVIEW_ENABLED": "true",
            "MARKET_REVIEW_REGION": profile.market,
        }
    )
    return env


def run_and_push(
    profile: MarketAnalysisProfile,
    *,
    project_root: Path,
    reports_dir: Path,
    timeout_seconds: int,
    portfolio_loader: Callable[[], list[str]] | None = None,
    market_phase_loader: Callable[[str], object] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    pusher: Callable[[str], dict[str, object]] = push_content,
) -> dict[str, object]:
    """Load positions read-only, analyze one market/phase, and push only fresh output."""
    if market_phase_loader is None:
        market_phase_loader = infer_market_phase

    calendar_phase = market_phase_loader(profile.market)
    calendar_phase_value = str(
        getattr(calendar_phase, "value", calendar_phase) or ""
    ).strip().lower()
    if calendar_phase_value in {"non_trading", "unknown", ""}:
        return {
            "success": True,
            "skipped": True,
            "profile": profile.name,
            "reason": (
                "non_trading_day"
                if calendar_phase_value == "non_trading"
                else "market_calendar_unavailable"
            ),
        }

    if portfolio_loader is None:
        from src.brokers.futu.portfolio import load_futu_stock_codes

        portfolio_loader = load_futu_stock_codes

    portfolio_codes = portfolio_loader()
    stock_codes = filter_market_stock_codes(portfolio_codes, profile.market)
    if not stock_codes:
        return {
            "success": True,
            "skipped": True,
            "profile": profile.name,
            "reason": "no_target_market_futu_holdings",
        }

    reports_dir.mkdir(parents=True, exist_ok=True)
    previous = find_latest_report(
        reports_dir,
        retention_days=DEFAULT_RETENTION_DAYS,
    )
    previous_mtime = previous.stat().st_mtime_ns if previous else 0
    started_at_ns = time.time_ns()

    command = [
        sys.executable,
        str(project_root / "main.py"),
        "--stocks",
        ",".join(stock_codes),
        "--analysis-phase",
        profile.phase,
        "--no-notify",
    ]
    completed = command_runner(
        command,
        cwd=project_root,
        env=_analysis_environment(reports_dir, profile),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"{profile.name} analysis failed with code {completed.returncode}: "
            f"{error[-1000:]}"
        )

    latest = find_latest_report(
        reports_dir,
        retention_days=DEFAULT_RETENTION_DAYS,
    )
    if latest is None:
        raise RuntimeError(
            f"{profile.name} analysis completed without an aggregate report"
        )
    latest_mtime = latest.stat().st_mtime_ns
    if latest_mtime <= previous_mtime or latest_mtime < started_at_ns:
        raise RuntimeError(
            f"{profile.name} analysis did not create or update an aggregate report; "
            "refusing to push stale content"
        )

    summary = build_qq_summary(latest, max_chars=DEFAULT_MAX_CHARS)
    content = (
        f"# {profile.market_label} · {profile.phase_label}分析\n\n"
        f"{summary}"
    )
    delivery = pusher(content)
    return {
        "success": True,
        "skipped": False,
        "profile": profile.name,
        "stock_count": len(stock_codes),
        "report": str(latest),
        "report_mtime_ns": latest_mtime,
        "delivery": delivery,
    }


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        required=True,
        help="hk/us market and premarket/intraday/postmarket phase",
    )
    parser.add_argument("--project-root", type=Path, default=project_root)
    parser.add_argument("--reports-dir", type=Path)
    parser.add_argument("--timeout", type=int, default=4 * 60 * 60)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        profile = parse_profile(args.profile)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    project_root = args.project_root.resolve()
    reports_dir = (
        args.reports_dir.expanduser().resolve()
        if args.reports_dir is not None
        else _default_reports_dir(project_root, profile)
    )
    result = run_and_push(
        profile,
        project_root=project_root,
        reports_dir=reports_dir,
        timeout_seconds=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
