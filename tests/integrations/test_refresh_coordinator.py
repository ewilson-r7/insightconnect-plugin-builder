"""Unit tests for structural-change refresh triggering (task 13.5; Req 22.3).

These cover :func:`detect_structural_change` (which spec edits count as
structural) and :class:`RefreshCoordinator` (which invokes ``insight-plugin
refresh`` exactly when a structural change is detected). The CLI is *mocked* --
the real ``insight-plugin`` binary is never required. The structural-refresh
*property* (refresh invoked + derived files equal refresh output) is covered
separately by the property test (task 13.6).
"""

import asyncio

from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec, SemVer
from icplugin_builder.integrations.insight_plugin_cli import ProjectTree
from icplugin_builder.integrations.refresh_coordinator import (
    STRUCTURAL_SECTIONS,
    RefreshCoordinator,
    StructuralChange,
    detect_structural_change,
)


def make_spec(**overrides):
    base = dict(
        name="my_plugin",
        title="My Plugin",
        description="A test plugin.",
        version=SemVer(1, 0, 0),
        vendor="acme_custom",
    )
    base.update(overrides)
    return PluginSpec(**base)


def action(title="Do", description="Does a thing", **fields):
    return Component(title=title, description=description, input=dict(fields))


class FakeCli:
    """A stand-in for :class:`InsightPluginCli` that records refresh calls."""

    def __init__(self, tree=None):
        self.refresh_calls = []
        self._tree = tree if tree is not None else ProjectTree(root=None, files={"help.md": "# help\n"})

    async def refresh(self, project_dir):
        self.refresh_calls.append(project_dir)
        return self._tree


class TestDetectStructuralChange:
    def test_identical_specs_are_not_structural(self):
        spec = make_spec(actions={"a": action()})
        change = detect_structural_change(spec, make_spec(actions={"a": action()}))
        assert change.is_structural is False
        assert change.changed_sections == ()
        assert bool(change) is False

    def test_metadata_only_edit_is_not_structural(self):
        old = make_spec(actions={"a": action()})
        # Title/description/version/vendor changes leave the structural surface intact.
        new = make_spec(
            title="Renamed",
            description="New blurb",
            version=SemVer(2, 0, 0),
            vendor="other_custom",
            actions={"a": action()},
        )
        assert detect_structural_change(old, new).is_structural is False

    def test_added_action_is_structural(self):
        old = make_spec(actions={"a": action()})
        new = make_spec(actions={"a": action(), "b": action(title="Second")})
        change = detect_structural_change(old, new)
        assert change.is_structural is True
        assert change.changed_sections == ("actions",)
        assert "action 'b' was added" in change.reasons

    def test_removed_action_is_structural(self):
        old = make_spec(actions={"a": action(), "b": action()})
        new = make_spec(actions={"a": action()})
        change = detect_structural_change(old, new)
        assert change.changed_sections == ("actions",)
        assert "action 'b' was removed" in change.reasons

    def test_modified_action_input_is_structural(self):
        old = make_spec(actions={"a": action(host=FieldSchema(type="string", required=False))})
        new = make_spec(actions={"a": action(host=FieldSchema(type="string", required=True))})
        change = detect_structural_change(old, new)
        assert change.changed_sections == ("actions",)
        assert "action 'a' was changed" in change.reasons

    def test_connection_change_is_structural(self):
        old = make_spec(connection={"api_key": FieldSchema(type="password", required=True)})
        new = make_spec(connection={"api_key": FieldSchema(type="string", required=True)})
        change = detect_structural_change(old, new)
        assert change.changed_sections == ("connection",)
        assert "connection field 'api_key' was changed" in change.reasons

    def test_trigger_and_task_changes_are_structural(self):
        old = make_spec()
        new = make_spec(
            triggers={"t": Component(title="Poll")},
            tasks={"k": Component(title="Sweep")},
        )
        change = detect_structural_change(old, new)
        assert change.changed_sections == ("triggers", "tasks")
        assert "trigger 't' was added" in change.reasons
        assert "task 'k' was added" in change.reasons

    def test_multiple_sections_reported_in_stable_order(self):
        old = make_spec()
        new = make_spec(
            connection={"token": FieldSchema(type="password")},
            actions={"a": action()},
            triggers={"t": Component(title="Poll")},
            tasks={"k": Component(title="Sweep")},
        )
        change = detect_structural_change(old, new)
        # Order follows STRUCTURAL_SECTIONS.
        assert change.changed_sections == STRUCTURAL_SECTIONS

    def test_none_old_treats_populated_sections_as_additions(self):
        new = make_spec(actions={"a": action()})
        change = detect_structural_change(None, new)
        assert change.changed_sections == ("actions",)
        assert "action 'a' was added" in change.reasons

    def test_none_old_with_empty_new_is_not_structural(self):
        assert detect_structural_change(None, make_spec()).is_structural is False


class TestRefreshCoordinator:
    def test_refreshes_on_structural_change(self, tmp_path):
        cli = FakeCli()
        coordinator = RefreshCoordinator(cli=cli)
        old = make_spec(actions={"a": action()})
        new = make_spec(actions={"a": action(), "b": action(title="Second")})

        tree = asyncio.run(coordinator.refresh_if_structural(old, new, tmp_path))

        assert cli.refresh_calls == [tmp_path]
        assert isinstance(tree, ProjectTree)
        assert tree.files == {"help.md": "# help\n"}

    def test_skips_refresh_on_non_structural_change(self, tmp_path):
        cli = FakeCli()
        coordinator = RefreshCoordinator(cli=cli)
        old = make_spec(actions={"a": action()})
        new = make_spec(title="Renamed", actions={"a": action()})

        result = asyncio.run(coordinator.refresh_if_structural(old, new, tmp_path))

        assert result is None
        assert cli.refresh_calls == []

    def test_none_old_first_iteration_refreshes_when_populated(self, tmp_path):
        cli = FakeCli()
        coordinator = RefreshCoordinator(cli=cli)
        new = make_spec(connection={"token": FieldSchema(type="password")})

        result = asyncio.run(coordinator.refresh_if_structural(None, new, tmp_path))

        assert result is not None
        assert cli.refresh_calls == [tmp_path]

    def test_default_cli_is_created_when_omitted(self):
        coordinator = RefreshCoordinator()
        assert coordinator.cli is not None


class TestStructuralChangeDataclass:
    def test_default_is_non_structural(self):
        change = StructuralChange()
        assert change.is_structural is False
        assert change.changed_sections == ()
        assert change.reasons == ()
