"""Unit tests for the boundary secret-masking routine (task 3.5; Req 14.4).

These cover specific examples and edge cases: full masking of a standalone
secret regardless of length, absence of empty/null secrets, and redaction of
secret occurrences embedded in larger text (logs/docs). The universal
"no plaintext character leaks" property is covered separately by the property
test (task 3.6, Property 28).
"""

import pytest

from icplugin_builder.core.masking import (
    MASK_PLACEHOLDER,
    mask_secret,
    redact_secret,
    redact_secrets,
)


class TestMaskSecret:
    def test_masks_to_fixed_placeholder(self):
        # Req 14.4: every character replaced by a fixed placeholder.
        assert mask_secret("super-secret-api-key") == MASK_PLACEHOLDER

    def test_placeholder_is_constant_regardless_of_length(self):
        # Length is not leaked: short and long secrets mask identically.
        assert mask_secret("a") == mask_secret("a" * 1000) == MASK_PLACEHOLDER

    @pytest.mark.parametrize("secret", ["", None])
    def test_empty_or_null_is_absent(self, secret):
        # No secret to mask -> absent (empty string), not a placeholder.
        assert mask_secret(secret) == ""

    def test_no_original_character_survives(self):
        secret = "Hunter2!"
        masked = mask_secret(secret)
        assert not any(ch in masked for ch in secret)


class TestRedactSecret:
    def test_replaces_single_occurrence(self):
        text = "Authorization: Bearer abc123"
        assert redact_secret(text, "abc123") == f"Authorization: Bearer {MASK_PLACEHOLDER}"

    def test_replaces_all_occurrences(self):
        text = "key=abc123 retry with abc123"
        redacted = redact_secret(text, "abc123")
        assert "abc123" not in redacted
        assert redacted.count(MASK_PLACEHOLDER) == 2

    @pytest.mark.parametrize("secret", ["", None])
    def test_empty_or_null_secret_leaves_text_unchanged(self, secret):
        text = "nothing to redact here"
        assert redact_secret(text, secret) == text

    def test_text_without_secret_unchanged(self):
        assert redact_secret("plain log line", "unmatched") == "plain log line"


class TestRedactSecrets:
    def test_redacts_multiple_secrets(self):
        text = "user=alice token=t0ken key=k3y"
        redacted = redact_secrets(text, ["t0ken", "k3y"])
        assert "t0ken" not in redacted
        assert "k3y" not in redacted
        assert redacted.count(MASK_PLACEHOLDER) == 2

    def test_ignores_empty_and_null_entries(self):
        text = "token=t0ken"
        redacted = redact_secrets(text, ["", None, "t0ken"])
        assert redacted == "token=" + MASK_PLACEHOLDER

    def test_overlapping_secret_substring_leaves_no_fragment(self):
        # A shorter secret that is a substring of a longer one must not leave a
        # partial fragment of the longer secret behind (longest-first redaction).
        text = "value=abcdef"
        redacted = redact_secrets(text, ["abc", "abcdef"])
        assert "abc" not in redacted
        assert "abcdef" not in redacted
        assert redacted == "value=" + MASK_PLACEHOLDER
