"""Tests for the loopback Custom Webhook to QQBot adapter."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts.qqbot_webhook_bridge import build_qqbot_payload, extract_report_text, send_to_qqbot


class QQBotWebhookBridgeTest(unittest.TestCase):
    def test_extract_report_text_uses_supported_custom_webhook_fields(self):
        self.assertEqual(extract_report_text({"text": "line 1\nline 2"}), "line 1\nline 2")
        self.assertEqual(extract_report_text({"text": "", "content": "fallback"}), "fallback")
        self.assertIsNone(extract_report_text({"unknown": "value"}))

    def test_build_qqbot_payload_preserves_newlines_after_json_decode(self):
        payload = json.loads(build_qqbot_payload("标题\n结论"))
        self.assertEqual(
            payload,
            {
                "text": "标题\n结论",
                "source": "daily-stock-analysis",
                "kind": "stock-report",
            },
        )

    @patch("scripts.qqbot_webhook_bridge.subprocess.run")
    def test_send_to_qqbot_uses_direct_channel_without_agent(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = json.dumps(
            {"success": True, "platform": "qqbot", "message_id": "message-1"}
        )
        mock_run.return_value.stderr = ""

        result = send_to_qqbot("第一行\n第二行", hermes_path="/opt/hermes", timeout_seconds=30)

        self.assertTrue(result["success"])
        command = mock_run.call_args.args[0]
        self.assertEqual(command[:4], ["/opt/hermes", "send", "--to", "qqbot"])
        self.assertEqual(json.loads(command[4])["text"], "第一行\n第二行")
        self.assertEqual(command[-1], "--json")


if __name__ == "__main__":
    unittest.main()
