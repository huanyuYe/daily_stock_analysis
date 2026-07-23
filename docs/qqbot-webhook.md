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

## Passive A-share report command

For QQ groups, the supported workflow is:

1. A group member sends `@机器人 a股报告`.
2. If a report exists within the retention window, the bot immediately
   returns its decision-dashboard summary.
3. If no report exists, the bot starts the configured analysis service in the
   background and immediately confirms that generation has started.
4. The member sends `@机器人 a股报告` again after generation finishes.

The reply contains only the dashboard section before the first Markdown
horizontal rule. This keeps the response inside one QQ passive message;
subsequent chunks would otherwise be treated as prohibited proactive group
messages.

The deterministic path is:

```text
QQ GROUP_AT_MESSAGE_CREATE
  -> exact phrase rewrite to /a-stock-report
  -> Hermes quick_commands exec
  -> scripts/qqbot_passive_report.py handle
  -> cached summary OR systemctl start --no-block
```

The exact phrase rewrite and command-only gate are installed with:

```bash
python scripts/patch_hermes_qqbot_report_command.py \
  /path/to/hermes/gateway/platforms/qqbot/adapter.py
```

Configure the QQBot gateway environment:

```dotenv
QQBOT_PASSIVE_REPORT_TRIGGER=a股报告
QQBOT_PASSIVE_REPORT_COMMAND_ONLY=true
```

Configure only the intended group in Hermes:

```yaml
platforms:
  qqbot:
    extra:
      group_policy: allowlist
      group_allow_from:
        - "GROUP_OPENID"

quick_commands:
  a-stock-report:
    type: exec
    command: "/usr/bin/sudo -n /path/to/venv/bin/python /path/to/project/scripts/qqbot_passive_report.py handle"
```

`QQBOT_PASSIVE_REPORT_COMMAND_ONLY=true` makes the adapter discard every other
message from the allowed group before gateway dispatch, so those messages do
not enter an Agent session.

The patch also keeps the inbound QQ `msg_id` on every response chunk. QQ group
passive replies allow at most five responses to one inbound message; without
that ID only the first chunk is passive and later chunks fail as unauthorized
proactive messages. The adapter therefore rejects output requiring more than
five chunks instead of silently sending a partial report.

The report command renders the decision dashboard and one compact detail block
for every symbol. Each block preserves the action, score, trend, news digest,
position-specific advice, quote, moving averages, guardrail, watch conditions,
operation levels, position sizing, data risks, strongest signals, and sectors.
Wide Markdown tables are flattened to readable fields so all symbols fit within
QQ's passive-reply budget. The command verifies that every code listed in the
dashboard has a detail block and remains present in the final QQ payload.
Missing coverage fails closed; it never returns the first few symbols as if the
report were complete. The default `QQBOT_PASSIVE_REPORT_MAX_CHARS=12000`
normally produces three or fewer QQ replies and stays below the platform's
five-reply ceiling.

Grant the gateway OS user permission to execute only that fixed report
command. Do not allow arbitrary arguments:

```sudoers
gateway-user ALL=(root) NOPASSWD: /path/to/venv/bin/python /path/to/project/scripts/qqbot_passive_report.py handle
```

### Seven-day cleanup

Run the cleanup action daily:

```bash
python scripts/qqbot_passive_report.py --retention-days 7 cleanup
```

Example systemd units:

```ini
# /etc/systemd/system/daily-stock-analysis-report-cleanup.service
[Unit]
Description=Clean expired Daily Stock Analysis reports

[Service]
Type=oneshot
User=root
WorkingDirectory=/path/to/project
ExecStart=/path/to/venv/bin/python scripts/qqbot_passive_report.py --retention-days 7 cleanup
```

```ini
# /etc/systemd/system/daily-stock-analysis-report-cleanup.timer
[Unit]
Description=Daily cleanup for Daily Stock Analysis reports

[Timer]
OnCalendar=*-*-* 03:20:00
Persistent=true
RandomizedDelaySec=5m

[Install]
WantedBy=timers.target
```

### Official active group push

When the QQ Bot application has group proactive-message permission, use
`scripts/qqbot_active_report.py` to call the official QQ group API directly.
This path does not require an inbound message and does not invoke an Agent.

`scripts/run_a_share_and_push_qq.py` starts the analysis service synchronously,
then verifies that the aggregate report was created or updated during that run.
It refuses to push stale content when analysis fails, produces no report, or
skips a non-trading day.

The production timers in `scripts/` use Asia/Shanghai time on weekdays:

- premarket: 08:15, one hour before the opening call auction;
- midday: 11:35;
- postmarket: 15:35, after the 15:05-15:30 fixed-price session.

All three timers activate the same
`daily-stock-analysis-qqbot-active.service`; `flock` prevents overlapping
analysis runs. Disable the legacy loopback Webhook when this path is enabled to
avoid duplicate delivery.
