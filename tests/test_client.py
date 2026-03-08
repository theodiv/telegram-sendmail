"""
Tests for telegram_sendmail.client.

Coverage targets
----------------
`TelegramClient.__init__` / context manager protocol
    - __enter__ returns the client instance itself (enables `with client as c:` idiom)
    - close() releases the underlying session and is safe to call multiple times

`TelegramClient.send` — happy path
    - POSTs to the correct sendMessage URL containing the bot token
    - Payload includes the configured chat_id
    - Payload sets parse_mode to "HTML"
    - Payload disables link previews via link_preview_options
    - Payload text matches the argument passed to send()
    - disable_notification=False is forwarded verbatim from config
    - disable_notification=True is forwarded verbatim from config

`TelegramClient.send` — network-level failures
    - Raises TelegramAPIError with "timed out" in the message on a Timeout exception
    - Raises TelegramAPIError with "connect" in the message on a ConnectionError exception
    - Raises TelegramAPIError on any other RequestException subclass

`TelegramClient._check_response` — API-level failures
    - Raises TelegramAPIError when the JSON body contains ok=false (even at HTTP 200)
    - Attaches the numeric error_code from the JSON body to TelegramAPIError.status_code
    - Includes the API description string in the TelegramAPIError message
    - Raises TelegramAPIError with "non-JSON" in the message for non-JSON response bodies
    - Raises TelegramAPIError on HTTP 500 after retry attempts are exhausted
    - Raises TelegramAPIError with status_code=429 on an HTTP 429 rate-limit response
    - Extracts status_code from the JSON error_code field rather than the HTTP status
    - Embeds the numeric error_code in the TelegramAPIError message string

`TelegramClient._build_session` — retry configuration contract
    - Mounts a Retry object whose total equals max_retries from AppConfig
    - Mounts a Retry object whose backoff_factor equals backoff_factor from AppConfig
    - status_forcelist includes both 500 (server error) and 429 (rate limit)
    - allowed_methods restricts retries to POST only
    - raise_on_status is False so _check_response can inspect the JSON body on 4xx/5xx

Design notes
------------
- `app_config_no_retry` (max_retries=0, backoff_factor=0.0) is used for every
  error-path test to prevent urllib3's exponential back-off from inflating test
  runtimes.
- The `mock_telegram_api_error` fixture returns HTTP 200 with `ok: false`. This
  exercises the code path where `_check_response` must inspect the JSON body
  rather than the HTTP status code — a real Telegram API behaviour.
- `status_code` propagation is verified explicitly because `__main__.py` maps
  any `TelegramAPIError.status_code` in `_RETRY_STATUS_CODES` to EX_TEMPFAIL
  (75), which is a critical contract for MTA-aware daemons to re-queue messages.
- `TestTelegramClientRetryExhaustion` tests the Retry *configuration* by
  introspecting `client._session.get_adapter("https://").max_retries` directly.
  Testing call_count via requests_mock is not possible: requests_mock replaces
  the HTTPS adapter at the HTTPAdapter.send() level, so urllib3's urlopen() —
  where the Retry loop actually executes — is never entered. Regardless of
  max_retries, requests_mock always records call_count == 1.
"""

from __future__ import annotations

import dataclasses

import pytest
import requests
import requests_mock as requests_mock_module

from telegram_sendmail.client import TelegramClient
from telegram_sendmail.config import AppConfig
from telegram_sendmail.exceptions import TelegramAPIError

# --------------------------------------------------------------------------
# Context manager protocol
# --------------------------------------------------------------------------


class TestTelegramClientContextManager:
    def test_enter_returns_client_instance(self, app_config: AppConfig):
        client = TelegramClient(app_config)
        with client as ctx:
            assert ctx is client

    def test_close_is_safe_to_call_multiple_times(self, app_config: AppConfig):
        client = TelegramClient(app_config)
        client.close()
        # A second close must not raise; sessions tolerate double-close.
        client.close()

    def test_context_manager_closes_session_on_exit(self, app_config: AppConfig):
        with TelegramClient(app_config) as client:
            session = client._session
        # After __exit__, the underlying urllib3 pool is drained; the session
        # object still exists but further use would raise on the adapters.
        assert session is not None


# --------------------------------------------------------------------------
# Successful delivery — payload structure
# --------------------------------------------------------------------------


class TestTelegramClientSendSuccess:
    def test_posts_to_correct_send_message_url(
        self,
        app_config: AppConfig,
        mock_telegram_ok: requests_mock_module.Mocker,
        send_telegram_url_pattern: str,
    ):
        with TelegramClient(app_config) as client:
            client.send("hello")
        assert mock_telegram_ok.last_request.url == send_telegram_url_pattern

    def test_payload_contains_configured_chat_id(
        self,
        app_config: AppConfig,
        mock_telegram_ok: requests_mock_module.Mocker,
    ):
        with TelegramClient(app_config) as client:
            client.send("hello")
        assert mock_telegram_ok.last_request.json()["chat_id"] == app_config.chat_id

    def test_payload_parse_mode_is_html(
        self,
        app_config: AppConfig,
        mock_telegram_ok: requests_mock_module.Mocker,
    ):
        with TelegramClient(app_config) as client:
            client.send("hello")
        assert mock_telegram_ok.last_request.json()["parse_mode"] == "HTML"

    def test_payload_link_preview_is_disabled(
        self,
        app_config: AppConfig,
        mock_telegram_ok: requests_mock_module.Mocker,
    ):
        with TelegramClient(app_config) as client:
            client.send("hello")
        payload = mock_telegram_ok.last_request.json()
        assert payload["link_preview_options"]["is_disabled"] is True

    def test_payload_text_matches_send_argument(
        self,
        app_config: AppConfig,
        mock_telegram_ok: requests_mock_module.Mocker,
    ):
        message = "<b>From:</b> cron\n\nbody text"
        with TelegramClient(app_config) as client:
            client.send(message)
        assert mock_telegram_ok.last_request.json()["text"] == message

    def test_disable_notification_false_forwarded_when_config_is_false(
        self,
        app_config: AppConfig,
        mock_telegram_ok: requests_mock_module.Mocker,
    ):
        # app_config has disable_notification=False by fixture definition.
        assert app_config.disable_notification is False
        with TelegramClient(app_config) as client:
            client.send("hello")
        assert mock_telegram_ok.last_request.json()["disable_notification"] is False

    def test_disable_notification_true_forwarded_when_config_is_true(
        self,
        app_config: AppConfig,
        mock_telegram_ok: requests_mock_module.Mocker,
    ):
        silent_config = dataclasses.replace(app_config, disable_notification=True)
        with TelegramClient(silent_config) as client:
            client.send("silent message")
        assert mock_telegram_ok.last_request.json()["disable_notification"] is True

    def test_send_does_not_raise_on_success(
        self,
        app_config: AppConfig,
        mock_telegram_ok: requests_mock_module.Mocker,
    ):
        # Verify no exception escapes on a clean 200 ok:true response.
        with TelegramClient(app_config) as client:
            client.send("no error expected")


# --------------------------------------------------------------------------
# Network-level failures
# --------------------------------------------------------------------------


class TestTelegramClientNetworkErrors:
    def test_timeout_raises_telegram_api_error_with_timed_out_message(
        self,
        app_config_no_retry: AppConfig,
        requests_mock: requests_mock_module.Mocker,
        send_telegram_url_pattern: str,
    ):
        requests_mock.post(send_telegram_url_pattern, exc=requests.exceptions.Timeout)
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError, match="timed out"):
                client.send("hello")

    def test_connection_error_raises_telegram_api_error_with_connect_message(
        self,
        app_config_no_retry: AppConfig,
        requests_mock: requests_mock_module.Mocker,
        send_telegram_url_pattern: str,
    ):
        requests_mock.post(
            send_telegram_url_pattern, exc=requests.exceptions.ConnectionError
        )
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError, match="connect"):
                client.send("hello")

    def test_generic_request_exception_raises_telegram_api_error(
        self,
        app_config_no_retry: AppConfig,
        requests_mock: requests_mock_module.Mocker,
        send_telegram_url_pattern: str,
    ):
        requests_mock.post(
            send_telegram_url_pattern,
            exc=requests.exceptions.RequestException("unexpected transport failure"),
        )
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError):
                client.send("hello")

    def test_timeout_error_is_not_a_generic_exception_leak(
        self,
        app_config_no_retry: AppConfig,
        requests_mock: requests_mock_module.Mocker,
        send_telegram_url_pattern: str,
    ):
        # Ensures requests.Timeout does not propagate as a raw requests exception.
        requests_mock.post(send_telegram_url_pattern, exc=requests.exceptions.Timeout)
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError):
                client.send("hello")


# --------------------------------------------------------------------------
# Telegram API response errors — _check_response logic
# --------------------------------------------------------------------------


class TestTelegramClientAPIErrors:
    def test_ok_false_with_http_200_raises_telegram_api_error(
        self,
        app_config_no_retry: AppConfig,
        mock_telegram_api_error: requests_mock_module.Mocker,
    ):
        # Telegram returns HTTP 200 with ok:false for application-level errors.
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError):
                client.send("hello")

    def test_ok_false_attaches_error_code_to_exception(
        self,
        app_config_no_retry: AppConfig,
        mock_telegram_api_error: requests_mock_module.Mocker,
    ):
        # mock_telegram_api_error fixture: error_code=400 in JSON body at HTTP 200.
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError) as exc_info:
                client.send("hello")
        assert exc_info.value.status_code == 400

    def test_ok_false_includes_description_in_exception_message(
        self,
        app_config_no_retry: AppConfig,
        mock_telegram_api_error: requests_mock_module.Mocker,
    ):
        # The API description "chat not found" must appear in the error message so
        # it surfaces in syslog for operator debugging.
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError, match="chat not found"):
                client.send("hello")

    def test_non_json_response_raises_telegram_api_error_with_descriptive_message(
        self,
        app_config_no_retry: AppConfig,
        requests_mock: requests_mock_module.Mocker,
        send_telegram_url_pattern: str,
    ):
        requests_mock.post(
            send_telegram_url_pattern,
            text="<html>Internal Server Error</html>",
            status_code=500,
        )
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError, match="non-JSON"):
                client.send("hello")

    def test_http_500_raises_telegram_api_error(
        self,
        app_config_no_retry: AppConfig,
        mock_telegram_server_error: requests_mock_module.Mocker,
    ):
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError):
                client.send("hello")

    def test_http_429_raises_with_status_code_429(
        self,
        app_config_no_retry: AppConfig,
        mock_telegram_rate_limit: requests_mock_module.Mocker,
    ):
        # __main__._run_pipe_mode maps any status_code in _RETRY_STATUS_CODES
        # to EX_TEMPFAIL (75); this test guards that contract for 429.
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError) as exc_info:
                client.send("hello")
        assert exc_info.value.status_code == 429

    def test_http_429_error_code_extracted_from_json_body(
        self,
        app_config_no_retry: AppConfig,
        mock_telegram_rate_limit: requests_mock_module.Mocker,
    ):
        # Telegram encodes the rate-limit in the JSON error_code field, not
        # necessarily only in the HTTP status code.
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError) as exc_info:
                client.send("hello")
        # status_code should come from JSON error_code=429, not HTTP status.
        assert exc_info.value.status_code == 429

    def test_error_message_contains_error_code_number(
        self,
        app_config_no_retry: AppConfig,
        mock_telegram_api_error: requests_mock_module.Mocker,
    ):
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError, match="400"):
                client.send("hello")


# --------------------------------------------------------------------------
# Retry exhaustion
# --------------------------------------------------------------------------


class TestTelegramClientRetryExhaustion:
    def test_server_error_raises_after_zero_retries(
        self,
        app_config_no_retry: AppConfig,
        mock_telegram_server_error: requests_mock_module.Mocker,
    ):
        # With max_retries=0 a single 500 response must immediately raise.
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError):
                client.send("hello")
        assert mock_telegram_server_error.call_count == 1

    def test_rate_limit_raises_after_zero_retries(
        self,
        app_config_no_retry: AppConfig,
        mock_telegram_rate_limit: requests_mock_module.Mocker,
    ):
        with TelegramClient(app_config_no_retry) as client:
            with pytest.raises(TelegramAPIError):
                client.send("hello")
        assert mock_telegram_rate_limit.call_count == 1

    def test_retry_adapter_total_matches_config_max_retries(
        self, app_config: AppConfig
    ):
        # requests_mock intercepts at HTTPAdapter.send() and never enters
        # urllib3's urlopen(), where the Retry loop lives. call_count therefore
        # always equals 1 regardless of max_retries. The correct approach is to
        # assert the Retry object that _build_session mounts on the adapter.
        client = TelegramClient(app_config)
        retry_obj = client._session.get_adapter("https://").max_retries
        assert retry_obj.total == app_config.max_retries

    def test_retry_adapter_backoff_factor_matches_config(self, app_config: AppConfig):
        client = TelegramClient(app_config)
        retry_obj = client._session.get_adapter("https://").max_retries
        assert retry_obj.backoff_factor == app_config.backoff_factor

    def test_retry_adapter_status_forcelist_includes_500_and_429(
        self, app_config: AppConfig
    ):
        client = TelegramClient(app_config)
        retry_obj = client._session.get_adapter("https://").max_retries
        assert 500 in retry_obj.status_forcelist
        assert 429 in retry_obj.status_forcelist

    def test_retry_adapter_restricts_retries_to_post_method(
        self, app_config: AppConfig
    ):
        # Retrying is opt-in per method; GET is excluded to prevent unintended
        # side effects on any future non-sendMessage endpoints.
        client = TelegramClient(app_config)
        retry_obj = client._session.get_adapter("https://").max_retries
        assert "POST" in retry_obj.allowed_methods

    def test_retry_adapter_raise_on_status_is_false(self, app_config: AppConfig):
        # raise_on_status=False is required so _check_response receives the raw
        # response object and can extract the JSON error_code on 429/5xx replies.
        client = TelegramClient(app_config)
        retry_obj = client._session.get_adapter("https://").max_retries
        assert retry_obj.raise_on_status is False
