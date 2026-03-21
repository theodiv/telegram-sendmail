"""
Email parsing and HTML-to-Telegram conversion for telegram-sendmail.

Public surface
--------------
- `ParsedEmail` — frozen dataclass holding the extracted email fields.
- `EmailParser` — parses a raw RFC 2822 string into a `ParsedEmail`
                  and formats it for the Telegram Bot API.

Internal components
-------------------
- `_HTMLSanitizer`     — strips dangerous HTML tags before conversion.
- `TelegramHTMLParser` — converts safe HTML to Telegram-compatible markup.
"""

import html
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from email.parser import Parser
from email.policy import default as email_default_policy
from html.parser import HTMLParser
from typing import Any, Final

from html2text import HTML2Text

from telegram_sendmail.config import AppConfig
from telegram_sendmail.exceptions import ParsingError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------


# Tags whose entire subtree (opening tag, content, closing tag) must be
# removed. These are content-bearing elements that pose a security risk
# or produce meaningless output in a plain-text/Telegram context.
_SUPPRESS_WITH_CONTENT: frozenset[str] = frozenset(
    {"script", "style", "iframe", "object", "embed", "svg", "math", "noscript"}
)

# Void elements with no content that should simply be dropped from output.
# No depth tracking is needed because HTML parsers never emit an end-tag
# for void elements.
_SUPPRESS_VOID: frozenset[str] = frozenset({"input", "meta", "link"})

# Mapping of common HTML inline tags to the strict subset supported by
# Telegram. Keys are the input HTML tag; values are the Telegram replacement.
_INLINE_TAG_MAP: Final[Mapping[str, str]] = {
    "b": "b", "strong": "b",
    'i': 'i', 'em': 'i',
    'u': 'u', 'ins': 'u',
    's': 's', 'strike': 's', 'del': 's',
    'code': 'code', 'kbd': 'code', 'tt': 'code',
}  # fmt: skip

_HORIZONTAL_RULE = "─" * 20

_TRUNCATED_FOOTER = "\n\n<i>[…message truncated…]</i>"
_ATTACHMENT_FOOTER = "\n\n<i>📎 This email contained attachments (not forwarded)</i>"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedEmail:
    """
    Immutable snapshot of the fields extracted from a raw RFC 2822 email.

    Attributes:
        sender:          Envelope / header `From` address.
        subject:         Sanitised, single-line subject string.
        body:            Email body converted to Telegram-safe HTML markup.
        has_attachments: True if the MIME structure contained at least one
                         attachment part.
    """

    sender: str
    subject: str
    body: str
    has_attachments: bool


# --------------------------------------------------------------------------
# HTML sanitiser (pre-processor)
# --------------------------------------------------------------------------


class _HTMLSanitizer(HTMLParser):
    """
    Strips dangerous HTML elements and their content before conversion.

    The sanitiser runs as a pure pre-processing pass over the raw HTML
    string. It does not produce Telegram markup; it produces a safe HTML
    string that is subsequently handed to `TelegramHTMLParser`.

    Two suppression strategies are used:

    - **Depth-tracked suppression** (`_SUPPRESS_WITH_CONTENT`): a counter
      is incremented on the opening tag and decremented on the closing tag.
      All data and nested tags within a suppressed subtree are discarded.
      Handles arbitrarily nested dangerous elements correctly.

    - **Void-element suppression** (`_SUPPRESS_VOID`): the opening tag is
      silently discarded. Because void elements never emit a closing tag,
      no counter manipulation is needed.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._suppress_depth: int = 0
        self._output: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SUPPRESS_WITH_CONTENT:
            self._suppress_depth += 1
            return
        if tag in _SUPPRESS_VOID:
            return
        if self._suppress_depth:
            return
        attr_str = self._serialise_attrs(attrs)
        self._output.append(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SUPPRESS_WITH_CONTENT:
            if self._suppress_depth > 0:
                self._suppress_depth -= 1
            return
        if self._suppress_depth:
            return
        self._output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._suppress_depth:
            return
        self._output.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._suppress_depth:
            return
        self._output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._suppress_depth:
            return
        self._output.append(f"&#{name};")

    @staticmethod
    def _serialise_attrs(attrs: list[tuple[str, str | None]]) -> str:
        """Re-serialise tag attributes, escaping values for HTML safety."""
        if not attrs:
            return ""
        parts: list[str] = []
        for name, value in attrs:
            if value is None:
                parts.append(name)
            else:
                parts.append(f'{name}="{html.escape(value)}"')
        return " " + " ".join(parts)

    def get_output(self) -> str:
        """Return the sanitised HTML string."""
        return "".join(self._output)


def _sanitise_html(raw_html: str) -> str:
    """Run `raw_html` through `_HTMLSanitizer` and return clean HTML."""
    sanitiser = _HTMLSanitizer()
    sanitiser.feed(raw_html)
    sanitiser.close()
    return sanitiser.get_output()


# --------------------------------------------------------------------------
# HTML to Telegram markup converter
# --------------------------------------------------------------------------


class TelegramHTMLParser(HTML2Text):
    """
    Converts sanitised HTML to Telegram Bot API-compatible HTML markup.

    Telegram supports a strict subset of HTML tags: `<b>`, `<i>`, `<u>`,
    `<s>`, `<a>`, `<code>`, `<pre>`, `<blockquote>`. This parser maps common
    HTML elements to that subset and discards everything else, producing
    clean output that the Telegram API will accept under `parse_mode="HTML"`.

    This class must only receive input that has already been processed by
    `_HTMLSanitizer`; it does not perform its own security checks.
    """

    def __init__(self) -> None:
        super().__init__()
        self.body_width = 0
        self.ignore_images = True
        self.ignore_tables = True
        self.unicode_snob = True
        self.backquote_code_style = True
        # Suppress html2text's own emphasis/strong markers; we emit explicit
        # tags instead via the tag callback.
        self.strong_mark = ""
        self.emphasis_mark = ""
        self.ul_item_mark = "•"
        self.tag_callback = self._tag_callback  # type: ignore[assignment]
        # Upstream html2text defines tag_callback as None in its type stubs.

    def _tag_callback(  # noqa: PLR0912 # ignored to maintain readability
        self,
        parser: Any,  # html2text callback protocol is untyped upstream
        tag: str,
        attrs: dict[str, str],
        start: bool,
    ) -> bool:
        """
        Custom tag handler that maps HTML elements to Telegram markup.

        Returning `True` signals to html2text that this tag has been
        fully handled and its default processing should be skipped.
        Returning `False` falls through to html2text's default behaviour.
        """
        match tag:
            case itag if itag in _INLINE_TAG_MAP:
                toggle_slash = "" if start else "/"
                parser.o(f"<{toggle_slash}{_INLINE_TAG_MAP[itag]}>")

            case "a":
                if start:
                    href = attrs.get("href", "")
                    if href and href != "#" and not href.startswith("javascript:"):
                        parser.o(f'<a href="{html.escape(href)}">')
                        parser.astack.append(attrs)
                    else:
                        parser.astack.append(None)
                elif parser.astack and parser.astack.pop() is not None:
                    parser.o("</a>")

            case "h1" | "h2" | "h3" | "h4" | "h5" | "h6":
                if start:
                    parser.p()
                    parser.o("<b>")
                else:
                    parser.o("</b>")
                    parser.p()

            case "pre":
                if start:
                    parser.p()
                    parser.o("<pre>")
                    parser.pre = True
                else:
                    parser.o("</pre>")
                    parser.pre = False
                    parser.p()

            case "blockquote":
                # Telegram flattens nested blockquotes; wrap inner ones in <i>
                # to preserve visual hierarchy.
                if start:
                    parser.p()
                    parser.o("<i>")
                else:
                    parser.o("</i>")
                    parser.p()

            case "hr" if start:
                parser.p()
                parser.o(_HORIZONTAL_RULE)
                parser.p()

            case _:
                return False

        return True

    def handle(self, data: str) -> str:
        """Convert sanitised HTML to Telegram-compatible text."""
        text: str = super().handle(data)
        # Resolve any remaining Unicode escape sequences (e.g. \u20ac).
        text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
        text = html.unescape(text).replace("\u00a0", " ")
        text = re.sub(r"\n([ \t]*\n)+", "\n\n", text)

        return text.strip()


# --------------------------------------------------------------------------
# Public interface
# --------------------------------------------------------------------------


def _sanitise_subject(subject: str) -> str:
    """Collapse all whitespace to a single space."""
    return " ".join(subject.split()).strip()


class EmailParser:
    """
    Parses a raw RFC 2822 email string and produces a `ParsedEmail`.

    Formatting for Telegram (truncation, HTML envelope) is also handled
    here because it requires `config.message_max_len`, which is a
    configuration concern, not a property of the parsed data.

    Usage::

        parser = EmailParser(config)
        parsed = parser.parse(raw_email_string)
        telegram_text = parser.format_for_telegram(parsed)
    """

    def __init__(self, config: AppConfig) -> None:
        self._max_len: int = config.message_max_len

    def parse(
        self,
        raw_email: str,
        sender_override: str | None = None,
        subject_override: str | None = None,
    ) -> ParsedEmail:
        """
        Parse `raw_email` and return a `ParsedEmail` dataclass.

        Args:
            raw_email:        Raw RFC 2822 email content.
            sender_override:  Replaces the `From` header value.
            subject_override: Replaces the `Subject` header value.

        Raises:
            ParsingError: If the MIME structure cannot be decoded or the body
                          cannot be extracted.
        """
        if not raw_email.strip():
            raise ParsingError("Received empty email input; nothing to parse.")

        try:
            parser = Parser(EmailMessage, policy=email_default_policy)
            msg: EmailMessage = parser.parsestr(raw_email)
        except Exception as exc:
            raise ParsingError(f"Failed to parse email MIME structure: {exc}") from exc

        sender = sender_override or str(msg.get("From", "")).strip()
        raw_subject = subject_override or str(msg.get("Subject", ""))
        subject = _sanitise_subject(raw_subject)

        has_attachments = any(True for _ in msg.iter_attachments())
        if has_attachments:
            attachment_count = sum(1 for _ in msg.iter_attachments())
            logger.info(
                "Email contains %d attachment(s); content will not be forwarded",
                attachment_count,
            )

        body = self._extract_body(msg)

        return ParsedEmail(
            sender=sender,
            subject=subject,
            body=body,
            has_attachments=has_attachments,
        )

    def _extract_body(self, msg: EmailMessage) -> str:
        """
        Extract and convert the body from the best available MIME part.

        Prefers `text/plain`; falls back to `text/html`. Returns an empty
        string if no body part can be found.

        Raises:
            ParsingError: If the selected body part raises an exception
                          during content extraction or conversion.
        """
        try:
            body_part = msg.get_body(preferencelist=("plain", "html"))
            if body_part is None:
                return ""

            is_html = body_part.get_content_type() == "text/html"
            raw_content: str = body_part.get_content()

            if is_html:
                safe_html = _sanitise_html(raw_content)
                return TelegramHTMLParser().handle(safe_html)

            return html.escape(raw_content)

        except Exception as exc:
            raise ParsingError(f"Failed to extract email body: {exc}") from exc

    def format_for_telegram(self, parsed: ParsedEmail) -> str:
        """
        Wrap a `ParsedEmail` in the Telegram HTML message envelope.

        Applies smart word-boundary truncation to the body if it exceeds
        `config.message_max_len`. The attachment footer, if present, is
        appended *after* truncation so it is always visible.

        Args:
            parsed: A `ParsedEmail` instance from `self.parse()`.

        Returns:
            A fully formatted string ready to be sent to the Telegram API
            with `parse_mode="HTML"`.
        """
        body = parsed.body
        truncated = False

        if len(body) > self._max_len:
            truncate_pos = body[: self._max_len].rfind(" ")
            # If the last space is too far back, hard-cut at the limit.
            if truncate_pos < self._max_len - 100:
                truncate_pos = self._max_len
            body = body[:truncate_pos].rstrip()
            truncated = True

        body += _TRUNCATED_FOOTER if truncated else ""
        body += _ATTACHMENT_FOOTER if parsed.has_attachments else ""

        sender = html.escape(parsed.sender) if parsed.sender else "(unknown sender)"
        subject = html.escape(parsed.subject) if parsed.subject else "(no subject)"
        content = body if body else "<i>(no content)</i>"

        return (
            f"📬 <b>{sender}</b>\n"
            f"<i>{subject}</i>\n\n"
            f"<blockquote expandable>{content}</blockquote>"
        )
