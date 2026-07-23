"""Tests for passive QQ A-share report retrieval and retention."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from scripts.patch_hermes_qqbot_report_command import MARKER, install_hook
from scripts.qqbot_passive_report import (
    MAX_QQ_PASSIVE_REPLIES,
    QQ_MESSAGE_CHARS,
    build_qq_summary,
    cleanup_reports,
    find_latest_report,
    handle_report_request,
)


class QQBotPassiveReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.reports_dir = Path(self.tempdir.name)
        self.now = datetime(2026, 7, 23, 13, 30)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_report(self, name: str, age_days: int, content: str = "dashboard") -> Path:
        path = self.reports_dir / name
        path.write_text(content, encoding="utf-8")
        modified = (self.now - timedelta(days=age_days)).timestamp()
        os.utime(path, (modified, modified))
        return path

    def _full_report(self, count: int = 9) -> str:
        summaries = []
        details = []
        for index in range(1, count + 1):
            code = f"{600000 + index:06d}"
            summaries.append(
                f"⚪ **测试股票{index}({code})**: 观望 | 评分 {40 + index} | 震荡"
            )
            details.append(
                "\n".join(
                    [
                        f"## ⚪ 测试股票{index} ({code})",
                        "",
                        f"**💭 舆情情绪**: 标的{index}舆情中性，等待可验证事件。",
                        f"**📊 业绩预期**: 标的{index}财务数据尚待确认。",
                        f"**📢 最新动态**: 标的{index}暂无重大公告。",
                        "",
                        f"> **一句话决策**: 标的{index}等待趋势确认。",
                        "",
                        "**数据限制**:",
                        f"- 标的{index}行情仅有一个来源",
                        "- quote: partial",
                    ]
                )
            )
        return "\n".join(
            [
                f"# 决策仪表盘\n\n> 共分析 **{count}** 只股票",
                "## 分析结果摘要",
                *summaries,
                "",
                "---",
                "",
                "\n\n---\n\n".join(details),
            ]
        )

    def test_cleanup_removes_reports_older_than_seven_days(self):
        old = self._write_report("report_old.md", 8)
        fresh = self._write_report("report_fresh.md", 6)
        non_report = self.reports_dir / "notes.txt"
        non_report.write_text("keep", encoding="utf-8")

        removed = cleanup_reports(
            self.reports_dir,
            retention_days=7,
            now=self.now,
        )

        self.assertEqual(removed, [old])
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(non_report.exists())

    def test_find_latest_uses_only_fresh_aggregate_reports(self):
        self._write_report("market_review_20260723.md", 0)
        expected = self._write_report("report_20260722.md", 1)
        self._write_report("report_20260710.md", 10)

        latest = find_latest_report(
            self.reports_dir,
            retention_days=7,
            now=self.now,
        )

        self.assertEqual(latest, expected)

    def test_summary_contains_compact_detail_for_every_stock(self):
        report = self._write_report(
            "report_20260723.md",
            0,
            self._full_report(),
        )

        summary = build_qq_summary(report, max_chars=12000)

        self.assertIn("共分析 **9** 只股票", summary)
        self.assertIn("## ⚪ 测试股票1 (600001) [1/9]", summary)
        self.assertIn("## ⚪ 测试股票9 (600009) [9/9]", summary)
        for index in range(1, 10):
            self.assertIn(f"{600000 + index:06d}", summary)
        self.assertLessEqual(
            len(summary),
            MAX_QQ_PASSIVE_REPLIES * QQ_MESSAGE_CHARS,
        )

    def test_summary_fails_closed_when_a_stock_detail_is_missing(self):
        content = self._full_report().replace(
            "## ⚪ 测试股票9 (600009)",
            "## 无效标题",
        )
        report = self._write_report("report_20260723.md", 0, content)

        with self.assertRaisesRegex(ValueError, "600009"):
            build_qq_summary(report, max_chars=12000)

    def test_handle_returns_cached_report_without_starting_service(self):
        self._write_report("report_20260723.md", 0, self._full_report())
        started: list[str] = []

        result = handle_report_request(
            self.reports_dir,
            retention_days=7,
            max_chars=12000,
            service_name="analysis.service",
            service_is_active=lambda _: False,
            start_service=lambda service: not started.append(service),
        )

        self.assertIn("决策仪表盘", result)
        self.assertEqual(started, [])

    def test_handle_starts_background_service_when_report_is_missing(self):
        started: list[str] = []

        result = handle_report_request(
            self.reports_dir,
            retention_days=7,
            max_chars=3500,
            service_name="analysis.service",
            service_is_active=lambda _: False,
            start_service=lambda service: not started.append(service),
        )

        self.assertIn("已触发一轮后台分析", result)
        self.assertEqual(started, ["analysis.service"])

    def test_handle_reports_existing_generation(self):
        result = handle_report_request(
            self.reports_dir,
            retention_days=7,
            max_chars=3500,
            service_name="analysis.service",
            service_is_active=lambda _: True,
            start_service=lambda _: self.fail("must not start twice"),
        )

        self.assertIn("正在生成中", result)


class HermesQQBotReportHookTest(unittest.TestCase):
    def _adapter_source(self) -> str:
        return "\n".join(
            [
                "before",
                "        text = self._strip_at_mention(content)",
                "middle",
                (
                    "        chunks = self.truncate_message("
                    "formatted, self.MAX_MESSAGE_LENGTH)"
                ),
                "        for chunk in chunks:",
                "            result = await self._send_chunk(chat_id, chunk, reply_to)",
                "            # Only reply_to the first chunk",
                "            reply_to = None",
                "after",
                "",
            ]
        )

    def test_hook_rewrites_only_the_configured_plaintext_trigger(self):
        source = self._adapter_source()

        updated = install_hook(source)

        self.assertIn(MARKER, updated)
        self.assertIn('text = "/a-stock-report"', updated)
        self.assertIn("QQBOT_PASSIVE_REPORT_COMMAND_ONLY", updated)
        self.assertIn("Passive QQ reply exceeds the 5-message limit", updated)
        self.assertNotIn("reply_to = None", updated)
        self.assertEqual(install_hook(updated), updated)

    def test_hook_fails_closed_when_anchor_changes(self):
        with self.assertRaisesRegex(ValueError, "anchor"):
            install_hook("unexpected source")

    def test_existing_trigger_install_receives_multi_reply_patch(self):
        source = install_hook(self._adapter_source())
        trigger_only = source.replace(
            "        # daily-stock-analysis retain passive reply id\n"
            "        if reply_to and len(chunks) > 5:\n"
            "            return SendResult(\n"
            "                success=False,\n"
            '                error="Passive QQ reply exceeds the 5-message limit",\n'
            "            )\n",
            "",
        ).replace(
            "            # Keep reply_to for every passive chunk. QQ permits up to five\n"
            "            # replies to the same inbound message; clearing it makes later\n"
            "            # chunks unauthorized proactive messages.\n",
            "            # Only reply_to the first chunk\n"
            "            reply_to = None\n",
        )

        updated = install_hook(trigger_only)

        self.assertIn("Passive QQ reply exceeds the 5-message limit", updated)
        self.assertNotIn("reply_to = None", updated)


if __name__ == "__main__":
    unittest.main()
