"""Property-based test for rejecting operations on non-existent components (task 4.4).

Covers design Property 29 with Hypothesis: for any modify or remove request that
names a component absent from the current draft, the operation is rejected with a
:class:`ComponentNotFoundError` (a not-found error) and the receiving draft is left
byte-identical -- both its :class:`PluginSpec` and its ``code_files`` mapping.
"""

import copy
from typing import Dict, List, Tuple

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.draft import (
    ComponentKind,
    ComponentNotFoundError,
    Draft,
)
from icplugin_builder.core.spec_model import Component, PluginSpec

from tests.strategies import plugin_specs, snake_case_names

_KIND_DIRECTORY: Dict[ComponentKind, str] = {
    ComponentKind.ACTION: "actions",
    ComponentKind.TRIGGER: "triggers",
    ComponentKind.TASK: "tasks",
}


def _component_names(spec: PluginSpec, kind: ComponentKind) -> List[str]:
    """Return the names of ``kind`` components present in ``spec``."""
    if kind is ComponentKind.ACTION:
        return list(spec.actions)
    if kind is ComponentKind.TRIGGER:
        return list(spec.triggers)
    return list(spec.tasks)


def _code_files_for(spec: PluginSpec) -> st.SearchStrategy[Dict[str, bytes]]:
    """Generate a ``{path: bytes}`` map, biased toward existing component packages.

    Some paths are placed under the conventional ``<dir>/<name>/`` directory of
    components that already exist in ``spec`` (so a rejected op provably must not
    touch owned files either); others are arbitrary repo paths.
    """
    owned_paths: List[str] = []
    for kind, directory in _KIND_DIRECTORY.items():
        for name in _component_names(spec, kind):
            owned_paths.append(f"icon_{spec.name or 'plugin'}/{directory}/{name}/main.py")
    arbitrary_paths = st.builds(
        lambda a, b: f"{a}/{b}.py",
        snake_case_names(),
        snake_case_names(),
    )
    path_choices = st.sampled_from(owned_paths) if owned_paths else arbitrary_paths
    paths = st.one_of(path_choices, arbitrary_paths)
    return st.dictionaries(paths, st.binary(max_size=16), max_size=6)


@st.composite
def _drafts_with_absent_name(draw: st.DrawFn) -> Tuple[Draft, ComponentKind, str]:
    """Generate a ``(draft, kind, absent_name)`` triple.

    ``absent_name`` is guaranteed not to name any component of ``kind`` in the
    draft's spec, so both ``modify`` and ``remove`` on it must be rejected.
    """
    spec = draw(plugin_specs())
    code_files = draw(_code_files_for(spec))
    kind = draw(st.sampled_from(list(ComponentKind)))

    existing = set(_component_names(spec, kind))
    candidate = draw(snake_case_names())
    absent = candidate
    while absent in existing:
        absent = f"{absent}_absent"

    return Draft(spec=spec, code_files=code_files), kind, absent


# Feature: insightconnect-plugin-builder, Property 29: Reject operations on non-existent named components
@settings(max_examples=200)
@given(_drafts_with_absent_name())
def test_operations_on_absent_component_are_rejected_and_leave_draft_unchanged(
    draft_kind_name: Tuple[Draft, ComponentKind, str],
) -> None:
    """Modify/remove of an absent component is rejected; the draft is unchanged.

    **Validates: Requirements 15.4**
    """
    draft, kind, absent_name = draft_kind_name

    before_spec = copy.deepcopy(draft.spec)
    before_code = copy.deepcopy(draft.code_files)

    # A replacement payload that must never be applied to the rejected draft.
    new_component = Component(title="should-not-be-applied")

    # modify on an absent name is rejected with a not-found error.
    try:
        draft.modify_component(kind, absent_name, new_component)
        raise AssertionError("modify_component should reject an absent component name")
    except ComponentNotFoundError as error:
        assert error.operation == "modify"
        assert error.kind is kind
        assert error.name == absent_name

    # remove on an absent name is rejected with a not-found error.
    try:
        draft.remove_component(kind, absent_name)
        raise AssertionError("remove_component should reject an absent component name")
    except ComponentNotFoundError as error:
        assert error.operation == "remove"
        assert error.kind is kind
        assert error.name == absent_name

    # The draft (spec + code files) is byte-identical after both rejected ops.
    assert draft.spec == before_spec
    assert draft.code_files == before_code
