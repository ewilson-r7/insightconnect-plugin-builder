"""Property-based test for empty/whitespace input rejection (task 4.9).

Covers design Property 3 with Hypothesis: across strings that are empty or
contain only whitespace -- built from spaces, tabs, newlines, carriage returns,
and arbitrary mixes of them at any length -- the conversation-input gate rejects
the submission, reports :attr:`InputRejectionReason.EMPTY_OR_WHITESPACE` with a
non-``None`` user-facing message, and (being a pure function) mutates nothing.

The length boundary is a separate concern (Req 1.1, Property 4), so this test
deliberately isolates blank content. Because
:func:`validate_conversation_input` performs no I/O and holds no draft state,
the "draft unchanged" guarantee of Req 1.6 is expressed here as *not acting* on
a rejected result: the caller leaves the draft untouched when ``accepted`` is
``False``, and there is no draft mutation to undo.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.input_validation import (
    InputRejectionReason,
    is_blank,
    validate_conversation_input,
)

#: The whitespace code points the property must cover: space, tab, newline, and
#: carriage return. Any string composed solely of these (or empty) is blank.
_WHITESPACE_CHARS = " \t\n\r"


def _blank_strings() -> st.SearchStrategy[str]:
    """Generate empty and whitespace-only strings of any length.

    Combines the empty string with strings drawn exclusively from spaces, tabs,
    newlines, and carriage returns in arbitrary length and arbitrary mixes, so
    every generated value is guaranteed blank.
    """
    whitespace_only = st.text(alphabet=_WHITESPACE_CHARS, min_size=1, max_size=200)
    return st.one_of(st.just(""), whitespace_only)


# Feature: insightconnect-plugin-builder, Property 3: Empty/whitespace input rejection
@settings(max_examples=200)
@given(text=_blank_strings())
def test_empty_or_whitespace_input_is_rejected(text: str):
    """Empty/whitespace-only input is rejected, leaving the draft unchanged.

    **Validates: Requirements 1.6**
    """
    # Every generated value is blank, independent of length or whitespace mix.
    assert is_blank(text) is True

    result = validate_conversation_input(text)

    # Rejected specifically as empty/whitespace, with a user-facing message.
    assert result.accepted is False
    assert result.reason is InputRejectionReason.EMPTY_OR_WHITESPACE
    assert result.message is not None
    assert result.message != ""

    # Validation is pure: a rejected result carries no accepted payload, so the
    # caller does not act on it and the current draft is left unchanged (Req 1.6).
