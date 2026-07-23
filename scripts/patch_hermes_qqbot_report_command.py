#!/usr/bin/env python3
"""Install an idempotent QQBot passive-report trigger into Hermes.

Hermes quick commands bypass the agent loop, but QQ group messages arrive as
plain text after the bot mention is stripped. This narrow hook rewrites one
configured phrase to a quick command and drops every other message when
command-only mode is enabled.
"""

from __future__ import annotations

import argparse
from pathlib import Path


MARKER = "# daily-stock-analysis passive QQ report hook"
MULTI_REPLY_MARKER = "# daily-stock-analysis retain passive reply id"
ANCHOR = "        text = self._strip_at_mention(content)\n"
CHUNKS_ANCHOR = (
    "        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)\n"
)
REPLY_RESET = """\
            # Only reply_to the first chunk
            reply_to = None
"""
HOOK = """\
        # daily-stock-analysis passive QQ report hook
        report_trigger = os.getenv("QQBOT_PASSIVE_REPORT_TRIGGER", "").strip()
        if report_trigger:
            if text.strip() == report_trigger:
                text = "/a-stock-report"
            elif os.getenv(
                "QQBOT_PASSIVE_REPORT_COMMAND_ONLY", ""
            ).strip().lower() in {"1", "true", "yes", "on"}:
                return
"""
CHUNKS_GUARD = """\
        # daily-stock-analysis retain passive reply id
        if reply_to and len(chunks) > 5:
            return SendResult(
                success=False,
                error="Passive QQ reply exceeds the 5-message limit",
            )
"""
RETAIN_REPLY = """\
            # Keep reply_to for every passive chunk. QQ permits up to five
            # replies to the same inbound message; clearing it makes later
            # chunks unauthorized proactive messages.
"""
def install_hook(source: str) -> str:
    """Return adapter source with trigger and passive multi-reply patches."""
    updated = source
    if MARKER not in updated:
        if updated.count(ANCHOR) != 1:
            raise ValueError("expected QQBot mention-strip anchor exactly once")
        updated = updated.replace(ANCHOR, ANCHOR + HOOK, 1)

    if MULTI_REPLY_MARKER not in updated:
        if updated.count(CHUNKS_ANCHOR) != 1:
            raise ValueError("expected QQBot chunks anchor exactly once")
        if updated.count(REPLY_RESET) != 1:
            raise ValueError("expected QQBot reply reset exactly once")
        updated = updated.replace(
            CHUNKS_ANCHOR,
            CHUNKS_ANCHOR + CHUNKS_GUARD,
            1,
        )
        updated = updated.replace(REPLY_RESET, RETAIN_REPLY, 1)

    return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Patch Hermes QQBot adapter for passive report commands"
    )
    parser.add_argument("adapter_path", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = args.adapter_path.resolve()
    original = path.read_text(encoding="utf-8")
    updated = install_hook(original)
    if updated == original:
        print("already-installed")
        return 0
    path.write_text(updated, encoding="utf-8")
    print("installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
