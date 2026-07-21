"""Integration test for the wired LLM_Generator -> Kiro CLI dispatch (task 14.2).

Where :mod:`test_llm_generator` unit-tests the generator's seams in isolation,
this module exercises the *wired flow* end-to-end against a **mocked** Kiro CLI
subprocess and a **real** :class:`CostController`:

* A reasoning-kind ``generate()`` that has no matching template
  (:func:`classify_request` -> ``Route.LLM``, Req 3.4) is dispatched through the
  Kiro CLI -- the tool's primary LLM provider (Req 20.3) -- by launching the
  configured ``kiro`` executable as a subprocess.
* Every dispatch is gated by the real ``CostController.authorize`` and its
  consumed tokens are recorded on the cumulative session total, honoring the
  reported-figure-then-estimate precedence across a multi-step session
  (Req 3.5, 3.6).
* Budget-reached and rate-limit decisions block the dispatch outright: the Kiro
  CLI is never launched and the session total is left unchanged (Req 4.2, 4.5).

The subprocess is mocked by patching ``asyncio.create_subprocess_exec`` so the
real ``kiro`` binary is never required; each coroutine is driven with
``asyncio.run`` so no async test plugin is needed. Every assertion runs against
the genuine ``CostController`` and generation-classification wiring, not a stub,
which is what makes this an integration rather than a unit test.

**Validates: Requirements 20.3, 3.4**
"""

import asyncio

import pytest

from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.core.generation import (
    REASONING_ARTIFACT_KINDS,
    ArtifactKind,
    GenerationRequest,
    Route,
    classify_request,
    default_template_library,
)
from icplugin_builder.integrations import llm_generator as lg
from icplugin_builder.integrations.llm_generator import (
    DEFAULT_EXECUTABLE,
    TOKEN_SOURCE_ESTIMATED,
    TOKEN_SOURCE_REPORTED,
    CostLimitError,
    LLMGenerator,
)


class ScriptedKiroCLI:
    """A mock Kiro CLI: replays scripted subprocess outputs and records dispatches.

    Each queued ``(returncode, stdout, stderr)`` triple is returned by one
    ``create_subprocess_exec`` call in order; the argument vectors are captured
    on :attr:`dispatched` so a test can assert exactly what reached the CLI.
    """

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.dispatched = []

    def install(self, monkeypatch):
        async def fake_exec(*command, stdin=None, stdout=None, stderr=None):
            self.dispatched.append(list(command))
            returncode, out, err = self._outputs.pop(0)
            return _FakeProcess(returncode, out, err)

        monkeypatch.setattr(lg.asyncio, "create_subprocess_exec", fake_exec)
        return self

    @property
    def dispatch_count(self):
        return len(self.dispatched)


class _FakeProcess:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdin_received = None

    async def communicate(self, stdin=None):
        self.stdin_received = stdin
        return self._stdout, self._stderr


def _reasoning_request_without_template():
    """A reasoning request that no shipped template matches (Req 3.4 -> LLM route)."""
    return GenerationRequest(
        kind=ArtifactKind.ACTION_LOGIC,
        pattern="bespoke_correlation_logic",  # not a known template pattern
        parameters={"action": "correlate_alerts"},
        name="correlate_alerts",
    )


def _run(generator, request, *, session_id="session-A", user_id="user-1"):
    """Drive the async ``generate`` for ``request`` to completion."""
    return asyncio.run(
        generator.generate(
            request.kind,
            request.parameters,
            session_id=session_id,
            user_id=user_id,
        )
    )


class TestWiredReasoningDispatch:
    """A no-template reasoning request flows through classification into the CLI."""

    def test_unmatched_reasoning_request_routes_to_llm_and_dispatches(self, monkeypatch):
        """Req 3.4/20.3: no template -> LLM route -> a Kiro CLI subprocess launch."""
        request = _reasoning_request_without_template()

        # The wiring decision that precedes dispatch: no template, so route to LLM.
        classification = classify_request(request, default_template_library())
        assert classification.route is Route.LLM
        assert classification.requires_llm is True
        assert request.kind in REASONING_ARTIFACT_KINDS

        cli = ScriptedKiroCLI([(0, b'{"usage": {"total_tokens": 84}}', b"")]).install(monkeypatch)
        controller = CostController()
        # Default executable is the Kiro CLI (Req 20.3).
        generator = LLMGenerator(controller)

        result = _run(generator, request)

        # Exactly one dispatch, and it went to the Kiro CLI executable for this kind.
        assert cli.dispatch_count == 1
        assert cli.dispatched[0][0] == DEFAULT_EXECUTABLE
        assert cli.dispatched[0] == [DEFAULT_EXECUTABLE, "--kind", ArtifactKind.ACTION_LOGIC.value]
        assert result.kind is ArtifactKind.ACTION_LOGIC
        # The reported figure was recorded on the real controller's session total.
        assert result.measurement.source == TOKEN_SOURCE_REPORTED
        assert result.session_total == 84
        assert controller.session_total("session-A") == 84

    def test_scoped_context_is_fed_to_the_cli_process(self, monkeypatch):
        """The scoped slice is delivered to the Kiro CLI on stdin, not as argv."""
        cli = ScriptedKiroCLI([(0, b"def run(self, params={}):\n    return {}\n", b"")]).install(monkeypatch)
        generator = LLMGenerator(CostController(), executable=["kiro", "generate"])

        request = GenerationRequest(
            kind=ArtifactKind.ACTION_LOGIC,
            pattern="bespoke",
            parameters={"action": "escalate_incident"},
        )
        result = _run(generator, request)

        assert cli.dispatched[0][:2] == ["kiro", "generate"]
        assert result.content == "def run(self, params={}):\n    return {}"


class TestWiredCostAccountingAcrossSession:
    """Token accounting is recorded on the real controller across a session."""

    def test_records_reported_then_estimated_tokens_cumulatively(self, monkeypatch):
        """Req 3.5/3.6: reported figure then estimate fallback both accumulate."""
        controller = CostController()
        generator = LLMGenerator(controller)

        # First invocation reports an exact figure; second omits one and is estimated.
        cli = ScriptedKiroCLI(
            [
                (0, b"total_tokens: 100", b""),
                (0, b"some generated code with no usage figure", b""),
            ]
        ).install(monkeypatch)

        first = _run(generator, _reasoning_request_without_template())
        second = _run(generator, _reasoning_request_without_template())

        assert cli.dispatch_count == 2
        assert first.measurement.source == TOKEN_SOURCE_REPORTED
        assert first.session_total == 100
        # The second invocation had no reported figure, so it was estimated.
        assert second.measurement.source == TOKEN_SOURCE_ESTIMATED
        assert second.tokens > 0
        # The cumulative session total is the sum of both recorded invocations.
        assert second.session_total == 100 + second.tokens
        assert controller.session_total("session-A") == 100 + second.tokens

    def test_failed_invocation_excluded_but_next_success_recorded(self, monkeypatch):
        """Req 3.7 wired: a failed CLI run records nothing; a later success does."""
        controller = CostController()
        generator = LLMGenerator(controller)

        cli = ScriptedKiroCLI(
            [
                (3, b"", b"kiro: internal error"),
                (0, b"total_tokens: 42", b""),
            ]
        ).install(monkeypatch)

        with pytest.raises(Exception):
            _run(generator, _reasoning_request_without_template())
        assert controller.session_total("session-A") == 0  # failure excluded

        ok = _run(generator, _reasoning_request_without_template())
        assert cli.dispatch_count == 2
        assert ok.session_total == 42
        assert controller.session_total("session-A") == 42


class TestWiredGatingBlocksDispatch:
    """The real CostController gates dispatch before the Kiro CLI is launched."""

    def test_budget_reached_blocks_before_any_cli_launch(self, monkeypatch):
        """Req 4.2 wired: an exhausted session budget blocks dispatch entirely."""
        cli = ScriptedKiroCLI([(0, b"total_tokens: 1", b"")]).install(monkeypatch)
        controller = CostController(token_budget=500)
        controller.record_usage("session-A", 500, succeeded=True)  # exhaust the budget
        generator = LLMGenerator(controller)

        with pytest.raises(CostLimitError) as excinfo:
            _run(generator, _reasoning_request_without_template())

        assert cli.dispatch_count == 0  # the Kiro CLI was never launched
        assert excinfo.value.decision.reason == "budget_reached"
        assert controller.session_total("session-A") == 500  # unchanged

    def test_rate_limit_blocks_second_dispatch(self, monkeypatch):
        """Req 4.5 wired: bursting past the per-user rate blocks the next dispatch."""
        cli = ScriptedKiroCLI([(0, b"total_tokens: 7", b"")]).install(monkeypatch)
        controller = CostController(rate_limit=1)
        generator = LLMGenerator(controller)

        first = _run(generator, _reasoning_request_without_template())
        assert first.session_total == 7

        with pytest.raises(CostLimitError) as excinfo:
            _run(generator, _reasoning_request_without_template())

        assert cli.dispatch_count == 1  # only the first reached the CLI
        decision = excinfo.value.decision
        assert decision.reason == "rate_limited"
        assert 0 < decision.retry_after_seconds <= 60
