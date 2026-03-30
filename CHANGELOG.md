# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Cap the assembled Telegram message at 4096 characters so that
  `message_max_length` values near the API limit no longer cause delivery
  failure. ([#33](https://github.com/theodiv/telegram-sendmail/issues/33))
- Signal `451 4.3.0 Temporary backend failure` in `-bs` mode when the
  Telegram API returns a retriable status (429, 5xx) instead of
  `554 5.0.0 Transaction failed`.

## [1.2.1] - 2026-03-22

### Fixed

- Fix Telegram delivery failure (`400 Bad Request`) for HTML emails containing
  `<a href>` links inside block elements. ([#30](https://github.com/theodiv/telegram-sendmail/issues/30))

### Changed

- Compact Telegram message envelope by removing the static "New Notification"
  header, along with the "From:" and "Subject:" labels.

## [1.2.0] - 2026-03-14

### Added

- `[filters]` INI section with `suppress_subject` and `suppress_sender`
  multi-line keys for glob-based message suppression. ([#12](https://github.com/theodiv/telegram-sendmail/issues/12))

## [1.1.0] - 2026-03-14

### Added

- `--probe` flag to validate configuration and verify Telegram API
  connectivity without reading from stdin. ([#9](https://github.com/theodiv/telegram-sendmail/issues/9))

### Fixed

- Send a `421` shutdown response on SIGTERM and SIGINT in `-bs` mode instead
  of terminating silently. ([#3](https://github.com/theodiv/telegram-sendmail/issues/3))

## [1.0.0] - 2026-03-08

Production release.

### Added

#### Drop-in sendmail compatibility

- Accepts all flags used by system daemons in practice: `-f`/`-r` (envelope
  sender), `-s` (subject), `-bs` (SMTP server mode), `-t`, `-i`, `-oi`.
  Positional recipient arguments (e.g. `sendmail root@localhost`) are consumed
  silently; all mail is forwarded to the configured Telegram `chat_id`.
- **Pipe mode**: reads a raw RFC 2822 email from `stdin`, the standard
  interface used by cron, logwatch, fail2ban, and the majority of Unix system
  daemons.
- **SMTP server mode** (`-bs`): runs a minimal SMTP dialogue on
  `stdin`/`stdout` without opening a network socket. Supported commands:
  `EHLO`, `HELO`, `MAIL FROM`, `RCPT TO`, `DATA`, `RSET`, `NOOP`, `QUIT`.
  Unrecognised commands return `250 Ok` for maximum daemon compatibility.
  RFC 5321 dot-stuffing and null reverse-path (`MAIL FROM:<>`) are handled
  correctly. A failed delivery attempt returns `554 Transaction failed` and
  resets the session state; subsequent messages in the same session are
  unaffected.

#### Zero-data-loss mail spooling

- Every raw email is written to a local spool file **before** any Telegram API
  call is attempted. If the network is unavailable or delivery fails, the
  original message is preserved on disk.
- The spool path defaults to `/var/mail/<username>`. If that directory is not
  writable — common in containers — the daemon falls back to
  `/tmp/.telegram-sendmail-spool/<username>` and emits a `WARNING` to syslog.
  This hidden subdirectory is created on demand with `0700` DAC permissions,
  preventing cross-user data exposure in the world-writable `/tmp` filesystem.
- A spool write failure is non-fatal: the message is still forwarded to
  Telegram and the failure is surfaced in syslog at `WARNING` level.
- File permissions on the spool file are enforced to `0600` via `os.fchmod`
  against the open file descriptor on every write, eliminating the TOCTOU race
  condition that would otherwise exist in world-writable directories.

#### Resilient Telegram delivery

- Retriable responses (HTTP 429 and 5xx) cause the process to exit with
  code `75` (`EX_TEMPFAIL`), signalling MTA-aware daemons to re-queue rather
  than treat the message as permanently undeliverable.
- HTTP retries with exponential backoff on `429 Too Many Requests` and `5xx`
  server errors. Retry count and backoff multiplier are operator-configurable.
- The Telegram API's `ok` field in the JSON response body is validated
  independently of the HTTP status code, since the API can return HTTP 200
  with `ok: false` for application-level errors such as an invalid `chat_id`.

#### HTML email support

- A two-pass sanitisation pipeline strips dangerous elements — including
  `<script>`, `<style>`, `<iframe>`, `<object>`, `<embed>`, `<svg>`, and
  `<noscript>` — and their entire subtrees before further processing.
  Arbitrarily nested dangerous tags are handled correctly via depth tracking.
- Safe markup is converted to Telegram's supported HTML subset: `<b>`, `<i>`,
  `<u>`, `<s>`, `<a>`, `<code>`, `<pre>`, `<blockquote>`. `javascript:` hrefs
  are silently discarded.
- Plain-text parts are preferred; HTML is used as a fallback when no
  `text/plain` part is present in the MIME structure.
- Multipart attachments are detected and a notice is appended to the Telegram
  message. Attachment filenames are never disclosed.
- Long bodies are truncated at the nearest word boundary below the configured
  limit; a visible truncation notice is appended.

#### Secure configuration

- Configuration is loaded from `/etc/telegram-sendmail.ini` (system-wide) or
  `~/.telegram-sendmail.ini` (per-user). The user file takes precedence.
- Config files with group or world read bits emit a `WARNING` to syslog on
  every startup. The check is advisory and does not block execution.
- Malformed or out-of-range values in `[options]` fall back to their defaults
  with a `WARNING` logged; a single bad option never blocks delivery.
- Available options: `spool_dir`, `message_max_length`, `smtp_timeout`,
  `telegram_timeout`, `max_retries`, `backoff_factor`, `disable_notification`.
- A fully-commented configuration template is provided as
  `telegram-sendmail.ini.example`.

#### Structured logging

- All output goes to syslog under the `LOG_MAIL` facility with the identifier
  `telegram-sendmail`, queryable via `journalctl -t telegram-sendmail`.
- `--console` flag routes log output to `stderr` for interactive debugging
  and CI environments.
- `--debug` flag sets the log level to `DEBUG`, exposing SMTP command traces
  and API request details.

#### Exit codes

| Code | Meaning                                                       |
|------|---------------------------------------------------------------|
| `0`  | Message delivered successfully.                               |
| `1`  | Operational failure (parse error, unrecoverable API error).   |
| `75` | Transient failure — retriable error; daemon should retry.     |
| `78` | Configuration error (missing file or missing required key).   |

[Unreleased]: https://github.com/theodiv/telegram-sendmail/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/theodiv/telegram-sendmail/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/theodiv/telegram-sendmail/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/theodiv/telegram-sendmail/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/theodiv/telegram-sendmail/releases/tag/v1.0.0
