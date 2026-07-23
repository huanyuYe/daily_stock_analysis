"""Tests for the loopback Custom Webhook to QQBot adapter."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts.qqbot_webhook_bridge import extract_report_text, send_to_qqbot


class QQBotWebhookBridgeTest(unittest.TestCase):
    def test_extract_report_text_uses_supported_custom_webhook_fields(self):
        self.assertEqual(extract_report_text({"text": "line 1\nline 2"}), "line 1\nline 2")
        self.assertEqual(extract_report_text({"text": "", "content": "fallback"}), "fallback")
        self.assertIsNone(extract_report_text({"unknown": "value"}))

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
        self.assertEqual(command[4], "第一行\n第二行")
        self.assertEqual(command[-1], "--json")


if __name__ == "__main__":
    unittest.main()
