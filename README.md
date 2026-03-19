<div align="center">

  ![Telegram Sendmail Banner](docs/assets/banner-dark.png#gh-dark-mode-only)
  ![Telegram Sendmail Banner](docs/assets/banner.png#gh-light-mode-only)

  A drop-in `sendmail` replacement that forwards system emails to Telegram.

  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
  [![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/downloads/)
  [![CI](https://github.com/theodiv/telegram-sendmail/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/theodiv/telegram-sendmail/actions/workflows/ci.yml)

</div>

---

## Overview

`telegram-sendmail` replaces `/usr/sbin/sendmail` on Linux hosts that do not
need a full MTA. It intercepts emails that system daemons — cron, logwatch,
fail2ban, and others — send through the standard `sendmail` interface,
converts them to Telegram-compatible markup, and forwards them to a
configured Telegram chat.

Two modes of operation are supported:

- **Pipe mode** (default): reads a raw RFC 2822 email from `stdin`, the
  standard interface used by cron and the majority of Unix system daemons.
- **SMTP server mode** (`-bs`): runs a minimal SMTP state machine on
  `stdin`/`stdout` for applications that speak SMTP directly to the
  `sendmail` binary.

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

## Full Documentation

For all installation methods, the full configuration reference, CLI flags,
exit codes, troubleshooting, and uninstall instructions, see the complete
[User Guide](docs/USER_GUIDE.md).

## Quick Start

### Installation

Standalone binaries for `x86_64` and `aarch64` are attached to every
[GitHub Release](https://github.com/theodiv/telegram-sendmail/releases).
They have no runtime dependencies — Python is not required on the target
host.

```bash
# Detect the host architecture (x86_64 or aarch64)
ARCH=$(uname -m)

# Download the binary and its SHA-256 checksum
curl -LO "https://github.com/theodiv/telegram-sendmail/releases/latest/download/telegram-sendmail-linux-${ARCH}"
curl -LO "https://github.com/theodiv/telegram-sendmail/releases/latest/download/telegram-sendmail-linux-${ARCH}.sha256"

# Verify integrity before installing
sha256sum --check "telegram-sendmail-linux-${ARCH}.sha256"

# Install to a system PATH location
sudo install -m 0755 "telegram-sendmail-linux-${ARCH}" \
    /usr/local/bin/telegram-sendmail

# Verify the installation
telegram-sendmail --version
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

### Registering as the System `sendmail`

```bash
TGSM_BIN=$(which telegram-sendmail)
sudo ln -sf "$TGSM_BIN" /usr/sbin/sendmail
```

For the `update-alternatives` method (when an existing MTA is present), see
the [User Guide](docs/USER_GUIDE.md#registering-as-the-system-sendmail).

### Verifying the Installation

```bash
telegram-sendmail --probe --console --debug
```

Exit code `0` confirms end-to-end connectivity: the configuration file is
valid, the bot token is accepted, and the `chat_id` is reachable. Exit code
`78` indicates a configuration error, `75` a transient network failure, and
`1` a permanent API rejection.

## Contributing

Development setup, coding standards, and the pull request process are
documented in [CONTRIBUTING.md](CONTRIBUTING.md).

## Code of Conduct

Behavioural expectations for all project participants are governed by
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Changelog

A detailed version history is maintained in [CHANGELOG.md](CHANGELOG.md).

## Security

The threat model, hardening recommendations, and vulnerability disclosure
process are documented in [SECURITY.md](SECURITY.md).

---

<p align="center">
  Distributed under the MIT License &nbsp;•&nbsp; Made with ♥ for the Linux sysadmin community
</p>
