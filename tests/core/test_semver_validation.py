"""Property-based test for semantic-version validation (task 2.2).

Design Property 15: a version value is accepted iff it is in strict
``MAJOR.MINOR.PATCH`` form, and every rejection names the ``version`` field and
the expected format. This is exercised at two layers that share the same
``MAJOR.MINOR.PATCH`` contract:

* :meth:`SemVer.is_valid` / :meth:`SemVer.parse` in ``core.spec_model`` -- the
  low-level check and parser.
* :class:`SpecValidator` in ``core.spec_validator`` -- the ``version`` check
  that surfaces a field-path + description in a :class:`ValidationReport`.

Validates: Requirements 7.3, 7.5
"""

# Feature: insightconnect-plugin-builder, Property 15: Semantic version validation

from hypothesis import given
from hypothesis import strategies as st

from icplugin_builder.core.spec_model import SemVer
from icplugin_builder.core.spec_validator import SpecValidator

_VALIDATOR = SpecValidator()

# A component of a strict semver is "0" or a run of digits with no leading zero.
_semver_component = st.one_of(
    st.just("0"),
    st.builds(
        lambda first, rest: first + rest,
        st.sampled_from("123456789"),
        st.text(alphabet="0123456789", min_size=0, max_size=3),
    ),
)


@st.composite
def valid_semver_strings(draw: st.DrawFn) -> str:
    """Generate strings in strict ``MAJOR.MINOR.PATCH`` form."""
    major = draw(_semver_component)
    minor = draw(_semver_component)
    patch = draw(_semver_component)
    return f"{major}.{minor}.{patch}"


@st.composite
def invalid_semver_strings(draw: st.DrawFn) -> str:
    """Generate strings that are NOT strict ``MAJOR.MINOR.PATCH``.

    Draws arbitrary text (biased toward version-like shapes) and rejects the
    rare case that it happens to be a valid semver, so the generator only
    yields genuine negatives.
    """
    version_like = st.builds(
        lambda parts: ".".join(parts),
        st.lists(st.text(alphabet="0123456789vx-", min_size=0, max_size=4), min_size=0, max_size=5),
    )
    free_text = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=0, max_size=12)
    candidate = draw(st.one_of(version_like, free_text))
    # Exclude the (rare) accidental valid semver so this stays a pure negative.
    if SemVer.is_valid(candidate):
        candidate = candidate + ".x"
    return candidate


def _validator_version_errors(value: str):
    """Return the version-field errors a SpecValidator raises for ``value``."""
    spec = {
        "plugin_spec_version": "v2",
        "name": "example",
        "title": "Example",
        "description": "An example plugin",
        "version": value,
        "vendor": "rapid7",
    }
    report = _VALIDATOR.validate(spec)
    return [err for err in report.errors if err.path == "version"]


@given(valid_semver_strings())
def test_valid_semver_is_accepted_everywhere(value: str):
    # is_valid accepts, parse succeeds and round-trips, and the validator
    # raises no version-field error.
    assert SemVer.is_valid(value) is True
    parsed = SemVer.parse(value)
    assert str(parsed) == value
    assert _validator_version_errors(value) == []


@given(invalid_semver_strings())
def test_invalid_semver_is_rejected_with_named_message(value: str):
    # is_valid rejects.
    assert SemVer.is_valid(value) is False

    # parse raises, and the message names the version field and expected format.
    try:
        SemVer.parse(value)
        raise AssertionError(f"expected ValueError for {value!r}")
    except ValueError as exc:
        message = str(exc)
        assert "version" in message
        assert "MAJOR.MINOR.PATCH" in message

    # The validator reports exactly one version-field error whose message names
    # the version field (via its path) and the expected format.
    errors = _validator_version_errors(value)
    assert len(errors) == 1
    assert errors[0].path == "version"
    assert "MAJOR.MINOR.PATCH" in errors[0].message


@given(st.one_of(valid_semver_strings(), invalid_semver_strings()))
def test_acceptance_iff_valid_format(value: str):
    # The core biconditional: accepted iff strict MAJOR.MINOR.PATCH. Acceptance
    # (is_valid) and successful parsing must agree, and both must agree with the
    # validator producing no version error.
    accepted = SemVer.is_valid(value)

    parsed_ok = True
    try:
        SemVer.parse(value)
    except ValueError:
        parsed_ok = False

    assert accepted == parsed_ok
    assert accepted == (_validator_version_errors(value) == [])
