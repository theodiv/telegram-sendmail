"""
Minimal SMTP server for telegram-sendmail.

Public surface
--------------
- `SMTPServer` — runs a sendmail-compatible SMTP state machine on
                 `stdin`/`stdout`.

This module implements the `-bs` (batch SMTP) mode that some system
daemons use to hand mail to `sendmail` via a synchronous SMTP dialogue
rather than a pipe. It does NOT open a network socket; all I/O is
strictly over the process's `stdin`/`stdout` file descriptors.

The server is deliberately ignorant of Telegram, parsing, and spooling.
It fires a caller-supplied `on_message` callback with the raw email
string and the envelope sender. Keeping delivery logic outside this
module means the state machine can be unit-tested without any network
or filesystem dependencies.

Internal components
-------------------
- `_SMTP`          — namespace class of `Final[str]` wire-format response
                     strings emitted by `SMTPServer`.
- `_State`         — `Enum` encoding the RFC 5321 command-sequence lifecycle.
- `_SessionState`  — mutable per-session context dataclass.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Final

from telegram_sendmail.config import AppConfig
from telegram_sendmail.exceptions import SMTPProtocolError

logger = logging.getLogger(__name__)

_MAX_MESSAGE_SIZE: int = 10_485_760  # 10 MiB, advertised via EHLO SIZE extension


# --------------------------------------------------------------------------
# SMTP response constants
# --------------------------------------------------------------------------


class _SMTP:
    """Wire-format SMTP response strings emitted by `SMTPServer`."""

    BANNER: Final = "220 telegram-bridge ESMTP Ready"
    EHLO_GREETING: Final = "250-telegram-bridge"
    EHLO_SIZE: Final = f"250-SIZE {_MAX_MESSAGE_SIZE}"
    EHLO_ENHANCED: Final = "250-ENHANCEDSTATUSCODES"
    EHLO_HELP: Final = "250 HELP"
    OK: Final = "250 2.0.0 Ok"
    MAIL_OK: Final = "250 2.1.0 Ok"
    RCPT_OK: Final = "250 2.1.5 Ok"
    DATA_START: Final = "354 End data with <CRLF>.<CRLF>"
    BYE: Final = "221 2.0.0 Bye"
    BAD_SEQUENCE: Final = "503 5.5.1 Bad sequence of commands"
    MSG_TOO_BIG: Final = "552 5.3.4 Message size exceeds fixed maximum message size"
    TRANSACTION_FAIL: Final = "554 5.0.0 Transaction failed"
    TIMEOUT: Final = "421 4.4.2 Connection timed out"
    INTERNAL_ERROR: Final = "421 4.3.0 Internal server error"

    @staticmethod
    def queued_as(queue_id: int) -> str:
        """Format a per-message acceptance reply with the hex queue ID."""
        return f"250 2.0.0 Ok: queued as {queue_id:012X}"


# --------------------------------------------------------------------------
# SMTP session state
# --------------------------------------------------------------------------


class _State(Enum):
    """
    Lifecycle states of a single SMTP session.

    The state machine enforces the RFC 5321 §4.1.1 command sequence:
    `EHLO`/`HELO` -> `MAIL FROM` -> `RCPT TO` -> `DATA`. Commands issued
    out of sequence receive `503 5.5.1 Bad sequence of commands`.

    `RCPT TO` is accepted without a state transition. All mail is routed
    to the configured Telegram `chat_id` regardless of the recipient, so
    tracking per-recipient state adds no value.
    """

    CONNECTED = auto()  # TCP-equivalent: banner sent, awaiting EHLO/HELO
    GREETED = auto()  # EHLO/HELO received
    MAIL_FROM = auto()  # MAIL FROM received
    DATA = auto()  # DATA command received, collecting message lines
    QUIT = auto()  # QUIT received or timeout; session terminated


@dataclass
class _SessionState:
    """
    Mutable SMTP session context.

    This dataclass is intentionally *not* frozen: the state machine mutates
    it in place as commands arrive. It is private to `SMTPServer.run()`
    and never escapes that scope.
    """

    state: _State = _State.CONNECTED
    envelope_sender: str | None = None
    message_buffer: list[str] = field(default_factory=list)
    buffer_size: int = 0
    oversized: bool = False

    def reset_buffer(self) -> None:
        """Discard all buffered DATA lines and clear size-tracking state."""
        self.message_buffer.clear()
        self.buffer_size = 0
        self.oversized = False


# --------------------------------------------------------------------------
# SMTP server
# --------------------------------------------------------------------------


class SMTPServer:
    """
    Runs a minimal SMTP dialogue on `stdin`/`stdout`.

    Supported commands: `EHLO`, `HELO`, `MAIL FROM`, `RCPT TO`,
    `DATA`, `RSET`, `NOOP`, `QUIT`. All other commands return `250 Ok`
    for maximum compatibility with system daemons that probe for
    capabilities before sending mail.

    The `on_message` callable is invoked once per successfully received
    message (after the `DATA` terminator `.` is seen). It receives
    the raw RFC 2822 email string and the envelope sender address (which
    may be `None` if `MAIL FROM` contained an empty reverse-path).

    If `on_message` raises any exception, a `554 Transaction failed`
    response is returned to the client and the session resets — the SMTP
    dialogue continues so the client can attempt another message.

    Usage::

        server = SMTPServer(config, on_message=deliver)
        server.run()

    Args:
        config:     Resolved `AppConfig` (only `smtp_timeout` is used).
        on_message: Callable with signature
                    `(raw_email: str, envelope_sender: str | None) -> None`.
    """

    def __init__(
        self,
        config: AppConfig,
        on_message: Callable[[str, str | None], None],
    ) -> None:
        self._timeout: int = config.smtp_timeout
        self._on_message = on_message

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Start the SMTP dialogue. Blocks until the session ends (QUIT,
        EOF on stdin, or timeout).

        Raises:
            SMTPProtocolError: If an unrecoverable I/O error occurs on
                               `stdout` that prevents sending any further
                               responses.
        """
        line_queue: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(
            target=self._stdin_reader,
            args=(line_queue,),
            daemon=True,
        )
        reader.start()

        try:
            sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
            # reconfigure is a TextIOWrapper-only attribute guaranteed to
            # exist on standard sys.stdout in Python 3.10+.
        except AttributeError:
            pass  # reconfigure not available in all environments (e.g. tests)

        session = _SessionState()
        self._write(_SMTP.BANNER)

        try:
            self._event_loop(session, line_queue)
        except SMTPProtocolError:
            raise
        except Exception as exc:
            logger.error("SMTP session crashed unexpectedly: %s", exc)
            self._write(_SMTP.INTERNAL_ERROR)

    # ------------------------------------------------------------------
    # Event loop
    # ------------------------------------------------------------------

    def _event_loop(
        self,
        session: _SessionState,
        line_queue: queue.Queue[str | None],
    ) -> None:
        """Drive the SMTP state machine until the session ends."""
        while True:
            try:
                line = line_queue.get(timeout=self._timeout)
            except queue.Empty:
                logger.warning("SMTP session timed out after %ds", self._timeout)
                self._write(_SMTP.TIMEOUT)
                return

            if line is None:
                logger.info("SMTP stdin closed (EOF)")
                return

            line = line.rstrip("\r\n")

            if session.state is _State.DATA:
                self._handle_data_line(line, session)
            else:
                done = self._handle_command(line, session)
                if done:
                    return

    # ------------------------------------------------------------------
    # DATA mode
    # ------------------------------------------------------------------

    def _handle_data_line(self, line: str, session: _SessionState) -> None:
        """Process one line received while in DATA collection mode."""
        if line == ".":
            if session.oversized:
                self._write(_SMTP.MSG_TOO_BIG)
                logger.warning(
                    "Rejected oversized message from %s",
                    session.envelope_sender or "<>",
                )
                session.state = _State.GREETED
                session.envelope_sender = None
                session.reset_buffer()
            else:
                self._finalise_message(session)
        elif not session.oversized:
            # RFC 5321 §4.5.2 dot-stuffing: a leading '..' is an escaped '.'
            cleaned = line[1:] if line.startswith("..") else line
            # +1 accounts for the '\n' separator used when joining the buffer
            increment = len(cleaned) + (1 if session.message_buffer else 0)
            if session.buffer_size + increment > _MAX_MESSAGE_SIZE:
                session.reset_buffer()
                session.oversized = True
            else:
                session.message_buffer.append(cleaned)
                session.buffer_size += increment

    def _finalise_message(self, session: _SessionState) -> None:
        """Invoke `on_message` and reset session state after `DATA` ends."""
        raw_email = "\n".join(session.message_buffer)
        envelope_sender = session.envelope_sender

        try:
            self._on_message(raw_email, envelope_sender)
            queue_id = int.from_bytes(os.urandom(6), byteorder="big")
            self._write(_SMTP.queued_as(queue_id))
            logger.info(
                "Message accepted and forwarded (envelope_sender=%s)",
                envelope_sender or "<>",
            )
        except Exception as exc:
            logger.error("Message delivery failed: %s", exc)
            self._write(_SMTP.TRANSACTION_FAIL)

        session.state = _State.GREETED
        session.envelope_sender = None
        session.reset_buffer()

    # ------------------------------------------------------------------
    # Command mode
    # ------------------------------------------------------------------

    def _handle_command(  # noqa: PLR0912 # single dispatcher; splitting state fragments
        self,
        line: str,
        session: _SessionState,
    ) -> bool:
        """
        Dispatch an SMTP command line.

        Returns:
            `True` if the session should terminate (QUIT received).
        """
        upper = line.upper().strip()
        if not upper:
            return False

        if upper.startswith(("EHLO", "HELO")):
            self._cmd_ehlo(session)

        elif upper.startswith("MAIL FROM"):
            if session.state is not _State.GREETED:
                self._write(_SMTP.BAD_SEQUENCE)
            else:
                self._cmd_mail_from(line, session)

        elif upper.startswith("RCPT TO"):
            if session.state is not _State.MAIL_FROM:
                self._write(_SMTP.BAD_SEQUENCE)
            else:
                # All recipients are silently accepted; delivery always goes
                # to the Telegram chat_id defined in config.
                self._write(_SMTP.RCPT_OK)

        elif upper.startswith("DATA"):
            if session.state is not _State.MAIL_FROM:
                self._write(_SMTP.BAD_SEQUENCE)
            else:
                session.state = _State.DATA
                session.reset_buffer()
                self._write(_SMTP.DATA_START)

        elif upper.startswith("RSET"):
            session.state = _State.GREETED
            session.envelope_sender = None
            session.reset_buffer()
            self._write(_SMTP.OK)

        elif upper.startswith("NOOP"):
            self._write(_SMTP.OK)

        elif upper.startswith("QUIT"):
            self._write(_SMTP.BYE)
            logger.info("SMTP session closed by client")
            return True

        else:
            # Return 250 for unrecognised commands rather than 502 to avoid
            # breaking daemons that probe for extensions before sending mail.
            logger.debug("Unrecognised SMTP command (ignored): %r", line)
            self._write(_SMTP.OK)

        return False

    def _cmd_ehlo(self, session: _SessionState) -> None:
        session.state = _State.GREETED
        self._write(_SMTP.EHLO_GREETING)
        self._write(_SMTP.EHLO_SIZE)
        self._write(_SMTP.EHLO_ENHANCED)
        self._write(_SMTP.EHLO_HELP)

    def _cmd_mail_from(self, line: str, session: _SessionState) -> None:
        session.state = _State.MAIL_FROM
        session.envelope_sender = self._parse_address(line)
        self._write(_SMTP.MAIL_OK)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_address(line: str) -> str | None:
        """
        Extract the address from a `MAIL FROM:<addr>` or `RCPT TO:<addr>`
        line. Returns `None` for a null reverse-path (`<>`).
        """
        start = line.find("<")
        end = line.find(">", start)
        if start != -1 and end != -1:
            addr = line[start + 1 : end].strip()
            return addr if addr else None
        parts = line.split(":", 1)
        if len(parts) == 2:
            return parts[1].strip() or None
        return None

    @staticmethod
    def _stdin_reader(q: queue.Queue[str | None]) -> None:
        """
        Read lines from `stdin` into `q` in a background thread.

        Pushes `None` as a sentinel when EOF is reached or if the read
        raises an unexpected exception.
        """
        try:
            for line in iter(sys.stdin.readline, ""):
                q.put(line)
        except Exception as exc:
            logger.warning("SMTP stdin reader encountered an error: %s", exc)
        finally:
            q.put(None)

    @staticmethod
    def _write(message: str) -> None:
        """
        Write a single SMTP response line to `stdout`.

        Raises:
            SMTPProtocolError: If the write fails (broken pipe, closed fd).
        """
        try:
            sys.stdout.write(f"{message}\r\n")
            sys.stdout.flush()
        except OSError as exc:
            raise SMTPProtocolError(
                f"Failed to write SMTP response to stdout: {exc}"
            ) from exc
