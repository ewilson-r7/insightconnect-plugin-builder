"""Additional unit tests for the visualization fallbacks (task 5.8).

These complement ``test_visualization.py`` (task 5.7) with focused coverage of:

* additional empty-state rendering cases and the empty/non-empty boundary
  (Req 5.5) -- including drafts that carry only non-component data (custom
  ``types`` or unmodeled top-level keys), and confirmation that any single
  component kind lifts a draft out of the empty state; and
* retention of the *most recently rendered* valid visualization across one or
  more successive parse failures, and recovery afterwards (Req 5.6).

They deliberately avoid repeating the single-step scenarios already asserted in
``test_visualization.py``.
"""

import pytest

from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec, SemVer
from icplugin_builder.core.view_model import CONNECTION
from icplugin_builder.core.visualization import (
    EMPTY_STATE_MESSAGE,
    VisualizationRenderer,
    VisualizationState,
    render_visualization,
)
from icplugin_builder.core.yaml_codec import dump_plugin_spec


def _nonempty_spec(name: str = "example_plugin") -> PluginSpec:
    """A spec with a single action, used as a valid visualization to retain."""
    return PluginSpec(
        name=name,
        title="Example Plugin",
        vendor="rapid7_custom",
        version=SemVer(1, 2, 3),
        actions={"list_things": Component(title="List Things")},
    )


class TestEmptyStateEdgeCases:
    """Empty-state (Req 5.5) beyond a bare/blank draft."""

    def test_types_only_spec_is_empty_state(self):
        # Custom `types` are not connection/actions/triggers/tasks, so a draft
        # that only declares types has no renderable components -> empty state.
        spec = PluginSpec(
            name="p",
            title="P",
            vendor="v_custom",
            types={"person": {"name": FieldSchema(type="string")}},
        )
        result = render_visualization(spec)
        assert result.is_empty_state
        assert result.state is VisualizationState.EMPTY
        assert result.message == EMPTY_STATE_MESSAGE
        assert result.error is None

    def test_types_only_text_draft_is_empty_state(self):
        # Same as above but exercised through the parse path from YAML text.
        text = dump_plugin_spec(
            PluginSpec(
                name="p",
                title="P",
                vendor="v_custom",
                types={"person": {"name": FieldSchema(type="string")}},
            )
        )
        result = render_visualization(text)
        assert result.is_empty_state
        assert result.message == EMPTY_STATE_MESSAGE

    def test_unmodeled_top_level_keys_only_is_empty_state(self):
        # A draft carrying only unmodeled top-level metadata still defines no
        # components, so it renders as empty state rather than a blank view.
        spec = PluginSpec(name="p", title="P", vendor="v_custom", extra={"hub_tags": {"keywords": ["x"]}})
        result = render_visualization(spec)
        assert result.is_empty_state

    def test_empty_state_view_model_has_connection_slot_but_no_components(self):
        # The empty-state render still carries a (component-free) view-model so
        # the UI can show a consistent connection slot; it exposes no defined
        # connection and no component nodes.
        result = render_visualization(PluginSpec(name="p", title="P", vendor="v_custom"))
        view_model = result.view_model
        assert view_model is not None
        assert view_model.is_empty
        assert not view_model.has_connection
        assert view_model.component_nodes() == ()
        # The lone node is the connection slot, still addressable for selection.
        assert view_model.nodes() == (view_model.connection,)
        assert view_model.select(CONNECTION) is view_model.connection

    def test_empty_state_message_is_identical_across_empty_draft_forms(self):
        # The empty-state indication is stable regardless of how the empty draft
        # is expressed (parsed spec, None, blank text).
        forms = [
            render_visualization(PluginSpec(name="p", title="P", vendor="v_custom")),
            render_visualization(None),
            render_visualization("   \n\t"),
        ]
        assert all(r.is_empty_state for r in forms)
        messages = {r.message for r in forms}
        assert messages == {EMPTY_STATE_MESSAGE}
        assert EMPTY_STATE_MESSAGE.strip()  # a non-blank, human-readable indication

    @pytest.mark.parametrize(
        "spec",
        [
            PluginSpec(name="p", title="P", vendor="v_custom", connection={"url": FieldSchema(type="string")}),
            PluginSpec(name="p", title="P", vendor="v_custom", actions={"a": Component(title="A")}),
            PluginSpec(name="p", title="P", vendor="v_custom", triggers={"t": Component(title="T")}),
            PluginSpec(name="p", title="P", vendor="v_custom", tasks={"k": Component(title="K")}),
        ],
        ids=["connection-only", "action-only", "trigger-only", "task-only"],
    )
    def test_any_single_component_kind_is_not_empty_state(self, spec):
        # Boundary of Req 5.5: defining any one of connection/actions/triggers/
        # tasks (even with no fields) lifts the draft out of the empty state.
        result = render_visualization(spec)
        assert result.is_ok
        assert not result.is_empty_state
        assert result.message is None


class TestParseFailureRetentionSequences:
    """Retaining the most recently rendered valid visualization (Req 5.6)."""

    def test_retains_across_consecutive_parse_failures(self):
        # A run of successive parse failures keeps showing the same last good
        # graph; the retained model is not disturbed by repeated failures.
        renderer = VisualizationRenderer()
        good = renderer.render(_nonempty_spec()).view_model

        first_fail = renderer.render("::: not yaml :::")
        second_fail = renderer.render("name: p\n  bad: : indent")
        assert first_fail.is_parse_error and second_fail.is_parse_error
        assert first_fail.view_model is good
        assert second_fail.view_model is good
        assert renderer.last_valid is good

    def test_retains_most_recently_rendered_valid_after_recovery(self):
        # Req 5.6 says the *most recently* rendered valid visualization is
        # retained: after recovering to a newer valid draft, a later parse
        # failure falls back to the newer model, not the original.
        renderer = VisualizationRenderer()
        renderer.render(_nonempty_spec("first"))
        renderer.render("::: not yaml :::")  # falls back to "first"

        newer = renderer.render(_nonempty_spec("second")).view_model
        after = renderer.render("::: not yaml :::")
        assert after.is_parse_error
        assert after.view_model is newer

    def test_first_valid_after_leading_parse_failures_becomes_retained(self):
        # Parse failures before any valid render retain nothing; the first valid
        # render then becomes the model retained by later failures.
        renderer = VisualizationRenderer()
        assert renderer.render("::: not yaml :::").view_model is None
        assert renderer.render(":\n:\n:").view_model is None
        assert renderer.last_valid is None

        good = renderer.render(_nonempty_spec()).view_model
        assert renderer.last_valid is good
        assert renderer.render("::: not yaml :::").view_model is good

    def test_render_visualization_is_pure_and_reusable_with_same_last_valid(self):
        # The pure entry point returns the supplied last_valid unchanged on each
        # call and never mutates it, so a caller can reuse one retained model
        # across multiple failing drafts.
        last = render_visualization(_nonempty_spec()).view_model
        first = render_visualization("::: not yaml :::", last_valid=last)
        second = render_visualization("- a\n- b\n", last_valid=last)
        assert first.is_parse_error and second.is_parse_error
        assert first.view_model is last
        assert second.view_model is last
        assert first.message is None and second.message is None
        assert first.error and second.error  # each identifies its own failure

    @pytest.mark.parametrize(
        "draft",
        ["just a bare scalar", "42", "true", "- a\n- b\n"],
        ids=["scalar-string", "scalar-int", "scalar-bool", "sequence"],
    )
    def test_non_mapping_drafts_are_parse_errors_that_retain_last_valid(self, draft):
        # Any draft that parses to a non-mapping is a parse failure identifying
        # the problem while retaining the last valid visualization (Req 5.6).
        last = render_visualization(_nonempty_spec()).view_model
        result = render_visualization(draft, last_valid=last)
        assert result.is_parse_error
        assert "mapping" in result.error.lower()
        assert result.view_model is last
