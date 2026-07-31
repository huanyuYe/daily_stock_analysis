"""Contracts for HK/US phase-specific QQ scheduled analysis."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.run_market_and_push_qq import (
    filter_market_stock_codes,
    parse_profile,
    run_and_push,
)
from src.notification import NotificationService


def _foreign_report(code: str = "AAPL") -> str:
    return "\n".join(
        [
            "# 🎯 2026-07-31 决策仪表盘",
            "",
            "> 共分析 **1** 只股票",
            "市场状态：美股 · 盘前",
            "## 📊 分析结果摘要",
            f"⚪ **苹果({code})**: 观望 | 评分 55 | 震荡",
            "",
            "---",
            "",
            f"## ⚪ 苹果 ({code})",
            "",
            "**💭 舆情情绪**: 中性。",
            "**📊 业绩预期**: 等待确认。",
            "**📢 最新动态**: 暂无重大事件。",
            "",
            "> **一句话决策**: 等待常规交易时段确认。",
        ]
    )


class MarketQQScheduleTest(unittest.TestCase):
    def test_profiles_are_limited_to_hk_and_us(self):
        profile = parse_profile("us-premarket")
        self.assertEqual(profile.market, "us")
        self.assertEqual(profile.phase, "premarket")
        with self.assertRaisesRegex(ValueError, "profile must be"):
            parse_profile("uk-premarket")

    def test_market_filter_keeps_only_target_futu_holdings(self):
        holdings = ["600519", "HK00700", "AAPL", "MSFT", "HK09988"]

        self.assertEqual(
            filter_market_stock_codes(holdings, "hk"),
            ["HK00700", "HK09988"],
        )
        self.assertEqual(
            filter_market_stock_codes(holdings, "us"),
            ["AAPL", "MSFT"],
        )

    def test_run_uses_read_only_holdings_and_pushes_only_fresh_target_report(self):
        profile = parse_profile("us-premarket")
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            reports_dir = project_root / "reports" / "us" / "premarket"
            captured: dict[str, object] = {}

            def runner(command, **kwargs):
                captured["command"] = command
                captured["env"] = kwargs["env"]
                reports_dir.mkdir(parents=True, exist_ok=True)
                (reports_dir / "report_20260731.md").write_text(
                    _foreign_report(),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            pushed: list[str] = []
            result = run_and_push(
                profile,
                project_root=project_root,
                reports_dir=reports_dir,
                timeout_seconds=60,
                portfolio_loader=lambda: ["600519", "HK00700", "AAPL"],
                market_phase_loader=lambda _market: "premarket",
                command_runner=runner,
                pusher=lambda content: pushed.append(content) or {"success": True},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["stock_count"], 1)
        self.assertIn("--stocks", captured["command"])
        self.assertIn("AAPL", captured["command"])
        self.assertNotIn("HK00700", captured["command"])
        self.assertIn("--analysis-phase", captured["command"])
        self.assertIn("premarket", captured["command"])
        self.assertEqual(captured["env"]["MARKET_REVIEW_REGION"], "us")
        self.assertEqual(captured["env"]["REPORTS_DIR"], str(reports_dir))
        self.assertEqual(len(pushed), 1)
        self.assertTrue(pushed[0].startswith("# 美股 · 盘前分析"))
        self.assertIn("AAPL", pushed[0])

    def test_no_target_holdings_skips_without_analysis_or_push(self):
        calls: list[str] = []
        result = run_and_push(
            parse_profile("hk-intraday"),
            project_root=Path("/tmp/project"),
            reports_dir=Path("/tmp/reports"),
            timeout_seconds=60,
            portfolio_loader=lambda: ["AAPL"],
            market_phase_loader=lambda _market: "intraday",
            command_runner=lambda *args, **kwargs: calls.append("analysis"),
            pusher=lambda content: calls.append("push"),
        )

        self.assertTrue(result["skipped"])
        self.assertEqual(calls, [])

    def test_non_trading_day_skips_before_reading_futu_portfolio(self):
        portfolio_loader = Mock(side_effect=AssertionError("must not query Futu"))
        result = run_and_push(
            parse_profile("hk-premarket"),
            project_root=Path("/tmp/project"),
            reports_dir=Path("/tmp/reports"),
            timeout_seconds=60,
            portfolio_loader=portfolio_loader,
            market_phase_loader=lambda _market: "non_trading",
            command_runner=Mock(side_effect=AssertionError("must not run")),
            pusher=Mock(side_effect=AssertionError("must not push")),
        )

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "non_trading_day")
        portfolio_loader.assert_not_called()

    def test_unknown_calendar_state_fails_closed_before_analysis(self):
        result = run_and_push(
            parse_profile("us-intraday"),
            project_root=Path("/tmp/project"),
            reports_dir=Path("/tmp/reports"),
            timeout_seconds=60,
            portfolio_loader=Mock(side_effect=AssertionError("must not query Futu")),
            market_phase_loader=lambda _market: "unknown",
            command_runner=Mock(side_effect=AssertionError("must not run")),
            pusher=Mock(side_effect=AssertionError("must not push")),
        )

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "market_calendar_unavailable")

    def test_report_directory_can_be_isolated_by_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"REPORTS_DIR": tmpdir},
        ):
            service = NotificationService()
            path = Path(service.save_report_to_file("content"))
            saved_content = path.read_text(encoding="utf-8")

        self.assertEqual(path.parent, Path(tmpdir))
        self.assertEqual(saved_content, "content")

    def test_timer_timezones_follow_each_market(self):
        scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
        hk = (scripts_dir / "daily-stock-analysis-qqbot-hk-premarket.timer").read_text(
            encoding="utf-8"
        )
        us = (scripts_dir / "daily-stock-analysis-qqbot-us-premarket.timer").read_text(
            encoding="utf-8"
        )

        self.assertIn("09:00:00 Asia/Hong_Kong", hk)
        self.assertIn("09:00:00 America/New_York", us)
        self.assertNotIn("Asia/Shanghai", us)


if __name__ == "__main__":
    unittest.main()
