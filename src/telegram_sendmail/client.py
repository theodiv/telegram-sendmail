"""
Telegram Bot API client for telegram-sendmail.

Public surface
--------------
- `TelegramClient` — sends formatted messages to a Telegram chat via the
                     Bot API, with configurable retry and timeout behaviour.

The client owns a `requests.Session` that is configured once at construction
time and reused for all requests within a delivery pipeline invocation. It
implements the context manager protocol so callers can use it with a `with`
statement for deterministic session cleanup.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram_sendmail.config import AppConfig
from telegram_sendmail.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"

# HTTP status codes that warrant an automatic retry. 429 (Too Many Requests)
# is the most common transient failure from the Telegram API.
_RETRY_STATUS_CODES: frozenset[int] = frozenset({
    HTTPStatus.TOO_MANY_REQUESTS,
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
})  # fmt: skip


class TelegramClient:
    """
    Sends messages to the Telegram Bot API.

    The underlying `requests.Session` is configured with an `HTTPAdapter`
    backed by a `urllib3.Retry` strategy. Retry behaviour (attempt count,
    backoff factor) is driven entirely by the values in `AppConfig`, so
    operators can tune it for their network environment without touching code.

    The class implements the context manager protocol. Although the session
    is closed automatically when the object is garbage-collected, using the
    context manager form is strongly preferred for production code paths
    where deterministic cleanup matters::

        with TelegramClient(config) as client:
            client.send(message_text)

    Args:
        config: A fully resolved `AppConfig` instance.
    """

    def __init__(self, config: AppConfig) -> None:
        self._token: str = config.token
        self._chat_id: str = config.chat_id
        self._timeout: int = config.telegram_timeout
        self._disable_notification: bool = config.disable_notification
        self._session: requests.Session = self._build_session(
            max_retries=config.max_retries,
            backoff_factor=config.backoff_factor,
        )

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying `requests.Session` and its connections."""
        self._session.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_session(max_retries: int, backoff_factor: float) -> requests.Session:
        """
        Construct a `requests.Session` with a retry-enabled HTTPS adapter.

        Retries are attempted only on the HTTP methods and status codes that
        are safe to repeat (POST on 429/5xx). The `raise_on_status` flag is
        left `False` here; response status is checked explicitly in `send()`
        so error messages can be extracted from the body.
        """
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=list(_RETRY_STATUS_CODES),
            allowed_methods=["POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session = requests.Session()
        session.mount("https://", adapter)

        return session

    def _api_url(self, method: str) -> str:
        return f"{_TELEGRAM_API_BASE}/bot{self._token}/{method}"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def send(self, text: str) -> None:
        """
        Send `text` to the configured Telegram chat.

        Args:
            text: Fully formatted message string with Telegram HTML markup.
                  Must not exceed 4096 characters (Telegram hard limit).

        Raises:
            TelegramAPIError: If the API returns a non-OK response after all
                              retry attempts are exhausted, or if the network
                              request itself fails.
        """
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": self._disable_notification,
            "link_preview_options": {"is_disabled": True},
        }

        logger.debug(
            "Sending message to chat_id=%s (length=%d chars)",
            self._chat_id,
            len(text),
        )

        try:
            response = self._session.post(
                url=self._api_url("sendMessage"),
                json=payload,
                timeout=self._timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise TelegramAPIError(
                f"Telegram API request timed out after {self._timeout}s: {exc}",
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise TelegramAPIError(
                f"Failed to connect to Telegram API: {exc}",
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise TelegramAPIError(
                f"Telegram API request failed: {exc}",
            ) from exc

        self._check_response(response)
        logger.info("Message delivered to Telegram (chat_id=%s)", self._chat_id)

    def _check_response(self, response: requests.Response) -> None:
        """
        Validate the Telegram API JSON response.

        The Telegram API always returns a JSON body with an `ok` boolean
        field, even for error responses. We check that field in preference
        to the HTTP status code because the API occasionally returns `200`
        with `ok: false` for application-level errors (e.g. bad chat ID).

        Raises:
            TelegramAPIError: If `ok` is `false` or the response body
                              cannot be decoded as JSON.
        """
        try:
            body: dict[str, object] = response.json()
        except ValueError as exc:
            raise TelegramAPIError(
                f"Telegram API returned non-JSON response "
                f"(HTTP {response.status_code}): {response.text[:200]}",
                status_code=response.status_code,
            ) from exc

        if not body.get("ok"):
            description = body.get("description", "no description provided")
            error_code = body.get("error_code", response.status_code)
            raise TelegramAPIError(
                f"Telegram API error {error_code}: {description}",
                status_code=(
                    int(error_code) if isinstance(error_code, (int, float)) else None
                ),
            )
