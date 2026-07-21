"""Unit tests for conversation input validation (task 4.7; Req 1.1, 1.6).

These cover specific examples and the length boundary: input at, just below, and
just above the 1..10,000 range; empty and whitespace-only rejection; and the
distinct rejection reasons. The universal properties are covered separately by
the property tests (task 4.8, Property 4; task 4.9, Property 3).
"""

import pytest

from icplugin_builder.core.input_validation import (
    InputRejectionReason,
    MAX_INPUT_LENGTH,
    MIN_INPUT_LENGTH,
    is_acceptable_length,
    is_blank,
    validate_conversation_input,
)


class TestIsAcceptableLength:
    def test_single_character_is_acceptable(self):
        assert is_acceptable_length("x") is True

    def test_empty_string_is_not_acceptable(self):
        assert is_acceptable_length("") is False

    def test_max_length_is_acceptable(self):
        assert is_acceptable_length("x" * MAX_INPUT_LENGTH) is True

    def test_one_over_max_is_not_acceptable(self):
        assert is_acceptable_length("x" * (MAX_INPUT_LENGTH + 1)) is False

    def test_min_length_boundary(self):
        assert is_acceptable_length("x" * MIN_INPUT_LENGTH) is True


class TestIsBlank:
    def test_empty_string_is_blank(self):
        assert is_blank("") is True

    @pytest.mark.parametrize("text", [" ", "   ", "\t", "\n", "\r\n", " \t \n "])
    def test_whitespace_only_is_blank(self, text):
        assert is_blank(text) is True

    def test_text_with_content_is_not_blank(self):
        assert is_blank("hello") is False

    def test_surrounded_content_is_not_blank(self):
        assert is_blank("  hello  ") is False


class TestValidateConversationInput:
    def test_accepts_normal_description(self):
        result = validate_conversation_input("Build a Slack plugin")
        assert result.accepted is True
        assert result.reason is None
        assert result.message is None

    def test_accepts_single_non_whitespace_character(self):
        # Req 1.1: length 1 is accepted (Req 1.2 needs >= 1 non-whitespace char).
        result = validate_conversation_input("x")
        assert result.accepted is True

    def test_accepts_at_max_length(self):
        result = validate_conversation_input("a" * MAX_INPUT_LENGTH)
        assert result.accepted is True

    def test_rejects_empty_input(self):
        # Req 1.6: empty input rejected; the caller leaves the draft unchanged.
        result = validate_conversation_input("")
        assert result.accepted is False
        assert result.reason is InputRejectionReason.EMPTY_OR_WHITESPACE
        assert result.message

    @pytest.mark.parametrize("text", [" ", "   ", "\t\n", " \t \n "])
    def test_rejects_whitespace_only_input(self, text):
        # Req 1.6: whitespace-only input rejected even when length is in range.
        result = validate_conversation_input(text)
        assert result.accepted is False
        assert result.reason is InputRejectionReason.EMPTY_OR_WHITESPACE

    def test_rejects_input_over_max_length(self):
        # Req 1.1: input longer than 10,000 characters is rejected.
        result = validate_conversation_input("a" * (MAX_INPUT_LENGTH + 1))
        assert result.accepted is False
        assert result.reason is InputRejectionReason.TOO_LONG
        assert result.message

    def test_whitespace_only_over_max_length_rejected_as_empty(self):
        # Blank check precedes length: whitespace-only reports empty, not too-long.
        result = validate_conversation_input(" " * (MAX_INPUT_LENGTH + 1))
        assert result.accepted is False
        assert result.reason is InputRejectionReason.EMPTY_OR_WHITESPACE
