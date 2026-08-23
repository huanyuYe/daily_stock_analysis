#!/usr/bin/env python3
"""Analyze Futu holdings plus configured watchlist symbols and push a fresh report."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
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
        extract_report_stock_codes,
        find_latest_report,
    )
except ModuleNotFoundError:
    from qqbot_active_report import push_content
    from qqbot_passive_report import (
        DEFAULT_MAX_CHARS,
        DEFAULT_RETENTION_DAYS,
        build_qq_summary,
        extract_report_stock_codes,
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
DEFAULT_TARGET_DURATION_SECONDS = 20 * 60


def _summarize_child_output(stdout: str, stderr: str) -> dict[str, int]:
    """Return non-sensitive counters from the captured analysis log."""

    combined = f"{stdout or ''}\n{stderr or ''}".lower()
    patterns = {
        "rate_limit": r"too many requests|rate.?limit|\b429\b",
        "timeout": r"timed out|请求超时|\btimeout\b",
        "earnings_options_failed": r"earnings/options fetch failed|earnings/options fetch timed out",
        "sec_failed": r"sec (?:ticker mapping|submissions|companyfacts) failed",
        "hkex_failed": r"hkexnews disclosure search failed",
        "search_failed": r"\[情报搜索\].*搜索失败",
    }
    return {name: len(re.findall(pattern, combined)) for name, pattern in patterns.items()}


def _build_report_coverage(
    expected_codes: Sequence[str],
    completed_codes: Sequence[str],
) -> dict[str, object]:
    """Build a deterministic completeness contract for one aggregate report."""

    expected = [str(code or "").strip().upper() for code in expected_codes if str(code or "").strip()]
    completed = [str(code or "").strip().upper() for code in completed_codes if str(code or "").strip()]
    expected_set = set(expected)
    completed_set = set(completed)
    duplicates = sorted(code for code, count in Counter(completed).items() if count > 1)
    missing = [code for code in expected if code not in completed_set]
    unexpected = [code for code in completed if code not in expected_set]
    complete = (
        not missing
        and not unexpected
        and not duplicates
        and len(completed) == len(expected)
    )
    return {
        "status": "complete" if complete else "partial",
        "expected_count": len(expected),
        "completed_count": len(completed),
        "expected_codes": expected,
        "completed_codes": completed,
        "missing_codes": missing,
        "unexpected_codes": unexpected,
        "duplicate_codes": duplicates,
    }


def _write_run_manifest(
    report_path: Path,
    *,
    profile: MarketAnalysisProfile,
    coverage: dict[str, object],
    upstream_diagnostics: dict[str, int],
    created_at: str,
    delivery_status: dict[str, object],
    parse_error: str | None = None,
) -> Path:
    """Persist a non-sensitive report/run contract next to the aggregate report."""

    manifest_path = report_path.with_name(f"{report_path.stem}.run.json")
    payload: dict[str, object] = {
        "schema_version": 1,
        "created_at": created_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile.name,
        "report": report_path.name,
        "report_mtime_ns": report_path.stat().st_mtime_ns,
        "coverage": coverage,
        "upstream_diagnostics": upstream_diagnostics,
        "delivery": delivery_status,
    }
    if parse_error:
        payload["parse_error"] = parse_error[:300]
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return manifest_path


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
    target_duration_seconds: int = DEFAULT_TARGET_DURATION_SECONDS,
    portfolio_loader: Callable[[], list[str]] | None = None,
    watchlist_loader: Callable[[], list[str]] | None = None,
    market_phase_loader: Callable[[str], object] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    pusher: Callable[[str], dict[str, object]] = push_content,
) -> dict[str, object]:
    """Load holdings/watchlist read-only, analyze one phase, and push fresh output."""
    run_started = time.monotonic()
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
    if watchlist_loader is None:
        from src.config import get_config

        watchlist_loader = lambda: list(get_config().stock_list)

    portfolio_codes = portfolio_loader()
    watchlist_codes = watchlist_loader()
    portfolio_market_codes = filter_market_stock_codes(portfolio_codes, profile.market)
    watchlist_market_codes = filter_market_stock_codes(watchlist_codes, profile.market)
    stock_codes = list(portfolio_market_codes)
    stock_codes.extend(code for code in watchlist_market_codes if code not in stock_codes)
    if not stock_codes:
        return {
            "success": True,
            "skipped": True,
            "profile": profile.name,
            "reason": "no_target_market_holdings_or_watchlist",
        }

    reports_dir.mkdir(parents=True, exist_ok=True)
    previous = find_latest_report(
        reports_dir,
        retention_days=DEFAULT_RETENTION_DAYS,
    )
    previous_mtime = previous.stat().st_mtime_ns if previous else 0
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
    analysis_duration_seconds = time.monotonic() - run_started
    upstream_diagnostics = _summarize_child_output(
        completed.stdout or "",
        completed.stderr or "",
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
    # ``previous_mtime`` is the authoritative freshness guard.  Comparing the
    # report mtime with a nanosecond wall-clock sample as well is unreliable on
    # filesystems whose timestamp resolution is coarser than ``time.time_ns``
    # (observed on the Linux deployment host), and can reject a report that the
    # child process has just created.  If no retained report existed before the
    # child ran, ``find_latest_report`` returning a file afterwards is already
    # sufficient evidence that it is new.
    if latest_mtime <= previous_mtime:
        raise RuntimeError(
            f"{profile.name} analysis did not create or update an aggregate report; "
            "refusing to push stale content"
        )

    parse_error: str | None = None
    try:
        completed_codes = extract_report_stock_codes(latest)
    except (OSError, ValueError) as exc:
        completed_codes = []
        parse_error = f"{type(exc).__name__}: {exc}"
    coverage = _build_report_coverage(stock_codes, completed_codes)
    manifest_created_at = datetime.now(timezone.utc).isoformat()
    manifest_path = _write_run_manifest(
        latest,
        profile=profile,
        coverage=coverage,
        upstream_diagnostics=upstream_diagnostics,
        created_at=manifest_created_at,
        delivery_status={"status": "not_attempted"},
        parse_error=parse_error,
    )
    if parse_error or coverage["status"] != "complete":
        raise RuntimeError(
            f"{profile.name} aggregate report is incomplete: "
            f"expected={coverage['expected_count']} completed={coverage['completed_count']} "
            f"missing={coverage['missing_codes']} unexpected={coverage['unexpected_codes']}; "
            f"manifest={manifest_path}"
        )

    summary = build_qq_summary(latest, max_chars=DEFAULT_MAX_CHARS)
    content = (
        f"# {profile.market_label} · {profile.phase_label}分析\n\n"
        f"{summary}"
    )
    try:
        delivery = pusher(content)
    except Exception as exc:
        _write_run_manifest(
            latest,
            profile=profile,
            coverage=coverage,
            upstream_diagnostics=upstream_diagnostics,
            created_at=manifest_created_at,
            delivery_status={
                "status": "failed",
                "failure_code": "delivery_exception",
                "error_type": type(exc).__name__,
            },
        )
        raise
    if not isinstance(delivery, dict) or delivery.get("success") is not True:
        _write_run_manifest(
            latest,
            profile=profile,
            coverage=coverage,
            upstream_diagnostics=upstream_diagnostics,
            created_at=manifest_created_at,
            delivery_status={
                "status": "failed",
                "failure_code": "delivery_not_confirmed",
            },
        )
        raise RuntimeError(f"{profile.name} QQ delivery did not confirm success")

    delivery_status: dict[str, object] = {"status": "delivered"}
    for key in ("parts", "characters"):
        value = delivery.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            delivery_status[key] = value
    _write_run_manifest(
        latest,
        profile=profile,
        coverage=coverage,
        upstream_diagnostics=upstream_diagnostics,
        created_at=manifest_created_at,
        delivery_status=delivery_status,
    )
    total_duration_seconds = time.monotonic() - run_started
    target_seconds = max(1, int(target_duration_seconds))
    return {
        "success": True,
        "skipped": False,
        "profile": profile.name,
        "stock_count": len(stock_codes),
        "portfolio_stock_count": len(portfolio_market_codes),
        "watchlist_stock_count": len(watchlist_market_codes),
        "report": str(latest),
        "report_mtime_ns": latest_mtime,
        "run_manifest": str(manifest_path),
        "report_coverage": coverage,
        "delivery": delivery,
        "analysis_duration_seconds": round(analysis_duration_seconds, 3),
        "total_duration_seconds": round(total_duration_seconds, 3),
        "target_duration_seconds": target_seconds,
        "within_target_duration": total_duration_seconds <= target_seconds,
        "upstream_diagnostics": upstream_diagnostics,
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
    parser.add_argument(
        "--target-duration",
        type=int,
        default=DEFAULT_TARGET_DURATION_SECONDS,
        help="report delivery SLO in seconds; records compliance without dropping data",
    )
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
        target_duration_seconds=args.target_duration,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
