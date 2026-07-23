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
rewrite report content. When `QQBOT_GROUP_OPENID` is configured, the bridge
uses the explicit `qqbot:<group-openid>` target and rejects a successful
response whose returned `chat_id` does not match that group.

## Start the adapter

Run it as the OS user whose Hermes home channel is configured:

```bash
export QQBOT_BRIDGE_BEARER_TOKEN='replace-with-a-random-secret'
export QQBOT_HERMES_PATH='/home/ubuntu/.local/bin/hermes'
# Optional: route only this bridge to an explicit QQ group without changing
# Hermes' existing private home channel.
export QQBOT_GROUP_OPENID='group-openid-from-an-inbound-group-at-event'
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

The adapter parses the JSON value and passes the report body to `hermes send`
as its plain positional message argument. The trailing `--json` controls only
Hermes' command result. It does not JSON-encode the message, so embedded
newlines remain newline characters instead of visible `\n` text.

## Smoke test

```bash
curl -fsS \
  -H "Authorization: Bearer ${QQBOT_BRIDGE_BEARER_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"text":"daily_stock_analysis QQBot test\nsecond line"}' \
  http://127.0.0.1:18770/notify
```

Do not expose the bridge port publicly or commit its bearer token.
Keep `QQBOT_GROUP_OPENID` in the host environment file; do not commit it.

## QQ official group limitation

An ordinary QQ official bot may receive group `@` events but still lack
permission for proactive group messages. Scheduled reports have no inbound
`msg_id`, so they cannot use QQ's short passive-reply window. In that case the
group endpoint returns error `40034105` (`proactive message not permitted`).

The bridge must not treat a successful C2C fallback as a successful group
delivery. It therefore uses an explicit group target and verifies that
Hermes returns the same `chat_id`. Keep the scheduled notification disabled
unless an end-to-end test confirms delivery to the group, or use one of:

- QQ private C2C delivery;
- a passive group command that returns the latest stored report after `@bot`;
- another notification platform that supports proactive group webhooks.
