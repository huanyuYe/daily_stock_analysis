#!/usr/bin/env python3
"""Loopback-only Custom Webhook adapter for a configured Hermes QQBot channel.

The stock analysis process POSTs its existing Custom Webhook payload to this
adapter. The adapter validates a bearer token, extracts the report text, and
invokes ``hermes send`` directly. No agent or gateway-generated content is
involved.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Optional


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18770
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
TEXT_FIELDS = ("text", "content", "message", "body")


def extract_report_text(payload: Mapping[str, Any]) -> Optional[str]:
    """Return the first non-empty report text supported by Custom Webhook."""
    for key in TEXT_FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def send_to_qqbot(content: str, *, hermes_path: str, timeout_seconds: int) -> dict[str, Any]:
    """Send one report through Hermes' already-configured QQBot channel."""
    command = [
        hermes_path,
        "send",
        "--to",
        "qqbot",
        content,
        "--json",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "unknown Hermes error").strip()
        raise RuntimeError(f"Hermes exited with code {completed.returncode}: {error[:500]}")

    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Hermes returned a non-JSON response") from exc
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError("Hermes reported an unsuccessful QQBot delivery")
    return result


class QQBotWebhookHandler(BaseHTTPRequestHandler):
    """Authenticated HTTP handler used by the loopback bridge."""

    server_version = "DailyStockAnalysisQQBotBridge/1.0"

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/notify":
            self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        if not self._authorized():
            self._write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return

        content_length = self.headers.get("Content-Length", "")
        try:
            body_length = int(content_length)
        except ValueError:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_content_length"})
            return
        if body_length <= 0 or body_length > self.server.max_body_bytes:
            self._write_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "body_too_large"})
            return

        try:
            payload = json.loads(self.rfile.read(body_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
            return
        if not isinstance(payload, dict):
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "json_object_required"})
            return

        content = extract_report_text(payload)
        if content is None:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "report_text_required"})
            return

        try:
            result = send_to_qqbot(
                content,
                hermes_path=self.server.hermes_path,
                timeout_seconds=self.server.hermes_timeout_seconds,
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.log_error("QQBot delivery failed: %s", exc)
            self._write_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": "qqbot_delivery_failed"})
            return

        self._write_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "platform": result.get("platform", "qqbot"),
                "message_id": result.get("message_id"),
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._write_json(HTTPStatus.OK, {"ok": True})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.bearer_token}"
        actual = self.headers.get("Authorization", "")
        return hmac.compare_digest(actual, expected)

    def _write_json(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[qqbot-bridge] {self.address_string()} - {fmt % args}", flush=True)


class QQBotWebhookServer(ThreadingHTTPServer):
    """Server state shared with request handlers."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        bearer_token: str,
        hermes_path: str,
        hermes_timeout_seconds: int,
        max_body_bytes: int,
    ) -> None:
        self.bearer_token = bearer_token
        self.hermes_path = hermes_path
        self.hermes_timeout_seconds = hermes_timeout_seconds
        self.max_body_bytes = max_body_bytes
        super().__init__(server_address, QQBotWebhookHandler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forward Custom Webhook reports to a Hermes QQBot channel")
    parser.add_argument("--host", default=os.getenv("QQBOT_BRIDGE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("QQBOT_BRIDGE_PORT", str(DEFAULT_PORT))))
    parser.add_argument(
        "--hermes-path",
        default=os.getenv("QQBOT_HERMES_PATH", "/home/ubuntu/.local/bin/hermes"),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("QQBOT_HERMES_TIMEOUT_SECONDS", "60")),
    )
    parser.add_argument(
        "--max-body-bytes",
        type=int,
        default=int(os.getenv("QQBOT_BRIDGE_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES))),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bearer_token = os.getenv("QQBOT_BRIDGE_BEARER_TOKEN", "").strip()
    if not bearer_token:
        raise SystemExit("QQBOT_BRIDGE_BEARER_TOKEN is required")
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("QQBot bridge must bind to a loopback address")

    server = QQBotWebhookServer(
        (args.host, args.port),
        bearer_token=bearer_token,
        hermes_path=args.hermes_path,
        hermes_timeout_seconds=args.timeout,
        max_body_bytes=args.max_body_bytes,
    )
    print(f"[qqbot-bridge] listening on http://{args.host}:{args.port}/notify", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
