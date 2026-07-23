#!/usr/bin/env python3
"""Run one A-share analysis and push only the newly generated report to QQ."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

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


def run_and_push(
    reports_dir: Path,
    *,
    analysis_service: str,
    timeout_seconds: int,
) -> dict[str, object]:
    previous = find_latest_report(
        reports_dir,
        retention_days=DEFAULT_RETENTION_DAYS,
    )
    previous_mtime = previous.stat().st_mtime_ns if previous else 0
    started_at_ns = time.time_ns()

    completed = subprocess.run(
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
    if latest_mtime <= previous_mtime or latest_mtime < started_at_ns:
        raise RuntimeError(
            "analysis did not create or update an aggregate report; "
            "refusing to push stale content"
        )

    delivery = push_content(build_latest_report(reports_dir))
    return {
        "success": True,
        "analysis_service": analysis_service,
        "report": str(latest),
        "report_mtime_ns": latest_mtime,
        "delivery": delivery,
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_and_push(
        args.reports_dir,
        analysis_service=args.analysis_service,
        timeout_seconds=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
