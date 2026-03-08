<div align="center">

  ![Telegram Sendmail Bridge](docs/assets/banner-dark.png#gh-dark-mode-only)
  ![Telegram Sendmail Bridge](docs/assets/banner.png#gh-light-mode-only)

  A drop-in `sendmail` replacement that forwards system emails to Telegram.

  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
  [![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/downloads/)
  [![CI](https://github.com/theodiv/telegram-sendmail/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/theodiv/telegram-sendmail/actions/workflows/ci.yml)

</div>

---

## Overview

`telegram-sendmail` intercepts emails that system daemons — cron, logwatch,
fail2ban, and others — send through the standard `sendmail` interface and
forwards them to a Telegram chat as formatted messages. It operates as a true
drop-in replacement: any application that calls `/usr/sbin/sendmail` will work
without modification.

Two modes of operation are supported:

- **Pipe mode** (default): reads a raw RFC 2822 email from `stdin`, the
  standard interface used by cron and the majority of Unix system daemons.
- **SMTP server mode** (`-bs`): runs a minimal SMTP state machine on
  `stdin`/`stdout` for applications that speak SMTP directly to the
  `sendmail` binary.

## Features

- Parses both plain-text and HTML MIME emails
- Sanitises HTML through a two-pass pipeline: dangerous tags (`<script>`,
  `<style>`, `<iframe>`, etc.) and their entire subtrees are stripped before
  conversion; safe content is then mapped to Telegram's supported markup subset
- Detects and reports MIME attachments without forwarding binary content or
  disclosing filenames
- Smart message truncation at word boundaries with a configurable length limit
- Archives every raw email to a local spool file before attempting Telegram
  delivery, ensuring no message is lost if the network is unavailable
- Structured logging to syslog (`LOG_MAIL` facility) with an optional
  `--console` mode for interactive debugging
- Full `sendmail`-compatible flag support: `-f`, `-r`, `-s`, `-bs`, `-t`,
  `-i`, `-oi`, plus silent acceptance of positional recipient arguments
- Configurable HTTP retry strategy with exponential backoff for resilience
  against transient Telegram API failures
- `EX_TEMPFAIL` (exit code 75) signalling on HTTP 429/5xx so MTA-aware daemons
  re-queue and retry automatically
- Distributed as a standalone binary with no runtime dependencies via the
  release pipeline

## Requirements

The standalone binary has no runtime requirements. Source-based installation
requires:

- Python 3.10 or later
- [`html2text`](https://pypi.org/project/html2text/) ≥ 2024.2.26
- [`requests`](https://pypi.org/project/requests/) ≥ 2.31.0

## Installation

### Step 1 — Install the binary

**Option A — Pre-built standalone binary (recommended):**

Standalone binaries for `x86_64` and `aarch64` are attached to every
[GitHub Release](https://github.com/theodiv/telegram-sendmail/releases).
They have no runtime dependencies — Python is not required on the target host.

Both binaries are built on Ubuntu 22.04 and link against **glibc 2.35**. They
are forward-compatible with any Linux distribution shipping glibc 2.35 or newer
(Debian 12, Ubuntu 22.04+, RHEL 9+, Fedora 36+). Source-based installation
is required on older distributions.

```bash
# Detect the host architecture (x86_64 or aarch64)
ARCH=$(uname -m)

# Download the binary and its SHA-256 checksum
curl -LO "https://github.com/theodiv/telegram-sendmail/releases/latest/download/telegram-sendmail-linux-${ARCH}"
curl -LO "https://github.com/theodiv/telegram-sendmail/releases/latest/download/telegram-sendmail-linux-${ARCH}.sha256"

# Verify integrity before installing
sha256sum --check "telegram-sendmail-linux-${ARCH}.sha256"

# Install to a system PATH location and verify
sudo install -m 0755 "telegram-sendmail-linux-${ARCH}" /usr/local/bin/telegram-sendmail
telegram-sendmail --version
```

**Option B — Install from PyPI**

Requires Python 3.10 or later on the target host.

```bash
# Recommended: install with pipx (automatic isolation)
pipx install telegram-sendmail

# Alternative: install into the system Python
pip install telegram-sendmail

# Verify the installation
telegram-sendmail --version
```

### Step 2 — Create a configuration file

Create `/etc/telegram-sendmail.ini` (system-wide) or
`~/.telegram-sendmail.ini` (per-user). The user config takes priority when
both files exist. A fully-commented template covering every available key is
included in the repository as
[`telegram-sendmail.ini.example`](telegram-sendmail.ini.example).

```bash
# Source installs: the example file is available in the repository clone
sudo cp telegram-sendmail.ini.example /etc/telegram-sendmail.ini

# Binary installs: download the template from the repository
sudo curl -L https://raw.githubusercontent.com/theodiv/telegram-sendmail/main/telegram-sendmail.ini.example -o \
    /etc/telegram-sendmail.ini

sudo chmod 600 /etc/telegram-sendmail.ini
sudo $EDITOR /etc/telegram-sendmail.ini
```

> **Security:** The config file contains the bot token. Permissions must be
> `0600`. A `WARNING` is emitted to syslog if the file is group- or
> world-readable, but execution is not blocked — the operator is expected to
> correct the permissions.

### Step 3 — Register as the system `sendmail`

`telegram-sendmail` intentionally does **not** register itself as
`/usr/sbin/sendmail` during installation to avoid silently overwriting an
existing MTA.

**Option A — Simple symlink (no existing MTA):**

```bash
TGSM_BIN=$(which telegram-sendmail)
sudo ln -sf "$TGSM_BIN" /usr/sbin/sendmail
sudo ln -sf "$TGSM_BIN" /usr/bin/sendmail
```

**Option B — `update-alternatives` (Debian/Ubuntu, existing MTA present):**

```bash
TGSM_BIN=$(which telegram-sendmail)

sudo update-alternatives --install \
    /usr/sbin/sendmail sendmail "$TGSM_BIN" 50

# Interactively select the active alternative
sudo update-alternatives --config sendmail
```

Set the priority value (`50` above) lower than the existing MTA's registered
priority if `telegram-sendmail` should serve as a fallback, or higher to make
it the active default.

To revert to the original MTA:

```bash
sudo update-alternatives --remove sendmail "$TGSM_BIN"
```

## Configuration

All configuration lives in a single INI file with two required keys and a
number of optional tuning parameters.

```ini
[telegram]
; Required — Telegram Bot API token from @BotFather
token = 123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

; Required — destination chat ID (personal, group, or channel)
chat_id = -1001234567890

[options]
; All keys below are optional. Out-of-range or malformed values fall
; back to their documented defaults with a WARNING logged to syslog.

; Spool directory. Effective path: <spool_dir>/<username>
; Default: /var/mail
spool_dir = /var/mail

; Maximum body length forwarded to Telegram (100–4096). Default: 3800
message_max_length = 3800

; SMTP session idle timeout in seconds (5–300). Default: 30
smtp_timeout = 30

; Telegram API request timeout in seconds (5–60). Default: 10
telegram_timeout = 10

; Retry attempts on 429/5xx responses (0–10). Default: 3
max_retries = 3

; Exponential backoff multiplier (0.0–10.0). Default: 0.5
; Effective pause before attempt n: backoff_factor × 2^(n-1) seconds
backoff_factor = 0.5

; Suppress notification sound and banner. Default: false
disable_notification = false
```

The complete annotated template is provided in
[`telegram-sendmail.ini.example`](telegram-sendmail.ini.example).

## Mail Spool — Reliability Design

Every email is archived to a local spool file **before** any attempt is made
to contact the Telegram API. This guarantees that the original message is
preserved on disk regardless of network availability or downstream parsing
failures.

The spool file path is `<spool_dir>/<username>` (e.g. `/var/mail/root`). If
the configured directory is not writable at startup — common in container
environments — the daemon falls back to
`/tmp/.telegram-sendmail-spool/<username>` and emits a `WARNING` to syslog.
This hidden subdirectory is created automatically with `0700` DAC permissions
to prevent cross-user data exposure in the world-writable `/tmp` filesystem.

A `SpoolError` is explicitly non-fatal: if the spool write fails, the message
is still forwarded to Telegram and the failure is logged at `WARNING` level.

File permissions on the spool file are enforced to `0600` via `os.fchmod`
against the open file descriptor on every write. This eliminates the TOCTOU
race condition that would exist between file creation and a subsequent
`os.chmod` call.

## Usage

In normal operation the tool is invoked by system daemons through the
`sendmail` interface. The examples below are useful for integration testing
and interactive debugging.

```bash
# Basic pipe mode
echo -e "Subject: Test\n\nThis is a test message." | telegram-sendmail

# Explicit sender and subject override
echo "Disk usage critical." | telegram-sendmail -f root@hostname -s "Disk Alert"

# As called by cron (positional recipient is silently discarded)
echo "Cron job failed." | telegram-sendmail -oi -t root@localhost

# SMTP server mode
telegram-sendmail -bs

# Interactive debugging with console logging
echo "Hello" | telegram-sendmail --console --debug
```

## Exit Codes

| Code | Name          | Meaning                                                                 |
|------|---------------|-------------------------------------------------------------------------|
| `0`  | `EX_OK`       | Message delivered successfully to Telegram.                             |
| `1`  | `EX_ERROR`    | Operational failure: parse error, API error, or unexpected exception.   |
| `75` | `EX_TEMPFAIL` | Transient failure (HTTP 429/5xx). Daemons will re-queue and retry.      |
| `78` | `EX_CONFIG`   | Configuration error: file missing, unreadable, or missing required key. |

Exit code `75` is the standard `EX_TEMPFAIL` value from `sysexits.h`.
POSIX-aware daemons interpret this as a signal to retry later rather than
treating the message as permanently undeliverable.

## Troubleshooting

### Checking logs

All output goes to syslog under the `LOG_MAIL` facility:

```bash
# Live log stream
journalctl -t telegram-sendmail -f

# Recent entries
journalctl -t telegram-sendmail --since "1 hour ago"
```

On systems without `journald`, check `/var/log/mail.log` or `/var/log/syslog`.

### Common issues

**No messages arriving in Telegram** — Verify `token` and `chat_id`, confirm
the bot has been started (`/start` in the Telegram app), and check that
outbound HTTPS to `api.telegram.org` is permitted by the host firewall.
Running with `--console --debug` reveals the full API error payload.

**Permission denied on config file** — The file must be owned by the process
user with `0600` permissions:

```bash
sudo chmod 600 /etc/telegram-sendmail.ini
sudo chown root:root /etc/telegram-sendmail.ini
```

**Cron emails not forwarded** — Verify the symlink resolves correctly:

```bash
ls -la /usr/sbin/sendmail
/usr/sbin/sendmail --version
```

**Spool directory warning in logs** — Either make the spool directory writable
by the process user, or set `spool_dir` in `[options]` to a path that is.

**Binary fails to start with "GLIBC not found" error** — The pre-built binary
requires glibc 2.35 or newer. Install from source on older distributions.

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md)
before opening a pull request.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a full history of notable changes.

## Security

Security vulnerabilities must not be disclosed via public issues. See
[SECURITY.md](SECURITY.md) for the responsible disclosure process.

---

<p align="center">
  Distributed under the MIT License &nbsp;•&nbsp; Made with ♥ for the Linux sysadmin community
</p>
