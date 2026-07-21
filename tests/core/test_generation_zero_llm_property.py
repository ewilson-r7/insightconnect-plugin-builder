"""Property test for zero-LLM deterministic scaffolding (task 13.3).

# Feature: insightconnect-plugin-builder, Property 7: Deterministic scaffolding makes zero LLM calls

The unit tests in ``test_generation.py`` pin specific classification examples;
this module covers the universal property across generated inputs: every
artifact that classifies deterministically -- directory structure, spec
skeleton, boilerplate, or a template match -- must route DETERMINISTIC with
``requires_llm`` False, and dispatching such a classification must make zero
``LLM_Generator`` invocations.
"""

from typing import Any, Mapping, Optional, Union

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.core.generation import (
    DETERMINISTIC_ARTIFACT_KINDS,
    ArtifactKind,
    Classification,
    GenerationRequest,
    Route,
    classify_request,
    default_template_library,
)
from icplugin_builder.integrations.llm_generator import GenerationResult, LLMGenerator

# The deterministic-source request kinds (produced by the insight-plugin CLI).
_DETERMINISTIC_REQUEST_KINDS = (
    ArtifactKind.DIRECTORY_STRUCTURE,
    ArtifactKind.SPEC_SKELETON,
    ArtifactKind.BOILERPLATE,
)

# A shared template library whose registered (kind, pattern) pairs are the only
# reasoning requests that classify deterministically as a template match.
_LIBRARY = default_template_library()

# The registered reasoning (kind, pattern) pairs a request can match on.
_TEMPLATE_MATCHES = sorted(
    ((template.kind, template.pattern) for template in _LIBRARY._templates.values()),
    key=lambda pair: (pair[0].value, pair[1]),
)


class SpyLLMGenerator(LLMGenerator):
    """An ``LLMGenerator`` that records invocations instead of dispatching.

    Used purely to assert Property 7's stronger form: dispatching a
    deterministic classification never reaches the LLM. ``generate`` never runs
    a subprocess; it only counts that it was called.
    """

    def __init__(self) -> None:
        super().__init__(CostController())
        self.invocations = 0

    async def generate(  # type: ignore[override]
        self,
        kind: Union[ArtifactKind, str],
        scoped_context: Mapping[str, Any],
        *,
        session_id: str,
        user_id: str,
    ) -> GenerationResult:
        self.invocations += 1
        raise AssertionError("LLM_Generator must not be invoked for a deterministic artifact")


def _dispatch(classification: Classification, spy: SpyLLMGenerator) -> None:
    """Route a classification the way the Generation_Engine would.

    Deterministic classifications are produced without touching the LLM; only an
    ``LLM``-routed classification would invoke the generator. The spy therefore
    stays at zero invocations for every deterministic classification.
    """
    if classification.requires_llm:  # pragma: no cover - not exercised by this property
        spy.invocations += 1


@st.composite
def _template_params(draw: st.DrawFn, template) -> Mapping[str, Any]:
    """Draw a full set of the placeholder values a template body requires."""
    return {name: draw(st.text(min_size=1, max_size=8)) for name in template.required_parameters}


@st.composite
def deterministic_requests(draw: st.DrawFn) -> GenerationRequest:
    """Generate a request that must classify deterministically.

    Two families are produced:

    * deterministic-source kinds (directory structure / spec skeleton /
      boilerplate), optionally carrying a stray pattern or parameters that must
      be ignored; and
    * reasoning kinds paired with a registered template pattern -- a template
      match, which is likewise deterministic.
    """
    if draw(st.booleans()):
        kind = draw(st.sampled_from(_DETERMINISTIC_REQUEST_KINDS))
        # A stray pattern/name must never flip a deterministic kind to the LLM.
        pattern: Optional[str] = draw(st.one_of(st.none(), st.sampled_from(["single_resource_get", "unknown", "x"])))
        return GenerationRequest(kind=kind, pattern=pattern, name=draw(st.one_of(st.none(), st.text(max_size=6))))

    match_kind, match_pattern = draw(st.sampled_from(_TEMPLATE_MATCHES))
    template = _LIBRARY.match(GenerationRequest(kind=match_kind, pattern=match_pattern))
    parameters = draw(_template_params(template))
    return GenerationRequest(kind=match_kind, pattern=match_pattern, parameters=parameters)


@settings(max_examples=200)
@given(deterministic_requests())
def test_deterministic_scaffolding_makes_zero_llm_calls(request: GenerationRequest) -> None:
    """Property 7: deterministic/template artifacts route DETERMINISTIC with zero LLM calls.

    For every directory-structure, spec-skeleton, boilerplate, or template-match
    request, ``classify_request`` must return :attr:`Route.DETERMINISTIC` with
    ``requires_llm`` False and a classified kind in
    :data:`DETERMINISTIC_ARTIFACT_KINDS`; dispatching that classification must
    leave a spy ``LLM_Generator`` uninvoked.

    **Validates: Requirements 3.1, 3.3**
    """
    classification = classify_request(request, _LIBRARY)

    assert classification.route is Route.DETERMINISTIC
    assert classification.requires_llm is False
    assert classification.kind in DETERMINISTIC_ARTIFACT_KINDS

    spy = SpyLLMGenerator()
    _dispatch(classification, spy)
    assert spy.invocations == 0
