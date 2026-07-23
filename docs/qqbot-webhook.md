# QQBot Custom Webhook adapter

The project does not natively own QQ credentials. For hosts where a Hermes
`qqbot` channel is already configured, `scripts/qqbot_webhook_bridge.py`
provides a loopback-only adapter:

```text
NotificationService
  -> CustomWebhookSender
  -> http://127.0.0.1:18770/notify
  -> hermes send --to qqbot
  -> QQ
```

Hermes is used only as the configured transport adapter. The script calls
`hermes send` directly; it does not invoke an agent and does not generate or
rewrite report content.

## Start the adapter

Run it as the OS user whose Hermes home channel is configured:

```bash
export QQBOT_BRIDGE_BEARER_TOKEN='replace-with-a-random-secret'
export QQBOT_HERMES_PATH='/home/ubuntu/.local/bin/hermes'
python scripts/qqbot_webhook_bridge.py
```

The server rejects non-loopback bind addresses and requires the bearer token
for every `/notify` request. `/health` is an unauthenticated loopback health
check and does not expose configuration.

## Configure daily_stock_analysis

```dotenv
CUSTOM_WEBHOOK_URLS=http://127.0.0.1:18770/notify
CUSTOM_WEBHOOK_BEARER_TOKEN=replace-with-the-same-random-secret
CUSTOM_WEBHOOK_BODY_TEMPLATE={"text":$content_json,"source":"daily-stock-analysis","kind":"stock-report"}
```

The adapter parses the JSON value before invoking Hermes, so embedded newlines
remain newline characters instead of visible `\n` text.

## Smoke test

```bash
curl -fsS \
  -H "Authorization: Bearer ${QQBOT_BRIDGE_BEARER_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"text":"daily_stock_analysis QQBot test\nsecond line"}' \
  http://127.0.0.1:18770/notify
```

Do not expose the bridge port publicly or commit its bearer token.
