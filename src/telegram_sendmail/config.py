"""
Configuration loading and validation for telegram-sendmail.

Public surface
--------------
- `AppConfig`    — frozen dataclass holding all resolved configuration.
- `ConfigLoader` — validates and parses the config file from an explicit
                   path or the default locations.

All other symbols in this module are private implementation details.
`config.py` is the only module in the package that imports `configparser`.
Every other module receives an `AppConfig` instance and has no knowledge of
how or where configuration is stored.
"""

import configparser
import errno
import getpass
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from telegram_sendmail.exceptions import ConfigurationError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------


_SYSTEM_CONFIG: Path = Path("/etc/telegram-sendmail.ini")
_USER_CONFIG: Path = Path.home() / ".telegram-sendmail.ini"
_DEFAULT_SPOOL_DIR: Path = Path("/var/mail")
_FALLBACK_SPOOL_BASE: Path = Path("/tmp/.telegram-sendmail-spool")

_DEFAULT_MESSAGE_MAX_LEN: int = 3800
_DEFAULT_SMTP_TIMEOUT: int = 30
_DEFAULT_TELEGRAM_TIMEOUT: int = 10
_DEFAULT_DISABLE_NOTIFICATION: bool = False
_DEFAULT_SPOOL_ENABLED: bool = True

_MESSAGE_MAX_LEN_BOUNDS: tuple[int, int] = (100, 4096)
_SMTP_TIMEOUT_BOUNDS: tuple[int, int] = (5, 300)
_TELEGRAM_TIMEOUT_BOUNDS: tuple[int, int] = (5, 60)

_DEFAULT_MAX_RETRIES: int = 3
_DEFAULT_BACKOFF_FACTOR: float = 0.5
_MAX_RETRIES_BOUNDS: tuple[int, int] = (0, 10)
_BACKOFF_FACTOR_BOUNDS: tuple[float, float] = (0.0, 10.0)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AppConfig:
    """
    Immutable snapshot of all resolved configuration values.

    Instances are constructed exclusively by `ConfigLoader.load` and are
    safe to pass freely between modules without defensive copying. All path
    fields are fully resolved `Path` objects; callers never manipulate raw
    strings from the config file.

    Attributes:
        token:                Telegram Bot API token.
        chat_id:              Destination chat ID (user, group, or channel).
        message_max_len:      Maximum body character count before truncation.
        smtp_timeout:         Seconds to wait for an SMTP client command.
        telegram_timeout:     Seconds to wait for a Telegram API response.
        disable_notification: Whether to suppress Telegram push notifications.
        spool_path:           Fully resolved path to the per-user spool file,
                              guaranteed to be writable at construction time,
                              or `None` when `spool_enabled = false` disables
                              mail spooling.
        max_retries:          Number of retry attempts for failed Telegram API
                              requests before raising `TelegramAPIError`.
        backoff_factor:       Multiplier applied between retry attempts by
                              `urllib3`'s exponential backoff algorithm.
                              Effective sleep between attempt *n* and *n+1* is
                              `backoff_factor * (2 ** (n - 1))` seconds.
        suppress_subject:     Case-insensitive glob patterns matched against
                              the Subject header. Matching messages are
                              skipped for Telegram delivery; spooling is
                              unaffected.
        suppress_sender:      Case-insensitive glob patterns matched against
                              the From header. Same suppression semantics as
                              `suppress_subject`.
    """

    token: str
    chat_id: str
    message_max_len: int
    smtp_timeout: int
    telegram_timeout: int
    disable_notification: bool
    spool_path: Path | None
    max_retries: int
    backoff_factor: float
    suppress_subject: tuple[str, ...] = ()
    suppress_sender: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ParsedOptions:
    """
    Validated `[options]` values, ready to be merged into `AppConfig`.

    Attributes:
        message_max_len:      Maximum body character count before truncation.
        smtp_timeout:         Seconds to wait for an SMTP client command.
        telegram_timeout:     Seconds to wait for a Telegram API response.
        disable_notification: Whether to suppress Telegram push notifications.
        spool_path:           Fully resolved path to the per-user spool file,
                              or `None` when spooling is disabled.
        max_retries:          Number of retry attempts for failed Telegram API
                              requests.
        backoff_factor:       Multiplier for `urllib3`'s exponential backoff.
    """

    message_max_len: int
    smtp_timeout: int
    telegram_timeout: int
    disable_notification: bool
    spool_path: Path | None
    max_retries: int
    backoff_factor: float


@dataclass(frozen=True)
class _ParsedFilters:
    """
    Extracted `[filters]` glob patterns, ready to be merged into `AppConfig`.

    Attributes:
        suppress_subject: Case-insensitive glob patterns matched against
                          the Subject header.
        suppress_sender:  Case-insensitive glob patterns matched against
                          the From header.
    """

    suppress_subject: tuple[str, ...]
    suppress_sender: tuple[str, ...]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _locate_config_file() -> Path:
    """
    Return the highest-priority config file that exists on disk.

    User config (~/.telegram-sendmail.ini) takes precedence over the
    system config (/etc/telegram-sendmail.ini), matching the convention
    established by most Unix tools.

    Raises:
        ConfigurationError: If neither file exists.
    """
    for candidate in (_USER_CONFIG, _SYSTEM_CONFIG):
        if candidate.exists():
            return candidate

    raise ConfigurationError(
        "Configuration file not found. Create one of:\n"
        f"  User config:   {_USER_CONFIG}\n"
        f"  System config: {_SYSTEM_CONFIG}\n\n"
        "Minimum required content:\n"
        "  [telegram]\n"
        "  token = YOUR_BOT_TOKEN\n"
        "  chat_id = YOUR_CHAT_ID"
    )


def _audit_permissions(config_file: Path) -> None:
    """
    Emit a WARNING if the config file is readable by group or others.

    The check is advisory: we do not refuse to run on a loose-permission
    file because the system administrator may have a legitimate reason
    (e.g., a shared service account). However, the warning is always
    emitted so it appears in syslog and cannot be silently ignored.
    """
    mode = stat.S_IMODE(config_file.stat().st_mode)
    if mode & 0o077:
        logger.warning(
            "Config file %s has insecure permissions %s — "
            "recommended permissions are 0600 (chmod 600 %s)",
            config_file,
            oct(mode),
            config_file,
        )


def _validate_range(
    value: int,
    bounds: tuple[int, int],
    key: str,
    default: int,
) -> int:
    """
    Return `value` if it falls within `bounds`, otherwise log a warning
    and return `default`.

    Keeping all range validation in one function ensures that the warning
    message format is consistent for every numeric option and that
    `_parse_options` stays readable.
    """
    lo, hi = bounds
    if lo <= value <= hi:
        return value

    logger.warning(
        "Config option '%s' value %d is outside the valid range [%d, %d]; "
        "falling back to default %d",
        key,
        value,
        lo,
        hi,
        default,
    )
    return default


def _validate_range_float(
    value: float,
    bounds: tuple[float, float],
    key: str,
    default: float,
) -> float:
    """
    Float-typed analogue of `_validate_range`.

    Separated to preserve strict MyPy compliance: a single generic helper
    would require a `TypeVar` bound to `int | float`, which complicates
    the signature without adding readability.
    """
    lo, hi = bounds
    if lo <= value <= hi:
        return value

    logger.warning(
        "Config option '%s' value %.2f is outside the valid range [%.2f, %.2f]; "
        "falling back to default %.2f",
        key,
        value,
        lo,
        hi,
        default,
    )
    return default


def _resolve_spool_path(raw_dir: str | None) -> Path:
    """
    Resolve the per-user spool file path from the configured directory.

    Resolution order:
    1. Use `raw_dir` if provided and its parent directory is writable.
    2. Use `_DEFAULT_SPOOL_DIR` (/var/mail) if `raw_dir` is absent.
    3. Fall back to `_FALLBACK_SPOOL_BASE/<username>`
       (/tmp/.telegram-sendmail-spool/<username>) with a WARNING if the
       resolved directory is not writable. The hidden subdirectory is
       created on demand with `0700` permissions (owner-only) to prevent
       other users on the same host from reading mail content spooled into
       the world-writable `/tmp` root. Its ownership is validated after
       creation: if the directory is not owned by the current UID — a sign
       of a pre-creation attack — an `OSError` is raised inside the
       existing `try` block and a `WARNING` is emitted instead of using
       the untrusted path.

    The current user's login name is always appended as the filename so
    that the spool path follows the standard Unix convention of
    `/var/mail/<username>`.

    Args:
        raw_dir: The raw string value from the `[options]` section, or
                 `None` if the key was not present in the config file.

    Returns:
        A `Path` pointing to the per-user spool file inside a writable
        directory.
    """
    username: str = getpass.getuser()

    configured_dir: Path = Path(raw_dir) if raw_dir else _DEFAULT_SPOOL_DIR
    candidate: Path = configured_dir / username

    # Test writability against the directory, not the file itself, because
    # the file may not exist yet on first run.
    target_dir: Path = candidate.parent
    if os.access(target_dir, os.W_OK):
        return candidate

    logger.warning(
        "Spool directory '%s' is not writable; falling back to '%s'",
        target_dir,
        _FALLBACK_SPOOL_BASE,
    )

    fallback_dir: Path = _FALLBACK_SPOOL_BASE
    try:
        # mkdir is atomic with respect to the directory name and safe to
        # call repeatedly with exist_ok=True.
        fallback_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Re-enforce permissions in case the directory already existed with
        # looser permissions from a previous installation.
        fallback_dir.chmod(0o700)
        dir_stat = fallback_dir.stat()
        if dir_stat.st_uid != os.getuid():
            raise OSError(
                errno.EPERM,
                f"fallback directory uid {dir_stat.st_uid} != effective uid "
                f"{os.getuid()}",
                str(fallback_dir),
            )
    except OSError as exc:
        logger.warning(
            "Could not create fallback spool directory '%s': %s",
            fallback_dir,
            exc,
        )

    return fallback_dir / username


def _require(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    config_file: Path,
) -> str:
    """
    Return the value of a required config key, raising `ConfigurationError`
    with a precise, operator-friendly message if it is absent or empty.
    """
    try:
        value = parser.get(section, option).strip()
    except (configparser.NoSectionError, configparser.NoOptionError):
        raise ConfigurationError(
            f"Required key '[{section}] {option}' is missing from {config_file}."
        )

    if not value:
        raise ConfigurationError(
            f"Required key '[{section}] {option}' in {config_file} must not be empty."
        )

    return value


def _parse_options(
    parser: configparser.ConfigParser,
    config_file: Path,
) -> _ParsedOptions:
    """
    Extract and validate all `[options]` keys from a loaded `ConfigParser`.

    Unknown or malformed values fall back to their defaults via
    `_validate_range`; the failure is always logged so it appears in syslog.
    This function never raises — it degrades gracefully to defaults so that a
    single bad option does not block delivery.

    Spool path resolution runs only when `spool_enabled` is true. Resolving
    probes `spool_dir` for writability and may create the `/tmp` fallback,
    so a disabled spool must skip it to leave no directory and no fallback
    `WARNING` behind.

    Returns:
        A `_ParsedOptions` instance containing the validated options.
    """
    message_max_len: int = _DEFAULT_MESSAGE_MAX_LEN
    smtp_timeout: int = _DEFAULT_SMTP_TIMEOUT
    telegram_timeout: int = _DEFAULT_TELEGRAM_TIMEOUT
    disable_notification: bool = _DEFAULT_DISABLE_NOTIFICATION
    spool_enabled: bool = _DEFAULT_SPOOL_ENABLED
    raw_spool_dir: str | None = None
    max_retries: int = _DEFAULT_MAX_RETRIES
    backoff_factor: float = _DEFAULT_BACKOFF_FACTOR

    if not parser.has_section("options"):
        return _ParsedOptions(
            message_max_len=message_max_len,
            smtp_timeout=smtp_timeout,
            telegram_timeout=telegram_timeout,
            disable_notification=disable_notification,
            spool_path=_resolve_spool_path(raw_spool_dir),
            max_retries=max_retries,
            backoff_factor=backoff_factor,
        )

    def _get_int(key: str, default: int, bounds: tuple[int, int]) -> int:
        if not parser.has_option("options", key):
            return default
        try:
            raw = parser.getint("options", key)
        except ValueError:
            logger.warning(
                "Config option '%s' in %s is not a valid integer; using default %d",
                key,
                config_file,
                default,
            )
            return default
        return _validate_range(raw, bounds, key, default)

    def _get_float(key: str, default: float, bounds: tuple[float, float]) -> float:
        if not parser.has_option("options", key):
            return default
        try:
            raw = parser.getfloat("options", key)
        except ValueError:
            logger.warning(
                "Config option '%s' in %s is not a valid number; using default %.2f",
                key,
                config_file,
                default,
            )
            return default
        return _validate_range_float(raw, bounds, key, default)

    message_max_len = _get_int(
        "message_max_length",
        _DEFAULT_MESSAGE_MAX_LEN,
        _MESSAGE_MAX_LEN_BOUNDS,
    )
    smtp_timeout = _get_int(
        "smtp_timeout",
        _DEFAULT_SMTP_TIMEOUT,
        _SMTP_TIMEOUT_BOUNDS,
    )
    telegram_timeout = _get_int(
        "telegram_timeout",
        _DEFAULT_TELEGRAM_TIMEOUT,
        _TELEGRAM_TIMEOUT_BOUNDS,
    )
    max_retries = _get_int(
        "max_retries",
        _DEFAULT_MAX_RETRIES,
        _MAX_RETRIES_BOUNDS,
    )
    backoff_factor = _get_float(
        "backoff_factor",
        _DEFAULT_BACKOFF_FACTOR,
        _BACKOFF_FACTOR_BOUNDS,
    )

    if parser.has_option("options", "disable_notification"):
        try:
            disable_notification = parser.getboolean("options", "disable_notification")
        except ValueError:
            logger.warning(
                "Config option 'disable_notification' in %s is not a valid boolean; "
                "using default %s",
                config_file,
                _DEFAULT_DISABLE_NOTIFICATION,
            )

    if parser.has_option("options", "spool_enabled"):
        try:
            spool_enabled = parser.getboolean("options", "spool_enabled")
        except ValueError:
            logger.warning(
                "Config option 'spool_enabled' in %s is not a valid boolean; "
                "using default %s",
                config_file,
                _DEFAULT_SPOOL_ENABLED,
            )

    if parser.has_option("options", "spool_dir"):
        raw_spool_dir = parser.get("options", "spool_dir").strip() or None

    return _ParsedOptions(
        message_max_len=message_max_len,
        smtp_timeout=smtp_timeout,
        telegram_timeout=telegram_timeout,
        disable_notification=disable_notification,
        spool_path=_resolve_spool_path(raw_spool_dir) if spool_enabled else None,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
    )


def _parse_filters(
    parser: configparser.ConfigParser,
) -> _ParsedFilters:
    """
    Extract glob patterns from the `[filters]` INI section.

    Each multi-line key is split on newlines; empty lines and
    whitespace-only entries are silently dropped. The function never
    raises — a missing section or empty keys simply produce empty tuples
    so that delivery is never blocked by a filter misconfiguration.

    Returns:
        A `_ParsedFilters` instance containing the extracted glob patterns.
    """
    if not parser.has_section("filters"):
        return _ParsedFilters(suppress_subject=(), suppress_sender=())

    def _read_patterns(key: str) -> tuple[str, ...]:
        if not parser.has_option("filters", key):
            return ()
        raw = parser.get("filters", key)
        patterns = tuple(line.strip() for line in raw.splitlines() if line.strip())
        if patterns:
            logger.debug(
                "Loaded %d suppression pattern(s) from [filters] %s",
                len(patterns),
                key,
            )
        return patterns

    return _ParsedFilters(
        suppress_subject=_read_patterns("suppress_subject"),
        suppress_sender=_read_patterns("suppress_sender"),
    )


# --------------------------------------------------------------------------
# Public interface
# --------------------------------------------------------------------------


class ConfigLoader:
    """
    Discovers, reads, validates, and returns a fully resolved `AppConfig`.

    Usage::

        config = ConfigLoader.load()
        config = ConfigLoader.load(Path("/opt/staging/sendmail.ini"))

    The class exposes a single class method rather than instance methods
    because loading configuration is a stateless, idempotent operation.
    There is no meaningful state to retain between calls.
    """

    @classmethod
    def load(cls, config_file: Path | None = None) -> AppConfig:
        """
        Locate the config file, validate its permissions, parse its contents,
        and return an immutable `AppConfig` instance.

        An explicit `config_file` replaces discovery with no fallback to the
        default locations, so a mistyped path raises instead of silently
        loading a different file.

        Args:
            config_file: Operator-supplied path from `--config`, or `None`
                         to search the default locations.

        Raises:
            ConfigurationError: If no config file is found, the explicit path
                                is not a regular file, the file cannot be
                                read, or a required key is absent or empty.
        """
        if config_file is None:
            config_file = _locate_config_file()
        elif not config_file.is_file():
            # configparser silently skips paths it cannot open, so a directory
            # would otherwise surface as a misleading missing-key error.
            raise ConfigurationError(
                f"Config file '{config_file}' is missing or not a regular file. "
                "Pass the path to an existing INI file via --config."
            )

        if not os.access(config_file, os.R_OK):
            raise ConfigurationError(
                f"Config file '{config_file}' exists but cannot be read. "
                "Check file ownership and permissions."
            )

        _audit_permissions(config_file)

        # interpolation=None prevents configparser from interpreting '%'
        # characters in values as interpolation tokens. Without this, a
        # spool path like '/data/50%full' or a future config value
        # containing '%' would raise InterpolationSyntaxError.
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(config_file, encoding="utf-8")
        except configparser.Error as exc:
            raise ConfigurationError(
                f"Failed to parse config file '{config_file}': {exc}"
            ) from exc

        token = _require(parser, "telegram", "token", config_file)
        chat_id = _require(parser, "telegram", "chat_id", config_file)

        options = _parse_options(parser, config_file)
        filters = _parse_filters(parser)

        logger.debug("Configuration loaded successfully from '%s'", config_file)

        return AppConfig(
            token=token,
            chat_id=chat_id,
            message_max_len=options.message_max_len,
            smtp_timeout=options.smtp_timeout,
            telegram_timeout=options.telegram_timeout,
            disable_notification=options.disable_notification,
            spool_path=options.spool_path,
            max_retries=options.max_retries,
            backoff_factor=options.backoff_factor,
            suppress_subject=filters.suppress_subject,
            suppress_sender=filters.suppress_sender,
        )
