"""
Shared pytest fixtures for the telegram-sendmail test suite.

Fixture inventory
-----------------
Configuration
  `app_config`                 — valid `AppConfig` with a `tmp_path` spool file
  `app_config_no_retry`        — `app_config` with `max_retries=0`, `backoff_factor=0.0`
  `config_file_factory`        — callable that writes an INI file to `tmp_path`
  `patched_config_loader`      — patches `ConfigLoader.load()`; `app_config` (no file I/O)

Email corpus
  `plain_raw_email`            — minimal plain-text RFC 2822 email string
  `html_raw_email`             — HTML email with both safe and dangerous markup
  `multipart_raw_email`        — multipart/mixed email with one attachment
  `empty_raw_email`            — whitespace-only string (triggers `ParsingError`)
  `encoded_subject_email`      — email with a multi-word, whitespace-heavy subject

Telegram API mocking
  `mock_telegram_ok`           — `requests_mock`: POST `/sendMessage` -> 200 `ok: true`
  `mock_telegram_api_error`    — `requests_mock`: POST `/sendMessage` -> 200 `ok: false`
  `mock_telegram_server_error` — `requests_mock`: POST `/sendMessage` -> 500
  `mock_telegram_rate_limit`   — `requests_mock`: POST `/sendMessage` -> 429

SMTP
  `smtp_callback`              — `MagicMock` for use as `SMTPServer` `on_message` callback

Filesystem
  `tmp_spool_path`             — writable `Path` inside `tmp_path`, does not pre-exist
  `writable_config_file`       — `Path` to a `0600`-permissioned INI file in `tmp_path`
  `unreadable_config_file`     — `Path` to a `0000`-permissioned file (permission tests)

Design notes
------------
- All email fixtures are plain strings, not `email.message.Message` objects,
  because every public entry point in the package takes the raw string form.
- `app_config` uses `tmp_path` for `spool_path` so no test ever touches
  a real filesystem path like `/var/mail`.
- Telegram API fixtures use `requests_mock` (via the `requests-mock` library)
  which patches `requests.Session` at the transport layer and does not
  require the package to be installed in a specific way. Each fixture yields
  the `requests_mock.Mocker` instance so that tests can inspect
  `mocker.last_request` for assertion on the outbound payload.
- All fixtures that write files use `tmp_path` (pytest-provided per-test
  temporary directory) to guarantee full isolation between tests.
 - `patched_config_loader` patches at the class level via `monkeypatch` so the
  patch is automatically reverted after each test without any manual teardown.
"""

from __future__ import annotations

import dataclasses
import textwrap
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests_mock as requests_mock_module

from telegram_sendmail.config import AppConfig

# --------------------------------------------------------------------------
# Configuration fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    """
    Return a valid `AppConfig` populated with safe test values.

    `spool_path` points into `tmp_path` so every test that triggers a
    spool write is fully isolated and never touches `/var/mail`.

    This is the primary config fixture. Tests that need a modified config
    should use `dataclasses.replace()` or the `config_file_factory`
    fixture to exercise `ConfigLoader` directly.
    """
    return AppConfig(
        token="123456789:AAtestBOTtokenXXXXXXXXXXXXXXXXXXX",
        chat_id="-1001234567890",
        message_max_len=3800,
        smtp_timeout=5,
        telegram_timeout=10,
        disable_notification=False,
        spool_path=tmp_path / "test_spool",
        max_retries=3,
        backoff_factor=0.5,
    )


@pytest.fixture
def app_config_no_retry(app_config: AppConfig) -> AppConfig:
    """
    Return `app_config` with retries disabled.

    Used in tests that exercise error paths (`429`, `500`) where `urllib3`'s
    exponential back-off would cause unacceptably long test runtimes if
    left enabled.
    """
    return dataclasses.replace(app_config, max_retries=0, backoff_factor=0.0)


@pytest.fixture
def config_file_factory(tmp_path: Path) -> Callable[[str], Path]:
    r"""
    Return a factory that writes an INI string to a `0600`-permissioned
    file in `tmp_path` and returns its `Path`.

    Usage::

        def test_missing_token(config_file_factory):
            path = config_file_factory(
                "[telegram]\nchat_id = 123\n"
            )
            # path points to a valid file with no [telegram] token key

    The factory is intentionally a closure over `tmp_path` so each test
    invocation gets its own isolated directory even if the factory is called
    multiple times within a single test.
    """

    def _factory(ini_content: str) -> Path:
        config_file = tmp_path / "telegram-sendmail.ini"
        config_file.write_text(textwrap.dedent(ini_content), encoding="utf-8")
        config_file.chmod(0o600)
        return config_file

    return _factory


@pytest.fixture
def patched_config_loader(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> AppConfig:
    """
    Patch `ConfigLoader.load()` to return `app_config` without reading
    any file from disk.

    This fixture is the standard way to prevent `main()` and other
    top-level functions from attempting to resolve `/etc/telegram-sendmail.ini`
    or `~/.telegram-sendmail.ini` during tests. The patch is applied at
    the class level via `monkeypatch` and is automatically reverted when
    the test finishes.

    Returns `app_config` so tests can reference it directly without
    requesting both fixtures.
    """
    import telegram_sendmail.config as cfg_module

    monkeypatch.setattr(
        cfg_module.ConfigLoader,
        "load",
        staticmethod(lambda: app_config),
    )
    return app_config


# --------------------------------------------------------------------------
# Email corpus fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def plain_raw_email() -> str:
    """
    Minimal well-formed plain-text RFC 2822 email.

    Covers: sender extraction, subject sanitisation, plain body pass-through,
    no-attachment path, `html.escape` applied to body content.
    """
    return textwrap.dedent("""\
        From: cron@hostname.local
        To: root@hostname.local
        Subject: Daily Backup Complete
        MIME-Version: 1.0
        Content-Type: text/plain; charset=utf-8

        Backup completed successfully.
        Duration: 4m 32s
        Files archived: 1,024
    """)


@pytest.fixture
def html_raw_email() -> str:
    """
    HTML email containing both safe markup and dangerous elements.

    Covers:
    - Safe tags that should survive into Telegram output: `<h1>`, `<h3>`,
      `<hr>`, `<blockquote>`, `<b>`, `<i>`, `<a href>`, `<ul>`, `<li>`, `<p>`
    - Dangerous tags whose content must be stripped entirely: `<script>`,
      `<style>`, `<iframe>`
    - Dangerous void elements that must be dropped: `<input>`
    - Nested dangerous tags (depth-tracking validation)
    - A `javascript:` href that must be silently discarded
    """
    return textwrap.dedent("""\
        From: monitoring@example.com
        To: admin@example.com
        Subject: Server Alert
        MIME-Version: 1.0
        Content-Type: text/html; charset=utf-8

        <html><body>
        <h1>CRITICAL: Disk Space Low</h1>
        <p><b>Disk usage critical</b> on <i>web-01</i>.</p>
        <hr>
        <h3>Volume Information</h3>
        <ul>
          <li>Partition: <code>/var</code></li>
          <li>Usage: <b>94%</b></li>
        </ul>
        <blockquote>
          Warning: Free space dropped below 5% threshold.
        </blockquote>
        <p><a href="https://grafana.example.com/d/abc">View dashboard</a></p>
        <p><a href="javascript:void(0)">Ignore this link</a></p>
        <script>alert('xss attempt')</script>
        <style>body { display: none; }</style>
        <iframe src="https://evil.com/">fallback</iframe>
        <input type="hidden" value="secret">
        <script><script>nested('deeper xss')</script></script>
        </body></html>
    """)


@pytest.fixture
def multipart_raw_email() -> str:
    """
    Multipart/mixed email with a plain-text body and one binary attachment.

    Covers:
    - Attachment detection (`has_attachments = True`)
    - Attachment footer appended to Telegram message body
    - Filename NOT disclosed in formatted output
    - Body still correctly extracted from the text/plain part
    """
    return textwrap.dedent("""\
        From: backup@hostname.local
        To: root@hostname.local
        Subject: Weekly Report
        MIME-Version: 1.0
        Content-Type: multipart/mixed; boundary="==boundary=="

        --==boundary==
        Content-Type: text/plain; charset=utf-8

        Weekly report attached. All systems nominal.

        --==boundary==
        Content-Type: application/octet-stream
        Content-Disposition: attachment; filename="report_2025-07.csv"
        Content-Transfer-Encoding: base64

        dGVzdGRhdGE=
        --==boundary==--
    """)


@pytest.fixture
def empty_raw_email() -> str:
    """
    Whitespace-only string that must raise `ParsingError`.

    Covers the empty-input guard in `EmailParser.parse()`.
    """
    return "   \n\t\n   "


@pytest.fixture
def encoded_subject_email() -> str:
    """
    Email with a subject containing leading/trailing whitespace and internal
    newlines, as produced by some system daemons that fold long headers.

    Covers `_sanitise_subject` collapsing all whitespace to a single space.
    """
    return textwrap.dedent("""\
        From: daemon@host
        Subject:   Disk usage   warning
          on web-01
        Content-Type: text/plain

        Disk at 95 percent.
    """)


# --------------------------------------------------------------------------
# Telegram API mock fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def send_telegram_url_pattern() -> str:
    """Return the expected Telegram `sendMessage` URL pattern."""
    return (
        "https://api.telegram.org/bot"
        "123456789:AAtestBOTtokenXXXXXXXXXXXXXXXXXXX"
        "/sendMessage"
    )


@pytest.fixture
def mock_telegram_ok(
    requests_mock: requests_mock_module.Mocker,
    send_telegram_url_pattern: str,
) -> requests_mock_module.Mocker:
    """
    Mock the Telegram `sendMessage` endpoint with a successful response.

    Returns the `requests_mock.Mocker` so tests can inspect
    `mock_telegram_ok.last_request.json()` to assert on the outbound
    payload (`text`, `parse_mode`, `chat_id`, etc.).
    """
    requests_mock.post(
        send_telegram_url_pattern,
        json={
            "ok": True,
            "result": {
                "message_id": 42,
                "chat": {"id": -1001234567890},
                "text": "forwarded",
            },
        },
        status_code=200,
    )
    return requests_mock


@pytest.fixture
def mock_telegram_api_error(
    requests_mock: requests_mock_module.Mocker,
    send_telegram_url_pattern: str,
) -> requests_mock_module.Mocker:
    """
    Mock the Telegram `sendMessage` endpoint with an application-level error.

    HTTP 200 with `ok: false` replicates the Telegram API's behaviour for
    errors such as bad `chat_id`, bot not started, or message too long.
    Validates that `TelegramClient._check_response` inspects the JSON body
    rather than relying solely on the HTTP status code.
    """
    requests_mock.post(
        send_telegram_url_pattern,
        json={
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: chat not found",
        },
        status_code=200,
    )
    return requests_mock


@pytest.fixture
def mock_telegram_server_error(
    requests_mock: requests_mock_module.Mocker,
    send_telegram_url_pattern: str,
) -> requests_mock_module.Mocker:
    """
    Mock the Telegram `sendMessage` endpoint with an HTTP 500 response.

    Used to test that the retry strategy is mounted and that all retry
    attempts are exhausted before `TelegramAPIError` is raised.
    Configuring `max_retries=0` in `AppConfig` when using this fixture
    avoids slow retry back-off in the test suite.
    """
    requests_mock.post(
        send_telegram_url_pattern,
        json={
            "ok": False,
            "error_code": 500,
            "description": "Internal Server Error",
        },
        status_code=500,
    )
    return requests_mock


@pytest.fixture
def mock_telegram_rate_limit(
    requests_mock: requests_mock_module.Mocker,
    send_telegram_url_pattern: str,
) -> requests_mock_module.Mocker:
    """
    Mock the Telegram `sendMessage` endpoint with an HTTP 429 response.

    Used to test that `_run_pipe_mode` maps this condition to exit code 75
    (`EX_TEMPFAIL`) and that `TelegramAPIError.status_code == 429`.
    Configuring `max_retries=0` in `AppConfig` when using this fixture
    avoids slow retry back-off in the test suite.
    """
    requests_mock.post(
        send_telegram_url_pattern,
        json={
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 30",
        },
        status_code=429,
    )
    return requests_mock


# --------------------------------------------------------------------------
# SMTP fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def smtp_callback() -> MagicMock:
    """
    Return a `MagicMock` suitable for use as the `SMTPServer.on_message`
    callback.

    Tests that verify the callback was invoked can use standard MagicMock
    assertion methods (`assert_called_once_with`, `call_args`, etc.).
    Tests that need to simulate a delivery failure should configure
    `smtp_callback.side_effect = RuntimeError("inject failure")`.
    """
    return MagicMock()


# --------------------------------------------------------------------------
# Filesystem fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def tmp_spool_path(tmp_path: Path) -> Path:
    """
    Return a `Path` for a spool file that does not yet exist.

    The parent directory (`tmp_path`) is guaranteed writable by pytest.
    Tests that exercise `MailSpooler.write()` should use this fixture
    rather than `app_config.spool_path` when they need to inspect the
    raw file contents after writing.
    """
    return tmp_path / "spool" / "testuser"


@pytest.fixture
def writable_config_file(tmp_path: Path) -> Path:
    """
    Return a `Path` to a minimal valid `0600`-permissioned config file.

    Contains only the two required keys so that `ConfigLoader.load()`
    succeeds without raising `ConfigurationError`. Tests that need
    additional options should append them via `path.open('a')`.
    """
    config_file = tmp_path / "telegram-sendmail.ini"
    config_file.write_text(
        "[telegram]\ntoken = testtoken\nchat_id = 123\n",
        encoding="utf-8",
    )
    config_file.chmod(0o600)
    return config_file


@pytest.fixture
def unreadable_config_file(tmp_path: Path) -> Path:
    """
    Return a `Path` to a `0000`-permissioned file.

    Used to test that `ConfigLoader.load()` raises `ConfigurationError`
    with an appropriate message when the config file cannot be read.

    Note: This fixture has no effect when tests are run as root (UID 0),
    because root bypasses DAC permission checks. Tests using this fixture
    should be skipped on root with::

        pytestmark = pytest.mark.skipif(
            os.getuid() == 0, reason="root bypasses file permissions"
        )
    """
    config_file = tmp_path / "unreadable.ini"
    config_file.write_text("[telegram]\ntoken = x\nchat_id = y\n", encoding="utf-8")
    config_file.chmod(0o000)
    return config_file
