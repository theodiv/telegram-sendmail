"""
Tests for telegram_sendmail.__main__.

Coverage targets
----------------
`_is_suppressed`
    - Returns False when no patterns are configured or neither header matches any pattern
    - Returns True when the subject or sender matches a configured glob pattern
    - Matching is case-insensitive for both subject and sender patterns
    - Subject patterns are checked before sender patterns
    - Logs a DEBUG message identifying the matched header and pattern

`_bounded_stdin_read`
    - Returns full content when input is under or exactly at `_MAX_PIPE_SIZE`
    - Truncates oversized input to exactly `_MAX_PIPE_SIZE` characters and emits a WARNING
    - Emits no WARNING when input is within the limit
    - Returns an empty string for empty stdin

`_TokenRedactFilter`
    - Replaces the bot token with a safe placeholder in log record messages
    - Handles %-style format-string args by resolving getMessage() before redacting
    - Passes through log records that do not contain the token
    - Always returns True regardless of whether redaction occurred

`_deliver`
    - Invokes all three pipeline stages (spool -> parse -> send) in order on the happy path
    - Skips format_for_telegram and TelegramClient.send when _is_suppressed returns True
    - Still invokes MailSpooler.write and EmailParser.parse for suppressed messages
    - Proceeds through the full pipeline when no suppression pattern matches
    - Catches SpoolError without propagating it so delivery continues
    - Invokes EmailParser.parse and TelegramClient.send even when the spool write fails
    - Logs a WARNING via the spool logger before raising SpoolError
    - Passes the text returned by format_for_telegram directly to send()
    - Propagates ParsingError raised by EmailParser.parse
    - Propagates TelegramAPIError raised by TelegramClient.send

`_run_pipe_mode`
    - Returns _EX_OK (0) when _deliver completes without error
    - Returns _EX_ERROR (1) when sys.stdin.read() itself raises
    - Returns _EX_ERROR (1) when _deliver raises ParsingError, TelegramSendmailError,
      or an unexpected Exception
    - Returns _EX_ERROR (1) when _deliver raises TelegramAPIError with a non-retriable
      or no status code (status_code == None)
    - Returns _EX_TEMPFAIL (75) when _deliver raises TelegramAPIError with a retriable
      status code (429, 500, 502)
    - Prepends "Subject: <override>" when subject_override is set and the raw email
      contains no Subject header
    - Does NOT modify the raw email when it already contains a Subject header
      (case-insensitive) or when subject_override is None
    - Logs a WARNING before returning _EX_TEMPFAIL on any retriable status

`_run_smtp_mode`
    - Returns _EX_OK (0) when SMTPServer.run() completes without error
    - Returns _EX_ERROR (1) when SMTPServer.run() raises TelegramSendmailError
      or an unexpected Exception
    - Constructs SMTPServer with a callable on_message handler

`_run_probe_mode`
    - Returns _EX_OK (0) when test message is delivered successfully
    - Sends _PROBE_MESSAGE as the text payload and to the configured chat_id
    - Returns _EX_ERROR (1) when TelegramAPIError has a non-retriable or no status code
    - Returns _EX_ERROR (1) when TelegramSendmailError or an unexpected Exception is raised
    - Returns _EX_TEMPFAIL (75) when TelegramAPIError has a retriable status code (429, 500)
    - Does not read from stdin and does not write to the spool file
    - Logs success at INFO level and an "EX_TEMPFAIL" WARNING on transient failure

`_run_interactive_mode`
    - Returns _EX_ERROR (1)
    - Writes all output to sys.stderr (stdout remains empty)
    - Output contains the package version string
    - Output mentions "Pipe mode:" with a usage example
    - Output mentions "SMTP mode:" and the -bs flag
    - Output mentions "Probe mode:" and the --probe flag
    - Output mentions "--help" to direct the operator to full documentation

`main`
    - Calls sys.exit(78) when ConfigLoader.load() raises ConfigurationError
    - Calls sys.exit(1) when sys.stdin.isatty() returns True (interactive guard)
    - Calls sys.exit(0) when pipe-mode delivery succeeds with non-TTY stdin
    - Invokes SMTPServer (via _run_smtp_mode) and calls sys.exit(0) when the -bs flag
      is present and the SMTP session completes cleanly
    - Calls sys.exit(0) when --probe flag is present and probe succeeds
    - --probe takes dispatch priority over -bs if both are present
    - Calls sys.exit(75) when pipe-mode hits HTTP 429 rate limit
    - Installs a _TokenRedactFilter on every root-logger handler after config is loaded
    - Logs ConfigurationError at ERROR level before exiting with code 78

Design notes
------------
- All dependencies inside `_deliver`, `_run_pipe_mode`, and `_run_smtp_mode`
  are patched in the `telegram_sendmail.__main__` namespace because that is
  where Python resolves the names at call time; patching the source module
  has no effect on already-bound references.
- The `patched_config_loader` fixture from conftest is used for all
  `TestMainDispatch` tests that need a successful config step; it patches
  `ConfigLoader.load` at the class level and is automatically reverted
  after each test.
- `TestTokenRedactFilter` constructs `logging.LogRecord` instances directly
  rather than emitting through a live logger, because the test target is the
  filter's string replacement logic, not the logging infrastructure.
"""

from __future__ import annotations

import io
import logging
import sys
from dataclasses import replace
from typing import Any, TextIO

import pytest
import requests_mock as requests_mock_module

import telegram_sendmail.__main__ as main_module
import telegram_sendmail.config as cfg_module
from telegram_sendmail import __version__
from telegram_sendmail.__main__ import (
    _EX_CONFIG,
    _EX_ERROR,
    _EX_OK,
    _EX_TEMPFAIL,
    _MAX_PIPE_SIZE,
    _PROBE_MESSAGE,
    _bounded_stdin_read,
    _deliver,
    _is_suppressed,
    _run_interactive_mode,
    _run_pipe_mode,
    _run_probe_mode,
    _run_smtp_mode,
    _TokenRedactFilter,
    main,
)
from telegram_sendmail.config import AppConfig
from telegram_sendmail.exceptions import (
    ConfigurationError,
    ParsingError,
    SpoolError,
    TelegramAPIError,
    TelegramSendmailError,
)
from telegram_sendmail.parser import ParsedEmail

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_FAKE_PARSED = ParsedEmail(
    sender="cron@host.local",
    subject="Disk Alert",
    body="body text",
    has_attachments=False,
)

_FAKE_FORMATTED = "📬 <b>root@localhost</b>\nFormatted body"


class _SpoolerOk:
    """Spooler stub that records the raw email and succeeds silently."""

    last_written: str = ""

    def __init__(self, config: AppConfig) -> None:
        pass

    def write(self, raw: str) -> None:
        _SpoolerOk.last_written = raw


class _SpoolerFails:
    """Spooler stub that mimics real MailSpooler.write: warns then raises."""

    def __init__(self, config: AppConfig) -> None:
        pass

    def write(self, raw: str) -> None:
        # Real MailSpooler.write emits this WARNING before raising SpoolError.
        logging.getLogger("telegram_sendmail.spool").warning(
            "Failed to write email to spool file '/tmp/test': disk full — "
            "the message will still be forwarded to Telegram"
        )
        raise SpoolError("disk full")


class _ParserOk:
    """EmailParser stub that returns _FAKE_PARSED and _FAKE_FORMATTED."""

    def __init__(self, config: AppConfig) -> None:
        pass

    def parse(
        self,
        raw: str,
        sender_override: str | None = None,
        subject_override: str | None = None,
    ) -> ParsedEmail:
        return _FAKE_PARSED

    def format_for_telegram(self, parsed: ParsedEmail) -> str:
        return _FAKE_FORMATTED


class _ClientOk:
    """TelegramClient context-manager stub that succeeds silently."""

    def __init__(self, config: AppConfig) -> None:
        pass

    def __enter__(self) -> _ClientOk:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def send(self, text: str) -> None:
        pass


class _SMTPServerOk:
    """SMTPServer stub whose run() completes without error."""

    def __init__(self, config: AppConfig, on_message: Any) -> None:
        pass

    def run(self) -> None:
        pass


@pytest.fixture
def no_setup_logging(monkeypatch: pytest.MonkeyPatch):
    """Prevent _setup_logging from attaching handlers to logging.root."""
    monkeypatch.setattr(main_module, "_setup_logging", lambda **_: None)


@pytest.fixture
def app_config_with_filters(app_config: AppConfig) -> AppConfig:
    """Return `app_config` with subject and sender suppression patterns."""
    return replace(
        app_config,
        suppress_subject=("cron *", "*logwatch*"),
        suppress_sender=("*@noreply.local",),
    )


# --------------------------------------------------------------------------
# _is_suppressed — filter pattern matching
# --------------------------------------------------------------------------


class TestIsSuppressed:
    def test_returns_false_when_no_patterns_configured(self, app_config: AppConfig):
        assert _is_suppressed(_FAKE_PARSED, app_config) is False

    def test_returns_true_when_subject_matches_pattern(
        self, app_config_with_filters: AppConfig
    ):
        parsed = replace(_FAKE_PARSED, subject="Cron Job Success")
        assert _is_suppressed(parsed, app_config_with_filters) is True

    def test_returns_true_when_sender_matches_pattern(
        self, app_config_with_filters: AppConfig
    ):
        parsed = replace(_FAKE_PARSED, sender="daemon@noreply.local")
        assert _is_suppressed(parsed, app_config_with_filters) is True

    def test_returns_false_when_neither_header_matches(
        self, app_config_with_filters: AppConfig
    ):
        assert _is_suppressed(_FAKE_PARSED, app_config_with_filters) is False

    def test_matching_is_case_insensitive(self, app_config: AppConfig):
        config = replace(app_config, suppress_subject=("CRON *",))
        parsed = replace(_FAKE_PARSED, subject="cron job output")
        assert _is_suppressed(parsed, config) is True

    def test_subject_checked_before_sender(
        self,
        app_config: AppConfig,
        caplog: pytest.LogCaptureFixture,
    ):
        config = replace(
            app_config, suppress_subject=("match*",), suppress_sender=("match@*",)
        )
        parsed = replace(_FAKE_PARSED, sender="match@host.local", subject="Match This")
        with caplog.at_level(logging.DEBUG, logger="telegram_sendmail.__main__"):
            result = _is_suppressed(parsed, config)
        assert result is True
        # Subject match fires first; the DEBUG log should reference Subject.
        assert any("Subject" in r.message for r in caplog.records)
        assert not any("From" in r.message for r in caplog.records)

    def test_logs_debug_with_matched_pattern(
        self,
        app_config_with_filters: AppConfig,
        caplog: pytest.LogCaptureFixture,
    ):
        parsed = replace(_FAKE_PARSED, subject="Logwatch Daily Report")
        with caplog.at_level(logging.DEBUG, logger="telegram_sendmail.__main__"):
            _is_suppressed(parsed, app_config_with_filters)
        assert any(
            "*logwatch*" in r.message and "Subject" in r.message for r in caplog.records
        )


# --------------------------------------------------------------------------
# _bounded_stdin_read — stdin size enforcement
# --------------------------------------------------------------------------


class TestBoundedStdinRead:
    def test_content_under_limit_is_returned_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        content = "x" * 100
        monkeypatch.setattr(sys, "stdin", io.StringIO(content))
        assert _bounded_stdin_read() == content

    def test_content_exactly_at_limit_is_returned_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        content = "x" * _MAX_PIPE_SIZE
        monkeypatch.setattr(sys, "stdin", io.StringIO(content))
        assert _bounded_stdin_read() == content

    def test_oversized_input_is_truncated_to_max_pipe_size(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        content = "x" * (_MAX_PIPE_SIZE + 1000)
        monkeypatch.setattr(sys, "stdin", io.StringIO(content))
        result = _bounded_stdin_read()
        assert len(result) == _MAX_PIPE_SIZE

    def test_oversized_input_emits_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        content = "x" * (_MAX_PIPE_SIZE + 1)
        monkeypatch.setattr(sys, "stdin", io.StringIO(content))
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.__main__"):
            _bounded_stdin_read()
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_content_under_limit_emits_no_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        monkeypatch.setattr(sys, "stdin", io.StringIO("small payload"))
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.__main__"):
            _bounded_stdin_read()
        assert not any(r.levelname == "WARNING" for r in caplog.records)

    def test_empty_stdin_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO())
        assert _bounded_stdin_read() == ""


# --------------------------------------------------------------------------
# _TokenRedactFilter — bot token redaction in log output
# --------------------------------------------------------------------------


class TestTokenRedactFilter:
    _TOKEN = "123456789:AAtestBOTtokenXXXXXXXXXXXXXXXXXXX"
    _PLACEHOLDER = "12345678[…REDACTED…]"

    def test_redacts_token_from_plain_message(self):
        filt = _TokenRedactFilter(self._TOKEN)
        record = logging.LogRecord(
            name="urllib3.connectionpool",
            level=logging.DEBUG,
            pathname="",
            lineno=0,
            msg=f"POST https://api.telegram.org/bot{self._TOKEN}/sendMessage",
            args=None,
            exc_info=None,
        )
        filt.filter(record)
        assert self._TOKEN not in record.getMessage()
        assert self._PLACEHOLDER in record.getMessage()

    def test_redacts_token_from_percent_style_args(self):
        filt = _TokenRedactFilter(self._TOKEN)
        record = logging.LogRecord(
            name="urllib3.connectionpool",
            level=logging.DEBUG,
            pathname="",
            lineno=0,
            msg="POST %s",
            args=(f"https://api.telegram.org/bot{self._TOKEN}/sendMessage",),
            exc_info=None,
        )
        filt.filter(record)
        assert self._TOKEN not in record.getMessage()
        assert record.args is None

    def test_passes_through_records_without_token(self):
        filt = _TokenRedactFilter(self._TOKEN)
        original_msg = "Connection pool is full"
        record = logging.LogRecord(
            name="urllib3.connectionpool",
            level=logging.DEBUG,
            pathname="",
            lineno=0,
            msg=original_msg,
            args=None,
            exc_info=None,
        )
        filt.filter(record)
        assert record.getMessage() == original_msg

    def test_filter_always_returns_true(self):
        # The filter redacts but never suppresses records.
        filt = _TokenRedactFilter(self._TOKEN)
        record = logging.LogRecord(
            name="test",
            level=logging.DEBUG,
            pathname="",
            lineno=0,
            msg=f"url={self._TOKEN}",
            args=None,
            exc_info=None,
        )
        assert filt.filter(record) is True


# --------------------------------------------------------------------------
# _deliver — pipeline wiring and SpoolError resilience
# --------------------------------------------------------------------------


class TestDeliverPipeline:
    _stages: list[str] = []

    class _TrackingSpooler:
        """Records each write attempt by appending "spool" to _stages."""

        def __init__(self, config: AppConfig) -> None:
            pass

        def write(self, raw: str) -> None:
            TestDeliverPipeline._stages.append("spool")

    class _TrackingParser:
        """Records each parse call via _stages; optionally raises."""

        def __init__(self, config: AppConfig, *, failing: bool = False) -> None:
            self._failing = failing

        def parse(self, raw: str, **kw: Any) -> ParsedEmail:
            if self._failing:
                raise ParsingError("malformed MIME")
            TestDeliverPipeline._stages.append("parse")
            return _FAKE_PARSED

        def format_for_telegram(self, p: ParsedEmail) -> str:
            return _FAKE_FORMATTED

    class _TrackingClient:
        """Context-manager stub that records send text and stage; optionally raises."""

        _sent_texts: list[str] = []

        def __init__(self, config: AppConfig, *, failing: bool = False) -> None:
            self._failing = failing

        def __enter__(self) -> TestDeliverPipeline._TrackingClient:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def send(self, text: str) -> None:
            if self._failing:
                raise TelegramAPIError("connection refused", status_code=503)
            self._sent_texts.append(text)
            TestDeliverPipeline._stages.append("send")

    def test_happy_path_calls_all_three_stages_in_order(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        type(self)._stages = []
        monkeypatch.setattr(main_module, "MailSpooler", self._TrackingSpooler)
        monkeypatch.setattr(main_module, "EmailParser", self._TrackingParser)
        monkeypatch.setattr(main_module, "TelegramClient", self._TrackingClient)
        _deliver("raw email", None, app_config)
        assert self._stages == ["spool", "parse", "send"]

    def test_non_matching_message_proceeds_through_full_pipeline(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        type(self)._stages = []
        config = replace(app_config, suppress_subject=("no match*",))
        monkeypatch.setattr(main_module, "MailSpooler", self._TrackingSpooler)
        monkeypatch.setattr(main_module, "EmailParser", self._TrackingParser)
        monkeypatch.setattr(main_module, "TelegramClient", self._TrackingClient)
        _deliver("raw email", None, config)
        assert self._stages == ["spool", "parse", "send"]

    def test_suppressed_message_still_spools_and_skips_send(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        type(self)._stages = []
        config = replace(app_config, suppress_subject=("disk alert",))
        monkeypatch.setattr(main_module, "MailSpooler", self._TrackingSpooler)
        monkeypatch.setattr(main_module, "EmailParser", self._TrackingParser)
        monkeypatch.setattr(main_module, "TelegramClient", self._TrackingClient)
        _deliver("raw email", None, config)
        assert self._stages == ["spool", "parse"]

    def test_spool_error_does_not_propagate(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(main_module, "MailSpooler", _SpoolerFails)
        monkeypatch.setattr(main_module, "EmailParser", _ParserOk)
        monkeypatch.setattr(main_module, "TelegramClient", _ClientOk)
        # Must not raise SpoolError or any other exception.
        _deliver("raw email", None, app_config)

    def test_parser_invoked_after_spool_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        type(self)._stages = []
        monkeypatch.setattr(main_module, "MailSpooler", _SpoolerFails)
        monkeypatch.setattr(main_module, "EmailParser", self._TrackingParser)
        monkeypatch.setattr(main_module, "TelegramClient", _ClientOk)
        _deliver("raw email", None, app_config)
        assert self._stages == ["parse"], (
            "EmailParser.parse must be called even after SpoolError"
        )

    def test_telegram_client_send_called_after_spool_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        type(self)._stages = []
        monkeypatch.setattr(main_module, "MailSpooler", _SpoolerFails)
        monkeypatch.setattr(main_module, "EmailParser", _ParserOk)
        monkeypatch.setattr(main_module, "TelegramClient", self._TrackingClient)
        _deliver("raw email", None, app_config)
        assert self._stages == ["send"], (
            "TelegramClient.send must be called even after SpoolError"
        )

    def test_spool_failure_warning_is_logged_by_spool_layer(
        self,
        app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        monkeypatch.setattr(main_module, "MailSpooler", _SpoolerFails)
        monkeypatch.setattr(main_module, "EmailParser", _ParserOk)
        monkeypatch.setattr(main_module, "TelegramClient", _ClientOk)
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.spool"):
            _deliver("raw email", None, app_config)
        # The warning is emitted by MailSpooler.write (spool layer), not _deliver.
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_send_receives_text_from_format_for_telegram(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        type(self)._TrackingClient._sent_texts = []
        monkeypatch.setattr(main_module, "MailSpooler", _SpoolerOk)
        monkeypatch.setattr(main_module, "EmailParser", _ParserOk)
        monkeypatch.setattr(main_module, "TelegramClient", self._TrackingClient)
        _deliver("raw email", None, app_config)
        assert self._TrackingClient._sent_texts == [_FAKE_FORMATTED]

    def test_parsing_error_propagates_from_deliver(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(main_module, "MailSpooler", _SpoolerOk)
        monkeypatch.setattr(main_module, "TelegramClient", _ClientOk)
        monkeypatch.setattr(
            main_module,
            "EmailParser",
            lambda cfg: self._TrackingParser(cfg, failing=True),
        )
        with pytest.raises(ParsingError):
            _deliver("raw email", None, app_config)

    def test_telegram_api_error_propagates_from_deliver(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(main_module, "MailSpooler", _SpoolerOk)
        monkeypatch.setattr(main_module, "EmailParser", _ParserOk)
        monkeypatch.setattr(
            main_module,
            "TelegramClient",
            lambda cfg: self._TrackingClient(cfg, failing=True),
        )
        with pytest.raises(TelegramAPIError):
            _deliver("raw email", None, app_config)


# --------------------------------------------------------------------------
# _run_pipe_mode — exit-code routing
# --------------------------------------------------------------------------


class TestRunPipeMode:
    class _UnreadableStdin:
        """stdin stub that raises OSError on read(), simulating a broken pipe."""

        def read(self) -> str:
            raise OSError("broken pipe")

    @staticmethod
    def _fail_with(exc: Exception) -> Any:
        """Return a callable that raises the given exception when invoked."""

        def _raiser(*args: Any) -> None:
            raise exc

        return _raiser

    @staticmethod
    def _capture_to(destination: list[str]) -> Any:
        """Return a callable that appends its first argument to destination."""

        def _capturer(raw: str, *args: Any) -> None:
            destination.append(raw)

        return _capturer

    def test_successful_delivery_returns_ex_ok(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(main_module, "_deliver", lambda *_: None)
        monkeypatch.setattr(sys, "stdin", io.StringIO("From: x\n\nbody"))
        assert _run_pipe_mode(None, None, app_config) == _EX_OK

    def test_stdin_read_error_returns_ex_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(sys, "stdin", self._UnreadableStdin())
        assert _run_pipe_mode(None, None, app_config) == _EX_ERROR

    def test_parsing_error_returns_ex_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "_deliver",
            self._fail_with(ParsingError("malformed MIME content")),
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("email"))
        assert _run_pipe_mode(None, None, app_config) == _EX_ERROR

    def test_telegram_rate_limit_429_returns_ex_tempfail(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "_deliver",
            self._fail_with(TelegramAPIError("too many requests", status_code=429)),
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("email"))
        assert _run_pipe_mode(None, None, app_config) == _EX_TEMPFAIL

    def test_telegram_server_error_500_returns_ex_tempfail(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "_deliver",
            self._fail_with(TelegramAPIError("internal server error", status_code=500)),
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("email"))
        assert _run_pipe_mode(None, None, app_config) == _EX_TEMPFAIL

    def test_telegram_bad_gateway_502_returns_ex_tempfail(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "_deliver",
            self._fail_with(TelegramAPIError("bad gateway", status_code=502)),
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("email"))
        assert _run_pipe_mode(None, None, app_config) == _EX_TEMPFAIL

    def test_telegram_api_error_non_retriable_status_returns_ex_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "_deliver",
            self._fail_with(TelegramAPIError("bad request", status_code=400)),
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("email"))
        assert _run_pipe_mode(None, None, app_config) == _EX_ERROR

    def test_telegram_api_error_with_no_status_returns_ex_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "_deliver",
            # status_code=None means the error arose before any HTTP response.
            self._fail_with(TelegramAPIError("connection failed", status_code=None)),
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("email"))
        assert _run_pipe_mode(None, None, app_config) == _EX_ERROR

    def test_telegram_sendmail_error_returns_ex_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "_deliver",
            self._fail_with(TelegramSendmailError("generic failure")),
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("email"))
        assert _run_pipe_mode(None, None, app_config) == _EX_ERROR

    def test_unexpected_bare_exception_returns_ex_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "_deliver",
            self._fail_with(RuntimeError("unexpected crash occurred")),
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("email"))
        assert _run_pipe_mode(None, None, app_config) == _EX_ERROR

    def test_subject_override_prepended_when_no_subject_header_present(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        received: list[str] = []
        monkeypatch.setattr(main_module, "_deliver", self._capture_to(received))
        monkeypatch.setattr(sys, "stdin", io.StringIO("From: cron@host\n\nbody"))
        _run_pipe_mode(None, "Injected Subject", app_config)
        assert received[0].startswith("Subject: Injected Subject\n")

    def test_subject_override_not_prepended_when_subject_header_already_present(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        received: list[str] = []
        monkeypatch.setattr(main_module, "_deliver", self._capture_to(received))
        # RFC 2822 §2.2: field names are case-insensitive; the guard checks
        # raw_email[:500].lower() so we also test a mixed-case "Subject:" header.
        monkeypatch.setattr(
            sys, "stdin", io.StringIO("Subject: Existing Subject\n\nbody")
        )
        _run_pipe_mode(None, "Override Attempt", app_config)
        # Original email must arrive unmodified.
        assert not received[0].startswith("Subject: Override Attempt")
        assert "Subject: Existing Subject" in received[0]

    def test_subject_override_is_case_insensitive_for_existing_header(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        received: list[str] = []
        monkeypatch.setattr(main_module, "_deliver", self._capture_to(received))
        # Lower-case "subject:" must also prevent duplication.
        monkeypatch.setattr(sys, "stdin", io.StringIO("subject: lowercase\n\nbody"))
        _run_pipe_mode(None, "Should Not Appear", app_config)
        assert not received[0].startswith("Subject: Should Not Appear")

    def test_none_subject_override_leaves_email_unmodified(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        received: list[str] = []
        original = "From: cron@host\n\nbody"
        monkeypatch.setattr(main_module, "_deliver", self._capture_to(received))
        monkeypatch.setattr(sys, "stdin", io.StringIO(original))
        _run_pipe_mode(None, None, app_config)
        assert received[0] == original

    def test_rate_limit_warning_is_logged_before_returning_tempfail(
        self,
        app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        monkeypatch.setattr(
            main_module,
            "_deliver",
            self._fail_with(TelegramAPIError("too many requests", status_code=429)),
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("email"))
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.__main__"):
            result = _run_pipe_mode(None, None, app_config)
        assert result == _EX_TEMPFAIL
        # A WARNING must be emitted so syslog records the rate-limit event.
        assert any(r.levelname == "WARNING" for r in caplog.records)


# --------------------------------------------------------------------------
# _run_smtp_mode — SMTP server invocation and exception mapping
# --------------------------------------------------------------------------


class TestRunSmtpMode:
    class _SMTPServerCapturing:
        """SMTPServer stub that stores the on_message callback for inspection."""

        captured_on_message: Any = None

        def __init__(self, config: AppConfig, on_message: Any) -> None:
            TestRunSmtpMode._SMTPServerCapturing.captured_on_message = on_message

        def run(self) -> None:
            pass

    @staticmethod
    def _smtp_server_raising(exc: BaseException) -> type[Any]:
        """Return a stub SMTPServer class whose run() raises exc."""

        class _Stub:
            def __init__(self, config: AppConfig, on_message: Any) -> None:
                pass

            def run(self) -> None:
                raise exc

        return _Stub

    def test_successful_server_run_returns_ex_ok(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(main_module, "SMTPServer", _SMTPServerOk)
        assert _run_smtp_mode(app_config) == _EX_OK

    def test_telegram_sendmail_error_returns_ex_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "SMTPServer",
            self._smtp_server_raising(TelegramSendmailError("API unreachable")),
        )
        assert _run_smtp_mode(app_config) == _EX_ERROR

    def test_unexpected_exception_returns_ex_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "SMTPServer",
            self._smtp_server_raising(RuntimeError("segfault simulation")),
        )
        assert _run_smtp_mode(app_config) == _EX_ERROR

    def test_smtp_server_constructed_with_on_message_callback(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        self._SMTPServerCapturing.captured_on_message = None
        monkeypatch.setattr(main_module, "SMTPServer", self._SMTPServerCapturing)
        _run_smtp_mode(app_config)
        assert self._SMTPServerCapturing.captured_on_message is not None
        assert callable(self._SMTPServerCapturing.captured_on_message)


# --------------------------------------------------------------------------
# _run_probe_mode — config validation and Telegram connectivity check
# --------------------------------------------------------------------------


class TestRunProbeMode:
    class _SpyStdin:
        """stdin stub that records whether read() was called."""

        def __init__(self, stdin: TextIO) -> None:
            self._original_stdin = stdin
            self._read_called = False

        def read(self, *args: Any) -> str:
            self._read_called = True
            return ""

        def isatty(self) -> bool:
            return self._original_stdin.isatty()

    @staticmethod
    def _client_raising(exc: BaseException) -> type[Any]:
        """Return a stub TelegramClient class whose send() raises exc."""

        class _Stub:
            def __init__(self, config: AppConfig) -> None:
                pass

            def __enter__(self) -> _Stub:
                return self

            def __exit__(self, *args: Any) -> None:
                pass

            def send(self, text: str) -> None:
                raise exc

        return _Stub

    def test_success_returns_ex_ok(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(main_module, "TelegramClient", _ClientOk)
        assert _run_probe_mode(app_config) == _EX_OK

    def test_sends_probe_message_text(
        self,
        app_config: AppConfig,
        mock_telegram_ok: requests_mock_module.Mocker,
    ):
        _run_probe_mode(app_config)
        assert mock_telegram_ok.last_request.json()["text"] == _PROBE_MESSAGE

    def test_sends_to_configured_chat_id(
        self,
        app_config: AppConfig,
        mock_telegram_ok: requests_mock_module.Mocker,
    ):
        _run_probe_mode(app_config)
        assert mock_telegram_ok.last_request.json()["chat_id"] == app_config.chat_id

    def test_telegram_api_error_non_retriable_returns_ex_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "TelegramClient",
            self._client_raising(TelegramAPIError("bad request", status_code=400)),
        )
        assert _run_probe_mode(app_config) == _EX_ERROR

    def test_telegram_api_error_429_returns_ex_tempfail(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "TelegramClient",
            self._client_raising(TelegramAPIError("rate limited", status_code=429)),
        )
        assert _run_probe_mode(app_config) == _EX_TEMPFAIL

    def test_telegram_api_error_500_returns_ex_tempfail(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "TelegramClient",
            self._client_raising(TelegramAPIError("server error", status_code=500)),
        )
        assert _run_probe_mode(app_config) == _EX_TEMPFAIL

    def test_telegram_api_error_no_status_returns_ex_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "TelegramClient",
            self._client_raising(TelegramAPIError("connection refused")),
        )
        assert _run_probe_mode(app_config) == _EX_ERROR

    def test_telegram_sendmail_error_returns_ex_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "TelegramClient",
            self._client_raising(TelegramSendmailError("generic failure")),
        )
        assert _run_probe_mode(app_config) == _EX_ERROR

    def test_unexpected_exception_returns_ex_error(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            main_module,
            "TelegramClient",
            self._client_raising(RuntimeError("unexpected error")),
        )
        assert _run_probe_mode(app_config) == _EX_ERROR

    def test_does_not_read_stdin(
        self,
        app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ):
        spy_stdin = self._SpyStdin(sys.stdin)
        monkeypatch.setattr(main_module, "TelegramClient", _ClientOk)
        monkeypatch.setattr(sys, "stdin", spy_stdin)
        _run_probe_mode(app_config)
        assert not spy_stdin._read_called

    def test_does_not_write_to_spool(
        self,
        app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(main_module, "TelegramClient", _ClientOk)
        _run_probe_mode(app_config)
        assert not app_config.spool_path.exists()

    def test_logs_info_on_success(
        self,
        app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        monkeypatch.setattr(main_module, "TelegramClient", _ClientOk)
        with caplog.at_level(logging.INFO, logger="telegram_sendmail.__main__"):
            _run_probe_mode(app_config)
        assert any("Probe succeeded" in r.message for r in caplog.records)

    def test_logs_warning_on_transient_failure(
        self,
        app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        monkeypatch.setattr(
            main_module,
            "TelegramClient",
            self._client_raising(TelegramAPIError("rate limited", status_code=429)),
        )
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.__main__"):
            _run_probe_mode(app_config)
        assert any(
            "EX_TEMPFAIL" in r.message and r.levelname == "WARNING"
            for r in caplog.records
        )


# --------------------------------------------------------------------------
# _run_interactive_mode — stderr output and return value
# --------------------------------------------------------------------------


class TestRunInteractiveMode:
    def test_returns_ex_error(self, capsys: pytest.CaptureFixture[str]):
        assert _run_interactive_mode() == _EX_ERROR

    def test_all_output_goes_to_stderr(self, capsys: pytest.CaptureFixture[str]):
        _run_interactive_mode()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err != ""

    def test_stderr_output_contains_package_version(
        self, capsys: pytest.CaptureFixture[str]
    ):
        _run_interactive_mode()
        assert __version__ in capsys.readouterr().err

    def test_stderr_output_mentions_pipe_mode_usage(
        self, capsys: pytest.CaptureFixture[str]
    ):
        _run_interactive_mode()
        assert "Pipe mode:" in capsys.readouterr().err

    def test_stderr_output_mentions_smtp_mode_and_bs_flag(
        self, capsys: pytest.CaptureFixture[str]
    ):
        _run_interactive_mode()
        err = capsys.readouterr().err
        assert "SMTP mode:" in err
        assert "-bs" in err

    def test_stderr_output_mentions_probe_mode_and_probe_flag(
        self, capsys: pytest.CaptureFixture[str]
    ):
        _run_interactive_mode()
        err = capsys.readouterr().err
        assert "Probe mode:" in err
        assert "--probe" in err

    def test_stderr_output_directs_to_help_flag(
        self, capsys: pytest.CaptureFixture[str]
    ):
        _run_interactive_mode()
        # Operators must know how to get full usage; --help must be mentioned.
        assert "--help" in capsys.readouterr().err


# --------------------------------------------------------------------------
# main() — full dispatch integration with sys.exit capture
# --------------------------------------------------------------------------


class TestMainDispatch:
    class _FakeTTY(io.StringIO):
        """StringIO whose isatty() returns True, simulating a real terminal."""

        def isatty(self) -> bool:
            return True

    @staticmethod
    def _raise_config_error() -> AppConfig:
        """Simulate config loading failure by raising ConfigurationError."""
        raise ConfigurationError("config file not found")

    @staticmethod
    def _raise_rate_limit(raw: str, sender: Any, config: Any):
        """Simulate Telegram API rate limit by raising TelegramAPIError with 429."""
        raise TelegramAPIError("too many requests", status_code=429)

    def test_configuration_error_exits_with_code_78(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_setup_logging: None,
    ):
        monkeypatch.setattr(cfg_module.ConfigLoader, "load", self._raise_config_error)
        monkeypatch.setattr(sys, "argv", ["telegram-sendmail"])
        monkeypatch.setattr(sys, "stdin", io.StringIO())
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == _EX_CONFIG

    def test_tty_stdin_exits_with_code_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        patched_config_loader: AppConfig,
        no_setup_logging: None,
    ):
        monkeypatch.setattr(sys, "argv", ["telegram-sendmail"])
        # _FakeTTY.isatty() returns True -> interactive guard -> EX_ERROR
        monkeypatch.setattr(sys, "stdin", self._FakeTTY())
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == _EX_ERROR

    def test_non_tty_stdin_pipe_mode_success_exits_0(
        self,
        monkeypatch: pytest.MonkeyPatch,
        patched_config_loader: AppConfig,
        no_setup_logging: None,
    ):
        monkeypatch.setattr(sys, "argv", ["telegram-sendmail"])
        monkeypatch.setattr(sys, "stdin", io.StringIO("From: x\n\nbody"))
        # Stub _deliver so no real network or filesystem access occurs.
        monkeypatch.setattr(main_module, "_deliver", lambda *_: None)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == _EX_OK

    def test_bs_flag_invokes_smtp_mode_and_exits_0(
        self,
        monkeypatch: pytest.MonkeyPatch,
        patched_config_loader: AppConfig,
        no_setup_logging: None,
    ):
        monkeypatch.setattr(sys, "argv", ["telegram-sendmail", "-bs"])
        monkeypatch.setattr(sys, "stdin", io.StringIO())
        monkeypatch.setattr(main_module, "SMTPServer", _SMTPServerOk)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == _EX_OK

    def test_probe_flag_invokes_probe_mode_and_exits_0(
        self,
        monkeypatch: pytest.MonkeyPatch,
        patched_config_loader: AppConfig,
        no_setup_logging: None,
    ):
        monkeypatch.setattr(sys, "argv", ["telegram-sendmail", "--probe"])
        # stdin is a TTY; probe must not fall through to interactive mode.
        monkeypatch.setattr(sys, "stdin", self._FakeTTY())
        monkeypatch.setattr(main_module, "TelegramClient", _ClientOk)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == _EX_OK

    def test_probe_flag_takes_priority_over_bs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        patched_config_loader: AppConfig,
        no_setup_logging: None,
    ):
        monkeypatch.setattr(sys, "argv", ["telegram-sendmail", "-bs", "--probe"])
        monkeypatch.setattr(sys, "stdin", self._FakeTTY())
        monkeypatch.setattr(main_module, "TelegramClient", _ClientOk)
        monkeypatch.setattr(main_module, "SMTPServer", RuntimeError, True)
        with pytest.raises(SystemExit) as exc_info:
            main()
        # --probe wins over -bs; exit 0 from probe, not 1 from SMTP.
        assert exc_info.value.code == _EX_OK

    def test_pipe_mode_rate_limit_exits_75_via_main(
        self,
        monkeypatch: pytest.MonkeyPatch,
        patched_config_loader: AppConfig,
        no_setup_logging: None,
    ):
        monkeypatch.setattr(sys, "argv", ["telegram-sendmail"])
        monkeypatch.setattr(sys, "stdin", io.StringIO("email"))
        monkeypatch.setattr(main_module, "_deliver", self._raise_rate_limit)
        with pytest.raises(SystemExit) as exc_info:
            main()
        # EX_TEMPFAIL (75) must propagate all the way through main() -> sys.exit
        assert exc_info.value.code == _EX_TEMPFAIL

    def test_config_error_is_logged_before_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_setup_logging: None,
        caplog: pytest.LogCaptureFixture,
    ):
        monkeypatch.setattr(cfg_module.ConfigLoader, "load", self._raise_config_error)
        monkeypatch.setattr(sys, "argv", ["telegram-sendmail"])
        monkeypatch.setattr(sys, "stdin", io.StringIO())
        with caplog.at_level(logging.ERROR, logger="telegram_sendmail.__main__"):
            with pytest.raises(SystemExit):
                main()
        assert any("Configuration error" in r.message for r in caplog.records), (
            "ConfigurationError must be logged at ERROR level before sys.exit"
        )

    def test_debug_mode_installs_token_redact_filter_on_handlers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        patched_config_loader: AppConfig,
        no_setup_logging: None,
    ):
        monkeypatch.setattr(sys, "argv", ["telegram-sendmail", "--debug"])
        monkeypatch.setattr(sys, "stdin", io.StringIO("From: x\n\nbody"))
        monkeypatch.setattr(main_module, "_deliver", lambda *_: None)
        with pytest.raises(SystemExit):
            main()
        for handler in logging.root.handlers:
            assert any(isinstance(f, _TokenRedactFilter) for f in handler.filters)
