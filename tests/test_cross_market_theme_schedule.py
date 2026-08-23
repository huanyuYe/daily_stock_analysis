"""Wrapper and systemd schedule contracts for cross-market theme reports."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.run_cross_market_theme_report import (
    load_report_watchlist,
    main,
    run_and_push,
)


class _Service:
    def __init__(self, result):
        self.result = dict(result)
        self.calls = []

    def generate(self, phase, watchlist):
        self.calls.append((phase, list(watchlist)))
        return dict(self.result)


def test_watchlist_union_is_limited_to_a_and_hk_targets():
    with patch(
        "scripts.run_cross_market_theme_report.get_config",
        return_value=SimpleNamespace(stock_list=["HK00700", "002409", "MSFT"]),
    ):
        watchlist, status = load_report_watchlist(
            portfolio_loader=lambda: ["AAPL", "HK09988", "600089"]
        )

    assert watchlist == ["HK09988", "600089", "HK00700", "002409"]
    assert status["a_hk_count"] == 4


def test_script_entrypoint_bootstraps_project_import_path():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_cross_market_theme_report.py"
    )
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory() as tmpdir:
        completed = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=tmpdir,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert completed.returncode == 0, completed.stderr
    assert "--phase" in completed.stdout


def test_wrapper_pushes_only_fresh_generated_content():
    service = _Service(
        {
            "success": True,
            "skipped": False,
            "phase": "morning",
            "report": "/tmp/report.md",
            "snapshot": "/tmp/report.json",
            "content": "# 主线报告",
        }
    )
    pushed = []
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        result = run_and_push(
            "morning",
            project_root=root,
            reports_root=root / "reports",
            output_root=root / "output",
            service=service,
            watchlist_loader=lambda: (["HK00700"], {"configured_count": 1}),
            pusher=lambda content: pushed.append(content) or {"success": True},
        )

    assert service.calls == [("morning", ["HK00700"])]
    assert pushed == ["# 主线报告"]
    assert result["within_target_duration"] is True
    assert "content" not in result


def test_wrapper_does_not_push_a_skipped_report():
    service = _Service(
        {
            "success": True,
            "skipped": True,
            "phase": "close",
            "reason": "same_day_morning_snapshot_required",
        }
    )
    pushed = []
    result = run_and_push(
        "close",
        project_root=Path("/tmp/project"),
        reports_root=Path("/tmp/project/reports"),
        output_root=Path("/tmp/project/output"),
        service=service,
        watchlist_loader=lambda: (["HK00700"], {}),
        pusher=lambda content: pushed.append(content) or {"success": True},
    )

    assert result["skipped"] is True
    assert result["success"] is False
    assert result["operational_status"] == "blocked_missing_dependency"
    assert pushed == []


def test_cli_returns_nonzero_for_missing_required_upstream_report():
    with patch(
        "scripts.run_cross_market_theme_report.parse_args",
        return_value=SimpleNamespace(
            phase="morning",
            project_root=Path("/tmp/project"),
            reports_root=Path("/tmp/project/reports"),
            output_root=Path("/tmp/project/output"),
            target_duration=1200,
            no_push=False,
        ),
    ), patch(
        "scripts.run_cross_market_theme_report.run_and_push",
        return_value={
            "success": False,
            "skipped": True,
            "reason": "fresh_us_postmarket_report_required",
        },
    ):
        assert main() == 2


def test_timers_run_twice_in_shanghai_timezone_and_wait_for_shared_lock():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    morning = (scripts / "daily-stock-analysis-cross-market-theme-morning.timer").read_text(
        encoding="utf-8"
    )
    close = (scripts / "daily-stock-analysis-cross-market-theme-close.timer").read_text(
        encoding="utf-8"
    )
    service = (scripts / "daily-stock-analysis-cross-market-theme@.service").read_text(
        encoding="utf-8"
    )

    assert "09:25:00 Asia/Shanghai" in morning
    assert "16:50:00 Asia/Shanghai" in close
    assert "--phase %i --target-duration 1200" in service
    assert "flock -w 1200 /run/lock/daily-stock-analysis-qqbot-active.lock" in service
