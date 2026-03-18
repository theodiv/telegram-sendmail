# Telegram Sendmail

`telegram-sendmail` replaces `/usr/sbin/sendmail` on Linux hosts that do
not need a full MTA. It accepts email input via the standard pipe interface
used by cron, logwatch, fail2ban, and other system daemons, converts it to
Telegram-compatible markup, and delivers it to a configured Telegram chat.
An SMTP server mode (`-bs`) is also provided for applications that speak
SMTP directly to the `sendmail` binary.

## Features

- **Zero data loss** — every raw email is spooled to disk before any network
  call; if Telegram is unreachable, the original message is preserved.
- **True drop-in** — supports all `sendmail` flags system daemons use
  in practice (`-f`, `-r`, `-s`, `-bs`, `-t`, `-i`, `-oi`) and ships
  as a standalone binary with no runtime dependencies.
- **Smart filtering** — suppresses known-noisy messages via glob patterns
  on `Subject` and `From` headers, keeping the chat focused on notifications
  that matter.
- **Retry-aware** — exponential backoff on HTTP 429/5xx with `EX_TEMPFAIL`
  (75) signalling so MTA-aware daemons re-queue automatically.
- **HTML-safe** — two-pass sanitisation strips dangerous tags, converts
  safe markup to Telegram's HTML subset, detects attachments without
  forwarding binary content, and truncates long bodies at word boundaries.
- **Observable** — structured syslog output (`LOG_MAIL` facility) with
  `--console` and `--debug` modes for interactive diagnosis, and a `--probe`
  flag for post-deploy verification, suitable for provisioning pipelines and
  smoke tests.

## Requirements

The standalone binary (available from
[GitHub Releases](https://github.com/theodiv/telegram-sendmail/releases))
has no runtime dependencies — Python is not required on the target host.

For PyPI installations, the following are required:

- Python 3.10 or later
- Runtime dependencies (installed automatically by `pip`):
  - [html2text](https://pypi.org/project/html2text/) ≥ 2024.2.26
  - [requests](https://pypi.org/project/requests/) ≥ 2.31.0

## Quick Start

### Installation

```bash
# Recommended: install with pipx for automatic isolation
pipx install telegram-sendmail

# Alternative: install into the system Python
pip install telegram-sendmail
```

### Creating a Telegram Bot

1. Open [@BotFather](https://t.me/botfather) in Telegram, send `/newbot`,
   and follow the instructions. Copy the bot token it generates.
2. Send any message to the new bot or add it to a group or channel, then
   visit `https://api.telegram.org/bot<TOKEN>/getUpdates`. The `chat.id`
   field in the response provides the required `chat_id`.

### Configuration

Create the configuration file with the two required keys:

```bash
sudo tee /etc/telegram-sendmail.ini > /dev/null << 'EOF'
[telegram]
token = 123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
chat_id = -1001234567890
EOF

sudo chmod 600 /etc/telegram-sendmail.ini
sudo chown root:root /etc/telegram-sendmail.ini
```

The per-user path `~/.telegram-sendmail.ini` is also supported and takes
precedence over the system-wide file when both are present.

### Registering as the System `sendmail`

```bash
TGSM_BIN=$(which telegram-sendmail)
sudo ln -sf "$TGSM_BIN" /usr/sbin/sendmail
```

For the `update-alternatives` method (when an existing MTA is present), see
the [User Guide](https://github.com/theodiv/telegram-sendmail/blob/main/docs/USER_GUIDE.md#registering-as-the-system-sendmail).

### Verifying the Installation

```bash
telegram-sendmail --probe --console --debug
```

Exit code `0` confirms end-to-end connectivity. `78` indicates a
configuration error, `75` a transient network failure, and `1` a permanent
API rejection.

## Full Documentation

The complete user guide — covering all installation methods, the full
configuration reference, SMTP server mode, the delivery pipeline,
troubleshooting, and security hardening — is available in the
[GitHub repository](https://github.com/theodiv/telegram-sendmail).
