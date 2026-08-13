from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.run_a_share_and_push_qq import (
    archive_a_share_phase_reports,
    detect_a_share_report_phase,
    run_and_push,
)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("盘前", "premarket"),
        ("盘中", "intraday"),
        ("午间休市", "intraday"),
        ("临近收盘", "intraday"),
        ("盘后", "postmarket"),
    ],
)
def test_detect_a_share_report_phase(label: str, expected: str) -> None:
    assert detect_a_share_report_phase(f"市场状态：A股 · {label}") == expected


def test_archive_a_share_phase_reports_preserves_report_and_fresh_review() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        reports_dir = Path(tmpdir)
        report = reports_dir / "report_20260812.md"
        review = reports_dir / "market_review_20260812.md"
        report.write_text("# report\n市场状态：A股 · 午间休市\n", encoding="utf-8")
        review.write_text("# review\n", encoding="utf-8")

        result = archive_a_share_phase_reports(reports_dir, report)

        assert result["phase"] == "intraday"
        assert (reports_dir / "cn/intraday/report_20260812.md").read_text(
            encoding="utf-8"
        ) == report.read_text(encoding="utf-8")
        assert (reports_dir / "cn/intraday/market_review_20260812.md").is_file()


def test_run_and_push_archives_fresh_report_before_delivery() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        reports_dir = Path(tmpdir)
        pushed: list[str] = []

        def runner(command, **_kwargs):
            (reports_dir / "report_20260812.md").write_text(
                "\n".join(
                    [
                        "# 🎯 2026-08-12 决策仪表盘",
                        "市场状态：A股 · 盘后",
                        "## 📊 分析结果摘要",
                        "⚪ **测试(600000)**: 观望 | 评分 50 | 震荡",
                    ]
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch(
            "scripts.run_a_share_and_push_qq.build_latest_report",
            return_value="summary",
        ):
            result = run_and_push(
                reports_dir,
                analysis_service="daily-stock-analysis.service",
                timeout_seconds=60,
                command_runner=runner,
                pusher=lambda content: pushed.append(content) or {"success": True},
            )

        assert result["success"] is True
        assert result["archive"]["phase"] == "postmarket"
        assert Path(result["archive"]["report"]).is_file()
        assert result["within_target_duration"] is True
        assert len(pushed) == 1
