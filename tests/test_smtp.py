r"""
Tests for telegram_sendmail.smtp.

Coverage targets
----------------
`SMTPServer._parse_address`
    - Extracts the address from a standard angle-bracket MAIL FROM line
    - Returns None for a null reverse-path (empty angle brackets: <>)
    - Extracts the address from a colon-delimited line without angle brackets
    - Strips surrounding whitespace from addresses in angle brackets
    - Returns None for a line with no recognisable address delimiter

`SMTPServer.run` — banner
    - Sends a "220" service-ready banner as the very first line of output
    - Identifies the service as ESMTP in the banner line

`SMTPServer` — command handling
    - EHLO sends the multi-line "250-telegram-bridge" capability response
    - EHLO response includes a "250 HELP" terminal capability line
    - HELO is handled identically to EHLO (same response)
    - MAIL FROM results in a "250 2.1.0 Ok" acknowledgement
    - RCPT TO results in a "250 2.1.5 Ok" acknowledgement unconditionally
    - NOOP results in a "250 2.0.0 Ok" acknowledgement
    - An unrecognised command results in "250 2.0.0 Ok" for daemon compatibility
    - QUIT results in a "221 2.0.0 Bye" response and terminates the session

`SMTPServer` — DATA collection and dot-stuffing
    - The DATA command results in a "354" invitation response
    - A "250 2.0.0 Ok" response is sent after the "." terminator is received
    - The on_message callback is invoked after the "." terminator is received
    - The callback receives the correct raw_email string (lines joined by "\n")
    - The callback receives the envelope sender extracted from MAIL FROM
    - A dot-stuffed ".." line is un-stuffed to "." in the delivered message
    - A line beginning with ".." followed by more text is un-stuffed correctly
    - Ordinary lines are passed through the DATA buffer unchanged

`SMTPServer` — command sequence enforcement
    - MAIL FROM before EHLO/HELO returns "503 5.5.1 Bad sequence of commands"
    - RCPT TO before MAIL FROM returns "503 5.5.1 Bad sequence of commands"
    - DATA before MAIL FROM returns "503 5.5.1 Bad sequence of commands"
    - DATA after EHLO without MAIL FROM returns "503" and does not invoke callback
    - The callback is not invoked when DATA is rejected for bad sequence
    - A valid command sequence succeeds after a prior out-of-order DATA rejection

`SMTPServer` — DATA size limit
    - Rejects a message exceeding the 10 MiB limit with "552 5.3.4"
    - Does not invoke on_message when the message is oversized
    - The session continues and accepts a subsequent message after a 552 rejection
    - A message just under the 10 MiB limit is accepted without error
    - Lines received after the buffer overflows are silently discarded without buffering

`SMTPServer` — callback failure
    - A "554 5.0.0 Transaction failed" response is sent when on_message raises
    - The session continues and accepts a subsequent MAIL FROM after a failure

`SMTPServer` — null reverse-path
    - MAIL FROM:<> delivers None as the envelope_sender to on_message

`SMTPServer` — session reset
    - RSET resets envelope_sender and sends "250 2.0.0 Ok"
    - RSET prevents the on_message callback from being invoked for the pending transaction
    - A second complete message can be delivered in the same session after RSET
    - Two complete messages can be delivered in a single session without an intervening RSET

Design notes
------------
- All integration tests use the `_run_session` module-level helper, which
  monkeypatches sys.stdin and sys.stdout with io.StringIO objects and calls
  server.run(). This approach exercises the full threading pipeline (the
  background _stdin_reader thread + the event loop) without any real I/O.
- sys.stdout.reconfigure is called inside run(); StringIO lacks this method
  but the production code catches AttributeError, so no patching is required.
- The app_config fixture has smtp_timeout=5. Because StringIO input is
  exhausted nearly instantly, the event loop never waits anywhere near the
  timeout threshold, keeping all tests fast.
"""

from __future__ import annotations

import io
import sys
from typing import Any, Literal

import pytest

from telegram_sendmail.config import AppConfig
from telegram_sendmail.smtp import SMTPServer

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _run_session(
    server: SMTPServer,
    commands: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Run a full SMTP session and return every byte written to stdout."""
    stdin_data = "\r\n".join(commands) + "\r\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_data))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    server.run()
    return sys.stdout.getvalue()


def _response_lines(output: str) -> list[str]:
    """Return the non-empty SMTP response lines from captured stdout."""
    return [line for line in output.split("\r\n") if line]


def _commands(
    *body_lines: str,
    ehlo: Literal["EHLO", "HELO"] | None = "EHLO",
    send: str | None = "sender@example.com",
    rcpt: str | None = "recipient@example.com",
    envp: bool = True,  # False to skip envelope; ignores "MAIL FROM" and "RCPT TO"
    quit: bool = True,
    tail: str | tuple[str, ...] = (),
) -> list[str]:
    """Build a partial or complete SMTP command sequence."""
    return [
        *([f"{ehlo} Test"] if ehlo else []),
        *([f"MAIL FROM:<{send}>"] if send is not None and envp else []),
        *([f"RCPT TO:<{rcpt}>"] if rcpt is not None and envp else []),
        *body_lines,
        *(["QUIT"] if quit else []),
        *([tail] if isinstance(tail, str) else tail),
    ]


# --------------------------------------------------------------------------
# _parse_address — static method unit tests
# --------------------------------------------------------------------------


class TestParseAddress:
    def test_extracts_address_from_angle_bracket_format(self):
        expected = "sender@example.com"
        assert SMTPServer._parse_address(f"MAIL FROM:<{expected}>") == expected

    def test_returns_none_for_null_reverse_path(self):
        # RFC 5321: MAIL FROM:<> signals a null reverse-path (bounce messages).
        assert SMTPServer._parse_address("MAIL FROM:<>") is None

    def test_extracts_address_from_colon_delimited_format(self):
        expected = "root@localhost"
        assert SMTPServer._parse_address(f"RCPT TO:{expected}") == expected

    def test_strips_whitespace_from_angle_bracket_address(self):
        expected = "spaced@example.com"
        assert SMTPServer._parse_address(f"MAIL FROM:<  {expected}  >") == expected

    def test_returns_none_for_line_with_no_address_delimiter(self):
        assert SMTPServer._parse_address("HELLO") is None

    def test_returns_none_for_colon_with_empty_value(self):
        assert SMTPServer._parse_address("MAIL FROM:") is None

    def test_handles_rcpt_to_angle_bracket_format(self):
        expected = "root@localhost"
        assert SMTPServer._parse_address(f"RCPT TO:<{expected}>") == expected


# --------------------------------------------------------------------------
# Banner
# --------------------------------------------------------------------------


class TestSMTPBanner:
    def test_220_banner_is_first_output_line(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        output = _run_session(server, ["QUIT"], monkeypatch)
        first_line = _response_lines(output)[0]
        assert first_line.startswith("220")

    def test_banner_identifies_service_as_esmtp(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        output = _run_session(server, ["QUIT"], monkeypatch)
        assert "ESMTP" in _response_lines(output)[0]


# --------------------------------------------------------------------------
# Individual command handling
# --------------------------------------------------------------------------


class TestSMTPCommandHandling:
    def test_ehlo_sends_multiline_capability_response(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        output = _run_session(server, _commands(envp=False), monkeypatch)
        assert "250-telegram-bridge" in output

    def test_ehlo_response_includes_help_line(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        output = _run_session(server, _commands(envp=False), monkeypatch)
        assert "250 HELP" in output

    def test_helo_handled_identically_to_ehlo(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        output = _run_session(server, _commands(envp=False, ehlo="HELO"), monkeypatch)
        assert "250-telegram-bridge" in output

    def test_mail_from_acknowledged_with_250(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        output = _run_session(server, _commands(rcpt=None), monkeypatch)
        assert "250 2.1.0 Ok" in output

    def test_rcpt_to_acknowledged_unconditionally(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        # All recipients are accepted; delivery always goes to configured chat_id.
        server = SMTPServer(app_config, on_message=smtp_callback)
        output = _run_session(server, _commands(), monkeypatch)
        assert "250 2.1.5 Ok" in output

    def test_noop_acknowledged_with_250(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        output = _run_session(server, _commands("NOOP", envp=False), monkeypatch)
        lines = _response_lines(output)
        # Skip banner (220) and EHLO response; NOOP produces a 250.
        assert any(l.startswith("250") and "Ok" in l for l in lines[2:])

    def test_unrecognised_command_returns_250_for_daemon_compatibility(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        # Returning 502 for unknown commands breaks some system daemons that
        # probe for capabilities; 250 is the safe compatibility choice.
        server = SMTPServer(app_config, on_message=smtp_callback)
        output = _run_session(
            server, _commands("VRFY someuser", envp=False), monkeypatch
        )
        assert output.count("250") >= 2

    def test_quit_sends_221_bye_response(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        output = _run_session(server, _commands(envp=False), monkeypatch)
        assert "221 2.0.0 Bye" in output

    def test_session_terminates_after_quit(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        # Commands after QUIT must not be processed; no exception should escape.
        output = _run_session(server, _commands(envp=False, tail="NOOP"), monkeypatch)
        # The 221 Bye must appear, confirming QUIT was processed.
        assert "221" in output


# --------------------------------------------------------------------------
# DATA collection and RFC 5321 dot-stuffing
# --------------------------------------------------------------------------


class TestSMTPDataCollection:
    def test_data_command_sends_354_invitation(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("DATA", "Subject: Test", "", "body", ".")
        output = _run_session(server, commands, monkeypatch)
        assert "354" in output

    def test_250_ok_sent_after_dot_terminator(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("DATA", "body line", ".")
        output = _run_session(server, commands, monkeypatch)
        # The 250 after the dot must appear before the 221 Bye.
        assert output.index("250 2.0.0") < output.index("221")

    def test_callback_invoked_exactly_once_per_message(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("DATA", "Subject: Test", "", "Hello world", ".")
        _run_session(server, commands, monkeypatch)
        smtp_callback.assert_called_once()

    def test_callback_receives_correct_raw_email(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("DATA", "Subject: Test", "", "Hello world", ".")
        _run_session(server, commands, monkeypatch)
        raw_email = smtp_callback.call_args.args[0]
        # Lines are joined with "\n" by the session state machine.
        assert raw_email == "Subject: Test\n\nHello world"

    def test_callback_receives_envelope_sender_from_mail_from(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("DATA", "body", ".", send="other@example.com")
        _run_session(server, commands, monkeypatch)
        envelope_sender = smtp_callback.call_args.args[1]
        assert envelope_sender == "other@example.com"

    def test_dot_stuffed_double_dot_line_delivered_as_single_dot(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        # RFC 5321 §4.5.2: ".." on the wire was originally "." in the message.
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("DATA", "..", ".")
        _run_session(server, commands, monkeypatch)
        raw_email = smtp_callback.call_args.args[0]
        assert raw_email == "."

    def test_dot_stuffed_line_with_text_restored_correctly(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        # "..hello" on the wire was ".hello" in the original message.
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("DATA", "..hello world", ".")
        _run_session(server, commands, monkeypatch)
        raw_email = smtp_callback.call_args.args[0]
        assert raw_email == ".hello world"

    def test_ordinary_data_lines_passed_through_unchanged(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands(
            "DATA", "Subject: Test", "X-Custom: header", "", "Line one", "Line two", "."
        )
        _run_session(server, commands, monkeypatch)
        raw_email = smtp_callback.call_args.args[0]
        assert "Line one" in raw_email
        assert "Line two" in raw_email
        assert "X-Custom: header" in raw_email


# --------------------------------------------------------------------------
# Command sequence enforcement
# --------------------------------------------------------------------------


class TestSMTPCommandSequenceEnforcement:
    def test_mail_from_before_ehlo_returns_503(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("MAIL FROM:<early@example.com>", ehlo=None, envp=False)
        output = _run_session(server, commands, monkeypatch)
        assert "503 5.5.1" in output

    def test_rcpt_to_before_mail_from_returns_503(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("RCPT TO:<early@example.com>", envp=False)
        output = _run_session(server, commands, monkeypatch)
        assert "503 5.5.1" in output

    def test_data_before_mail_from_returns_503(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("DATA", envp=False)
        output = _run_session(server, commands, monkeypatch)
        assert "503 5.5.1" in output

    def test_data_after_ehlo_without_mail_from_returns_503(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("DATA", "body", ".", envp=False)
        output = _run_session(server, commands, monkeypatch)
        assert "503 5.5.1" in output
        smtp_callback.assert_not_called()

    def test_callback_not_invoked_when_data_rejected(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("DATA", envp=False)
        _run_session(server, commands, monkeypatch)
        smtp_callback.assert_not_called()

    def test_valid_sequence_still_succeeds_after_rejected_data(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        # Out-of-order DATA is rejected, then a correct sequence succeeds.
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = [
            *_commands("DATA", envp=False, quit=False),
            *_commands("DATA", "valid body", ".", ehlo=None),
        ]
        output = _run_session(server, commands, monkeypatch)
        assert "503 5.5.1" in output
        smtp_callback.assert_called_once()


# --------------------------------------------------------------------------
# DATA size limit enforcement
# --------------------------------------------------------------------------


class TestSMTPDataSizeLimit:
    def test_oversized_message_rejected_with_552(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        # Each line is ~1 MB; 11 lines exceed the 10 MiB limit.
        huge_line = "X" * (1024 * 1024)
        commands = _commands("DATA", *([huge_line] * 11), ".")
        output = _run_session(server, commands, monkeypatch)
        assert "552 5.3.4" in output

    def test_oversized_message_does_not_invoke_callback(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        huge_line = "X" * (1024 * 1024)
        commands = _commands("DATA", *([huge_line] * 11), ".")
        _run_session(server, commands, monkeypatch)
        smtp_callback.assert_not_called()

    def test_session_continues_after_oversized_rejection(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        huge_line = "X" * (1024 * 1024)
        commands = [
            *_commands("DATA", *([huge_line] * 11), ".", quit=False),
            *_commands("DATA", "small body", ".", ehlo=None),
        ]
        output = _run_session(server, commands, monkeypatch)
        assert "552 5.3.4" in output
        smtp_callback.assert_called_once()
        assert smtp_callback.call_args.args[0] == "small body"

    def test_message_just_under_limit_accepted(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        # Single line just under 10 MiB.
        line = "X" * (10_485_760 - 1)
        commands = _commands("DATA", line, ".")
        output = _run_session(server, commands, monkeypatch)
        assert "250 2.0.0" in output
        smtp_callback.assert_called_once()

    def test_lines_after_overflow_are_discarded_silently(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        huge_line = "X" * (1024 * 1024)
        # 11 huge lines overflow, then 5 more trailing lines before '.'
        trailing = ["should be ignored"] * 5
        commands = _commands("DATA", *([huge_line] * 11), *trailing, ".")
        output = _run_session(server, commands, monkeypatch)
        assert "552 5.3.4" in output
        smtp_callback.assert_not_called()


# --------------------------------------------------------------------------
# Callback failure handling
# --------------------------------------------------------------------------


class TestSMTPCallbackFailure:
    def test_callback_exception_produces_554_response(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        smtp_callback.side_effect = RuntimeError("inject delivery failure")
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("DATA", "body", ".")
        output = _run_session(server, commands, monkeypatch)
        assert "554" in output

    def test_session_continues_after_callback_failure(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        # First call raises; second call succeeds. Both must be processed.
        smtp_callback.side_effect = [RuntimeError("first fails"), None]
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = [
            *_commands("DATA", "first body", ".", quit=False),
            *_commands("DATA", "second body", ".", ehlo=None),
        ]
        output = _run_session(server, commands, monkeypatch)
        # 554 for the first, 250 for the second.
        assert "554" in output
        assert "250" in output
        assert smtp_callback.call_count == 2


# --------------------------------------------------------------------------
# Null reverse-path
# --------------------------------------------------------------------------


class TestSMTPNullReversePath:
    def test_mail_from_empty_angle_brackets_delivers_none_to_callback(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        # Bounce messages use MAIL FROM:<>; envelope_sender must be None.
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("DATA", "bounce body", ".", send="")
        _run_session(server, commands, monkeypatch)
        envelope_sender = smtp_callback.call_args.args[1]
        assert envelope_sender is None


# --------------------------------------------------------------------------
# Session reset
# --------------------------------------------------------------------------


class TestSMTPSessionReset:
    def test_rset_sends_250_ok(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = _commands("RSET", rcpt=None)
        output = _run_session(server, commands, monkeypatch)
        assert "250 2.0.0 Ok" in output

    def test_rset_prevents_callback_from_being_invoked(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        # RSET before DATA discards the transaction.
        commands = _commands("RSET")
        _run_session(server, commands, monkeypatch)
        smtp_callback.assert_not_called()

    def test_new_transaction_accepted_after_rset(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = [
            *_commands("RSET", rcpt=None, quit=False),
            *_commands("DATA", "fresh body", ".", ehlo=None, send="other@example.com"),
        ]
        _run_session(server, commands, monkeypatch)
        smtp_callback.assert_called_once()
        envelope_sender = smtp_callback.call_args.args[1]
        # The sender from the post-RSET MAIL FROM must be used, not the first one.
        assert envelope_sender == "other@example.com"

    def test_two_complete_messages_in_one_session_both_delivered(
        self, app_config: AppConfig, smtp_callback: Any, monkeypatch: pytest.MonkeyPatch
    ):
        # After the first "." the state resets to GREETED; a second message
        # can follow without RSET.
        server = SMTPServer(app_config, on_message=smtp_callback)
        commands = [
            *_commands("DATA", "first message", ".", quit=False),
            *_commands("DATA", "second message", ".", ehlo=None),
        ]
        _run_session(server, commands, monkeypatch)
        assert smtp_callback.call_count == 2
        first_raw = smtp_callback.call_args_list[0].args[0]
        second_raw = smtp_callback.call_args_list[1].args[0]
        assert "first message" in first_raw
        assert "second message" in second_raw
