"""Property test for the schema-aware version bumper (task 2.6).

# Feature: insightconnect-plugin-builder, Property 24: Version-bump monotonicity

The unit tests in ``test_version_bump.py`` pin specific examples; this module
covers the universal property across generated inputs: the selected version is
strictly greater than every prior, breaking yields ``(major+1, 0, 0)`` relative
to the highest known version, non-breaking yields a patch bump, and with no
prior export the version is unchanged.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.spec_model import SemVer
from icplugin_builder.core.version_bump import (
    BUMP_MAJOR,
    BUMP_NONE,
    BUMP_PATCH,
    bump_version,
)
from tests import strategies as strat


@settings(max_examples=200)
@given(
    current=strat.semvers(),
    priors=st.lists(strat.semvers(), min_size=0, max_size=6),
    is_breaking=st.booleans(),
)
def test_version_bump_monotonicity(current, priors, is_breaking):
    """Property 24: no prior -> unchanged; breaking -> (major+1, 0, 0) off the
    highest known version; non-breaking -> patch bump; every bump strictly
    greater than all priors.

    **Validates: Requirements 12.3, 12.4, 12.5, 12.7**
    """
    result = bump_version(current, priors, is_breaking=is_breaking)

    # The bump never disturbs the reported previous version.
    assert result.previous == current

    if not priors:
        # Req 12.7: no prior export -> keep the current version unchanged.
        assert result.new == current
        assert result.kind == BUMP_NONE
        assert result.changed is False
        return

    # With priors, the bump is computed off the highest known version so it can
    # never collide with or fall below an already-exported version (Req 12.5).
    base = max([current, *priors])

    if is_breaking:
        # Req 12.3: breaking -> (major + 1, 0, 0).
        assert result.new == SemVer(base.major + 1, 0, 0)
        assert result.kind == BUMP_MAJOR
    else:
        # Req 12.4: non-breaking -> patch increment.
        assert result.new == SemVer(base.major, base.minor, base.patch + 1)
        assert result.kind == BUMP_PATCH

    # Req 12.5: the new version is strictly greater than every prior version
    # (and than the current draft version too, since it feeds the base).
    assert all(result.new > prior for prior in priors)
    assert result.new > current
    assert result.changed is True
