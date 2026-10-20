# Tailnet Usage

A single-page dashboard for Claude and Codex accounts across a Tailscale network.
Monitor account quotas and reset times, compare machine workloads, protect a
director account, and get Telegram alerts when assignments need attention.

Python 3.11+ standard library only. No frontend build or database server required.

## Quick start

Install Python, Tailscale, and the Claude/Codex CLIs on the relevant machines.
Configure SSH aliases and trusted host keys for remote machines. Collectors run
as the user who owns the CLI logins.

```sh
git clone https://github.com/collinschaafsma/tailnet-usage.git
cd tailnet-usage
cp config.example.json config.local.json
chmod 600 config.local.json
```

Edit `config.local.json` with your account emails, machine names, SSH destinations,
director preferences, and dashboard URL. This file is ignored by Git. A machine
with `"local": true` is collected on the dashboard host itself. Use Tailscale SSH
destinations for remote machines.

```sh
python3 server.py
# Open http://127.0.0.1:8787

# Or listen on this host's Tailscale IPv4 address:
python3 server.py --bind "$(tailscale ip -4)"
```

Open port 8787 on the host's Tailscale name or IP. Set `dashboard_url` to that
address for notification links. The server accepts only loopback or Tailscale
IPv4 bind addresses. No public hosting or Funnel is configured.

For startup, copy `tailnet-usage.service.example` into your systemd user units as
`tailnet-usage.service`, adjust its checkout location, CLI PATH, and bind address:

```sh
systemctl --user daemon-reload
systemctl --user enable --now tailnet-usage.service
```

The host and its user service must remain running for monitoring and alerts.

## Independent account monitoring

```sh
python3 accounts.py login all
# Or reconnect one configured account:
python3 accounts.py login director@example.com
```

Each Claude account gets an isolated dashboard `CLAUDE_CONFIG_DIR` under
`~/.local/state/tailnet-usage/profiles/<email>/` (or `$XDG_STATE_HOME`). Usage remains
visible even when no worker uses that account. Worker logins are not changed and
their rotating refresh tokens are never copied into monitoring profiles.

Quotas poll every five minutes. Near access-token expiry, the dashboard runs
Claude's local noninteractive `/usage` command, then re-reads the credentials saved
by the CLI. On the tested CLI this reported zero model turns and zero model cost;
native token replacement, saved refresh-token rotation, and usage retrieval after
renewal were also verified. This is version-sensitive behavior, not a documented
standalone refresh API. Unexpected model activity pauses renewal.

Revoked or nonrenewable logins still need browser sign-in. Cards show token expiry,
last renewal, login expiry when available, and early warnings. Cached readings
survive restarts and are marked stale after failures. HTTP 429 backs off.

Claude quotas use an undocumented OAuth usage endpoint, which may change or
become unavailable. Codex uses documented app-server `account/read` and
`account/rateLimits/read` methods. Missing limits appear as unavailable, never zero.
Session means the provider's short quota window, not an individual conversation.
Quota is shared by account across machines.

## Workload and recommendations

Machines incrementally index numeric usage from local Claude JSONL history into
`~/.local/state/tailnet-usage/workload.sqlite3`. Only timestamps and token counts
are stored; conversation text is never cached or returned. Streamed responses
are deduplicated by message ID. Collection is bounded to eight seconds / 64 MiB
per normal poll. Warm the seven-day index with `python3 backfill_workload.py`.

The planner uses the higher of seven-day average daily tokens and last-24-hour
tokens, plus remaining quota and reset times. It balances worker assignments
together and gives a designated director first choice. The director account is
reserved exclusively by default; workers are told to pause if no other account
is usable. A healthy current director assignment is preferred to needless switches.

Defaults target 30% session and 20% weekly quota remaining for the director.
Override `recommendations` in your local configuration; `recommendations.json`
contains public defaults. `preferred_director_account` breaks ties.

After at least an hour and three stable intervals, quota changes are apportioned
by machine workload to estimate weekly percentage points consumed per day. Reset
boundaries, assignment changes, and long gaps are excluded.

Token counts include cached input and are a relative workload proxy, not an exact
quota conversion. Different tiers, models, outside usage, and workload changes
affect estimates. Recommendations reduce risk, without guaranteeing uninterrupted
access. Recommendations require an explicit Switch click to change a login.

## Switching accounts

Click **Switch** beside a recommendation to queue that machine's change. The
queue survives server restarts, checks roughly every 30 seconds, can be cancelled
before the write starts, and expires after 12 hours. All Claude processes must
close first, including idle CLI terminals and helpers. Processes are never killed
or restarted. Close Claude, wait for **Switched**, then start a new session.
Leave Claude closed while the write is applying; process checks cannot prevent a
new CLI from starting at precisely the same moment. Default CLI profiles only are
supported; custom profile directories or environment-provided logins need manual
switching. This changes Claude accounts only.

Before applying, the server rechecks the current recommendation, fresh usable
target quota, and the machine's identity. A changed recommendation blocks the
queued request until valid again or cancelled. Workers cannot take the director
reserve; move existing workers off a new reserve before switching the director.
Offline machines retry. On macOS, the user's login Keychain must be unlocked and
accessible to the SSH session; a locked Keychain blocks the switch.

The dashboard retains each profile's rotating refresh token. Switched machines
receive only short-lived access tokens over local stdin or SSH stdin. The server
synchronizes renewed access tokens for the same account, including while Claude
is running, without distributing refresh tokens. This uses native credential
files (and macOS Keychain), which are version-sensitive implementation details;
clients that retain cached credentials may need a restart after renewal. The
dashboard host must remain online. A manual browser login or account change stops
automatic synchronization rather than overwriting it. Sync failures appear in
the assignment row.

The original machine login is backed up privately under
`~/.local/state/tailnet-usage/switching/backup-<request-id>.json` with mode 0600.
Queue state contains account names and statuses only. No credentials are returned
through HTTP. Mutations require a per-server CSRF header and a matching browser
origin. Any tailnet member permitted to access this dashboard can also request
switches: restrict the port with tailnet ACLs as appropriate.

## Telegram alerts

1. Open [BotFather](https://t.me/BotFather), send `/newbot`, and create a dedicated bot.
2. On the dashboard host, run `python3 notifications.py setup`.
3. Paste the bot token at the hidden prompt.
4. Open the one-time pairing link on your phone and tap **Start**.

Only a private chat matching the pairing challenge and sender identity can be
connected. Existing webhooks are never removed. Pairing enables alerts and sends
a confirmation. Only outbound HTTPS is needed, with no public webhook or dashboard.

Alerts run once a minute even with the page closed. They cover director risks,
assignment changes, worker shortages, confirmed quota recovery, and login problems.
Director risks and confirmed account exhaustion are immediate; other conditions must persist two minutes. Fresh
quota readings are required, including after a reset.

Continuous conditions are sent once across restarts. Defaults are a one-hour
cooldown and six automatic submissions per rolling 24 hours. Critical director
alerts and confirmed exhaustion bypass cooldown, but not the cap. Telegram batches
up to 3,500 characters. Setup confirmations and test messages do not start the
operational cooldown or consume the automatic-alert cap.

```sh
python3 notifications.py status
python3 notifications.py test
python3 notifications.py disable
python3 notifications.py enable
python3 notifications.py retry
```

Private credentials live in
`~/.local/state/tailnet-usage/notification-config.json` (0600). The API never returns
the bot token or private chat ID. A durable outbox is saved before sending;
uncertain submissions are not blindly retried. Check the destination before
rearming. A provider acknowledgement is not proof a phone displayed an alert.
Telegram needs internet; the private dashboard link needs Tailscale.

For optional Twilio SMS use `python3 notifications.py setup-sms` and complete its
sender and recipient requirements. SMS has a 300-character limit and may have
sender charges and multiple billable segments. Legacy local `sms-config.json`
is used only if the provider-neutral configuration is absent.

## Privacy and public source

This repository contains code, synthetic fixtures, and generic examples. Actual
emails, machine destinations, local service files, and deployment details belong
in ignored local files. Credentials, profiles, workload databases, alert history,
and monitoring caches belong in private runtime state outside source control.
They must not be committed or included in public dotfile backups.

The running dashboard intentionally displays account emails and usage. It has no
separate application login: access is controlled by loopback/Tailscale binding,
SSH authentication, and tailnet ACLs. Tailnet members permitted to reach the port
can see this information. Publishing the source does not publish the dashboard.

```sh
git diff --cached
python3 -m unittest -v
```

## References

- [Codex app server](https://developers.openai.com/codex/app-server)
- [Claude authentication](https://code.claude.com/docs/en/authentication)
- [Reported short-lived CLI refresh race](https://github.com/anthropics/claude-code/issues/95822)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Twilio messaging](https://www.twilio.com/docs/messaging/api/message-resource)
