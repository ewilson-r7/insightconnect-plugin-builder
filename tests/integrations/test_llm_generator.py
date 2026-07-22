"""Unit tests for the LLM_Generator Kiro CLI dispatch (task 14.1).

These cover scoped, cost-gated dispatch of the *reasoning* artifact kinds
through a **mocked** Kiro CLI subprocess (the real binary is never required):

* only reasoning kinds are dispatched; deterministic kinds are rejected before
  any subprocess launch (Req 3.2);
* every call is routed through :meth:`CostController.authorize`, and a blocked
  decision (budget reached / rate limited) prevents dispatch (Req 4.2, 4.5);
* successful invocations record their tokens on the session total, measured by
  the reported figure with a tokenizer-estimate fallback (Req 3.5, token
  accounting precedence);
* failed invocations halt the step and are excluded from the total (Req 3.7).

The subprocess is mocked by monkeypatching ``asyncio.create_subprocess_exec``;
tests drive the coroutines with ``asyncio.run`` so no async test plugin is
required.
"""

import asyncio

import pytest

from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.core.generation import ArtifactKind
from icplugin_builder.integrations import llm_generator as lg
from icplugin_builder.integrations.llm_generator import (
    TOKEN_SOURCE_ESTIMATED,
    TOKEN_SOURCE_REPORTED,
    CostLimitError,
    LLMGenerator,
    LLMGeneratorError,
    TokenMeasurement,
    estimate_tokens,
    parse_reported_tokens,
)


class FakeProcess:
    """A stand-in for the object returned by ``create_subprocess_exec``."""

    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdin_received = None

    async def communicate(self, stdin=None):
        self.stdin_received = stdin
        return self._stdout, self._stderr


def install_fake_exec(monkeypatch, *, returncode=0, stdout=b"", stderr=b"", side_effect=None):
    """Patch ``asyncio.create_subprocess_exec`` and record each invocation.

    Returns the list that collects ``command`` lists so a test can assert on the
    argument vector actually dispatched.
    """
    calls = []
    proc = FakeProcess(returncode=returncode, stdout=stdout, stderr=stderr)

    async def fake_exec(*command, stdin=None, stdout=None, stderr=None):
        calls.append(list(command))
        if side_effect is not None:
            side_effect(list(command))
        return proc

    monkeypatch.setattr(lg.asyncio, "create_subprocess_exec", fake_exec)
    return calls, proc


def run_generate(generator, kind=ArtifactKind.ACTION_LOGIC, context=None, session="s1", user="u1"):
    return asyncio.run(generator.generate(kind, context or {"action": "list_things"}, session_id=session, user_id=user))


class TestReasoningRestriction:
    def test_deterministic_kind_rejected_without_dispatch(self, monkeypatch):
        calls, _ = install_fake_exec(monkeypatch, stdout=b"x")
        generator = LLMGenerator(CostController())

        with pytest.raises(ValueError):
            run_generate(generator, kind=ArtifactKind.BOILERPLATE)

        assert calls == []  # never dispatched

    def test_unknown_kind_rejected(self, monkeypatch):
        install_fake_exec(monkeypatch, stdout=b"x")
        generator = LLMGenerator(CostController())
        with pytest.raises(ValueError):
            run_generate(generator, kind="not_a_kind")

    @pytest.mark.parametrize(
        "kind",
        [ArtifactKind.ACTION_LOGIC, ArtifactKind.FIELD_DESCRIPTION, ArtifactKind.HELP_TEXT],
    )
    def test_each_reasoning_kind_dispatches(self, monkeypatch, kind):
        calls, _ = install_fake_exec(monkeypatch, stdout=b"generated\n")
        generator = LLMGenerator(CostController())

        result = run_generate(generator, kind=kind)

        assert result.kind is kind
        assert calls and "chat" in calls[0] and "--no-interactive" in calls[0]


class TestDispatch:
    def test_dispatches_scoped_prompt_on_stdin(self, monkeypatch):
        calls, proc = install_fake_exec(monkeypatch, stdout=b"def run(): pass\n")
        generator = LLMGenerator(CostController(), executable=["kiro", "generate"])

        result = run_generate(generator, context={"action": "get_thing"})

        assert calls[0][:2] == ["kiro", "generate"]
        # The scoped context is fed to the CLI on stdin.
        assert b"get_thing" in proc.stdin_received
        assert result.content == "def run(): pass"

    def test_records_reported_tokens_on_success(self, monkeypatch):
        install_fake_exec(monkeypatch, stdout=b'{"usage": {"total_tokens": 123}}')
        controller = CostController()
        generator = LLMGenerator(controller)

        result = run_generate(generator)

        assert result.measurement == TokenMeasurement(tokens=123, source=TOKEN_SOURCE_REPORTED)
        assert result.estimated is False
        assert result.session_total == 123
        assert controller.session_total("s1") == 123

    def test_estimates_tokens_when_no_figure_reported(self, monkeypatch):
        install_fake_exec(monkeypatch, stdout=b"some generated content without a usage line")
        controller = CostController()
        generator = LLMGenerator(controller)

        result = run_generate(generator)

        assert result.measurement.source == TOKEN_SOURCE_ESTIMATED
        assert result.estimated is True
        assert result.tokens > 0
        assert controller.session_total("s1") == result.tokens

    def test_accumulates_across_successful_invocations(self, monkeypatch):
        install_fake_exec(monkeypatch, stdout=b"tokens: 40")
        controller = CostController()
        generator = LLMGenerator(controller)

        first = run_generate(generator)
        second = run_generate(generator)

        assert first.session_total == 40
        assert second.session_total == 80


class TestCostGating:
    def test_budget_reached_blocks_before_dispatch(self, monkeypatch):
        calls, _ = install_fake_exec(monkeypatch, stdout=b"x")
        controller = CostController(token_budget=100)
        controller.record_usage("s1", 100, succeeded=True)  # exhaust the budget
        generator = LLMGenerator(controller)

        with pytest.raises(CostLimitError) as excinfo:
            run_generate(generator)

        assert calls == []  # nothing dispatched
        assert excinfo.value.decision.reason == "budget_reached"
        assert controller.session_total("s1") == 100  # unchanged

    def test_rate_limit_blocks_before_dispatch(self, monkeypatch):
        calls, _ = install_fake_exec(monkeypatch, stdout=b"tokens: 5")
        controller = CostController(rate_limit=1)
        generator = LLMGenerator(controller)

        run_generate(generator)  # consumes the single allowed slot
        with pytest.raises(CostLimitError) as excinfo:
            run_generate(generator)

        assert len(calls) == 1  # only the first was dispatched
        decision = excinfo.value.decision
        assert decision.reason == "rate_limited"
        assert 0 < decision.retry_after_seconds <= 60


class TestFailureHandling:
    def test_nonzero_exit_halts_and_excludes_from_total(self, monkeypatch):
        install_fake_exec(monkeypatch, returncode=2, stdout=b"", stderr=b"boom")
        controller = CostController()
        generator = LLMGenerator(controller)

        with pytest.raises(LLMGeneratorError) as excinfo:
            run_generate(generator)

        assert excinfo.value.returncode == 2
        assert excinfo.value.stderr == "boom"
        assert controller.session_total("s1") == 0  # excluded (Req 3.7)

    def test_missing_executable_halts_and_excludes(self, monkeypatch):
        async def fake_exec(*command, stdin=None, stdout=None, stderr=None):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(lg.asyncio, "create_subprocess_exec", fake_exec)
        controller = CostController()
        generator = LLMGenerator(controller, executable="does-not-exist")

        with pytest.raises(LLMGeneratorError) as excinfo:
            run_generate(generator)

        assert "not found" in str(excinfo.value)
        assert controller.session_total("s1") == 0

    def test_failure_does_not_block_subsequent_success(self, monkeypatch):
        # First invocation fails, second succeeds; only the success counts.
        controller = CostController()
        generator = LLMGenerator(controller)

        install_fake_exec(monkeypatch, returncode=1, stderr=b"nope")
        with pytest.raises(LLMGeneratorError):
            run_generate(generator)

        install_fake_exec(monkeypatch, stdout=b"total_tokens: 30")
        result = run_generate(generator)
        assert result.session_total == 30


class TestEstimateTokens:
    def test_empty_text_is_zero(self):
        assert estimate_tokens("") == 0

    def test_four_chars_per_token_rounds_up(self):
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("abcde") == 2  # ceil(5/4)


class TestParseReportedTokens:
    def test_whole_output_json(self):
        assert parse_reported_tokens('{"total_tokens": 42}') == 42

    def test_nested_usage_json(self):
        assert parse_reported_tokens('{"usage": {"total_tokens": 7, "prompt_tokens": 3}}') == 7

    def test_usage_line_among_other_output(self):
        assert parse_reported_tokens("generated code here\ntokens = 55\ndone") == 55

    def test_json_line_among_other_output(self):
        assert parse_reported_tokens('hello\n{"token_count": 9}\nbye') == 9

    def test_no_figure_returns_none(self):
        assert parse_reported_tokens("just some prose with no numbers") is None

    def test_empty_returns_none(self):
        assert parse_reported_tokens("") is None

    def test_negative_reported_ignored(self):
        assert parse_reported_tokens('{"total_tokens": -5}') is None
