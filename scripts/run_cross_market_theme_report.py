#!/usr/bin/env python3
"""Generate and optionally push one cross-market theme tracking report."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_config
from src.core.trading_calendar import get_market_for_stock
from src.services.cross_market_theme_service import (
    CrossMarketThemeService,
    merge_watchlist,
)

try:
    from scripts.qqbot_active_report import push_content
except ModuleNotFoundError:
    from qqbot_active_report import push_content


DEFAULT_TARGET_DURATION_SECONDS = 20 * 60
BLOCKING_SKIP_REASONS = frozenset({
    "fresh_us_postmarket_report_required",
    "same_day_morning_snapshot_required",
    "morning_snapshot_unreadable",
    "fresh_cn_or_hk_postmarket_report_required",
})


def load_report_watchlist(
    *,
    portfolio_loader: Optional[Callable[[], list[str]]] = None,
) -> tuple[list[str], dict[str, Any]]:
    """Read Futu holdings without making them a prerequisite for configured symbols."""

    config = get_config()
    if portfolio_loader is None:
        from src.brokers.futu.portfolio import load_futu_stock_codes

        portfolio_loader = load_futu_stock_codes
    portfolio: list[str] = []
    portfolio_status = "available"
    try:
        portfolio = list(portfolio_loader())
    except Exception as exc:
        portfolio_status = f"failed:{type(exc).__name__}"
    configured = list(config.stock_list)
    merged = merge_watchlist(portfolio, configured)
    a_hk_watchlist = [
        code for code in merged if get_market_for_stock(code) in {"cn", "hk"}
    ]
    return a_hk_watchlist, {
        "portfolio_status": portfolio_status,
        "portfolio_count": len(portfolio),
        "configured_count": len(configured),
        "a_hk_count": len(a_hk_watchlist),
    }


def run_and_push(
    phase: str,
    *,
    project_root: Path,
    reports_root: Path,
    output_root: Path,
    target_duration_seconds: int = DEFAULT_TARGET_DURATION_SECONDS,
    service: Optional[CrossMarketThemeService] = None,
    watchlist_loader: Callable[[], tuple[list[str], dict[str, Any]]] = load_report_watchlist,
    pusher: Callable[[str], dict[str, object]] = push_content,
    push_enabled: bool = True,
) -> dict[str, Any]:
    """Generate the evidence pack first and push only its freshly rendered report."""

    started = time.monotonic()
    watchlist, watchlist_status = watchlist_loader()
    generator = service or CrossMarketThemeService(
        project_root=project_root,
        reports_root=reports_root,
        output_root=output_root,
    )
    generated = generator.generate(phase, watchlist)
    content = str(generated.pop("content", "") or "")
    dependency_blocked = (
        bool(generated.get("skipped"))
        and generated.get("reason") in BLOCKING_SKIP_REASONS
    )
    if dependency_blocked:
        generated["success"] = False
        generated["operational_status"] = "blocked_missing_dependency"
    elif generated.get("skipped"):
        generated["operational_status"] = "skipped_not_applicable"
    else:
        generated["operational_status"] = "completed"
    delivery: dict[str, object] | None = None
    if not generated.get("skipped") and push_enabled:
        if not content:
            raise RuntimeError("cross-market theme report completed without content")
        delivery = pusher(content)
    total_duration = time.monotonic() - started
    target_seconds = max(1, int(target_duration_seconds))
    return {
        **generated,
        "watchlist_status": watchlist_status,
        "watchlist_count": len(watchlist),
        "delivery": delivery,
        "push_enabled": push_enabled,
        "total_duration_seconds": round(total_duration, 3),
        "target_duration_seconds": target_seconds,
        "within_target_duration": total_duration <= target_seconds,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("morning", "close"), required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--reports-root", type=Path, default=PROJECT_ROOT / "reports")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "reports" / "cross_market_theme",
    )
    parser.add_argument("--target-duration", type=int, default=DEFAULT_TARGET_DURATION_SECONDS)
    parser.add_argument("--no-push", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_and_push(
        args.phase,
        project_root=args.project_root,
        reports_root=args.reports_root,
        output_root=args.output_root,
        target_duration_seconds=args.target_duration,
        push_enabled=not args.no_push,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") is not False else 2


if __name__ == "__main__":
    raise SystemExit(main())
