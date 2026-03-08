"""
Tests for telegram_sendmail.parser.

Coverage targets
----------------
`_HTMLSanitizer` / `_sanitise_html`
    - Preserves safe inline tags (b, strong, i, em, u, s, code, kbd, tt) in output
    - Preserves safe block-level and structural tags (h1-h6, pre, blockquote, hr, a, p, ul, li)
    - Strips script, style, iframe, object, embed, svg, math, noscript tag content entirely
    - Drops void dangerous elements (input, meta, link) without corrupting surrounding text
    - Correctly tracks suppression depth for arbitrarily nested dangerous tags
    - Discards javascript: href attributes at the sanitizer level (anchor passes through)
    - Preserves surrounding plain text when dangerous and safe tags are interleaved

`_sanitise_subject`
    - Collapses leading, trailing, and internal whitespace sequences to a single space
    - Handles RFC 2822 folded Subject headers with embedded newlines and indentation

`EmailParser.parse`
    - Raises ParsingError on whitespace-only or empty input
    - Extracts From and Subject headers into ParsedEmail fields correctly
    - Applies sender_override to supersede the From header
    - Sets has_attachments=True for multipart/mixed emails containing a binary attachment
    - Sets has_attachments=False for plain single-part emails
    - Logs attachment count at INFO level when attachments are detected

`EmailParser._extract_body`
    - Prefers text/plain when a multipart/alternative email contains both part types
    - Falls back to HTML conversion when no text/plain part exists
    - Returns an empty string when no inline body part is present (attachment-only MIME)
    - HTML-escapes angle brackets and ampersands in plain-text bodies

`EmailParser.format_for_telegram`
    - Does not truncate a body whose length is exactly at the configured limit
    - Appends a truncation notice when the body exceeds the limit by a single character
    - Truncates at the nearest word boundary when a space exists within 100 chars of the cutoff
    - Falls back to a hard character cut when no word boundary exists within the 100-char window
    - Appends the attachment footer when has_attachments is True
    - Omits the attachment footer for emails with no attachments
    - Renders the unknown-sender fallback markup when sender is an empty string
    - Renders the no-subject fallback markup when subject is an empty string
    - Renders the no-content fallback markup when the body is an empty string
    - HTML-escapes angle brackets in the sender field
    - HTML-escapes angle brackets in the subject field
    - Wraps the body in an expandable blockquote with the Telegram envelope header

Design notes
------------
- `_sanitise_html` is imported directly to enable exhaustive whitelist/blacklist
  testing of the sanitizer in isolation, independent of the full MIME parse pipeline.
- The `test_returns_empty_string_when_no_body_part` test uses an attachment-only
  multipart email to reach the `body_part is None` branch in `_extract_body`; a
  plain email with an empty body would still have a text/plain part.
"""

from __future__ import annotations

import pytest

from telegram_sendmail.config import AppConfig
from telegram_sendmail.exceptions import ParsingError
from telegram_sendmail.parser import (
    EmailParser,
    ParsedEmail,
    _sanitise_html,
)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _make_parsed(
    *,
    body: str = "body text",
    sender: str = "user@example.com",
    subject: str = "Test Subject",
    has_attachments: bool = False,
) -> ParsedEmail:
    """Return a ParsedEmail with controlled fields for formatting assertions."""
    return ParsedEmail(
        sender=sender,
        subject=subject,
        body=body,
        has_attachments=has_attachments,
    )


# --------------------------------------------------------------------------
# HTML Sanitizer — safe tag whitelist
# --------------------------------------------------------------------------


class TestHTMLSanitizerSafeTags:
    def test_preserves_bold_tag(self):
        result = _sanitise_html("<b>important</b>")
        assert "<b>important</b>" in result

    def test_preserves_italic_tag(self):
        result = _sanitise_html("<i>slanted</i>")
        assert "<i>slanted</i>" in result

    def test_preserves_underline_tag(self):
        result = _sanitise_html("<u>underlined</u>")
        assert "<u>underlined</u>" in result

    def test_preserves_strikethrough_tag(self):
        result = _sanitise_html("<s>struck</s>")
        assert "<s>struck</s>" in result

    def test_preserves_code_tag(self):
        result = _sanitise_html("<code>snippet</code>")
        assert "<code>snippet</code>" in result

    def test_preserves_strong_tag(self):
        # <strong> is an alias for <b> in Telegram; sanitizer passes it through.
        result = _sanitise_html("<strong>bold text</strong>")
        assert "bold text" in result
        assert "<strong>" in result

    def test_preserves_em_tag(self):
        result = _sanitise_html("<em>emphasis</em>")
        assert "emphasis" in result

    def test_preserves_anchor_with_safe_https_href(self):
        result = _sanitise_html('<a href="https://example.com">link text</a>')
        assert "https://example.com" in result
        assert "link text" in result

    def test_preserves_preformatted_tag(self):
        result = _sanitise_html("<pre>raw block</pre>")
        assert "raw block" in result
        assert "<pre>" in result

    def test_preserves_blockquote_tag(self):
        result = _sanitise_html("<blockquote>quoted</blockquote>")
        assert "quoted" in result

    def test_preserves_paragraph_tag(self):
        result = _sanitise_html("<p>paragraph content</p>")
        assert "paragraph content" in result

    def test_preserves_heading_tag(self):
        result = _sanitise_html("<h1>Title</h1>")
        assert "Title" in result

    def test_preserves_list_item_content(self):
        result = _sanitise_html("<ul><li>item one</li></ul>")
        assert "item one" in result

    def test_preserves_surrounding_text_around_safe_tags(self):
        result = _sanitise_html("prefix <b>bold</b> suffix")
        assert "prefix" in result
        assert "suffix" in result


# --------------------------------------------------------------------------
# HTML Sanitizer — dangerous tag blacklist (content suppression)
# --------------------------------------------------------------------------


class TestHTMLSanitizerDangerousTags:
    def test_strips_script_content(self):
        result = _sanitise_html("<script>alert('xss')</script>safe text")
        assert "alert" not in result
        assert "xss" not in result
        assert "safe text" in result

    def test_strips_style_content(self):
        result = _sanitise_html("<style>body { display: none; }</style>visible")
        assert "display" not in result
        assert "none" not in result
        assert "visible" in result

    def test_strips_iframe_content(self):
        result = _sanitise_html('<iframe src="https://evil.com">fallback</iframe>after')
        assert "fallback" not in result
        assert "after" in result

    def test_strips_object_content(self):
        result = _sanitise_html("<object>payload data</object>clean")
        assert "payload data" not in result
        assert "clean" in result

    def test_strips_embed_content(self):
        result = _sanitise_html("<embed>hidden content</embed>visible")
        assert "hidden content" not in result
        assert "visible" in result

    def test_strips_svg_and_subtree(self):
        result = _sanitise_html("<svg><path d='M0 0'/><text>label</text></svg>after")
        assert "<svg>" not in result
        assert "label" not in result
        assert "after" in result

    def test_strips_math_content(self):
        result = _sanitise_html("<math><mi>x</mi></math>result")
        assert "<math>" not in result
        assert "result" in result

    def test_strips_noscript_content(self):
        result = _sanitise_html("<noscript>fallback content</noscript>main content")
        assert "fallback content" not in result
        assert "main content" in result

    def test_nested_dangerous_tags_fully_suppressed(self):
        # Depth counter must handle a <script> tag nested inside another <script>.
        result = _sanitise_html(
            "<script><script>inner('xss')</script></script>clean output"
        )
        assert "inner" not in result
        assert "xss" not in result
        assert "clean output" in result

    def test_dangerous_tag_nested_inside_safe_tag_is_suppressed(self):
        # A <script> inside a <p> must still be removed.
        result = _sanitise_html("<p>text<script>bad()</script>more</p>")
        assert "bad()" not in result
        assert "text" in result
        assert "more" in result

    def test_drops_void_input_element(self):
        result = _sanitise_html('<input type="hidden" value="secret">visible text')
        assert "secret" not in result
        assert "<input" not in result
        assert "visible text" in result

    def test_drops_void_meta_element(self):
        result = _sanitise_html('<meta charset="utf-8">content')
        assert "<meta" not in result
        assert "content" in result

    def test_drops_void_link_element(self):
        result = _sanitise_html('<link rel="stylesheet" href="x.css">after')
        assert "<link" not in result
        assert "after" in result

    def test_javascript_href_anchor_text_preserved(self):
        # The sanitizer passes the anchor tag through; TelegramHTMLParser drops the href.
        # At minimum the sanitizer must not corrupt the text node.
        result = _sanitise_html('<a href="javascript:void(0)">click me</a>')
        assert "click me" in result

    def test_interleaved_safe_and_dangerous_tags(self):
        result = _sanitise_html("start<script>bad</script>middle<b>end</b>tail")
        assert "start" in result
        assert "bad" not in result
        assert "middle" in result
        assert "end" in result
        assert "tail" in result


# --------------------------------------------------------------------------
# HTML Sanitizer — full pipeline (via EmailParser)
# --------------------------------------------------------------------------


class TestHTMLSanitizerFullPipeline:
    def test_html_email_strips_script_content(
        self, html_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(html_raw_email)
        assert "xss attempt" not in parsed.body
        assert "nested" not in parsed.body

    def test_html_email_strips_style_content(
        self, html_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(html_raw_email)
        assert "display: none" not in parsed.body

    def test_html_email_strips_iframe_fallback_text(
        self, html_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(html_raw_email)
        assert "fallback" not in parsed.body

    def test_html_email_preserves_heading_content(
        self, html_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(html_raw_email)
        assert "CRITICAL" in parsed.body

    def test_html_email_preserves_safe_anchor_href(
        self, html_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(html_raw_email)
        assert "grafana.example.com" in parsed.body

    def test_html_email_discards_javascript_href(
        self, html_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(html_raw_email)
        assert "javascript:" not in parsed.body


# --------------------------------------------------------------------------
# Subject sanitization
# --------------------------------------------------------------------------


class TestSanitiseSubject:
    def test_collapses_internal_whitespace_runs(self, app_config: AppConfig):
        raw = "From: x\nSubject: one   two   three\n\nbody"
        parsed = EmailParser(app_config).parse(raw)
        assert parsed.subject == "one two three"

    def test_strips_leading_and_trailing_whitespace(self, app_config: AppConfig):
        raw = "From: x\nSubject:    padded   \n\nbody"
        parsed = EmailParser(app_config).parse(raw)
        assert parsed.subject == "padded"

    def test_folded_header_collapses_to_single_space(
        self, encoded_subject_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(encoded_subject_email)
        # RFC 2822 folded headers include embedded newlines; all whitespace runs collapse.
        assert "  " not in parsed.subject
        assert parsed.subject == parsed.subject.strip()
        assert len(parsed.subject) > 0


# --------------------------------------------------------------------------
# EmailParser.parse — MIME extraction and header handling
# --------------------------------------------------------------------------


class TestEmailParserParse:
    def test_raises_parsing_error_on_empty_input(
        self, empty_raw_email: str, app_config: AppConfig
    ):
        with pytest.raises(ParsingError, match="empty"):
            EmailParser(app_config).parse(empty_raw_email)

    def test_extracts_sender_from_from_header(
        self, plain_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(plain_raw_email)
        assert "cron@hostname.local" in parsed.sender

    def test_extracts_subject_from_subject_header(
        self, plain_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(plain_raw_email)
        assert parsed.subject == "Daily Backup Complete"

    def test_sender_override_supersedes_from_header(
        self, plain_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(
            plain_raw_email, sender_override="override@example.com"
        )
        assert parsed.sender == "override@example.com"
        assert "cron@hostname.local" not in parsed.sender

    def test_plain_email_has_no_attachments(
        self, plain_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(plain_raw_email)
        assert parsed.has_attachments is False

    def test_multipart_email_sets_has_attachments_true(
        self, multipart_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(multipart_raw_email)
        assert parsed.has_attachments is True

    def test_attachment_detection_logged_at_info(
        self,
        multipart_raw_email: str,
        app_config: AppConfig,
        caplog: pytest.LogCaptureFixture,
    ):
        import logging

        with caplog.at_level(logging.INFO, logger="telegram_sendmail.parser"):
            EmailParser(app_config).parse(multipart_raw_email)
        assert any("attachment" in r.message.lower() for r in caplog.records)

    def test_body_contains_plain_text_content(
        self, plain_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(plain_raw_email)
        assert "Backup completed successfully" in parsed.body


# --------------------------------------------------------------------------
# EmailParser._extract_body — MIME part selection and conversion
# --------------------------------------------------------------------------


class TestEmailParserExtractBody:
    def test_plain_body_escapes_angle_brackets(self, app_config: AppConfig):
        raw = "From: x\nSubject: s\nContent-Type: text/plain\n\n<b>not bold</b>"
        parsed = EmailParser(app_config).parse(raw)
        assert "&lt;b&gt;" in parsed.body
        assert "<b>" not in parsed.body

    def test_plain_body_escapes_ampersand(self, app_config: AppConfig):
        raw = "From: x\nSubject: s\nContent-Type: text/plain\n\nA & B"
        parsed = EmailParser(app_config).parse(raw)
        assert "&amp;" in parsed.body

    def test_multipart_body_contains_plain_text_part(
        self, multipart_raw_email: str, app_config: AppConfig
    ):
        # multipart/mixed has text/plain as first inline part.
        parsed = EmailParser(app_config).parse(multipart_raw_email)
        assert "Weekly report attached" in parsed.body

    def test_returns_empty_string_when_no_inline_body_part(self, app_config: AppConfig):
        # An attachment-only multipart email has no text/plain or text/html part,
        # so get_body() returns None and _extract_body maps that to "".
        raw = (
            "From: x\nSubject: s\n"
            "MIME-Version: 1.0\n"
            'Content-Type: multipart/mixed; boundary="b"\n\n'
            "--b\n"
            "Content-Type: application/octet-stream\n"
            'Content-Disposition: attachment; filename="data.bin"\n\n'
            "binarydata\n"
            "--b--\n"
        )
        parsed = EmailParser(app_config).parse(raw)
        assert parsed.body == ""

    def test_html_email_body_is_non_empty_string(
        self, html_raw_email: str, app_config: AppConfig
    ):
        parsed = EmailParser(app_config).parse(html_raw_email)
        assert isinstance(parsed.body, str)
        assert len(parsed.body) > 0


# --------------------------------------------------------------------------
# EmailParser.format_for_telegram — envelope, truncation, and footers
# --------------------------------------------------------------------------


class TestFormatForTelegram:
    def test_no_truncation_when_body_is_exactly_at_limit(self, app_config: AppConfig):
        body = "a" * app_config.message_max_len
        result = EmailParser(app_config).format_for_telegram(_make_parsed(body=body))
        assert "truncated" not in result

    def test_truncation_triggered_one_character_over_limit(self, app_config: AppConfig):
        body = "a" * (app_config.message_max_len + 1)
        result = EmailParser(app_config).format_for_telegram(_make_parsed(body=body))
        assert "truncated" in result

    def test_truncation_at_nearest_word_boundary(self, app_config: AppConfig):
        # Place a space 10 characters before the limit so a clear word boundary exists.
        # The trailing 50 'y' chars fall beyond the word boundary and must be dropped.
        body = "x" * (app_config.message_max_len - 10) + " " + "y" * 50
        result = EmailParser(app_config).format_for_telegram(_make_parsed(body=body))
        assert "y" * 50 not in result
        assert "truncated" in result

    def test_hard_cut_when_no_word_boundary_within_window(self, app_config: AppConfig):
        # A body with no spaces forces rfind to return -1, which is < limit-100,
        # so the code falls back to a hard cut at the exact character limit.
        body = "z" * (app_config.message_max_len + 50)
        result = EmailParser(app_config).format_for_telegram(_make_parsed(body=body))
        assert "truncated" in result

    def test_attachment_footer_present_when_has_attachments_true(
        self, app_config: AppConfig
    ):
        result = EmailParser(app_config).format_for_telegram(
            _make_parsed(has_attachments=True)
        )
        # The footer uses the paperclip emoji as a stable anchor.
        assert "📎" in result

    def test_attachment_footer_absent_when_has_attachments_false(
        self, app_config: AppConfig
    ):
        result = EmailParser(app_config).format_for_telegram(
            _make_parsed(has_attachments=False)
        )
        assert "📎" not in result

    def test_unknown_sender_fallback_for_empty_sender(self, app_config: AppConfig):
        result = EmailParser(app_config).format_for_telegram(_make_parsed(sender=""))
        assert "unknown sender" in result

    def test_no_subject_fallback_for_empty_subject(self, app_config: AppConfig):
        result = EmailParser(app_config).format_for_telegram(_make_parsed(subject=""))
        assert "no subject" in result

    def test_no_content_fallback_for_empty_body(self, app_config: AppConfig):
        result = EmailParser(app_config).format_for_telegram(_make_parsed(body=""))
        assert "no content" in result

    def test_sender_angle_brackets_are_html_escaped(self, app_config: AppConfig):
        result = EmailParser(app_config).format_for_telegram(
            _make_parsed(sender="<evil>@example.com")
        )
        assert "<evil>" not in result
        assert "&lt;evil&gt;" in result

    def test_subject_special_characters_are_html_escaped(self, app_config: AppConfig):
        result = EmailParser(app_config).format_for_telegram(
            _make_parsed(subject="CPU > 90% & rising")
        )
        assert "CPU > 90%" not in result
        assert "&gt;" in result
        assert "&amp;" in result

    def test_output_contains_telegram_envelope_header(self, app_config: AppConfig):
        result = EmailParser(app_config).format_for_telegram(_make_parsed())
        assert "📬 New Notification" in result
        assert "<b>From:</b>" in result
        assert "<b>Subject:</b>" in result

    def test_output_wraps_body_in_expandable_blockquote(self, app_config: AppConfig):
        result = EmailParser(app_config).format_for_telegram(_make_parsed())
        assert "<blockquote expandable>" in result
        assert "</blockquote>" in result

    def test_truncation_and_attachment_footer_both_present(self, app_config: AppConfig):
        # Attachment footer must appear even when the body is also truncated.
        body = "a" * (app_config.message_max_len + 1)
        result = EmailParser(app_config).format_for_telegram(
            _make_parsed(body=body, has_attachments=True)
        )
        assert "truncated" in result
        assert "📎" in result
