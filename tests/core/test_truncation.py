"""Unit tests for the error-output truncation utility (task 3.11; Req 19.1, 19.5).

These cover specific examples and the 10,000-character boundary: outputs at, just
below, and just above the limit; the empty-output case; and preservation of full
access when truncated. The universal property is covered separately by the
property test (task 3.12, Property 36).
"""

import pytest

from icplugin_builder.core.truncation import (
    MAX_DISPLAY_CHARS,
    TruncatedOutput,
    truncate_error_output,
)


class TestTruncateErrorOutput:
    def test_short_output_returned_in_full(self):
        result = truncate_error_output("boom")
        assert result == TruncatedOutput(displayed="boom", full="boom", truncated=False)

    def test_empty_output_returned_in_full(self):
        result = truncate_error_output("")
        assert result.displayed == ""
        assert result.full == ""
        assert result.truncated is False

    def test_output_at_limit_is_not_truncated(self):
        # Exactly MAX_DISPLAY_CHARS characters: displayed in full, not truncated.
        output = "x" * MAX_DISPLAY_CHARS
        result = truncate_error_output(output)
        assert result.truncated is False
        assert result.displayed == output
        assert result.full == output

    def test_output_one_below_limit_is_not_truncated(self):
        output = "x" * (MAX_DISPLAY_CHARS - 1)
        result = truncate_error_output(output)
        assert result.truncated is False
        assert result.displayed == output

    def test_output_one_over_limit_is_truncated(self):
        # Req 19.5: exceeding the limit shows exactly the first 10,000 chars.
        output = "x" * (MAX_DISPLAY_CHARS + 1)
        result = truncate_error_output(output)
        assert result.truncated is True
        assert len(result.displayed) == MAX_DISPLAY_CHARS
        assert result.displayed == output[:MAX_DISPLAY_CHARS]

    def test_truncated_full_output_remains_accessible(self):
        # Req 19.5: the complete output stays available through the handle.
        output = "a" * MAX_DISPLAY_CHARS + "b" * 500
        result = truncate_error_output(output)
        assert result.truncated is True
        assert result.full == output
        assert len(result.full) == MAX_DISPLAY_CHARS + 500
        assert result.omitted_char_count == 500

    def test_displayed_is_prefix_of_full_when_truncated(self):
        output = "0123456789" * 1500  # 15,000 chars
        result = truncate_error_output(output)
        assert result.truncated is True
        assert result.full.startswith(result.displayed)
        assert result.omitted_char_count == len(output) - MAX_DISPLAY_CHARS

    def test_custom_limit_honored(self):
        result = truncate_error_output("abcdef", limit=3)
        assert result.truncated is True
        assert result.displayed == "abc"
        assert result.full == "abcdef"

    def test_zero_limit_truncates_all_nonempty(self):
        result = truncate_error_output("x", limit=0)
        assert result.truncated is True
        assert result.displayed == ""
        assert result.full == "x"

    def test_negative_limit_rejected(self):
        with pytest.raises(ValueError):
            truncate_error_output("x", limit=-1)
