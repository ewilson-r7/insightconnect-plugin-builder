"""Property-based test for error-output truncation (task 3.12).

Covers design Property 36 with Hypothesis: across error outputs of every
length (below, at, and above the display limit), the displayed portion is
exactly the first ``limit`` characters when the output exceeds the limit
(with the complete output still retained), and equals the full output
otherwise. This complements the example-based cases in ``test_truncation.py``.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.truncation import (
    MAX_DISPLAY_CHARS,
    truncate_error_output,
)


def _sized_output(body: str, fill: str, length: int) -> str:
    """Build a string of exactly ``length`` chars seeded by ``body``.

    Long outputs cannot be generated as raw random text (Hypothesis bounds
    string size), so we take a short random ``body`` for content variety and
    pad it out to ``length`` with a random ``fill`` character. The result keeps
    varied leading content while reaching arbitrary lengths.
    """
    if length == 0:
        return ""
    seed = (body or fill)[:length]
    return seed + fill * (length - len(seed))


def error_outputs() -> st.SearchStrategy[str]:
    """Generate error outputs spanning the boundary at ``MAX_DISPLAY_CHARS``.

    Covers well below the limit, the exact boundary and its immediate
    neighbours, and comfortably above the limit, so the truncation predicate
    ``len(output) > limit`` is exercised on both sides and at the edge.
    """
    body = st.text(max_size=200)
    fill = st.characters()
    lengths = st.one_of(
        st.integers(min_value=0, max_value=200),
        st.integers(min_value=MAX_DISPLAY_CHARS - 2, max_value=MAX_DISPLAY_CHARS + 2),
        st.integers(min_value=MAX_DISPLAY_CHARS + 1, max_value=MAX_DISPLAY_CHARS + 5000),
    )
    return st.builds(_sized_output, body, fill, lengths)


# Feature: insightconnect-plugin-builder, Property 36: Error output truncation preserves full access
@settings(max_examples=100)
@given(output=error_outputs())
def test_error_output_truncation_preserves_full_access(output: str):
    """Displayed is the first ``limit`` chars when over limit, else the full output.

    In every case the complete output is retained in ``full`` so no error text
    is ever lost.

    **Validates: Requirements 19.5, 19.1**
    """
    result = truncate_error_output(output)

    # Full output is always retained verbatim.
    assert result.full == output

    if len(output) > MAX_DISPLAY_CHARS:
        assert result.truncated is True
        assert result.displayed == output[:MAX_DISPLAY_CHARS]
        assert len(result.displayed) == MAX_DISPLAY_CHARS
        assert result.full.startswith(result.displayed)
    else:
        assert result.truncated is False
        assert result.displayed == output
