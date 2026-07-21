"""Property test for the breaking-change classifier (task 2.4).

# Feature: insightconnect-plugin-builder, Property 23: Breaking-change classification

The unit tests in ``test_classifier.py`` pin specific examples; this module
covers the universal property across generated inputs: the classifier's verdict
must agree with the mutation's known breaking label for every applicable edit.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.classifier import classify_change, is_breaking_change
from tests import strategies as strat


@settings(max_examples=200)
@given(st.data())
def test_classification_agrees_with_mutation_label(data):
    """Property 23: breaking iff a removal/type-change/optional->required on an
    existing action or connection; additions are never breaking.

    A base spec is mutated by exactly one labeled edit whose breaking status is
    known. ``classify_change`` (old=base, new=mutated) must agree with that
    label.

    **Validates: Requirements 12.2**
    """
    base = data.draw(strat.mutatable_plugin_specs())
    mutation = data.draw(strat.labeled_mutations(base))

    result = classify_change(base, mutation.spec)

    assert result.is_breaking == mutation.breaking
    # The boolean wrapper must agree with the full classification.
    assert is_breaking_change(base, mutation.spec) == mutation.breaking
    # A breaking verdict must be justified with at least one reason, and a
    # non-breaking verdict must carry none.
    assert bool(result.reasons) == mutation.breaking
