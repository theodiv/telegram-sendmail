"""
Mail spooler for telegram-sendmail.

Public surface
--------------
- `MailSpooler` — archives raw email content to a per-user spool file.

The spooler is intentionally decoupled from the Telegram delivery path: it
runs first, so the original message is always preserved even if parsing or
network delivery subsequently fails. A `SpoolError` is non-fatal from
the caller's perspective; `__main__` logs it at WARNING level and
continues with Telegram delivery.
"""

import logging
import os
import stat
import time
from pathlib import Path

from telegram_sendmail.config import AppConfig
from telegram_sendmail.exceptions import SpoolError

logger = logging.getLogger(__name__)

_SEPARATOR = "-" * 60


class MailSpooler:
    """
    Archives raw email content to the per-user spool file defined in
    `config.spool_path`.

    Each email is written as an append to the spool file, preceded by a
    timestamped separator line that mirrors the traditional mbox `From_`
    envelope format well enough for human inspection without requiring a
    full mbox parser.

    The spool file is opened with `os.open()` using `O_NOFOLLOW` so that a
    local attacker cannot redirect writes to an arbitrary file by
    pre-placing a symlink at the spool path before the daemon's first run.
    File permissions are enforced to `0600` on every write using
    `os.fchmod` against the open file descriptor rather than `os.chmod`
    against the path, which would be vulnerable to a TOCTOU race on
    systems where the spool directory is world-writable.

    Usage::

        spooler = MailSpooler(config)
        spooler.write(raw_email_string)
    """

    def __init__(self, config: AppConfig) -> None:
        self._spool_path: Path = config.spool_path

    def write(self, raw_email: str) -> None:
        """
        Append `raw_email` to the spool file.

        The parent directory of the spool file is created on demand if it
        does not exist. When the directory is newly created it receives
        `0700` permissions so that only the owning user can read its
        contents, which is a necessary precaution when the spool path sits
        inside a world-writable directory such as the fallback
        `/tmp/.telegram-sendmail-spool`.

        The file is opened with `O_NOFOLLOW`; if the path is a symlink the
        kernel raises `OSError(ELOOP)`, which is caught and re-raised as
        `SpoolError`.

        Args:
            raw_email: Raw RFC 2822 email string exactly as received from
                       `stdin` or the SMTP DATA stream.

        Raises:
            SpoolError: If the write fails for any reason (permissions,
                        disk full, I/O error, or symlink detected at the
                        spool path). The caller is expected to log this at
                        WARNING level and continue processing.
        """
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
        separator = f"\n{_SEPARATOR}\n{timestamp}\n{_SEPARATOR}\n"

        try:
            # mode=0o700 applies only when the directory is newly created;
            # existing directories are not retroactively chmod'd here because
            # config.py already enforced permissions on the fallback path at
            # startup.
            self._spool_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

            _flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
            raw_fd = os.open(self._spool_path, _flags, 0o600)
            with os.fdopen(raw_fd, "a", encoding="utf-8") as fh:
                # Enforce 0600 on the open fd to override the process umask,
                # which may have been set to a more permissive value by the
                # calling daemon.
                os.fchmod(fh.fileno(), stat.S_IRUSR | stat.S_IWUSR)
                fh.write(separator)
                fh.write(raw_email)
                if not raw_email.endswith("\n"):
                    fh.write("\n")

        except OSError as exc:
            logger.warning(
                "Failed to write email to spool file '%s': %s — "
                "the message will still be forwarded to Telegram",
                self._spool_path,
                exc,
            )
            raise SpoolError(
                f"Could not write to spool file '{self._spool_path}': {exc}"
            ) from exc
