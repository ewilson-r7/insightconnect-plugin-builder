"""Tests for the interpretation layer's judgment about vendor APIs (Req 28.12).

Whether a request means to call somebody's HTTP API is a question about intent, so
it is answered where the user's words are read rather than by a check on the tree.
That makes it the one input to the definition-of-done surface that is a judgment,
and it can be wrong in both directions -- a missed vendor produces a plugin built on
guessed endpoints, and a phantom vendor produces a pointless question. These tests
pin down the parsing around that judgment: what the field means, and that the
shapes a model actually emits for "no vendor" are all read as no vendor.

Only the response parsing is exercised. The interpreter contacts the Kiro CLI,
which is not run here.
"""

import json

from icplugin_builder.orchestrator.interpreter import Interpreter, _parse_vendor_api


def _plan(payload: dict):
    """Parse a model response into a TurnPlan without invoking the CLI."""
    return Interpreter(executable="kiro")._parse_response(json.dumps(payload))


def _action_payload(**extra):
    payload = {
        "operations": [],
        "reasoning": [{"kind": "action_logic", "parameters": {"action": "check_ip"}}],
        "clarification": None,
    }
    payload.update(extra)
    return payload


class TestVendorApiField:
    def test_a_named_vendor_is_carried_onto_the_plan(self):
        plan = _plan(_action_payload(vendor_api="AbuseIPDB"))
        assert plan.vendor_api == "AbuseIPDB"

    def test_an_absent_field_means_no_vendor(self):
        plan = _plan(_action_payload())
        assert plan.vendor_api is None

    def test_a_null_vendor_means_no_vendor(self):
        assert _plan(_action_payload(vendor_api=None)).vendor_api is None

    def test_the_strings_a_model_uses_for_nothing_are_all_read_as_nothing(self):
        # A model asked for `null | "name"` will sometimes send the word. Treating
        # any of these as a vendor name would produce a request for documentation
        # about a vendor called "null".
        for raw in ("null", "none", "None", "N/A", "na", "false", "", "   "):
            assert _parse_vendor_api(raw) is None, raw

    def test_a_non_string_is_ignored_rather_than_coerced(self):
        for raw in (0, 1, True, False, [], {}, ["Okta"]):
            assert _parse_vendor_api(raw) is None, raw

    def test_surrounding_whitespace_is_trimmed(self):
        assert _parse_vendor_api("  CrowdStrike \n") == "CrowdStrike"


class TestProceedWithoutReference:
    def test_it_defaults_to_false(self):
        assert _plan(_action_payload()).proceed_without_reference is False

    def test_only_a_real_true_counts(self):
        # Never inferred from silence, and never from a truthy-looking value: this
        # flag is the difference between asking the user and building on guesses.
        for raw in ("true", "yes", 1, "1", None, "", [], {}):
            assert _plan(_action_payload(proceed_without_reference=raw)).proceed_without_reference is False, raw
        assert _plan(_action_payload(proceed_without_reference=True)).proceed_without_reference is True


class TestJudgmentSurvivesAClarifyingTurn:
    def test_both_fields_are_carried_on_a_clarification(self):
        # A user answering an ambiguity question while also declining to supply
        # documentation should not have to say the second part twice.
        plan = _plan(
            {
                "operations": [],
                "reasoning": [],
                "clarification": "Which IP field did you mean?",
                "vendor_api": "AbuseIPDB",
                "proceed_without_reference": True,
            }
        )
        assert plan.is_ambiguous
        assert plan.vendor_api == "AbuseIPDB"
        assert plan.proceed_without_reference is True

    def test_both_fields_are_carried_when_nothing_actionable_was_produced(self):
        plan = _plan({"operations": [], "reasoning": [], "vendor_api": "Okta"})
        assert plan.is_ambiguous  # nothing to do -> clarification
        assert plan.vendor_api == "Okta"


class TestPromptGuidance:
    """The prompt has to explain the distinction, since the model makes the call."""

    def test_it_asks_for_the_field_and_explains_both_failure_directions(self):
        from icplugin_builder.orchestrator.interpreter import _SYSTEM_PROMPT

        assert "vendor_api" in _SYSTEM_PROMPT
        assert "invent endpoints" in _SYSTEM_PROMPT
        # Both ways of being wrong are named, so the model is not pushed toward
        # answering "vendor" for everything to be safe. Whitespace is collapsed
        # because the prompt is hard-wrapped and the phrases straddle line breaks.
        collapsed = " ".join(_SYSTEM_PROMPT.split())
        assert "A false null produces a plugin built on guesses" in collapsed
        assert "a false vendor name produces a pointless question" in collapsed

    def test_it_gives_examples_of_local_only_work(self):
        from icplugin_builder.orchestrator.interpreter import _SYSTEM_PROMPT

        for example in ("encoding", "hashing", "regular expressions"):
            assert example in _SYSTEM_PROMPT, example

    def test_it_forbids_inferring_the_override_from_silence(self):
        from icplugin_builder.orchestrator.interpreter import _SYSTEM_PROMPT

        assert "Never infer it from silence" in _SYSTEM_PROMPT
