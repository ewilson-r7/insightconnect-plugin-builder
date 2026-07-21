"""Unit tests for the empty-state and parse-failure visualization fallbacks (task 5.7).

These cover the two draft-quality fallbacks layered around the view-model
builder:

* empty-state indication for an empty draft (Req 5.5), and
* a parse-failure error indication that retains the most recently rendered
  valid visualization (Req 5.6).

Task 5.8 adds further coverage; these tests exercise the core behavior.
"""

from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec, SemVer
from icplugin_builder.core.view_model import VisualizationViewModel
from icplugin_builder.core.visualization import (
    EMPTY_STATE_MESSAGE,
    VisualizationRenderer,
    VisualizationState,
    render_visualization,
)
from icplugin_builder.core.yaml_codec import dump_plugin_spec


def _nonempty_spec() -> PluginSpec:
    return PluginSpec(
        name="example_plugin",
        title="Example Plugin",
        description="Does example things.",
        version=SemVer(1, 2, 3),
        vendor="rapid7_custom",
        actions={
            "list_things": Component(
                title="List Things",
                input={"query": FieldSchema(type="string", required=False)},
                output={"things": FieldSchema(type="[]string", required=True)},
            )
        },
    )


class TestEmptyState:
    def test_empty_pluginspec_yields_empty_state(self):
        # Req 5.5: no connection/actions/triggers/tasks -> empty-state indication.
        result = render_visualization(PluginSpec(name="p", title="P", vendor="v_custom"))
        assert result.is_empty_state
        assert result.state is VisualizationState.EMPTY
        assert result.message == EMPTY_STATE_MESSAGE
        assert result.view_model is not None and result.view_model.is_empty
        assert result.error is None

    def test_blank_text_draft_yields_empty_state(self):
        # A whitespace-only draft has no spec but is still an empty draft, not a
        # parse failure (Req 5.5).
        for blank in ("", "   ", "\n\t  \n"):
            result = render_visualization(blank)
            assert result.is_empty_state, blank
            assert result.message == EMPTY_STATE_MESSAGE

    def test_none_draft_yields_empty_state(self):
        result = render_visualization(None)
        assert result.is_empty_state
        assert result.view_model is not None and result.view_model.is_empty

    def test_parseable_but_component_free_text_yields_empty_state(self):
        text = dump_plugin_spec(PluginSpec(name="p", title="P", vendor="v_custom"))
        result = render_visualization(text)
        assert result.is_empty_state


class TestOkState:
    def test_nonempty_spec_yields_ok(self):
        result = render_visualization(_nonempty_spec())
        assert result.is_ok
        assert result.state is VisualizationState.OK
        assert result.error is None and result.message is None
        assert [n.name for n in result.view_model.actions] == ["list_things"]

    def test_nonempty_text_draft_yields_ok(self):
        result = render_visualization(dump_plugin_spec(_nonempty_spec()))
        assert result.is_ok
        assert result.view_model is not None and not result.view_model.is_empty


class TestParseFailure:
    def test_unparseable_yaml_yields_parse_error(self):
        # Req 5.6: unparseable draft -> error indication identifying the failure.
        result = render_visualization("name: p\n  bad: : indent:\n :::")
        assert result.is_parse_error
        assert result.state is VisualizationState.PARSE_ERROR
        assert result.error  # non-empty description of the parse failure
        assert result.message is None

    def test_non_mapping_draft_yields_parse_error(self):
        result = render_visualization("- just\n- a\n- list\n")
        assert result.is_parse_error
        assert "mapping" in result.error.lower()

    def test_bad_version_yields_parse_error(self):
        result = render_visualization("name: p\ntitle: P\nvendor: v_custom\nversion: not-a-semver\n")
        assert result.is_parse_error
        assert "version" in result.error.lower()

    def test_parse_error_retains_supplied_last_valid(self):
        # Req 5.6: retain the most recently rendered valid visualization.
        last = render_visualization(_nonempty_spec()).view_model
        assert isinstance(last, VisualizationViewModel)
        result = render_visualization("::: not yaml :::", last_valid=last)
        assert result.is_parse_error
        assert result.view_model is last

    def test_parse_error_with_no_prior_valid_has_no_view_model(self):
        result = render_visualization("::: not yaml :::")
        assert result.is_parse_error
        assert result.view_model is None


class TestVisualizationRenderer:
    def test_retains_last_valid_across_parse_failure(self):
        # Req 5.6: a parse failure keeps showing the last good graph.
        renderer = VisualizationRenderer()
        ok = renderer.render(_nonempty_spec())
        assert ok.is_ok
        good_model = ok.view_model

        broken = renderer.render("::: not yaml :::")
        assert broken.is_parse_error
        assert broken.view_model is good_model
        assert renderer.last_valid is good_model

    def test_empty_state_counts_as_a_valid_visualization_to_retain(self):
        renderer = VisualizationRenderer()
        empty = renderer.render(PluginSpec(name="p", title="P", vendor="v_custom"))
        assert empty.is_empty_state
        broken = renderer.render("::: not yaml :::")
        assert broken.is_parse_error
        assert broken.view_model is empty.view_model

    def test_first_render_parse_failure_has_no_retained_model(self):
        renderer = VisualizationRenderer()
        result = renderer.render("::: not yaml :::")
        assert result.is_parse_error
        assert result.view_model is None
        assert renderer.last_valid is None

    def test_valid_render_updates_retained_model(self):
        renderer = VisualizationRenderer()
        first = renderer.render(_nonempty_spec())
        second_spec = PluginSpec(
            name="other",
            title="Other",
            vendor="v_custom",
            triggers={"on_x": Component(title="On X", input={"i": FieldSchema(type="integer")})},
        )
        second = renderer.render(second_spec)
        assert second.is_ok
        assert renderer.last_valid is second.view_model
        assert renderer.last_valid is not first.view_model
