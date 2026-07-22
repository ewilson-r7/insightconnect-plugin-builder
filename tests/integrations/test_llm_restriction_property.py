"""Property test for LLM restriction to reasoning content (task 13.4).

# Feature: insightconnect-plugin-builder, Property 8: LLM invocations are restricted to reasoning content

Property 8 states that across any sequence of generation requests, every
``LLM_Generator`` invocation that occurs has an artifact kind in
{action logic, field description, help text}, and no invocation ever occurs for
a directory-structure, spec-skeleton, or boilerplate artifact (Req 3.2).

This is enforced at two seams and both are checked here:

* :func:`classify_request` -- the routing decision. Only reasoning kinds may be
  routed to the LLM (``requires_llm`` True); every deterministic kind
  (directory structure, spec skeleton, boilerplate) is always routed
  deterministically, and a reasoning request that matches a template is
  reclassified as ``template_match`` and produced with zero LLM calls.
* :meth:`LLMGenerator.generate` -- the only seam that actually invokes the Kiro
  CLI. It must dispatch a subprocess only for reasoning kinds and must reject
  every non-reasoning kind *before* any dispatch occurs.

**Validates: Requirements 3.2**
"""

import asyncio
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.core.generation import (
    DETERMINISTIC_ARTIFACT_KINDS,
    REASONING_ARTIFACT_KINDS,
    REQUESTABLE_ARTIFACT_KINDS,
    GenerationRequest,
    Route,
    classify_request,
    default_template_library,
)
from icplugin_builder.integrations import llm_generator as lg
from icplugin_builder.integrations.llm_generator import LLMGenerator

# The requestable kinds a caller may submit, as a stable sorted list for drawing.
_REQUESTABLE = sorted(REQUESTABLE_ARTIFACT_KINDS, key=lambda kind: kind.value)

# Patterns the shipped template library recognizes, so some reasoning requests
# match a template (routed deterministically) and some do not (routed to LLM).
_KNOWN_PATTERNS = [
    "single_resource_get",
    "paginated_rest_list",
    "webhook_trigger",
    "api_key",
    "username",
    "connection_setup",
]


@st.composite
def generation_requests(draw):
    """Draw a :class:`GenerationRequest` spanning every requestable kind.

    Patterns are drawn from the known template patterns, an unknown pattern, or
    ``None`` so both template-matching and non-matching reasoning requests are
    exercised alongside every deterministic kind.
    """
    kind = draw(st.sampled_from(_REQUESTABLE))
    pattern = draw(
        st.one_of(
            st.none(),
            st.sampled_from(_KNOWN_PATTERNS),
            st.text(min_size=1, max_size=12),
        )
    )
    # Supply generous parameters so any matched template could render; rendering
    # itself is not under test here, only the routing/invocation restriction.
    parameters = {
        "id_param": "id",
        "base_path": "/things",
        "output_key": "result",
        "page_size": 50,
        "items_key": "items",
        "webhook_path": "/hook",
        "service_name": "Example",
    }
    name = draw(st.none() | st.text(max_size=8))
    return GenerationRequest(kind=kind, pattern=pattern, parameters=parameters, name=name)


@settings(max_examples=200)
@given(request=generation_requests())
def test_classification_routes_only_reasoning_kinds_to_llm(request):
    """Property 8 (routing half): an LLM route implies a reasoning kind.

    For any request, if classification requires an LLM call then the request's
    own kind is one of the reasoning kinds and the classified kind is that same
    reasoning kind (never a deterministic kind). Conversely every deterministic
    request is routed deterministically with zero LLM calls.

    **Validates: Requirements 3.2**
    """
    library = default_template_library()
    classification = classify_request(request, library)

    if classification.requires_llm:
        # Any invocation that will occur is for a reasoning kind only.
        assert request.kind in REASONING_ARTIFACT_KINDS
        assert classification.kind in REASONING_ARTIFACT_KINDS
        assert classification.route is Route.LLM
        # Never a directory-structure / spec-skeleton / boilerplate invocation.
        assert classification.kind not in DETERMINISTIC_ARTIFACT_KINDS
        assert classification.template is None
    else:
        assert classification.route is Route.DETERMINISTIC

    # A deterministic-kind request can never be routed to the LLM.
    if request.kind in DETERMINISTIC_ARTIFACT_KINDS:
        assert not classification.requires_llm
        assert classification.kind is request.kind


class _RecordingProcess:
    """Stand-in for the object returned by ``create_subprocess_exec``."""

    def __init__(self):
        self.returncode = 0

    async def communicate(self, stdin=None):
        # Emit a machine-readable token figure so a successful reasoning
        # dispatch records deterministically.
        return b'{"total_tokens": 5}', b""


@settings(max_examples=200)
@given(kind=st.sampled_from(_REQUESTABLE))
def test_generate_dispatches_only_for_reasoning_kinds(kind):
    """Property 8 (invocation half): only reasoning kinds reach the Kiro CLI.

    ``LLMGenerator.generate`` dispatches a subprocess for a reasoning kind and
    raises ``ValueError`` for every deterministic kind *before* any dispatch, so
    no directory-structure/spec-skeleton/boilerplate artifact ever produces an
    LLM invocation.

    The Kiro CLI subprocess is stubbed with a context-managed patch so the fake
    is reset for every generated input (each example starts with zero recorded
    dispatches).

    **Validates: Requirements 3.2**
    """
    calls = []

    async def fake_exec(*command, stdin=None, stdout=None, stderr=None):
        calls.append(list(command))
        return _RecordingProcess()

    with mock.patch.object(lg.asyncio, "create_subprocess_exec", fake_exec):
        generator = LLMGenerator(CostController())

        if kind in REASONING_ARTIFACT_KINDS:
            result = asyncio.run(generator.generate(kind, {"action": "list_things"}, session_id="s1", user_id="u1"))
            assert result.kind is kind
            # Exactly one dispatch occurred, tagged with the reasoning kind.
            assert len(calls) == 1
            assert "chat" in calls[0] and "--no-interactive" in calls[0]
        else:
            with pytest.raises(ValueError):
                asyncio.run(generator.generate(kind, {"action": "list_things"}, session_id="s1", user_id="u1"))
            # Nothing was dispatched for a non-reasoning kind.
            assert calls == []
