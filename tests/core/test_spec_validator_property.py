"""Property test for validation error report completeness (task 5.2).

# Feature: insightconnect-plugin-builder, Property 16: Validation error report completeness

The unit tests in ``test_spec_validator.py`` pin specific examples; this module
covers the universal property across generated inputs: for any spec carrying
one or more independent schema violations, the ``Spec_Validator`` report
includes an entry for every violation, and each entry carries a non-empty field
path and a non-empty description of the violation.
"""

from typing import Callable, Dict, List, Tuple

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.spec_validator import validate_spec


def _valid_mapping(name: str, version: str) -> Dict:
    """A minimal, schema-valid ``plugin.spec.yaml`` mapping.

    ``name`` and ``version`` are supplied by the generator so the untouched
    base varies across examples; every value here is otherwise schema-valid so
    the only violations present are the ones injected by the test.
    """
    return {
        "plugin_spec_version": "v2",
        "name": name,
        "title": "Example Plugin",
        "description": "An example plugin.",
        "version": version,
        "vendor": "rapid7_custom",
        "connection": {"url": {"type": "string", "required": True}},
        "actions": {
            "run": {
                "title": "Run",
                "input": {"host": {"type": "string", "required": True}},
                "output": {"result": {"type": "string"}},
            }
        },
    }


# Each injector applies exactly one independent violation at a distinct
# location and returns a predicate identifying the error entry that must appear
# for that violation. Keeping the locations disjoint means N injectors produce
# N independently detectable violations.


def _inject_missing_title(mapping: Dict) -> Callable[[str, str], bool]:
    del mapping["title"]
    return lambda path, message: path == "(root)" and "title" in message


def _inject_missing_description(mapping: Dict) -> Callable[[str, str], bool]:
    del mapping["description"]
    return lambda path, message: path == "(root)" and "description" in message


def _inject_missing_vendor(mapping: Dict) -> Callable[[str, str], bool]:
    del mapping["vendor"]
    return lambda path, message: path == "(root)" and "vendor" in message


def _inject_bad_name(mapping: Dict) -> Callable[[str, str], bool]:
    mapping["name"] = "Not Snake Case"
    return lambda path, message: path == "name"


def _inject_bad_version(mapping: Dict) -> Callable[[str, str], bool]:
    mapping["version"] = "1.0"  # valid string, invalid semver
    return lambda path, message: path == "version"


def _inject_connection_field_no_type(mapping: Dict) -> Callable[[str, str], bool]:
    mapping["connection"]["extra_field"] = {"required": True}
    return lambda path, message: path == "connection.extra_field"


def _inject_action_input_no_type(mapping: Dict) -> Callable[[str, str], bool]:
    mapping["actions"]["run"]["input"]["extra_input"] = {"required": False}
    return lambda path, message: path == "actions.run.input.extra_input"


#: All independent violation injectors, keyed by a stable label.
_INJECTORS: Dict[str, Callable[[Dict], Callable[[str, str], bool]]] = {
    "missing_title": _inject_missing_title,
    "missing_description": _inject_missing_description,
    "missing_vendor": _inject_missing_vendor,
    "bad_name": _inject_bad_name,
    "bad_version": _inject_bad_version,
    "connection_field_no_type": _inject_connection_field_no_type,
    "action_input_no_type": _inject_action_input_no_type,
}


@st.composite
def _specs_with_known_violations(draw: st.DrawFn) -> Tuple[Dict, List[Callable[[str, str], bool]]]:
    """Build a spec mapping seeded with a non-empty subset of independent violations.

    Returns the mutated mapping plus one predicate per injected violation. The
    ``name``/``version`` fields not chosen for mutation are drawn as valid
    values so the base stays clean.
    """
    labels = draw(st.lists(st.sampled_from(sorted(_INJECTORS)), min_size=1, max_size=len(_INJECTORS), unique=True))
    valid_name = draw(st.sampled_from(["example_plugin", "my_plugin", "acme_tool", "widget"]))
    valid_version = draw(st.sampled_from(["1.0.0", "2.3.4", "0.1.0", "10.20.30"]))

    mapping = _valid_mapping(name=valid_name, version=valid_version)
    predicates = [_INJECTORS[label](mapping) for label in labels]
    return mapping, predicates


@settings(max_examples=200)
@given(_specs_with_known_violations())
def test_report_has_an_entry_for_every_violation(case):
    """Property 16: the report includes an entry for every violation, each
    carrying a field path and a description of the violation.

    A schema-valid base is seeded with a non-empty subset of independent
    violations at disjoint locations. The report must (a) be invalid, (b) carry
    a non-empty field path and non-empty description on every entry, and (c)
    contain at least one matching entry for each injected violation.

    **Validates: Requirements 7.2**
    """
    mapping, predicates = case
    report = validate_spec(mapping)

    # (a) Any violation makes the spec invalid.
    assert report.is_valid is False

    # (b) Every reported entry carries a non-empty field path and description.
    for error in report.errors:
        assert isinstance(error.path, str) and error.path != ""
        assert isinstance(error.message, str) and error.message.strip() != ""

    # (c) Completeness: every injected violation is represented by an entry.
    for predicate in predicates:
        assert any(
            predicate(error.path, error.message) for error in report.errors
        ), "an injected violation had no corresponding report entry"

    # The report never drops violations: at least one entry per injection.
    assert len(report.errors) >= len(predicates)
