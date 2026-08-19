"""Property 72: the session total is the sum of the paid calls that succeeded.

Covers design Property 72 with Hypothesis, over arbitrary interleavings of
successful and failed ``Interpreter`` and ``Plugin_Agent`` invocations: the
cumulative session total equals the sum of the successful invocations, the
interpreter included, and a failed invocation contributes nothing (Req 2.18,
consistent with parent Property 9 and Req 3.5-3.7).

Why this is a property rather than an example. `bugfix.md` 1.13 recorded a total
that sat at zero through five interpretations and then jumped when the agent ran,
because :class:`Interpreter` held no controller at all. The example-based tests in
``test_preview_fidelity_bug_conditions.py`` pin that one recorded sequence. What
they cannot see is an accounting error that depends on *order* -- a total that is
correct for interpret-then-agent and wrong for agent-then-interpret, or one that
drops a call following a failure. Generating the interleaving is the point.

Both invocations run through their real production methods, including the code that
decides success from the exit status, so what is asserted is that a paid call reaches
the controller from where it actually happens. Only the subprocess is substituted --
in-process rather than as a stand-in binary, because a property at 200 examples would
otherwise spawn thousands of processes to prove something about accounting.

The reference sum is built from each invocation's *observed* contribution (the total
before it subtracted from the total after), never from a re-implementation of the
billing formula. Recomputing ``estimate_tokens`` here would assert only that this
file and ``llm_generator`` agree with each other.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Sequence, Tuple

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.integrations import plugin_agent as agent_module
from icplugin_builder.integrations.plugin_agent import PluginAgent, PluginAgentError
from icplugin_builder.orchestrator import interpreter as interpreter_module
from icplugin_builder.orchestrator.interpreter import Interpreter, InterpreterError

#: What the interpreter's stand-in prints. Has to parse as a plan, or a successful
#: exit status would still raise and the invocation would not be a success.
PLAN_RESPONSE = json.dumps(
    {
        "operations": [],
        "reasoning": [],
        "clarification": None,
        "vendor_api": None,
    }
)

#: What the agent's stand-in prints. Non-empty, because the agent bills the
#: instruction plus the transcript and an empty transcript would narrow the margin
#: this test relies on for "a successful call contributes something".
AGENT_RESPONSE = "wrote the connection, the API client and three actions\n"

INTERPRETER = "interpreter"
AGENT = "agent"

#: Fixed per kind, so the same multiset of invocations bills the same amount however
#: it is ordered. Order independence is only a meaningful claim if the content does
#: not vary with position.
MESSAGE = "Add an action that lists users."
INSTRUCTION = "Implement the actions and their unit tests."


class _FakeProcess:
    """Stands in for what ``create_subprocess_exec`` returns."""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self._stdout = stdout.encode("utf-8")

    async def communicate(self, _input: bytes | None = None) -> Tuple[bytes, bytes]:
        return self._stdout, b"" if self.returncode == 0 else b"the stand-in failed"


@contextmanager
def _substituted_cli(outcomes: List[Tuple[str, bool]]) -> Iterator[None]:
    """Answer both modules' subprocess calls from ``outcomes``, in order.

    Patched per module rather than on :mod:`asyncio` itself, because that is where
    each caller looks the function up, and patching the shared module would also
    silence anything else the event loop starts.
    """
    pending = list(outcomes)

    def _next(response: str):
        kind, succeeded = pending.pop(0)
        return _FakeProcess(0 if succeeded else 1, response if succeeded else "")

    async def fake_interpreter_exec(*_command, **_kwargs):
        return _next(PLAN_RESPONSE)

    async def fake_agent_exec(*_command, **_kwargs):
        return _next(AGENT_RESPONSE)

    original_interpreter = interpreter_module.asyncio.create_subprocess_exec
    original_agent = agent_module.asyncio.create_subprocess_exec
    interpreter_module.asyncio.create_subprocess_exec = fake_interpreter_exec
    try:
        # The two modules import the same `asyncio`, so one assignment is visible to
        # both. Kept explicit so the agent's boundary is named rather than assumed.
        agent_module.asyncio.create_subprocess_exec = fake_agent_exec
        yield
    finally:
        interpreter_module.asyncio.create_subprocess_exec = original_interpreter
        agent_module.asyncio.create_subprocess_exec = original_agent


async def _drive(sequence: Sequence[Tuple[str, bool]], root: Path, cost: CostController) -> List[Tuple[str, bool, int]]:
    """Perform ``sequence`` against one session, returning each call's contribution.

    Returns ``(kind, succeeded, delta)`` per invocation, where ``delta`` is what the
    session total moved by. A failed invocation raises from production code; that is
    the path that must still record nothing, so it is driven rather than skipped.
    """
    observed: List[Tuple[str, bool, int]] = []
    interpreter = Interpreter(executable="stand-in-cli", cost_controller=cost)
    agent = PluginAgent(cost, executable="stand-in-cli")

    for kind, succeeded in sequence:
        before = cost.session_total("s1")
        if kind == INTERPRETER:
            if succeeded:
                await interpreter.interpret(MESSAGE, None, session_id="s1", user_id="u1")
            else:
                with pytest.raises(InterpreterError):
                    await interpreter.interpret(MESSAGE, None, session_id="s1", user_id="u1")
        else:
            if succeeded:
                await agent.implement(root, INSTRUCTION, session_id="s1", user_id="u1")
            else:
                with pytest.raises(PluginAgentError):
                    await agent.implement(root, INSTRUCTION, session_id="s1", user_id="u1")
        observed.append((kind, succeeded, cost.session_total("s1") - before))

    return observed


def _controller() -> CostController:
    """A controller whose limits cannot bind.

    ``PluginAgent.implement`` authorizes before running, so the budget and the rate
    limit are both reachable from here. A refused invocation is a different
    behaviour with its own requirement (Req 4.x) and would make this property
    order-dependent for a reason that has nothing to do with accounting, so both
    limits are set clear of anything these short invocations bill.
    """
    return CostController(token_budget=10_000_000, rate_limit=1000)


def sequences() -> st.SearchStrategy[List[Tuple[str, bool]]]:
    """Interleavings of both kinds of paid call, each independently pass or fail.

    Bounded at 12 because the claim is about interleaving rather than volume, and
    every ordering that distinguishes a correct total from a sequence-dependent one
    is reachable well below that. Empty sequences are included: a session that made
    no paid call totals zero, which is the baseline the recorded bug's ``0`` was
    mistaken for.
    """
    return st.lists(
        st.tuples(st.sampled_from((INTERPRETER, AGENT)), st.booleans()),
        max_size=12,
    )


# Feature: export-gate-and-preview-fidelity, Property 72: The session token total equals the sum of the
# successful paid invocations, the interpreter included; failed invocations are excluded
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(sequence=sequences())
def test_the_total_is_the_sum_of_the_paid_calls_that_succeeded(sequence, tmp_path_factory):
    """Every successful call adds its own cost; every failed one adds nothing.

    **Validates: Requirements 2.18**
    """
    root = tmp_path_factory.mktemp("tree")
    cost = _controller()

    with _substituted_cli(list(sequence)):
        observed = asyncio.run(_drive(sequence, root, cost))

    total = cost.session_total("s1")

    for kind, succeeded, delta in observed:
        if succeeded:
            # The recorded defect in one line: an interpretation that ran, was paid
            # for, and moved the total by nothing.
            assert delta > 0, (
                f"a successful {kind} invocation moved the session total by {delta}. It ran through the "
                "production path and returned a result, so it was paid for and has to be counted"
            )
        else:
            assert delta == 0, (
                f"a failed {kind} invocation added {delta} tokens to the session total. A failed "
                "invocation is excluded (Req 3.7), so the operator is not charged for it"
            )

    successful = sum(delta for _kind, succeeded, delta in observed if succeeded)
    assert total == successful, (
        f"the session total is {total} where the successful invocations sum to {successful} "
        f"over {[(kind, ok) for kind, ok, _ in observed]}"
    )
    assert total == sum(delta for _kind, _ok, delta in observed)
    assert isinstance(total, int) and not isinstance(total, bool)
    assert total >= 0


# Feature: export-gate-and-preview-fidelity, Property 72: The session token total equals the sum of the
# successful paid invocations, the interpreter included; failed invocations are excluded
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(sequence=sequences(), seed=st.integers(min_value=0, max_value=2**32 - 1))
def test_the_total_does_not_depend_on_the_order_the_calls_arrived_in(sequence, seed, tmp_path_factory):
    """The same calls in a different order reach the same total.

    A sum has this property and a total maintained by ad-hoc bookkeeping may not.
    This is what generating the interleaving buys over the recorded sequence: an
    error that only appears when an agent run precedes an interpretation, or when a
    failure sits between two successes, shows up here and nowhere else.

    **Validates: Requirements 2.18**
    """
    import random

    shuffled = list(sequence)
    random.Random(seed).shuffle(shuffled)

    first_cost = _controller()
    with _substituted_cli(list(sequence)):
        asyncio.run(_drive(sequence, tmp_path_factory.mktemp("first"), first_cost))

    second_cost = _controller()
    with _substituted_cli(shuffled):
        asyncio.run(_drive(shuffled, tmp_path_factory.mktemp("second"), second_cost))

    assert first_cost.session_total("s1") == second_cost.session_total("s1"), (
        f"{[(k, ok) for k, ok in sequence]} totals {first_cost.session_total('s1')} but the same calls as "
        f"{[(k, ok) for k, ok in shuffled]} total {second_cost.session_total('s1')}"
    )
