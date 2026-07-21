"""Property test for component preservation under targeted Draft operations (task 4.2).

# Feature: insightconnect-plugin-builder, Property 1: Component preservation under targeted operations

The unit tests in ``test_draft.py`` pin specific add/modify/remove examples; this
module covers the universal property across generated specs and code trees: a
targeted operation on *one* named component must leave every *other* component
and every code file *not* owned by the target byte-identical before and after.
"""

import copy
from typing import Dict, List, Optional, Tuple

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.draft import ComponentKind, Draft
from icplugin_builder.core.spec_model import Component, PluginSpec
from tests import strategies as strat

#: Conventional package sub-directory holding each kind's code files, mirroring
#: the ownership convention the Draft uses.
_KIND_DIRECTORY: Dict[ComponentKind, str] = {
    ComponentKind.ACTION: "actions",
    ComponentKind.TRIGGER: "triggers",
    ComponentKind.TASK: "tasks",
}

#: The package root prefix used for all generated code-file paths.
_PKG = "icon_demo"


def _component_map(spec: PluginSpec, kind: ComponentKind) -> Dict[str, Component]:
    """Return the name-keyed component map on ``spec`` for ``kind``."""
    return {
        ComponentKind.ACTION: spec.actions,
        ComponentKind.TRIGGER: spec.triggers,
        ComponentKind.TASK: spec.tasks,
    }[kind]


def _all_named_components(spec: PluginSpec) -> List[Tuple[ComponentKind, str]]:
    """Enumerate every ``(kind, name)`` named component present in ``spec``."""
    named: List[Tuple[ComponentKind, str]] = []
    for kind in ComponentKind:
        for name in _component_map(spec, kind):
            named.append((kind, name))
    return named


def _owned_files_for(kind: ComponentKind, name: str) -> Dict[str, bytes]:
    """Build the conventional set of code files a named component owns."""
    directory = _KIND_DIRECTORY[kind]
    base = f"{_PKG}/{directory}/{name}"
    return {
        f"{base}/action.py": f"# {directory} {name} logic\n".encode(),
        f"{base}/schema.py": f"# {directory} {name} schema\n".encode(),
    }


def _build_code_files(spec: PluginSpec) -> Dict[str, bytes]:
    """Build a code tree with owned files per component plus unowned files.

    Every action/trigger/task gets its owned package files, and a handful of
    non-component-owned files (connection, shared utilities) are included so the
    property has unowned files to protect.
    """
    files: Dict[str, bytes] = {}
    for kind, name in _all_named_components(spec):
        files.update(_owned_files_for(kind, name))
    files[f"{_PKG}/connection/connection.py"] = b"# connection\n"
    files[f"{_PKG}/util/helper.py"] = b"# shared helper\n"
    files["setup.py"] = b"# setup\n"
    return files


def _owns(path: str, directory: str, name: str) -> bool:
    """Return ``True`` iff ``path`` lies under ``<directory>/<name>/``."""
    segments = [segment for segment in path.replace("\\", "/").split("/") if segment]
    for index in range(len(segments) - 1):
        if segments[index] == directory and segments[index + 1] == name:
            return True
    return False


def _fresh_name(existing: List[str], candidate: str) -> str:
    """Return ``candidate`` made unique against ``existing`` names."""
    name = candidate
    suffix = 0
    while name in existing:
        suffix += 1
        name = f"{candidate}_{suffix}"
    return name


def _assert_preserved(
    before: Draft,
    after: Draft,
    target_kind: ComponentKind,
    target_name: str,
) -> None:
    """Assert every non-target component and unowned code file is byte-identical."""
    for kind in ComponentKind:
        before_map = _component_map(before.spec, kind)
        after_map = _component_map(after.spec, kind)
        for name, component in before_map.items():
            if kind is target_kind and name == target_name:
                continue
            assert name in after_map, f"non-target {kind.value} '{name}' vanished"
            assert after_map[name] == component, f"non-target {kind.value} '{name}' changed"

    directory = _KIND_DIRECTORY[target_kind]
    for path, content in before.code_files.items():
        if _owns(path, directory, target_name):
            continue
        assert path in after.code_files, f"unowned code file '{path}' vanished"
        assert after.code_files[path] == content, f"unowned code file '{path}' changed"


@settings(max_examples=200)
@given(st.data())
def test_targeted_operation_preserves_other_components(data):
    """Property 1: a targeted add/modify/remove on one named component leaves
    every other component and every code file not owned by the target
    byte-identical.

    A structurally valid spec is paired with a code tree (owned files per
    component plus unowned connection/utility files). Exactly one targeted
    operation is applied to a single named component; every other component and
    every unowned code file must be identical before and after.

    **Validates: Requirements 1.3, 2.3, 15.1, 15.2, 15.3, 22.1, 22.2**
    """
    spec = data.draw(strat.plugin_specs())
    code_files = _build_code_files(spec)
    draft = Draft(spec=spec, code_files=dict(code_files))
    # An independent snapshot to compare against after the operation.
    before = Draft(spec=copy.deepcopy(spec), code_files=dict(code_files))

    named = _all_named_components(spec)
    operations = ["add"] + (["modify", "remove"] if named else [])
    operation = data.draw(st.sampled_from(operations))

    if operation == "add":
        kind = data.draw(st.sampled_from(list(ComponentKind)))
        name = _fresh_name(list(_component_map(spec, kind)), data.draw(strat.snake_case_names()))
        after = draft.add_component(
            kind,
            name,
            data.draw(strat.components()),
            code_files=_owned_files_for(kind, name),
        )
        target_kind, target_name = kind, name
    elif operation == "modify":
        kind, name = data.draw(st.sampled_from(named))
        new_code: Optional[Dict[str, bytes]] = _owned_files_for(kind, name) if data.draw(st.booleans()) else None
        after = draft.modify_component(kind, name, data.draw(strat.components()), code_files=new_code)
        target_kind, target_name = kind, name
    else:  # remove
        kind, name = data.draw(st.sampled_from(named))
        after = draft.remove_component(kind, name)
        target_kind, target_name = kind, name

    _assert_preserved(before, after, target_kind, target_name)
    # The operation must be non-mutating: the receiver still equals the snapshot.
    assert draft.spec == before.spec
    assert draft.code_files == before.code_files
