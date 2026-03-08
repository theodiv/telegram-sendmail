"""
Exception hierarchy for telegram-sendmail.

All package-specific exceptions inherit from `TelegramSendmailError`, which
carries the originating message so that call sites can log it uniformly
without re-formatting. Concrete subclasses express the *domain* of the
failure; catching them individually enables targeted recovery strategies
in the CLI and SMTP state machine.
"""


class TelegramSendmailError(Exception):
    """
    Root exception for all telegram-sendmail failures.

    Subclasses must not suppress or re-format `message`; it is the single
    authoritative description of the failure and is written verbatim to
    syslog by the top-level error handler in `__main__`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class ConfigurationError(TelegramSendmailError):
    """
    Raised when the configuration file is absent, unreadable, structurally
    invalid, or missing a required key (`token`, `chat_id`).

    This error is fatal: the application cannot proceed without a valid,
    complete configuration.
    """


class TelegramAPIError(TelegramSendmailError):
    """
    Raised when the Telegram Bot API returns a non-2xx response or when
    the outbound HTTP request fails after all retry attempts are exhausted.

    Carries an optional `status_code` for structured inspection by
    callers that want to distinguish rate-limit responses (429) from
    server errors (5xx).
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SMTPProtocolError(TelegramSendmailError):
    """
    Raised when the SMTP state machine encounters an unrecoverable protocol
    violation — for example, a `DATA` command issued before `MAIL FROM`,
    or a malformed command stream that cannot be safely continued.

    Transient issues (timeouts, empty lines) are handled internally by
    the state machine and do not raise this exception.
    """


class ParsingError(TelegramSendmailError):
    """
    Raised when the email MIME structure cannot be decoded or when the
    HTML-to-Telegram conversion produces output that is unsafe to send.

    A `ParsingError` does not prevent the raw email from being written
    to the spool file; spooling happens before parsing in the processing
    pipeline so the original message is always preserved.
    """


class SpoolError(TelegramSendmailError):
    """
    Raised when the mail spooler cannot write to either the configured
    spool directory or the `/tmp/.telegram-sendmail-spool` fallback.

    This is intentionally non-fatal for the Telegram delivery path: a
    `SpoolError` is logged at `WARNING` level and the message is
    still forwarded to Telegram. It becomes fatal only if both the
    primary path *and* the fallback are unwritable, at which point the
    operator has a filesystem problem that must be resolved manually.
    """
