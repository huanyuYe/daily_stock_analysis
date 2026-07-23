"""Tests for the loopback Custom Webhook to QQBot adapter."""

from __future__ import annotations

import json
import os
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
        self.assertIsInstance(mock_run.call_args.kwargs["env"], dict)

    @patch.dict(
        os.environ,
        {
            "QQBOT_GROUP_OPENID": "group-openid-1",
            "QQBOT_HOME_CHANNEL": "private-openid-1",
        },
        clear=False,
    )
    @patch("scripts.qqbot_webhook_bridge.subprocess.run")
    def test_send_to_qqbot_scopes_group_target_to_child_process(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = json.dumps(
            {"success": True, "platform": "qqbot", "message_id": "message-2"}
        )
        mock_run.return_value.stderr = ""

        send_to_qqbot("群聊报告", hermes_path="/opt/hermes", timeout_seconds=30)

        command_env = mock_run.call_args.kwargs["env"]
        self.assertEqual(command_env["QQBOT_HOME_CHANNEL"], "group-openid-1")
        self.assertEqual(os.environ["QQBOT_HOME_CHANNEL"], "private-openid-1")


if __name__ == "__main__":
    unittest.main()
