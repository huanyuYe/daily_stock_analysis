#!/usr/bin/env python3
"""Push a cached A-share report through the official QQ group Bot API."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from scripts.qqbot_passive_report import (
        DEFAULT_MAX_CHARS,
        DEFAULT_RETENTION_DAYS,
        build_qq_summary,
        find_latest_report,
    )
except ModuleNotFoundError:
    from qqbot_passive_report import (
        DEFAULT_MAX_CHARS,
        DEFAULT_RETENTION_DAYS,
        build_qq_summary,
        find_latest_report,
    )


TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
MAX_MESSAGE_CHARS = 3900
MAX_REPORT_PARTS = 5


def _post_json(
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"QQ API HTTP {exc.code}: {payload[:500]}"
        ) from exc
    result = json.loads(payload)
    if not isinstance(result, dict):
        raise RuntimeError("QQ API returned a non-object response")
    return result


def get_access_token(app_id: str, client_secret: str) -> str:
    result = _post_json(
        TOKEN_URL,
        {"appId": app_id, "clientSecret": client_secret},
    )
    token = result.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"QQ token response missing access_token: {result}")
    return token


def split_message(content: str) -> list[str]:
    """Split at paragraph boundaries while retaining every character."""
    chunks: list[str] = []
    current = ""
    for paragraph in content.split("\n\n"):
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= MAX_MESSAGE_CHARS:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(paragraph) > MAX_MESSAGE_CHARS:
            chunks.append(paragraph[:MAX_MESSAGE_CHARS])
            paragraph = paragraph[MAX_MESSAGE_CHARS:]
        current = paragraph
    if current:
        chunks.append(current)
    if len(chunks) > MAX_REPORT_PARTS:
        raise RuntimeError(
            f"report requires {len(chunks)} QQ messages; limit is {MAX_REPORT_PARTS}"
        )
    return chunks


def send_group_message(
    group_openid: str,
    content: str,
    *,
    token: str,
    msg_seq: int,
) -> dict[str, Any]:
    return _post_json(
        f"{API_BASE}/v2/groups/{group_openid}/messages",
        {
            "content": content,
            "msg_type": 0,
            "msg_seq": msg_seq,
        },
        headers={"Authorization": f"QQBot {token}"},
    )


def push_content(content: str) -> dict[str, Any]:
    app_id = os.environ["QQ_APP_ID"].strip()
    client_secret = os.environ["QQ_CLIENT_SECRET"].strip()
    group_openid = os.environ["QQBOT_GROUP_OPENID"].strip()
    token = get_access_token(app_id, client_secret)
    chunks = split_message(content)
    message_ids: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        result = send_group_message(
            group_openid,
            chunk,
            token=token,
            msg_seq=index,
        )
        message_ids.append(str(result.get("id", "")))
        if index < len(chunks):
            time.sleep(1)
    return {
        "success": True,
        "parts": len(chunks),
        "characters": len(content),
        "message_ids": message_ids,
    }


def build_latest_report(reports_dir: Path) -> str:
    latest = find_latest_report(
        reports_dir,
        retention_days=DEFAULT_RETENTION_DAYS,
    )
    if latest is None:
        raise RuntimeError("no report from the last 7 days is available")
    return build_qq_summary(latest, max_chars=DEFAULT_MAX_CHARS)


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("test", "report"))
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=project_root / "reports",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.action == "test":
        content = (
            "QQBot 主动推送权限测试\n"
            "权限验证成功；系统将在 10 分钟后推送最新 A 股分析报告。"
        )
    else:
        content = build_latest_report(args.reports_dir)
    print(json.dumps(push_content(content), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
