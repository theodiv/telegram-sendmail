"""
Tests for telegram_sendmail.spool.

Coverage targets
----------------
`MailSpooler.write` — directory lifecycle
    - Creates the parent directory when it does not exist
    - Creates the parent directory with exactly 0700 (owner-only) permissions
    - Succeeds without error when the parent directory already exists

`MailSpooler.write` — content format
    - Written file contains the raw email content verbatim
    - A separator block is present in the file before the email content
    - A human-readable timestamp appears inside the separator block
    - Appends a trailing newline when the raw email does not end with one
    - Does not append a double newline when the raw email already ends with one

`MailSpooler.write` — file permissions
    - Enforces exactly 0600 (owner read/write only) permissions on the spool file
    - Re-enforces 0600 on subsequent writes to a file that already exists

`MailSpooler.write` — append behavior
    - A second write appends to the existing file rather than overwriting it
    - Both email bodies are present in the file after two writes
    - Two separator blocks are present after two writes (one per message)

`MailSpooler.write` — error handling
    - Raises SpoolError when an OSError occurs during the file write
    - Emits a WARNING log message before raising SpoolError
    - The WARNING log message references the spool file path
    - SpoolError wraps the underlying OSError as its cause

Design notes
------------
- All tests use `tmp_spool_path` (tmp_path/"spool"/"testuser") via
  `dataclasses.replace(app_config, spool_path=tmp_spool_path)`. The parent
  directory does not pre-exist, allowing the "creates parent on demand" path
  to run naturally without any extra setup.
- File permission enforcement tests (0600, 0700) are skipped on UID 0 because
  root bypasses DAC permission checks; the assertions would always pass and
  provide false confidence.
- The OSError trigger relies on making the parent directory non-writable via
  chmod 0o555, which prevents file creation inside it. The directory permissions
  are restored in a finally block so that pytest can clean up tmp_path.
- `os.fchmod` (not `os.chmod`) is what the production code uses to set 0600 on
  the spool file. This test verifies the *observable outcome* (the permission
  bit) rather than asserting that fchmod was called, ensuring the test
  validates behavior rather than implementation.
"""

from __future__ import annotations

import dataclasses
import os
import stat
from pathlib import Path

import pytest

from telegram_sendmail.config import AppConfig
from telegram_sendmail.exceptions import SpoolError
from telegram_sendmail.spool import MailSpooler

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _make_spooler(app_config: AppConfig, spool_path: Path) -> MailSpooler:
    """Return a MailSpooler wired to the given spool_path."""
    config = dataclasses.replace(app_config, spool_path=spool_path)
    return MailSpooler(config)


# --------------------------------------------------------------------------
# Directory lifecycle
# --------------------------------------------------------------------------


class TestMailSpoolerDirectoryManagement:
    def test_creates_parent_directory_when_it_does_not_exist(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        assert not tmp_spool_path.parent.exists()
        _make_spooler(app_config, tmp_spool_path).write("email body")
        assert tmp_spool_path.parent.is_dir()

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_created_parent_directory_has_0700_permissions(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        _make_spooler(app_config, tmp_spool_path).write("email body")
        mode = stat.S_IMODE(tmp_spool_path.parent.stat().st_mode)
        assert mode == 0o700

    def test_write_succeeds_when_parent_directory_already_exists(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        tmp_spool_path.parent.mkdir(parents=True)
        # Second call must not raise even though the directory exists.
        _make_spooler(app_config, tmp_spool_path).write("first email")
        _make_spooler(app_config, tmp_spool_path).write("second email")

    def test_write_raises_spool_error_when_spool_path_is_a_symlink(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        # Simulate a symlink-attack scenario: an adversary who can predict
        # the spool filename pre-places a symlink at the expected path
        # before the daemon's first run.
        tmp_spool_path.parent.mkdir(parents=True, exist_ok=True)
        target = tmp_spool_path.parent / "attacker_file"
        target.write_text("attacker content")
        tmp_spool_path.symlink_to(target)

        with pytest.raises(SpoolError):
            _make_spooler(app_config, tmp_spool_path).write("email content")

        # O_NOFOLLOW must have blocked the open before any write occurred.
        assert "email content" not in target.read_text()


# --------------------------------------------------------------------------
# Content format
# --------------------------------------------------------------------------


class TestMailSpoolerWriteContent:
    def test_written_file_contains_raw_email_content(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        _make_spooler(app_config, tmp_spool_path).write(
            "Subject: Test\n\nHello from spool"
        )
        content = tmp_spool_path.read_text(encoding="utf-8")
        assert "Hello from spool" in content

    def test_separator_block_is_present_before_email_content(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        _make_spooler(app_config, tmp_spool_path).write("body text")
        content = tmp_spool_path.read_text(encoding="utf-8")
        # The separator is a line of dashes; its position must precede the body.
        sep_pos = content.find("-" * 10)
        body_pos = content.find("body text")
        assert sep_pos != -1
        assert sep_pos < body_pos

    def test_separator_block_contains_a_timestamp(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        _make_spooler(app_config, tmp_spool_path).write("body")
        content = tmp_spool_path.read_text(encoding="utf-8")
        # The timestamp format produced by time.strftime is "YYYY-MM-DD HH:MM:SS".
        import re

        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", content)

    def test_trailing_newline_appended_when_absent(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        # Email without trailing newline must be terminated so adjacent writes
        # do not bleed together in the mbox-style spool file.
        _make_spooler(app_config, tmp_spool_path).write("no newline at end")
        content = tmp_spool_path.read_text(encoding="utf-8")
        assert content.endswith("\n")

    def test_no_extra_newline_when_email_already_ends_with_one(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        _make_spooler(app_config, tmp_spool_path).write("has newline\n")
        content = tmp_spool_path.read_text(encoding="utf-8")
        # Exactly one trailing newline, not two.
        assert not content.endswith("\n\n")

    def test_email_content_with_special_characters_preserved(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        email = "Subject: Disk > 90%\n\n<b>Alert</b> & warning"
        _make_spooler(app_config, tmp_spool_path).write(email)
        content = tmp_spool_path.read_text(encoding="utf-8")
        assert "<b>Alert</b>" in content
        assert "Disk > 90%" in content


# --------------------------------------------------------------------------
# File permissions
# --------------------------------------------------------------------------


class TestMailSpoolerFilePermissions:
    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_spool_file_has_0600_permissions_after_first_write(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        _make_spooler(app_config, tmp_spool_path).write("first write")
        mode = stat.S_IMODE(tmp_spool_path.stat().st_mode)
        # os.fchmod enforces 0600 on the open fd, overriding the process umask.
        assert mode == 0o600

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_spool_file_permissions_re_enforced_on_subsequent_write(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        spooler = _make_spooler(app_config, tmp_spool_path)
        spooler.write("first write")
        # Manually loosen the permissions to simulate a race or external change.
        tmp_spool_path.chmod(0o644)
        spooler.write("second write")
        mode = stat.S_IMODE(tmp_spool_path.stat().st_mode)
        # fchmod on the open fd must restore 0600 regardless of prior state.
        assert mode == 0o600


# --------------------------------------------------------------------------
# Append behavior
# --------------------------------------------------------------------------


class TestMailSpoolerAppendBehavior:
    def test_second_write_does_not_overwrite_first_email(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        spooler = _make_spooler(app_config, tmp_spool_path)
        spooler.write("first email body")
        spooler.write("second email body")
        content = tmp_spool_path.read_text(encoding="utf-8")
        assert "first email body" in content
        assert "second email body" in content

    def test_two_separator_blocks_present_after_two_writes(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        spooler = _make_spooler(app_config, tmp_spool_path)
        spooler.write("email one")
        spooler.write("email two")
        content = tmp_spool_path.read_text(encoding="utf-8")
        # Each write prepends a separator line; count occurrences.
        separator_count = content.count("-" * 60)
        # The separator is written twice per message (above and below timestamp).
        assert separator_count >= 2

    def test_emails_appear_in_write_order(
        self, tmp_spool_path: Path, app_config: AppConfig
    ):
        spooler = _make_spooler(app_config, tmp_spool_path)
        spooler.write("alpha email")
        spooler.write("beta email")
        content = tmp_spool_path.read_text(encoding="utf-8")
        assert content.index("alpha email") < content.index("beta email")


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------


class TestMailSpoolerErrorHandling:
    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_oserror_on_write_raises_spool_error(
        self, tmp_path: Path, app_config: AppConfig
    ):
        # Lock the parent directory so file creation inside it raises PermissionError.
        locked_dir = tmp_path / "locked"
        locked_dir.mkdir()
        locked_dir.chmod(0o555)
        spool_path = locked_dir / "testuser"
        spooler = _make_spooler(app_config, spool_path)
        try:
            with pytest.raises(SpoolError):
                spooler.write("email content")
        finally:
            locked_dir.chmod(0o755)

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_warning_logged_before_spool_error_is_raised(
        self,
        tmp_path: Path,
        app_config: AppConfig,
        caplog: pytest.LogCaptureFixture,
    ):
        import logging

        locked_dir = tmp_path / "locked2"
        locked_dir.mkdir()
        locked_dir.chmod(0o555)
        spool_path = locked_dir / "testuser"
        spooler = _make_spooler(app_config, spool_path)
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.spool"):
            try:
                spooler.write("email content")
            except SpoolError:
                pass
            finally:
                locked_dir.chmod(0o755)
        assert any(r.levelname == "WARNING" for r in caplog.records)

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_warning_message_references_spool_file_path(
        self,
        tmp_path: Path,
        app_config: AppConfig,
        caplog: pytest.LogCaptureFixture,
    ):
        import logging

        locked_dir = tmp_path / "locked3"
        locked_dir.mkdir()
        locked_dir.chmod(0o555)
        spool_path = locked_dir / "testuser"
        spooler = _make_spooler(app_config, spool_path)
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.spool"):
            try:
                spooler.write("email content")
            except SpoolError:
                pass
            finally:
                locked_dir.chmod(0o755)
        # The operator must be able to locate the problematic path from the log.
        assert any(str(spool_path) in r.message for r in caplog.records)

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_spool_error_wraps_underlying_oserror(
        self, tmp_path: Path, app_config: AppConfig
    ):
        locked_dir = tmp_path / "locked4"
        locked_dir.mkdir()
        locked_dir.chmod(0o555)
        spool_path = locked_dir / "testuser"
        spooler = _make_spooler(app_config, spool_path)
        try:
            with pytest.raises(SpoolError) as exc_info:
                spooler.write("email content")
        finally:
            locked_dir.chmod(0o755)
        # SpoolError must chain the original OSError for debuggability.
        assert isinstance(exc_info.value.__cause__, OSError)
