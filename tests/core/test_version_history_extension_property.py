"""Property test for version-history extension on a version bump (task 2.8).

# Feature: insightconnect-plugin-builder, Property 25: Version bump extends version_history

The unit tests in ``test_version_bump.py`` pin specific examples for
:func:`apply_version_bump`; this module covers the universal property across
generated specs and bumps: applying a bump yields a spec whose
``version_history`` has exactly one more entry than the input, that new entry
references the new version, and the previous -> new transition is exposed for
display before the build begins.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.version_bump import (
    VERSION_HISTORY_KEY,
    apply_version_bump,
    bump_version,
)
from tests import strategies as strat


def _history_entries() -> st.SearchStrategy:
    """Generate a list of pre-existing ``version_history`` entry strings."""
    entry = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=30)
    return st.lists(entry, max_size=5)


@settings(max_examples=200)
@given(
    spec=strat.plugin_specs(),
    priors=st.lists(strat.semvers(), min_size=0, max_size=6),
    is_breaking=st.booleans(),
    existing_history=st.one_of(st.none(), _history_entries()),
    description=st.one_of(st.none(), st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40)),
)
def test_version_bump_extends_version_history(spec, priors, is_breaking, existing_history, description):
    """Property 25: applying a bump adds exactly one ``version_history`` entry
    that references the new version, and exposes previous -> new for display.

    **Validates: Requirements 12.6**
    """
    # Optionally seed the draft with pre-existing history to prove the extension
    # is relative to whatever the spec already carries (including no key at all).
    if existing_history is not None:
        spec.extra[VERSION_HISTORY_KEY] = list(existing_history)
    before = len(spec.extra.get(VERSION_HISTORY_KEY, []))

    bump = bump_version(spec.version, priors, is_breaking=is_breaking)
    result = apply_version_bump(spec, bump, description=description)

    history = result.spec.extra[VERSION_HISTORY_KEY]

    # Exactly one additional entry, placed first (newest-first ordering).
    assert len(history) == before + 1
    assert history[0] == result.entry

    # The new entry references the new version.
    assert result.entry.startswith(f"{bump.new} - ")

    # previous -> new is exposed for display before the build begins.
    assert result.previous == bump.previous
    assert result.new == bump.new
    assert result.display == f"{bump.previous} -> {bump.new}"
    assert result.spec.version == bump.new

    # The input spec is left untouched.
    assert len(spec.extra.get(VERSION_HISTORY_KEY, [])) == before
