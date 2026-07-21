"""Property-based test for the custom vendor-suffix operation (task 3.2).

Covers design Property 26 with Hypothesis: across arbitrary vendor inputs the
result always ends in the literal ``_custom`` and the operation is idempotent
(``f(f(x)) == f(x)``).
"""

from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.vendor import (
    CUSTOM_VENDOR_SUFFIX,
    apply_custom_vendor_suffix,
)


def vendor_inputs() -> st.SearchStrategy[Optional[str]]:
    """Generate arbitrary vendor values across every relevant shape.

    Includes ``None`` and empty strings (unset vendor), plain free text, values
    already ending in an exact ``_custom``, and mixed casings such as
    ``_Custom``/``_CUSTOM`` that must not be treated as the exact suffix.
    """
    text = st.text(max_size=40)
    return st.one_of(
        st.none(),
        text,
        text.map(lambda v: v + CUSTOM_VENDOR_SUFFIX),
        text.map(lambda v: v + "_Custom"),
        text.map(lambda v: v + "_CUSTOM"),
    )


# Feature: insightconnect-plugin-builder, Property 26: Custom vendor suffix is idempotent
@settings(max_examples=200)
@given(vendor=vendor_inputs())
def test_custom_vendor_suffix_is_idempotent(vendor: Optional[str]):
    """Result ends in ``_custom`` and applying twice equals applying once.

    **Validates: Requirements 13.1, 13.2, 13.3**
    """
    once = apply_custom_vendor_suffix(vendor)
    assert once.endswith(CUSTOM_VENDOR_SUFFIX)

    twice = apply_custom_vendor_suffix(once)
    assert twice == once
