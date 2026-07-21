"""Property-based test for the input length acceptance boundary (task 4.8).

Covers design Property 4 with Hypothesis: across non-blank strings of widely
varied lengths -- including the exact boundaries 1 and 10,000 and the values
just outside them -- the conversation-input gate accepts the input *if and only
if* its character count is within the inclusive range ``1..10,000``.

Whitespace-only rejection is a separate concern (Req 1.6, Property 3), so this
test deliberately uses non-blank content (every character a printable,
non-whitespace code point) to isolate the length boundary. Under that condition
:func:`validate_conversation_input` agrees exactly with the length-only
predicate :func:`is_acceptable_length`.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.input_validation import (
    MAX_INPUT_LENGTH,
    MIN_INPUT_LENGTH,
    InputRejectionReason,
    is_acceptable_length,
    is_blank,
    validate_conversation_input,
)

#: Printable, non-whitespace ASCII code points, so any string drawn from this
#: alphabet is guaranteed non-blank regardless of length.
_NON_WHITESPACE = st.characters(min_codepoint=33, max_codepoint=126)


def _lengths_around_boundaries() -> st.SearchStrategy[int]:
    """Generate string lengths spanning and straddling the accepted range.

    Biases toward the interesting territory: the exact boundaries
    (``MIN_INPUT_LENGTH`` and ``MAX_INPUT_LENGTH``), the values immediately
    inside and outside them, lengths comfortably within range, and lengths well
    beyond the maximum, so acceptance is exercised on both sides of each edge.
    """
    return st.one_of(
        st.just(MIN_INPUT_LENGTH),
        st.just(MIN_INPUT_LENGTH + 1),
        st.just(MAX_INPUT_LENGTH - 1),
        st.just(MAX_INPUT_LENGTH),
        st.just(MAX_INPUT_LENGTH + 1),
        st.integers(min_value=MIN_INPUT_LENGTH, max_value=MAX_INPUT_LENGTH),
        st.integers(min_value=MAX_INPUT_LENGTH + 1, max_value=MAX_INPUT_LENGTH + 500),
    )


def _nonblank_of_length(length: int) -> st.SearchStrategy[str]:
    """Build a non-blank string of exactly ``length`` characters.

    A single non-whitespace character is drawn and repeated to the requested
    length. This reaches the 10,000-character boundary reliably (which
    element-wise text generation cannot) while keeping every character
    non-whitespace, so the string is guaranteed non-blank.
    """
    return _NON_WHITESPACE.map(lambda character: character * length)


_NONBLANK_STRINGS = _lengths_around_boundaries().flatmap(_nonblank_of_length)


# Feature: insightconnect-plugin-builder, Property 4: Input length acceptance boundary
@settings(max_examples=200)
@given(text=_NONBLANK_STRINGS)
def test_nonblank_input_accepted_iff_length_within_inclusive_range(text: str):
    """Non-blank input is accepted iff its length is in ``1..10,000`` inclusive.

    **Validates: Requirements 1.1**
    """
    within_range = MIN_INPUT_LENGTH <= len(text) <= MAX_INPUT_LENGTH

    # The generated content is non-blank, so the length gate is the only gate.
    assert is_blank(text) is False
    assert is_acceptable_length(text) is within_range

    result = validate_conversation_input(text)
    assert result.accepted is within_range

    if within_range:
        assert result.reason is None
        assert result.message is None
    else:
        # Non-blank input over the maximum is rejected specifically as too long.
        assert result.reason is InputRejectionReason.TOO_LONG
        assert result.message
