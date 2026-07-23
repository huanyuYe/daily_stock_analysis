"""Tests for official QQ active report delivery helpers."""

from __future__ import annotations

import unittest

from scripts.qqbot_active_report import (
    MAX_MESSAGE_CHARS,
    MAX_REPORT_PARTS,
    split_message,
)


class QQBotActiveReportTest(unittest.TestCase):
    def test_split_message_preserves_all_content(self):
        content = "\n\n".join(
            [
                "A" * MAX_MESSAGE_CHARS,
                "B" * MAX_MESSAGE_CHARS,
                "C",
            ]
        )

        chunks = split_message(content)

        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(chunk) <= MAX_MESSAGE_CHARS for chunk in chunks))
        self.assertEqual(
            "".join(chunk.replace("\n\n", "") for chunk in chunks),
            content.replace("\n\n", ""),
        )

    def test_split_message_fails_before_partial_delivery(self):
        content = "\n\n".join(
            chr(65 + index) * MAX_MESSAGE_CHARS
            for index in range(MAX_REPORT_PARTS + 1)
        )

        with self.assertRaisesRegex(RuntimeError, "limit"):
            split_message(content)


if __name__ == "__main__":
    unittest.main()
