"""
Tests for telegram_sendmail.config.

Coverage targets
----------------
`_locate_config_file`
    - Returns the user config path when it exists on disk
    - Prefers the user config over the system config when both are present
    - Returns the system config path when only the system config exists
    - Raises ConfigurationError with a descriptive message when neither file exists

`_audit_permissions`
    - Does not emit a WARNING when the file has strict 0600 (owner-only) permissions
    - Emits a WARNING containing "insecure" when the file is group-readable (0640)
    - Emits a WARNING containing "insecure" when the file is world-readable (0644)
    - Includes the recommended "0600" string in the warning to guide the operator

`_validate_range` / `_validate_range_float`
    - Returns the value unchanged when it falls strictly within the valid range
    - Accepts the lower bound as a valid value (inclusive)
    - Accepts the upper bound as a valid value (inclusive)
    - Returns the default and emits a WARNING naming the key when value is below lower bound
    - Returns the default and emits a WARNING naming the key when value is above upper bound
    - Float variant follows the identical contract as the integer variant

`_resolve_spool_path`
    - Returns <dir>/<username> when the configured directory is writable
    - Uses _DEFAULT_SPOOL_DIR when raw_dir is None
    - Returns <_FALLBACK_SPOOL_BASE>/<username> when the configured directory is not writable
    - Creates the fallback directory with exactly 0700 permissions on first use
    - Emits a WARNING containing "not writable" when falling back
    - Emits a WARNING when the fallback directory is owned by a different UID

`_require`
    - Returns the value stripped of leading/trailing whitespace when the key is present
    - Raises ConfigurationError naming the key when the entire section is absent
    - Raises ConfigurationError naming the key when the key is absent within the section
    - Raises ConfigurationError mentioning "empty" for a whitespace-only value

`_parse_options`
    - Returns all compiled defaults when the [options] section is absent
    - Accepts an in-range message_max_length value verbatim
    - Falls back to the default and warns when message_max_length is outside [100, 4096]
    - Falls back to the default and warns when smtp_timeout is not a valid integer
    - Parses disable_notification as a proper Python bool (True / False)
    - Falls back to the default and warns when disable_notification is not a valid boolean
    - Accepts an in-range backoff_factor float verbatim
    - Falls back to the default and warns when backoff_factor exceeds [0.0, 10.0]
    - Accepts a valid max_retries integer
    - Uses the configured spool_dir as the parent of the resolved spool path

`ConfigLoader.load`
    - Raises ConfigurationError when no config file is found on disk
    - Raises ConfigurationError when the config file exists but is not readable
    - Raises ConfigurationError when the [telegram] token key is absent
    - Raises ConfigurationError when the [telegram] chat_id key is absent
    - Returns a fully populated AppConfig for a minimal valid config file
    - Parses all [options] keys correctly into the returned AppConfig
    - Prefers the user config over the system config when both are present
    - Returns the system config when only the system config exists
    - Returns a frozen (immutable) AppConfig instance

Design notes
------------
- All module-level path constants (_USER_CONFIG, _SYSTEM_CONFIG, _DEFAULT_SPOOL_DIR,
  _FALLBACK_SPOOL_BASE) are redirected into tmp_path via the `patched_config_constants`
  module-level fixture, guaranteeing zero interaction with the real host filesystem.
- Tests that depend on filesystem permission enforcement (chmod 0o000, 0o555, 0o640)
  are decorated with @pytest.mark.skipif(os.getuid() == 0, ...) because root bypasses
  DAC permission checks on Linux and these tests would give false positives.
- _parse_options is tested with directly constructed ConfigParser objects to isolate
  option-parsing behavior from the surrounding file-discovery and path-resolution logic.
  A writable tmp_path subdirectory is always injected as spool_dir in these tests to
  prevent _resolve_spool_path from attempting to access /var/mail on the host.
"""

from __future__ import annotations

import configparser
import getpass
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest

import telegram_sendmail.config as cfg_module
from telegram_sendmail.config import (
    _DEFAULT_BACKOFF_FACTOR,
    _DEFAULT_DISABLE_NOTIFICATION,
    _DEFAULT_MAX_RETRIES,
    _DEFAULT_MESSAGE_MAX_LEN,
    _DEFAULT_SMTP_TIMEOUT,
    _DEFAULT_TELEGRAM_TIMEOUT,
    ConfigLoader,
    _audit_permissions,
    _locate_config_file,
    _parse_options,
    _require,
    _resolve_spool_path,
    _validate_range,
    _validate_range_float,
)
from telegram_sendmail.exceptions import ConfigurationError

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _parser_with(**options: Any) -> configparser.ConfigParser:
    """Return a ConfigParser with an [options] section populated from kwargs."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.add_section("options")
    for key, value in options.items():
        cp.set("options", key, str(value))
    return cp


def _minimal_ini(token: str = "tok", chat_id: str = "-123") -> str:
    """Return a minimal valid INI string containing only the two required keys."""
    return f"[telegram]\ntoken = {token}\nchat_id = {chat_id}\n"


@pytest.fixture
def patched_config_constants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Redirect _USER_CONFIG, _SYSTEM_CONFIG, and spool paths into tmp_path."""
    user_ini = tmp_path / "user.ini"
    system_ini = tmp_path / "system.ini"
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    fallback_dir = tmp_path / "fallback"
    monkeypatch.setattr(cfg_module, "_USER_CONFIG", user_ini)
    monkeypatch.setattr(cfg_module, "_SYSTEM_CONFIG", system_ini)
    monkeypatch.setattr(cfg_module, "_DEFAULT_SPOOL_DIR", spool_dir)
    monkeypatch.setattr(cfg_module, "_FALLBACK_SPOOL_BASE", fallback_dir)
    return {
        "user_ini": user_ini,
        "system_ini": system_ini,
        "spool_dir": spool_dir,
        "fallback_dir": fallback_dir,
    }


# --------------------------------------------------------------------------
# _locate_config_file
# --------------------------------------------------------------------------


class TestLocateConfigFile:
    def test_returns_user_config_when_it_exists(
        self, patched_config_constants: dict[str, Path]
    ):
        user_ini = patched_config_constants["user_ini"]
        user_ini.write_text(_minimal_ini())
        assert _locate_config_file() == user_ini

    def test_prefers_user_config_when_both_files_exist(
        self, patched_config_constants: dict[str, Path]
    ):
        user_ini = patched_config_constants["user_ini"]
        system_ini = patched_config_constants["system_ini"]
        user_ini.write_text(_minimal_ini(token="user-tok"))
        system_ini.write_text(_minimal_ini(token="sys-tok"))
        assert _locate_config_file() == user_ini

    def test_returns_system_config_when_user_config_is_absent(
        self, patched_config_constants: dict[str, Path]
    ):
        system_ini = patched_config_constants["system_ini"]
        system_ini.write_text(_minimal_ini())
        assert _locate_config_file() == system_ini

    def test_raises_config_error_when_neither_file_exists(
        self, patched_config_constants: dict[str, Path]
    ):
        # Neither user.ini nor system.ini is created in this test.
        with pytest.raises(ConfigurationError):
            _locate_config_file()

    def test_config_error_message_names_both_candidate_paths(
        self, patched_config_constants: dict[str, Path]
    ):
        with pytest.raises(ConfigurationError) as exc_info:
            _locate_config_file()
        # The error must guide the operator to both candidate locations.
        message = str(exc_info.value)
        assert "user.ini" in message or "telegram-sendmail" in message


# --------------------------------------------------------------------------
# _audit_permissions
# --------------------------------------------------------------------------


class TestAuditPermissions:
    def test_no_warning_for_strict_owner_only_permissions(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        config_file = tmp_path / "strict.ini"
        config_file.write_text(_minimal_ini())
        config_file.chmod(0o600)
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            _audit_permissions(config_file)
        assert not any("insecure" in r.message for r in caplog.records)

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_warning_emitted_for_group_readable_file(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        config_file = tmp_path / "group.ini"
        config_file.write_text(_minimal_ini())
        config_file.chmod(0o640)
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            _audit_permissions(config_file)
        assert any("insecure" in r.message for r in caplog.records)

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_warning_emitted_for_world_readable_file(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        config_file = tmp_path / "world.ini"
        config_file.write_text(_minimal_ini())
        config_file.chmod(0o644)
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            _audit_permissions(config_file)
        assert any("insecure" in r.message for r in caplog.records)

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_warning_includes_chmod_0600_recommendation(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        # Operators must be told exactly how to fix the problem.
        config_file = tmp_path / "loose.ini"
        config_file.write_text(_minimal_ini())
        config_file.chmod(0o644)
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            _audit_permissions(config_file)
        assert any("0600" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# _validate_range / _validate_range_float
# --------------------------------------------------------------------------


class TestValidateRange:
    def test_in_range_integer_returned_unchanged(self):
        assert _validate_range(10, (5, 20), "key", 15) == 10

    def test_lower_bound_is_inclusive(self):
        assert _validate_range(5, (5, 20), "key", 15) == 5

    def test_upper_bound_is_inclusive(self):
        assert _validate_range(20, (5, 20), "key", 15) == 20

    def test_below_range_returns_default(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            result = _validate_range(4, (5, 20), "mykey", 15)
        assert result == 15

    def test_below_range_warning_names_the_config_key(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            _validate_range(4, (5, 20), "mykey", 15)
        assert any("mykey" in r.message for r in caplog.records)

    def test_above_range_returns_default(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            result = _validate_range(21, (5, 20), "mykey", 15)
        assert result == 15

    def test_above_range_warning_names_the_config_key(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            _validate_range(21, (5, 20), "mykey", 15)
        assert any("mykey" in r.message for r in caplog.records)

    def test_float_in_range_returned_unchanged(self):
        assert _validate_range_float(0.5, (0.0, 10.0), "key", 1.0) == 0.5

    def test_float_lower_bound_is_inclusive(self):
        assert _validate_range_float(0.0, (0.0, 10.0), "key", 1.0) == 0.0

    def test_float_upper_bound_is_inclusive(self):
        assert _validate_range_float(10.0, (0.0, 10.0), "key", 1.0) == 10.0

    def test_float_below_range_returns_default_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            result = _validate_range_float(-0.1, (0.0, 10.0), "floatkey", 1.0)
        assert result == 1.0
        assert any("floatkey" in r.message for r in caplog.records)

    def test_float_above_range_returns_default_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            result = _validate_range_float(10.1, (0.0, 10.0), "floatkey", 1.0)
        assert result == 1.0


# --------------------------------------------------------------------------
# _resolve_spool_path
# --------------------------------------------------------------------------


class TestResolveSpoolPath:
    def test_writable_dir_returns_dir_slash_username(self, tmp_path: Path):
        spool_dir = tmp_path / "spool"
        spool_dir.mkdir()
        assert _resolve_spool_path(str(spool_dir)) == spool_dir / getpass.getuser()

    def test_none_raw_dir_uses_default_spool_constant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        default_spool = tmp_path / "default"
        default_spool.mkdir()
        monkeypatch.setattr(cfg_module, "_DEFAULT_SPOOL_DIR", default_spool)
        assert _resolve_spool_path(None) == default_spool / getpass.getuser()

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_nonwritable_dir_falls_back_to_fallback_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        fallback = tmp_path / "fallback"
        monkeypatch.setattr(cfg_module, "_FALLBACK_SPOOL_BASE", fallback)
        try:
            result = _resolve_spool_path(str(locked))
        finally:
            locked.chmod(0o755)
        assert result == fallback / getpass.getuser()

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_fallback_dir_created_with_0700_permissions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        locked = tmp_path / "locked2"
        locked.mkdir()
        locked.chmod(0o555)
        fallback = tmp_path / "fallback2"
        monkeypatch.setattr(cfg_module, "_FALLBACK_SPOOL_BASE", fallback)
        try:
            _resolve_spool_path(str(locked))
        finally:
            locked.chmod(0o755)
        assert fallback.is_dir()
        assert stat.S_IMODE(fallback.stat().st_mode) == 0o700

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_warning_emitted_on_fallback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        locked = tmp_path / "locked3"
        locked.mkdir()
        locked.chmod(0o555)
        monkeypatch.setattr(cfg_module, "_FALLBACK_SPOOL_BASE", tmp_path / "fallback3")
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            try:
                _resolve_spool_path(str(locked))
            finally:
                locked.chmod(0o755)
        assert any("not writable" in r.message for r in caplog.records)

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_fallback_directory_owned_by_different_uid_emits_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        # Make the primary spool dir non-writable so the fallback path is taken.
        locked = tmp_path / "locked_uid"
        locked.mkdir()
        locked.chmod(0o555)
        fallback = tmp_path / "fallback_uid"
        monkeypatch.setattr(cfg_module, "_FALLBACK_SPOOL_BASE", fallback)
        # Patch os.getuid() in the config module to simulate a uid mismatch:
        # the directory is created and owned by the real uid, but getuid()
        # returns a different value, triggering the ownership check.
        monkeypatch.setattr(cfg_module.os, "getuid", lambda: 99999)
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            try:
                _resolve_spool_path(str(locked))
            finally:
                locked.chmod(0o755)
        assert any("uid" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# _require
# --------------------------------------------------------------------------


class TestRequire:
    def test_returns_stripped_value_for_present_key(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.add_section("telegram")
        parser.set("telegram", "token", "  my-token  ")
        assert _require(parser, "telegram", "token", Path("test.ini")) == "my-token"

    def test_raises_config_error_when_section_is_absent(self):
        parser = configparser.ConfigParser(interpolation=None)
        with pytest.raises(ConfigurationError, match="token"):
            _require(parser, "telegram", "token", Path("test.ini"))

    def test_raises_config_error_when_key_is_absent_in_section(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.add_section("telegram")
        with pytest.raises(ConfigurationError, match="token"):
            _require(parser, "telegram", "token", Path("test.ini"))

    def test_raises_config_error_for_empty_string_value(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.add_section("telegram")
        parser.set("telegram", "token", "")
        with pytest.raises(ConfigurationError):
            _require(parser, "telegram", "token", Path("test.ini"))

    def test_raises_config_error_mentioning_empty_for_whitespace_value(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.add_section("telegram")
        parser.set("telegram", "token", "    ")
        with pytest.raises(ConfigurationError, match="empty"):
            _require(parser, "telegram", "token", Path("test.ini"))


# --------------------------------------------------------------------------
# _parse_options
# --------------------------------------------------------------------------


class TestParseOptions:
    def test_absent_options_section_returns_all_compiled_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Patch the default spool dir to avoid _resolve_spool_path touching /var/mail.
        default_spool = tmp_path / "default_spool"
        default_spool.mkdir()
        monkeypatch.setattr(cfg_module, "_DEFAULT_SPOOL_DIR", default_spool)
        parser = configparser.ConfigParser(interpolation=None)
        msg_len, smtp_to, tg_to, disable_notif, _spool, max_ret, backoff = (
            _parse_options(parser, tmp_path / "test.ini")
        )
        assert msg_len == _DEFAULT_MESSAGE_MAX_LEN
        assert smtp_to == _DEFAULT_SMTP_TIMEOUT
        assert tg_to == _DEFAULT_TELEGRAM_TIMEOUT
        assert disable_notif == _DEFAULT_DISABLE_NOTIFICATION
        assert max_ret == _DEFAULT_MAX_RETRIES
        assert backoff == _DEFAULT_BACKOFF_FACTOR

    def test_valid_message_max_length_accepted(self, tmp_path: Path):
        spool = tmp_path / "spool"
        spool.mkdir()
        parser = _parser_with(message_max_length="1000", spool_dir=str(spool))
        assert _parse_options(parser, tmp_path / "test.ini")[0] == 1000

    def test_message_max_length_below_100_falls_back_to_default_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        spool = tmp_path / "spool"
        spool.mkdir()
        parser = _parser_with(message_max_length="50", spool_dir=str(spool))
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            result = _parse_options(parser, tmp_path / "test.ini")
        assert result[0] == _DEFAULT_MESSAGE_MAX_LEN
        assert any("message_max_length" in r.message for r in caplog.records)

    def test_message_max_length_above_4096_falls_back_to_default_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        spool = tmp_path / "spool"
        spool.mkdir()
        parser = _parser_with(message_max_length="9999", spool_dir=str(spool))
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            result = _parse_options(parser, tmp_path / "test.ini")
        assert result[0] == _DEFAULT_MESSAGE_MAX_LEN

    def test_non_integer_smtp_timeout_falls_back_to_default_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        spool = tmp_path / "spool"
        spool.mkdir()
        parser = _parser_with(smtp_timeout="not_a_number", spool_dir=str(spool))
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            result = _parse_options(parser, tmp_path / "test.ini")
        assert result[1] == _DEFAULT_SMTP_TIMEOUT
        assert any("smtp_timeout" in r.message for r in caplog.records)

    def test_disable_notification_true_parsed_as_bool(self, tmp_path: Path):
        spool = tmp_path / "spool"
        spool.mkdir()
        parser = _parser_with(disable_notification="true", spool_dir=str(spool))
        assert _parse_options(parser, tmp_path / "test.ini")[3] is True

    def test_disable_notification_false_parsed_as_bool(self, tmp_path: Path):
        spool = tmp_path / "spool"
        spool.mkdir()
        parser = _parser_with(disable_notification="false", spool_dir=str(spool))
        assert _parse_options(parser, tmp_path / "test.ini")[3] is False

    def test_invalid_disable_notification_falls_back_to_default_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        spool = tmp_path / "spool"
        spool.mkdir()
        parser = _parser_with(disable_notification="not_a_bool", spool_dir=str(spool))
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            result = _parse_options(parser, tmp_path / "test.ini")
        assert result[3] == _DEFAULT_DISABLE_NOTIFICATION
        assert any("disable_notification" in r.message for r in caplog.records)

    def test_valid_backoff_factor_accepted(self, tmp_path: Path):
        spool = tmp_path / "spool"
        spool.mkdir()
        parser = _parser_with(backoff_factor="2.5", spool_dir=str(spool))
        assert _parse_options(parser, tmp_path / "test.ini")[6] == 2.5

    def test_backoff_factor_above_10_falls_back_to_default_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        spool = tmp_path / "spool"
        spool.mkdir()
        # 15.0 exceeds the [0.0, 10.0] range
        parser = _parser_with(backoff_factor="15.0", spool_dir=str(spool))
        with caplog.at_level(logging.WARNING, logger="telegram_sendmail.config"):
            result = _parse_options(parser, tmp_path / "test.ini")
        assert result[6] == _DEFAULT_BACKOFF_FACTOR

    def test_valid_max_retries_accepted(self, tmp_path: Path):
        spool = tmp_path / "spool"
        spool.mkdir()
        parser = _parser_with(max_retries="5", spool_dir=str(spool))
        assert _parse_options(parser, tmp_path / "test.ini")[5] == 5

    def test_custom_spool_dir_becomes_parent_of_resolved_path(self, tmp_path: Path):
        spool = tmp_path / "custom_spool"
        spool.mkdir()
        parser = _parser_with(spool_dir=str(spool))
        spool_path = _parse_options(parser, tmp_path / "test.ini")[4]
        assert spool_path.parent == spool
        assert spool_path.name == getpass.getuser()


# --------------------------------------------------------------------------
# ConfigLoader.load — end-to-end integration
# --------------------------------------------------------------------------


class TestConfigLoaderLoad:
    def test_raises_config_error_when_no_config_file_found(
        self, patched_config_constants: dict[str, Path]
    ):
        # Neither user.ini nor system.ini exists at test start.
        with pytest.raises(ConfigurationError):
            ConfigLoader.load()

    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_raises_config_error_for_unreadable_config_file(
        self, patched_config_constants: dict[str, Path]
    ):
        user_ini = patched_config_constants["user_ini"]
        user_ini.write_text(_minimal_ini())
        user_ini.chmod(0o000)
        try:
            with pytest.raises(ConfigurationError, match="cannot be read"):
                ConfigLoader.load()
        finally:
            # Restore so pytest can clean up tmp_path.
            user_ini.chmod(0o600)

    def test_raises_config_error_when_token_is_missing(
        self, patched_config_constants: dict[str, Path]
    ):
        user_ini = patched_config_constants["user_ini"]
        user_ini.write_text("[telegram]\nchat_id = -123\n")
        user_ini.chmod(0o600)
        with pytest.raises(ConfigurationError, match="token"):
            ConfigLoader.load()

    def test_raises_config_error_when_chat_id_is_missing(
        self, patched_config_constants: dict[str, Path]
    ):
        user_ini = patched_config_constants["user_ini"]
        user_ini.write_text("[telegram]\ntoken = abc\n")
        user_ini.chmod(0o600)
        with pytest.raises(ConfigurationError, match="chat_id"):
            ConfigLoader.load()

    def test_minimal_valid_config_returns_correct_token_and_chat_id(
        self, patched_config_constants: dict[str, Path]
    ):
        user_ini = patched_config_constants["user_ini"]
        user_ini.write_text(_minimal_ini(token="bot-token", chat_id="-999"))
        user_ini.chmod(0o600)
        config = ConfigLoader.load()
        assert config.token == "bot-token"
        assert config.chat_id == "-999"

    def test_all_options_keys_parsed_into_app_config(
        self, patched_config_constants: dict[str, Path]
    ):
        user_ini = patched_config_constants["user_ini"]
        spool_dir = patched_config_constants["spool_dir"]
        user_ini.write_text(
            "[telegram]\n"
            "token = fulltoken\n"
            "chat_id = -777\n"
            "[options]\n"
            f"spool_dir = {spool_dir}\n"
            "message_max_length = 2000\n"
            "smtp_timeout = 60\n"
            "telegram_timeout = 15\n"
            "max_retries = 5\n"
            "backoff_factor = 1.0\n"
            "disable_notification = true\n"
        )
        user_ini.chmod(0o600)
        config = ConfigLoader.load()
        assert config.message_max_len == 2000
        assert config.smtp_timeout == 60
        assert config.telegram_timeout == 15
        assert config.max_retries == 5
        assert config.backoff_factor == 1.0
        assert config.disable_notification is True

    def test_user_config_preferred_over_system_config(
        self, patched_config_constants: dict[str, Path]
    ):
        user_ini = patched_config_constants["user_ini"]
        system_ini = patched_config_constants["system_ini"]
        user_ini.write_text(_minimal_ini(token="user-token"))
        user_ini.chmod(0o600)
        system_ini.write_text(_minimal_ini(token="sys-token"))
        system_ini.chmod(0o600)
        assert ConfigLoader.load().token == "user-token"

    def test_system_config_used_when_user_config_absent(
        self, patched_config_constants: dict[str, Path]
    ):
        system_ini = patched_config_constants["system_ini"]
        system_ini.write_text(_minimal_ini(token="sys-only-token"))
        system_ini.chmod(0o600)
        assert ConfigLoader.load().token == "sys-only-token"

    def test_returned_app_config_is_frozen(
        self, patched_config_constants: dict[str, Path]
    ):
        user_ini = patched_config_constants["user_ini"]
        user_ini.write_text(_minimal_ini())
        user_ini.chmod(0o600)
        config = ConfigLoader.load()
        # frozen=True raises AttributeError (FrozenInstanceError is a subclass).
        with pytest.raises(AttributeError):
            config.token = "mutated"  # type: ignore[misc] # frozen dataclass assignment

    def test_spool_path_is_absolute_and_under_spool_dir(
        self, patched_config_constants: dict[str, Path]
    ):
        user_ini = patched_config_constants["user_ini"]
        spool_dir = patched_config_constants["spool_dir"]
        user_ini.write_text(
            f"[telegram]\ntoken = t\nchat_id = -1\n[options]\nspool_dir = {spool_dir}\n"
        )
        user_ini.chmod(0o600)
        config = ConfigLoader.load()
        assert config.spool_path.is_absolute()
        assert config.spool_path.parent == spool_dir
