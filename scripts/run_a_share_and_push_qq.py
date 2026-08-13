#!/usr/bin/env python3
"""Run one A-share analysis and push only the newly generated report to QQ."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

try:
    from scripts.qqbot_active_report import build_latest_report, push_content
    from scripts.qqbot_passive_report import (
        DEFAULT_RETENTION_DAYS,
        find_latest_report,
    )
except ModuleNotFoundError:
    from qqbot_active_report import build_latest_report, push_content
    from qqbot_passive_report import (
        DEFAULT_RETENTION_DAYS,
        find_latest_report,
    )


DEFAULT_ANALYSIS_SERVICE = "daily-stock-analysis.service"
DEFAULT_TARGET_DURATION_SECONDS = 20 * 60

_A_SHARE_PHASE_PATTERNS = (
    (re.compile(r"市场状态：\s*A股\s*·\s*盘前"), "premarket"),
    (re.compile(r"市场状态：\s*A股\s*·\s*(?:盘中|午间休市|临近收盘)"), "intraday"),
    (re.compile(r"市场状态：\s*A股\s*·\s*盘后"), "postmarket"),
)


def detect_a_share_report_phase(report_text: str) -> str | None:
    """Map the rendered A-share market-state header to an archive phase."""

    for pattern, phase in _A_SHARE_PHASE_PATTERNS:
        if pattern.search(report_text):
            return phase
    return None


def archive_a_share_phase_reports(
    reports_dir: Path,
    report_path: Path,
    *,
    previous_market_review_mtime_ns: int = 0,
) -> dict[str, str | None]:
    """Preserve the fresh root report under ``reports/cn/<phase>``."""

    report_text = report_path.read_text(encoding="utf-8")
    phase = detect_a_share_report_phase(report_text)
    if phase is None:
        return {"phase": None, "report": None, "market_review": None}

    archive_dir = reports_dir / "cn" / phase
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_report = archive_dir / report_path.name
    shutil.copy2(report_path, archived_report)

    review_name = report_path.name.replace("report_", "market_review_", 1)
    source_review = reports_dir / review_name
    archived_review: Path | None = None
    if (
        source_review.is_file()
        and source_review.stat().st_mtime_ns > previous_market_review_mtime_ns
    ):
        archived_review = archive_dir / source_review.name
        shutil.copy2(source_review, archived_review)

    return {
        "phase": phase,
        "report": str(archived_report),
        "market_review": str(archived_review) if archived_review else None,
    }


def run_and_push(
    reports_dir: Path,
    *,
    analysis_service: str,
    timeout_seconds: int,
    target_duration_seconds: int = DEFAULT_TARGET_DURATION_SECONDS,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    pusher: Callable[[str], dict[str, object]] = push_content,
) -> dict[str, object]:
    run_started = time.monotonic()
    previous = find_latest_report(
        reports_dir,
        retention_days=DEFAULT_RETENTION_DAYS,
    )
    previous_mtime = previous.stat().st_mtime_ns if previous else 0
    previous_review_mtimes = {
        path.name: path.stat().st_mtime_ns
        for path in reports_dir.glob("market_review_*.md")
        if path.is_file()
    } if reports_dir.is_dir() else {}

    completed = command_runner(
        ["systemctl", "start", analysis_service],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"{analysis_service} failed with code {completed.returncode}: "
            f"{error[:500]}"
        )

    latest = find_latest_report(
        reports_dir,
        retention_days=DEFAULT_RETENTION_DAYS,
    )
    if latest is None:
        raise RuntimeError("analysis completed without an aggregate report")
    latest_mtime = latest.stat().st_mtime_ns
    if latest_mtime <= previous_mtime:
        raise RuntimeError(
            "analysis did not create or update an aggregate report; "
            "refusing to push stale content"
        )

    review_name = latest.name.replace("report_", "market_review_", 1)
    archive = archive_a_share_phase_reports(
        reports_dir,
        latest,
        previous_market_review_mtime_ns=previous_review_mtimes.get(review_name, 0),
    )
    delivery = pusher(build_latest_report(reports_dir))
    total_duration_seconds = time.monotonic() - run_started
    target_seconds = max(1, int(target_duration_seconds))
    return {
        "success": True,
        "analysis_service": analysis_service,
        "report": str(latest),
        "report_mtime_ns": latest_mtime,
        "archive": archive,
        "delivery": delivery,
        "total_duration_seconds": round(total_duration_seconds, 3),
        "target_duration_seconds": target_seconds,
        "within_target_duration": total_duration_seconds <= target_seconds,
    }


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=project_root / "reports",
    )
    parser.add_argument(
        "--analysis-service",
        default=DEFAULT_ANALYSIS_SERVICE,
    )
    parser.add_argument("--timeout", type=int, default=4 * 60 * 60)
    parser.add_argument(
        "--target-duration",
        type=int,
        default=DEFAULT_TARGET_DURATION_SECONDS,
        help="report delivery SLO in seconds; records compliance without truncating data",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_and_push(
        args.reports_dir,
        analysis_service=args.analysis_service,
        timeout_seconds=args.timeout,
        target_duration_seconds=args.target_duration,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
