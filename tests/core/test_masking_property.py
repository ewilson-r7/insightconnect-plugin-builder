"""Property test for the boundary secret-masking routine (task 3.6).

# Feature: insightconnect-plugin-builder, Property 28: Secret masking leaks no plaintext character

The unit tests in ``test_masking.py`` pin specific examples; this module covers
the universal property across generated inputs: wherever a secret value is
displayed, logged, documented, or packaged, the emitted representation is either
absent or fully masked such that no character of the original secret value
appears (the placeholder is a fixed run of ``*`` and reveals nothing about the
secret).

To make "no character of the original secret appears" a meaningful assertion
even when a secret is embedded in surrounding text, the generated surrounding
text is drawn from an alphabet disjoint from the secret's own characters (and
from the placeholder character ``*``). That way any secret character surviving
in the output could only have come from the secret itself -- exactly the leak
the property forbids.
"""

from typing import List

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.masking import (
    MASK_PLACEHOLDER,
    mask_secret,
    redact_secret,
    redact_secrets,
)

# The placeholder is composed solely of this character; secrets and surrounding
# text deliberately exclude it so that its presence is never mistaken for a leak.
_PLACEHOLDER_CHAR = "*"

#: Non-empty secret values drawn from arbitrary printable characters, excluding
#: the placeholder character so a masked result can be checked character-wise.
_secrets = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126, blacklist_characters=_PLACEHOLDER_CHAR),
    min_size=1,
    max_size=40,
)


@st.composite
def _secret_in_text(draw: st.DrawFn):
    """Generate ``(text, secret)`` where ``secret`` is embedded in disjoint filler.

    The surrounding filler cannot contain any character of the secret (nor the
    placeholder character), so after redaction the only way a secret character
    could appear in the output is if the secret itself leaked.
    """
    secret = draw(_secrets)
    forbidden = set(secret) | {_PLACEHOLDER_CHAR}
    filler = st.text(
        alphabet=st.characters(
            min_codepoint=32,
            max_codepoint=126,
            blacklist_characters="".join(forbidden),
        ),
        min_size=0,
        max_size=20,
    )
    # Interleave 1..4 secret occurrences with filler segments on both sides.
    occurrences = draw(st.integers(min_value=1, max_value=4))
    segments = draw(st.lists(filler, min_size=occurrences + 1, max_size=occurrences + 1))
    parts: List[str] = []
    for index, segment in enumerate(segments):
        parts.append(segment)
        if index < occurrences:
            parts.append(secret)
    return "".join(parts), secret, occurrences


@st.composite
def _disjoint_secrets_in_text(draw: st.DrawFn):
    """Generate ``(text, secrets)`` with mutually disjoint secrets and filler.

    Each secret uses a distinct single character (disjoint from the others and
    from the placeholder), guaranteeing that redacting all of them can leave no
    secret character behind unless a leak occurs.
    """
    count = draw(st.integers(min_value=1, max_value=4))
    # Distinct characters, none equal to the placeholder character.
    chars = draw(
        st.lists(
            st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters=_PLACEHOLDER_CHAR),
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    secrets = [c * draw(st.integers(min_value=1, max_value=6)) for c in chars]
    forbidden = set().union(*(set(s) for s in secrets)) | {_PLACEHOLDER_CHAR}
    filler = st.text(
        alphabet=st.characters(
            min_codepoint=32,
            max_codepoint=126,
            blacklist_characters="".join(forbidden),
        ),
        min_size=0,
        max_size=15,
    )
    text = draw(filler)
    for secret in secrets:
        text += secret + draw(filler)
    return text, secrets


@settings(max_examples=200)
@given(secret=st.one_of(st.none(), st.just(""), _secrets))
def test_standalone_secret_is_absent_or_fully_masked(secret):
    """Property 28: a standalone secret masks to absent ("") or the fixed
    placeholder, and no character of the secret survives.

    **Validates: Requirements 14.3, 14.4, 18.3**
    """
    result = mask_secret(secret)

    # Emitted representation is either absent or the fixed placeholder.
    assert result in ("", MASK_PLACEHOLDER)

    if secret:
        # A present secret is fully masked (never absent, never partial).
        assert result == MASK_PLACEHOLDER
        # No character of the original secret appears in the output.
        assert not any(ch in result for ch in secret)
    else:
        # No secret to mask -> absent.
        assert result == ""


@settings(max_examples=200)
@given(data=_secret_in_text())
def test_embedded_secret_leaves_no_plaintext_character(data):
    """Property 28: redacting a secret embedded in surrounding text leaves no
    character of the secret in the output.

    **Validates: Requirements 14.3, 14.4, 18.3**
    """
    text, secret, occurrences = data
    redacted = redact_secret(text, secret)

    # The plaintext secret does not survive as a substring.
    assert secret not in redacted
    # No character of the secret appears anywhere (filler is disjoint).
    assert not any(ch in redacted for ch in secret)
    # Every occurrence was replaced by the placeholder.
    assert redacted.count(MASK_PLACEHOLDER) == occurrences


@settings(max_examples=200)
@given(data=_disjoint_secrets_in_text())
def test_multiple_secrets_leave_no_plaintext_character(data):
    """Property 28: redacting several secrets from text leaves no character of
    any secret in the output.

    **Validates: Requirements 14.3, 14.4, 18.3**
    """
    text, secrets = data
    redacted = redact_secrets(text, secrets)

    for secret in secrets:
        assert secret not in redacted
        assert not any(ch in redacted for ch in secret)
