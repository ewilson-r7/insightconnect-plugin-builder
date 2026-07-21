"""Unit tests for generation classification and the TemplateLibrary (task 13.2).

These cover specific examples and edge cases for Req 3.1-3.4. The universal
zero-LLM and LLM-restriction properties across generated inputs are covered
separately by the property tests (tasks 13.3, 13.4).
"""

import pytest

from icplugin_builder.core.generation import (
    ArtifactKind,
    GenerationRequest,
    REASONING_ARTIFACT_KINDS,
    Route,
    Template,
    TemplateError,
    TemplateLibrary,
    classify_request,
    default_template_library,
)


class TestClassifyDeterministicKinds:
    @pytest.mark.parametrize(
        "kind",
        [
            ArtifactKind.DIRECTORY_STRUCTURE,
            ArtifactKind.SPEC_SKELETON,
            ArtifactKind.BOILERPLATE,
        ],
    )
    def test_deterministic_kinds_never_route_to_llm(self, kind):
        # Req 3.1: directory/skeleton/boilerplate produce zero LLM calls.
        result = classify_request(GenerationRequest(kind=kind), default_template_library())
        assert result.route is Route.DETERMINISTIC
        assert result.requires_llm is False
        assert result.kind is kind
        assert result.template is None

    def test_deterministic_kinds_ignore_a_matching_pattern(self):
        # Even if a pattern is supplied, deterministic kinds never consult templates.
        lib = default_template_library()
        result = classify_request(GenerationRequest(kind=ArtifactKind.BOILERPLATE, pattern="single_resource_get"), lib)
        assert result.route is Route.DETERMINISTIC
        assert result.kind is ArtifactKind.BOILERPLATE


class TestClassifyReasoningKinds:
    @pytest.mark.parametrize("kind", sorted(REASONING_ARTIFACT_KINDS, key=lambda k: k.value))
    def test_reasoning_without_template_routes_to_llm(self, kind):
        # Req 3.2/3.4: reasoning content with no matching template goes to the LLM.
        result = classify_request(GenerationRequest(kind=kind, pattern="does_not_exist"), default_template_library())
        assert result.route is Route.LLM
        assert result.requires_llm is True
        assert result.kind is kind
        assert result.template is None

    def test_reasoning_without_pattern_routes_to_llm(self):
        result = classify_request(GenerationRequest(kind=ArtifactKind.ACTION_LOGIC), default_template_library())
        assert result.route is Route.LLM
        assert result.kind is ArtifactKind.ACTION_LOGIC

    def test_reasoning_without_library_routes_to_llm(self):
        result = classify_request(GenerationRequest(kind=ArtifactKind.FIELD_DESCRIPTION, pattern="api_key"))
        assert result.route is Route.LLM

    def test_matching_template_reclassifies_as_template_match(self):
        # Req 3.3: a matching template is produced with zero LLM calls.
        lib = default_template_library()
        result = classify_request(GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, pattern="single_resource_get"), lib)
        assert result.kind is ArtifactKind.TEMPLATE_MATCH
        assert result.route is Route.DETERMINISTIC
        assert result.requires_llm is False
        assert result.template is not None
        assert result.template.pattern == "single_resource_get"


class TestClassifyErrors:
    def test_template_match_is_not_a_requestable_kind(self):
        with pytest.raises(ValueError):
            classify_request(GenerationRequest(kind=ArtifactKind.TEMPLATE_MATCH))


class TestTemplate:
    def test_required_parameters_derived_from_body(self):
        template = Template(
            name="t",
            kind=ArtifactKind.FIELD_DESCRIPTION,
            pattern="p",
            body="Hello $a and $b and $a again.",
        )
        assert template.required_parameters == ["a", "b"]

    def test_render_substitutes_parameters(self):
        template = Template(
            name="api_key",
            kind=ArtifactKind.FIELD_DESCRIPTION,
            pattern="api_key",
            body="An API key for the $service_name API.",
        )
        assert template.render({"service_name": "Acme"}) == "An API key for the Acme API."

    def test_render_ignores_extra_parameters(self):
        template = Template(name="t", kind=ArtifactKind.HELP_TEXT, pattern="p", body="Hi $name.")
        assert template.render({"name": "Sam", "unused": "x"}) == "Hi Sam."

    def test_render_missing_parameter_raises(self):
        template = Template(name="t", kind=ArtifactKind.HELP_TEXT, pattern="p", body="Hi $name.")
        with pytest.raises(TemplateError):
            template.render({})

    def test_template_rejects_non_reasoning_kind(self):
        with pytest.raises(ValueError):
            Template(name="bad", kind=ArtifactKind.BOILERPLATE, pattern="p", body="x")


class TestTemplateLibrary:
    def test_match_returns_none_without_pattern(self):
        lib = default_template_library()
        assert lib.match(GenerationRequest(kind=ArtifactKind.ACTION_LOGIC)) is None

    def test_match_requires_matching_kind(self):
        lib = default_template_library()
        # "api_key" is registered under FIELD_DESCRIPTION, not ACTION_LOGIC.
        assert lib.match(GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, pattern="api_key")) is None
        assert lib.match(GenerationRequest(kind=ArtifactKind.FIELD_DESCRIPTION, pattern="api_key")) is not None

    def test_render_matched_request(self):
        lib = default_template_library()
        rendered = lib.render(
            GenerationRequest(
                kind=ArtifactKind.ACTION_LOGIC,
                pattern="paginated_rest_list",
                parameters={
                    "base_path": "/widgets",
                    "page_size": 50,
                    "items_key": "widgets",
                    "output_key": "results",
                },
            )
        )
        assert "def run(self, params={}):" in rendered
        assert "/widgets" in rendered
        assert "results" in rendered
        assert "$" not in rendered  # every placeholder was substituted

    def test_render_unmatched_request_raises(self):
        lib = default_template_library()
        with pytest.raises(TemplateError):
            lib.render(GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, pattern="nope"))

    def test_register_duplicate_pattern_raises(self):
        template = Template(name="a", kind=ArtifactKind.HELP_TEXT, pattern="dup", body="x $y")
        lib = TemplateLibrary([template])
        with pytest.raises(ValueError):
            lib.register(Template(name="b", kind=ArtifactKind.HELP_TEXT, pattern="dup", body="z $w"))

    def test_patterns_listing_and_filter(self):
        lib = default_template_library()
        all_patterns = lib.patterns()
        assert "single_resource_get" in all_patterns
        assert "api_key" in all_patterns
        action_patterns = lib.patterns(ArtifactKind.ACTION_LOGIC)
        assert "single_resource_get" in action_patterns
        assert "api_key" not in action_patterns

    def test_contains_and_len(self):
        lib = default_template_library()
        assert len(lib) == 6
        assert GenerationRequest(kind=ArtifactKind.FIELD_DESCRIPTION, pattern="username") in lib
        assert GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, pattern="missing") not in lib

    def test_default_templates_all_render_with_their_required_params(self):
        lib = default_template_library()
        # Every default template renders deterministically once its params are supplied.
        for pattern in lib.patterns():
            for kind in REASONING_ARTIFACT_KINDS:
                template = lib.match(GenerationRequest(kind=kind, pattern=pattern))
                if template is None:
                    continue
                params = {name: "value" for name in template.required_parameters}
                rendered = template.render(params)
                assert isinstance(rendered, str)
                assert "$" not in rendered
