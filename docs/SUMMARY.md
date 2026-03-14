# telegram-sendmail

A drop-in `sendmail` replacement that forwards system emails to Telegram.

`telegram-sendmail` intercepts emails that system daemons — cron, logwatch,
fail2ban, and others — send through the standard `sendmail` interface and
forwards them to a Telegram chat as formatted messages. Any application that
calls `/usr/sbin/sendmail` works without modification.

Two delivery modes:

- **Pipe mode** (default) — reads a raw RFC 2822 email from `stdin`, the
  standard interface used by cron and the majority of Unix system daemons.
- **SMTP server mode** (`-bs`) — runs a minimal SMTP state machine on
  `stdin`/`stdout` for applications that speak SMTP directly to `sendmail`.

## Features

- Parses plain-text and HTML MIME emails; sanitises HTML through a two-pass
  pipeline before conversion to Telegram-compatible markup
- Detects MIME attachments without forwarding binary content
- Archives every raw email to a local spool file before Telegram delivery;
  no message is lost if the network is unavailable
- Smart message truncation at word boundaries with a configurable length limit
- Structured logging to syslog (`LOG_MAIL` facility) with an optional
  `--console` mode for interactive debugging
- Full `sendmail`-compatible flag support: `-f`, `-r`, `-s`, `-bs`, `-t`,
  `-i`, `-oi`, plus silent acceptance of positional recipient arguments
- Configurable HTTP retry strategy with exponential backoff
- Suppresses known-noisy messages via case-insensitive glob patterns on
  `Subject` and `From` headers
- `EX_TEMPFAIL` (exit code 75) on HTTP 429/5xx so MTA-aware daemons
  re-queue and retry automatically

## Requirements

- Python 3.10 or later
- [`html2text`](https://pypi.org/project/html2text/) ≥ 2024.2.26
- [`requests`](https://pypi.org/project/requests/) ≥ 2.31.0

## Installation

Pre-built binaries for `x86_64` and `aarch64` with no runtime dependencies
are attached to every [GitHub Release](https://github.com/theodiv/telegram-sendmail/releases).
Installing from PyPI requires Python 3.10 or later:

```bash
# Recommended: install with pipx (automatic isolation)
pipx install telegram-sendmail

# Alternative: install into the system Python
pip install telegram-sendmail
```

## Configuration

Create `/etc/telegram-sendmail.ini` (system-wide) or
`~/.telegram-sendmail.ini` (per-user):

```ini
[telegram]
token   = 123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
chat_id = -1001234567890

[options]
; All keys are optional — out-of-range values fall back to defaults.
;
; spool_dir             = /var/mail
; message_max_length    = 3800      (100–4096)
; smtp_timeout          = 30        (5–300)
; telegram_timeout      = 10        (5–60)
; max_retries           = 3         (0–10)
; backoff_factor        = 0.5       (0.0–10.0)
; disable_notification  = false

[filters]
; suppress_subject =                (case-insensitive globs)
; suppress_sender  =                (case-insensitive globs)
```

The config file contains the bot token. Permissions **must** be `0600`; a
`WARNING` is emitted to syslog on group- or world-readable files.

```bash
sudo chmod 600 /etc/telegram-sendmail.ini
```

A fully-annotated template covering every available key is available at
[`telegram-sendmail.ini.example`](https://github.com/theodiv/telegram-sendmail/blob/main/telegram-sendmail.ini.example).

## Registering as the system `sendmail`

`telegram-sendmail` does not register itself as `/usr/sbin/sendmail`
automatically to avoid silently overwriting an existing MTA.

**Simple symlink (no existing MTA):**

```bash
TGSM_BIN=$(which telegram-sendmail)
sudo ln -sf "$TGSM_BIN" /usr/sbin/sendmail
sudo ln -sf "$TGSM_BIN" /usr/bin/sendmail
```

**`update-alternatives` (Debian/Ubuntu, existing MTA present):**

```bash
TGSM_BIN=$(which telegram-sendmail)
sudo update-alternatives --install /usr/sbin/sendmail sendmail "$TGSM_BIN" 50
sudo update-alternatives --config sendmail
```

## Exit Codes

| Code | Name          | Meaning                                                                 |
|------|---------------|-------------------------------------------------------------------------|
| `0`  | `EX_OK`       | Message delivered successfully to Telegram.                             |
| `1`  | `EX_ERROR`    | Operational failure: parse error, API error, or unexpected exception.   |
| `75` | `EX_TEMPFAIL` | Transient failure (HTTP 429/5xx). Daemons will re-queue and retry.      |
| `78` | `EX_CONFIG`   | Configuration error: file missing, unreadable, or missing required key. |

---

For comprehensive setup instructions, configuration file templates, usage
examples, and troubleshooting guides, refer to the full README on the
[telegram-sendmail GitHub repository](https://github.com/theodiv/telegram-sendmail).
