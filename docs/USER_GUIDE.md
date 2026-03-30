# Telegram Sendmail User Guide

## Table of Contents

- [Introduction](#introduction)
- [Requirements](#requirements)
- [Installation](#installation)
  - [Option A — Pre-built Standalone Binary](#option-a--pre-built-standalone-binary-recommended)
  - [Option B — Install from PyPI](#option-b--install-from-pypi)
  - [Option C — Install from Source](#option-c--install-from-source)
- [Configuration](#configuration)
  - [Creating a Telegram Bot](#creating-a-telegram-bot)
  - [Configuration File Reference](#configuration-file-reference)
  - [Email Filtering](#email-filtering)
- [Registering as the System `sendmail`](#registering-as-the-system-sendmail)
  - [Option A — Simple Symlink](#option-a--simple-symlink-no-existing-mta)
  - [Option B — `update-alternatives`](#option-b--update-alternatives-debianubuntu-existing-mta-present)
- [SMTP Server Mode](#smtp-server-mode)
  - [Supported Commands](#supported-commands)
  - [Message Size Limit](#message-size-limit)
  - [Dot-Stuffing](#dot-stuffing)
  - [Session Reset](#session-reset)
  - [Graceful Shutdown](#graceful-shutdown)
  - [Timeout](#timeout)
- [CLI Reference](#cli-reference)
- [Delivery Pipeline](#delivery-pipeline)
- [Mail Spooling](#mail-spooling)
- [HTML Processing Pipeline](#html-processing-pipeline)
- [Security Considerations](#security-considerations)
- [Exit Codes](#exit-codes)
- [Usage Examples](#usage-examples)
- [Troubleshooting](#troubleshooting)
  - [Checking Logs](#checking-logs)
  - [Validating a New Installation](#validating-a-new-installation)
  - [Common Issues](#common-issues)
- [Uninstalling](#uninstalling)
- [Contributing](#contributing)
- [Code of Conduct](#code-of-conduct)
- [Changelog](#changelog)
- [Security](#security)

## Introduction

`telegram-sendmail` is a drop-in replacement for `/usr/sbin/sendmail` that
forwards system emails to a Telegram chat. It accepts email input via the
standard pipe interface used by cron, logwatch, fail2ban, and the majority
of Unix system daemons, as well as via a minimal SMTP server mode for
applications that speak SMTP directly to the `sendmail` binary.

Every raw email is archived to a local spool file before any network call
is attempted, ensuring zero data loss even when Telegram is temporarily
unreachable. The tool ships as a standalone binary with no runtime
dependencies and supports all `sendmail` flags that system daemons use in
practice, making it suitable for headless servers, containers, and
appliances where a full MTA is unnecessary overhead.

## Requirements

The standalone binary has no runtime dependencies. Python is not required
on the target host.

For PyPI installations, the following are required:

- Python 3.10 or later (3.10, 3.11, 3.12, and 3.13 are tested in CI)
- Runtime dependencies (installed automatically by `pip`):
  - [html2text](https://pypi.org/project/html2text/) ≥ 2024.2.26
  - [requests](https://pypi.org/project/requests/) ≥ 2.31.0

## Installation

### Option A — Pre-built Standalone Binary (recommended)

Standalone binaries for `x86_64` and `aarch64` are attached to every
[GitHub Release](https://github.com/theodiv/telegram-sendmail/releases).
They have no runtime dependencies — Python is not required on the target
host.

Both binaries are built on Ubuntu 22.04 and link against glibc 2.35. They
are forward-compatible with any Linux distribution shipping glibc 2.35 or
newer, including Debian 12, Ubuntu 22.04+, RHEL 9+, and Fedora 36+.
Installing via PyPI is required on older distributions where glibc is older
than 2.35.

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

### Option B — Install from PyPI

```bash
# Recommended: install with pipx for automatic isolation
pipx install telegram-sendmail

# Alternative: install into the system Python
pip install telegram-sendmail

# Verify the installation
telegram-sendmail --version
```

This is also the required method on older distributions where the pre-built
binary cannot run due to a glibc version older than 2.35 (Debian 11,
Ubuntu 20.04, RHEL 8, and earlier). If Python 3.10 is not available in the
distribution's default repositories,
[pyenv](https://github.com/pyenv/pyenv) or a third-party repository such
as the
[deadsnakes PPA](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa)
(Ubuntu) can be used to install a compatible version.

### Option C — Install from Source

```bash
git clone https://github.com/theodiv/telegram-sendmail.git
cd telegram-sendmail
pip install -e .
```

See the [Contributing Guide](../CONTRIBUTING.md) for instructions on
installing the development dependencies and configuring the full quality
toolchain.

## Configuration

### Creating a Telegram Bot

Before configuring the INI file, a Telegram bot and its target chat ID must
be obtained:

1. Open [@BotFather](https://t.me/botfather) in Telegram, send `/newbot`,
   and follow the instructions. Copy the bot token it generates.
2. Send any message to the new bot or add it to a group or channel, then
   visit `https://api.telegram.org/bot<TOKEN>/getUpdates`. The `chat.id`
   field in the response provides the required `chat_id`.

### Configuration File Reference

The configuration file is loaded from one of two locations, in order of
precedence:

1. `~/.telegram-sendmail.ini` — per-user file (takes precedence)
2. `/etc/telegram-sendmail.ini` — system-wide file

When both files exist, only the per-user file is read. If neither file
exists, the process exits with code `78` (`EX_CONFIG`) and logs a
descriptive error message.

A minimal configuration requires only the `[telegram]` section with the
`token` and `chat_id` keys:

```ini
[telegram]
token = 123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
chat_id = -1001234567890
```

The file must be owned by the process user with `0600` permissions.
Group- or world-readable permissions trigger a `WARNING` in syslog on every
startup but do not block execution:

```bash
chmod 600 /etc/telegram-sendmail.ini
chown root:root /etc/telegram-sendmail.ini
```

Out-of-range or malformed optional values fall back to their documented
defaults with a `WARNING` logged to syslog. A single bad optional key
never blocks delivery.

The complete annotated configuration template is provided in the repository
as [telegram-sendmail.ini.example](../telegram-sendmail.ini.example).

#### `[telegram]` Section

| Key       | Type   | Required | Behaviour                                       |
|-----------|--------|----------|-------------------------------------------------|
| `token`   | string | Yes      | Telegram Bot API token obtained from @BotFather |
| `chat_id` | string | Yes      | Destination chat ID (user, group, or channel)   |

#### `[options]` Section

All keys in this section are optional. Absent keys use the documented default.

| Key                    | Type  | Default   | Range      | Behaviour                                                                                                       |
|------------------------|-------|-----------|------------|-----------------------------------------------------------------------------------------------------------------|
| `spool_dir`            | path  | /var/mail | any path   | Parent directory for the per-user spool file; username is appended automatically                                |
| `message_max_length`   | int   | 3800      | 100–4096   | Maximum body character count before truncation; the total message is further capped at 4096 by the Telegram API |
| `smtp_timeout`         | int   | 30        | 5–300      | Seconds to wait for a line of SMTP input; applies only in `-bs` mode                                            |
| `telegram_timeout`     | int   | 10        | 5–60       | Seconds to wait for a Telegram API response per attempt                                                         |
| `max_retries`          | int   | 3         | 0–10       | Additional delivery attempts after the first failure on HTTP 429/5xx                                            |
| `backoff_factor`       | float | 0.5       | 0.0–10.0   | Exponential backoff multiplier; pause before attempt *n* is `factor × 2^(n−1)` seconds                          |
| `disable_notification` | bool  | false     | true/false | Send messages silently — no sound or banner alert on the recipient's device                                     |

The `spool_dir` value has the current user's login name appended
automatically, producing a final path of `<spool_dir>/<username>`
(e.g. `/var/mail/root`). If the resolved directory is not writable at
runtime, the tool falls back to `/tmp/.telegram-sendmail-spool/<username>`
and logs a `WARNING`. This hidden subdirectory is created automatically with
`0700` permissions so that only the owning user can read its contents.

The `backoff_factor` controls the pause between retry attempts using
`urllib3`'s exponential backoff algorithm. The effective sleep before
attempt *n* (1-indexed) is `backoff_factor × 2^(n−1)` seconds. With the
default value of `0.5`, the pauses are 0.5 s, 1 s, 2 s, and so on.

#### `[filters]` Section

| Key                | Type      | Default | Behaviour                                                         |
|--------------------|-----------|---------|-------------------------------------------------------------------|
| `suppress_subject` | glob list | (none)  | Suppress messages whose Subject header matches any listed pattern |
| `suppress_sender`  | glob list | (none)  | Suppress messages whose From header matches any listed pattern    |

### Email Filtering

The `[filters]` section controls which messages are suppressed from
Telegram delivery. Each key accepts one or more case-insensitive glob
patterns using Python `fnmatch` semantics:

- `*` matches any sequence of characters
- `?` matches any single character
- `[seq]` matches any character in *seq*
- `[!seq]` matches any character not in *seq*

Multiple patterns are specified one per line, indented under the key:

```ini
[filters]
suppress_subject =
    cron *
    *logwatch*
    daily backup*

suppress_sender =
    *@noreply.local
    root@monitoring.*
```

Pattern matching runs against the fully resolved sender and subject —
including CLI overrides and RFC 2047 decoding — so patterns match exactly
what would appear in the Telegram message. Messages matching a suppression
pattern are still archived to the spool file; only the Telegram delivery
step is skipped.

## Registering as the System `sendmail`

`telegram-sendmail` intentionally does not register itself as
`/usr/sbin/sendmail` during installation to avoid silently overwriting an
existing MTA.

### Option A — Simple Symlink (no existing MTA)

```bash
TGSM_BIN=$(which telegram-sendmail)
sudo ln -sf "$TGSM_BIN" /usr/sbin/sendmail
```

The `-f` flag silently overwrites any existing `/usr/sbin/sendmail` symlink
or binary. If an MTA is already installed, Option B should be used instead.

### Option B — `update-alternatives` (Debian/Ubuntu, existing MTA present)

```bash
TGSM_BIN=$(which telegram-sendmail)

sudo update-alternatives --install \
    /usr/sbin/sendmail sendmail "$TGSM_BIN" 50

# Interactively select the active alternative
sudo update-alternatives --config sendmail
```

The priority value (`50`) determines which alternative is selected
automatically when `auto` mode is active. A higher value wins. Setting a
lower priority than the existing MTA keeps the MTA as the default while
still making `telegram-sendmail` available as a manual selection. Setting a
higher priority makes `telegram-sendmail` the automatic default.

To revert:

```bash
sudo update-alternatives --remove sendmail "$TGSM_BIN"
```

On RHEL, CentOS, and Fedora, `alternatives` is substituted for
`update-alternatives` — the syntax is identical.

## SMTP Server Mode

Invoking `telegram-sendmail -bs` runs a minimal SMTP state machine on
`stdin`/`stdout`. This mode exists for applications that speak SMTP directly
to the `sendmail` binary rather than using the standard pipe interface. Most
system daemons use pipe mode; SMTP server mode is primarily needed by tools
that invoke `sendmail -bs` explicitly.

The server does not open a network socket. All I/O is strictly over the
process's `stdin`/`stdout` file descriptors.

### Supported Commands

The server implements the subset of RFC 5321 required in practice: `EHLO`,
`HELO`, `MAIL FROM`, `RCPT TO`, `DATA`, `RSET`, `NOOP`, and `QUIT`.
Unknown commands return `250 Ok` rather than `502`, which some system tools
misinterpret as a fatal error.

Command sequence enforcement follows RFC 5321 §4.1.1: `EHLO`/`HELO` must
precede `MAIL FROM`, which must precede `RCPT TO` and `DATA`. Commands
issued out of sequence receive `503 5.5.1 Bad sequence of commands`.

All recipients specified via `RCPT TO` are silently accepted; delivery
always goes to the configured Telegram `chat_id` regardless of the
recipient addresses provided.

### Message Size Limit

The server enforces a hard 10 MiB limit per message, advertised via the
`EHLO SIZE` extension. Messages exceeding this limit are rejected with
`552 5.3.4 Message size exceeds fixed maximum message size` before the
delivery callback is invoked. The session continues normally after a size
rejection — subsequent messages in the same session are not affected.

### Dot-Stuffing

RFC 5321 §4.5.2 dot-stuffing is applied during `DATA` collection. A line
consisting of `..` on the wire is delivered as a single `.`, and a line
beginning with `..` followed by content is delivered with the leading dot
stripped.

### Session Reset

`RSET` discards the current transaction state (envelope sender and
buffered data) and returns the session to the post-`EHLO` state. A
completed `DATA` transaction resets state automatically, so multiple
messages can be delivered in a single session without an explicit `RSET`
between them.

### Graceful Shutdown

The server handles `SIGTERM` and `SIGINT` by sending a
`421 4.4.2 Service shutting down` response and terminating the session
cleanly. Signal handlers are restored to their previous values after the
session ends, so any process-manager signal handling configured by the
parent process is not permanently altered.

### Timeout

The `smtp_timeout` option controls how long the server waits for a line of
input before closing the session with `421 4.4.2 Connection timed out`.
This timeout applies only in SMTP server mode and has no effect in pipe
mode.

## CLI Reference

| Flag        | Argument  | Behaviour                                                   |
|-------------|-----------|-------------------------------------------------------------|
| `-f`, `-r`  | `ADDRESS` | Override the envelope sender                                |
| `-s`        | `SUBJECT` | Override the Subject header                                 |
| `-bs`       | —         | Run in SMTP server mode                                     |
| `-t`        | —         | Extract recipients from headers (accepted, ignored)         |
| `-i`, `-oi` | —         | Do not treat lone `.` as end-of-message (accepted, ignored) |
| `--probe`   | —         | Validate config and verify Telegram connectivity; no stdin  |
| `--console` | —         | Log to stderr instead of syslog                             |
| `--debug`   | —         | Set log level to DEBUG                                      |
| `--version` | —         | Print version and exit                                      |
| `--help`    | —         | Print usage and exit                                        |

Positional recipient arguments (e.g. `sendmail root@localhost`) are
consumed silently. All mail is forwarded to the configured `chat_id`
regardless of recipients specified on the command line.

When `-s` is used and the raw email does not already contain a Subject
header, a synthetic `Subject:` header is prepended to the message before
parsing. When a Subject header is already present (case-insensitive match
within the first 500 characters of the input), the `-s` override is
ignored.

The `-t`, `-i`, and `-oi` flags are accepted for compatibility with system
daemons that always pass them but have no effect on processing.

When stdin is a TTY (interactive terminal), the tool exits with `EX_ERROR`
(1) and prints usage information to stderr. This guard prevents accidental
interactive invocations from blocking indefinitely on stdin.

## Delivery Pipeline

The delivery pipeline processes a single message through five stages,
implemented in `_deliver()`:

1. **Spool** — The raw email is archived to the per-user spool file before
   any other processing occurs. This ensures the original message is always
   preserved on disk regardless of whether downstream stages succeed or
   fail. A `SpoolError` is non-fatal: a spool failure is logged at
   `WARNING` level and delivery continues.

2. **Parse** — The raw RFC 2822 email string is decoded from its MIME
   structure. The body is extracted (preferring `text/plain`, falling back
   to `text/html`), HTML content is sanitised and converted to Telegram-
   compatible markup, and the sender, subject, and attachment metadata are
   extracted into a structured representation.

3. **Filter** — The resolved Subject and From header values are checked
   against the configured suppression patterns. If any pattern matches, the
   message is logged at `DEBUG` level and the pipeline returns early without
   sending to Telegram. The spool write from stage 1 is not affected.

4. **Format** — The parsed email fields are wrapped in the Telegram HTML
   message envelope. The body is truncated at the nearest word boundary if
   it exceeds `message_max_length`, with a visible truncation notice
   appended. The total message (envelope markup, footers, and body) is
   additionally capped at the Telegram API hard limit of 4096 characters.
   An attachment notice is appended if the MIME structure contained
   attachments.

5. **Send** — The formatted message is delivered to the Telegram Bot API
   via HTTPS. Retries with exponential backoff are attempted on HTTP 429
   and 5xx responses.

## Mail Spooling

Every raw email is written to a local spool file before any Telegram API
call is attempted. The spool serves as a durable archive: if Telegram
delivery fails, the original message is preserved on disk for manual
recovery or re-delivery.

The spool file path is derived as `<spool_dir>/<username>`, where
`<username>` is the current user's login name (e.g. `/var/mail/root`). Each
message is appended with a timestamped separator in mbox-style format,
suitable for human inspection.

When the configured `spool_dir` (default: `/var/mail`) is not writable, the
spooler falls back to `/tmp/.telegram-sendmail-spool/<username>`. This
fallback directory is created on demand with `0700` permissions (owner-only
access), and its ownership is validated against the current UID. If the
directory already exists but is owned by a different user — a sign of a
pre-creation attack — a `WARNING` is emitted and the untrusted path is not
used.

File permissions on the spool file are enforced to `0600` via `os.fchmod`
against the open file descriptor on every write. Using `os.fchmod` on the
fd rather than `os.chmod` on the path eliminates the TOCTOU race condition
that would otherwise exist when the spool directory is world-writable
(e.g. the `/tmp` fallback).

The spool file is opened with the `O_NOFOLLOW` flag. If the spool path is a
symlink, the kernel raises `ELOOP` rather than following the link, preventing
a local attacker from redirecting writes to an arbitrary file by pre-placing
a symlink at the spool path.

`SpoolError` is intentionally non-fatal. A full disk or permission error
during spool writes is logged at `WARNING` level and never silently drops
a Telegram notification. The message proceeds through the rest of the
delivery pipeline regardless.

## HTML Processing Pipeline

HTML email bodies are processed through a two-pass pipeline before delivery
to Telegram.

**Pass 1 — `_HTMLSanitizer` (pre-processing)**

The sanitiser strips dangerous HTML elements and their full content subtrees
before any conversion occurs. Two suppression strategies are applied:

Tags suppressed with their full content subtree (depth-tracked to handle
arbitrary nesting): `script`, `style`, `iframe`, `object`, `embed`, `svg`,
`math`, `noscript`.

Void elements silently dropped: `input`, `meta`, `link`.

**Pass 2 — `TelegramHTMLParser` (conversion)**

Safe HTML elements are mapped to the strict subset supported by the Telegram
Bot API:

| HTML Tag             | Telegram Tag |
|----------------------|--------------|
| `b`, `strong`        | `b`          |
| `i`, `em`            | `i`          |
| `u`, `ins`           | `u`          |
| `s`, `strike`, `del` | `s`          |
| `code`, `kbd`, `tt`  | `code`       |

Headings (`h1`–`h6`) are rendered as bold text. Links (`<a href>`) are
preserved, with `javascript:` hrefs silently discarded. `<pre>` blocks,
`<blockquote>` elements, and horizontal rules are also mapped to their
Telegram equivalents. All other tags fall through to `html2text`'s default
plain-text conversion.

Plain-text email bodies (`text/plain`) are preferred when both plain-text
and HTML parts are present in the MIME structure. HTML conversion is used
only as a fallback when no `text/plain` part exists.

Attachments are detected and a notice is appended to the Telegram message.
Binary attachment content is never forwarded; only the presence of
attachments is reported.

Long bodies are truncated at the nearest word boundary below the configured
`message_max_length` limit, with a visible `[…message truncated…]` notice
appended. The total assembled message is additionally capped at the Telegram
API hard limit of 4096 characters, so values of `message_max_length` near
4096 may result in earlier truncation than the configured value implies.

## Security Considerations

**Token Redaction**: The `_TokenRedactFilter` logging filter is installed on
every root-logger handler after the bot token is loaded. It automatically
redacts the bot token from all log output at every log level, including
output from third-party loggers such as `urllib3` and `requests`. This
prevents accidental token exposure via syslog or console output.

**Config File Permissions**: A `WARNING` is emitted on every startup if the
configuration file is group- or world-readable. The recommended permissions
are `0600` (owner read/write only). The warning is advisory and does not
block execution, because the system administrator may have a legitimate
reason for looser permissions.

**Spool File TOCTOU Protection**: Spool file permissions are enforced via
`os.fchmod` against the open file descriptor rather than `os.chmod` against
the path. The `O_NOFOLLOW` flag prevents symlink redirection attacks. These
measures are described in detail in the [Mail Spooling](#mail-spooling) section.

For the full threat model, hardening recommendations, and vulnerability
reporting process, see the [Security Policy](../SECURITY.md).

## Exit Codes

| Code | Name          | Meaning                                                                |
|------|---------------|------------------------------------------------------------------------|
| `0`  | `EX_OK`       | Message delivered successfully to Telegram                             |
| `1`  | `EX_ERROR`    | Operational failure: parse error, API error, or unexpected exception   |
| `75` | `EX_TEMPFAIL` | Transient failure (HTTP 429/5xx). Daemons will re-queue and retry      |
| `78` | `EX_CONFIG`   | Configuration error: file missing, unreadable, or missing required key |

Exit code `75` is the standard `EX_TEMPFAIL` value from `sysexits.h`.
POSIX-aware daemons such as cron and Postfix interpret it as a signal to
re-queue the message and retry delivery later, rather than treating the
message as permanently undeliverable. This distinction is critical for
reliable operation: transient Telegram API failures (rate limits, server
errors) trigger re-queuing rather than silent message loss.

## Usage Examples

```bash
# Basic pipe mode — minimal RFC 2822 message
echo -e "Subject: Test\n\nThis is a test message." | telegram-sendmail

# Override the envelope sender and subject from the CLI
echo "Disk usage critical." | telegram-sendmail -f root@hostname -s "Disk Alert"

# Simulate a cron invocation (positional recipient is silently discarded)
echo "Cron job failed." | telegram-sendmail -oi -t root@localhost

# Interactive debugging with console logging at DEBUG level
echo "Hello" | telegram-sendmail --console --debug

# Run in SMTP server mode
telegram-sendmail -bs

# Validate configuration and Telegram connectivity without reading stdin
telegram-sendmail --probe

# Full post-install verification with verbose output
telegram-sendmail --probe --console --debug
```

## Troubleshooting

### Checking Logs

All output goes to syslog under the `LOG_MAIL` facility:

```bash
# Live log stream
journalctl -t telegram-sendmail -f

# Recent entries
journalctl -t telegram-sendmail --since "1 hour ago"
```

On systems without `journald`, check `/var/log/mail.log` or `/var/log/syslog`.

### Validating a New Installation

The `--probe` flag verifies configuration syntax, bot token validity, and
`chat_id` reachability without reading from stdin:

```bash
telegram-sendmail --probe --console --debug
```

Exit code `0` confirms end-to-end connectivity. `78` indicates a config
error, `75` a transient network failure, and `1` a permanent API rejection.
This flag is suitable for Ansible tasks, cloud-init `runcmd` assertions,
and manual post-install smoke tests.

### Common Issues

#### *No messages arriving in Telegram*

Verify the `token` and `chat_id` values in the configuration file. Confirm
the bot has been started by sending `/start` to it in the Telegram app.
Check that outbound HTTPS to `api.telegram.org` (TCP/443) is permitted by
the host firewall. Running with `--console --debug` reveals the full API
error payload.

#### *Bot added to a group but messages not delivered*

The bot must be a member of the group and, for channels, must be added as
an administrator. The `chat_id` for groups is a negative number
(e.g. `-1001234567890`). Verify with `--probe --console --debug`; a
`chat not found` error in the output confirms a wrong `chat_id` or missing
group membership.

#### *`--probe` exits with code 1 but the token appears correct*

The bot may not have been initialised. Open Telegram, search for the bot by
its username, and send `/start`. Then re-run `--probe`. If the error
persists, the token may have been revoked in @BotFather.

#### *Permission denied on config file*

The file must be owned by the process user with `0600` permissions:

```bash
sudo chmod 600 /etc/telegram-sendmail.ini
sudo chown root:root /etc/telegram-sendmail.ini
```

#### *Cron emails not forwarded*

Verify the symlink resolves correctly:

```bash
ls -la /usr/sbin/sendmail
/usr/sbin/sendmail --version
```

If the symlink points to a different binary, re-create it following the
instructions in
[Registering as the System `sendmail`](#registering-as-the-system-sendmail).

#### *Spool directory warning in logs*

Either make the spool directory writable by the process user, or set
`spool_dir` in `[options]` to a writable path. In container environments
where `/var/mail` does not exist, setting `spool_dir` to a path such as
`/tmp/spool` avoids the automatic fallback entirely.

#### *Binary fails to start with "GLIBC not found" error*

The pre-built binary requires glibc 2.35 or newer. Install via PyPI on
older distributions. See
[Option B — Install from PyPI](#option-b--install-from-pypi) for instructions.

#### *Large emails not delivered*

The pipe mode input cap is 10 MiB. Emails exceeding this limit are
truncated before processing and a `WARNING` is logged. In SMTP server
mode, messages exceeding 10 MiB are rejected with
`552 5.3.4 Message size exceeds fixed maximum message size`.

## Uninstalling

#### Symlink Installation

```bash
sudo rm /usr/sbin/sendmail
```

#### `update-alternatives` Installation (Debian/Ubuntu)

```bash
TGSM_BIN=$(which telegram-sendmail)
sudo update-alternatives --remove sendmail "$TGSM_BIN"
```

On RHEL, CentOS, and Fedora, `alternatives` is substituted for
`update-alternatives`.

#### Remove the Binary and Config

```bash
# Binary install
sudo rm /usr/local/bin/telegram-sendmail

# PyPI / pipx install
pipx uninstall telegram-sendmail   # or: pip uninstall telegram-sendmail

# Config and spool files
sudo rm /etc/telegram-sendmail.ini
sudo rm -rf /var/mail/$(whoami)    # adjust if spool_dir was customised
```

## Contributing

Development setup instructions, coding standards, and the pull request
process are documented in the [Contributing Guide](../CONTRIBUTING.md).

## Code of Conduct

Behavioural expectations for all project participants are governed by
the [Code of Conduct](../CODE_OF_CONDUCT.md).

## Changelog

A detailed version history following the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format is
maintained in the [Changelog](../CHANGELOG.md).

## Security

The threat model, security hardening recommendations, and the vulnerability
disclosure process are documented in the [Security Policy](../SECURITY.md).
