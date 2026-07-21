"""Schema-aware breaking-change classifier (design Property 23, Req 12.2).

Compares two :class:`~icplugin_builder.core.spec_model.PluginSpec` trees and
decides whether the change from the *old* (previously exported) spec to the
*new* (current draft) spec is a **breaking** schema change.

A change is breaking **iff**, on an *existing* action or the connection
(present in both specs), one of the following holds:

* a field is removed from an input/output/connection schema,
* a field's ``type`` changes,
* a previously optional field is made required, or
* an existing action or the connection is removed entirely.

Everything else is non-breaking. In particular, adding a new optional field,
adding a whole new action/trigger/task, or making a required field optional is
never classified as breaking. Triggers and tasks are intentionally out of scope
for this classifier: Req 12.2 defines a breaking schema change strictly in
terms of existing *actions* and the *connection*.

The result carries a human-readable list of reasons so the version bumper can
record why a MAJOR bump was chosen in the plugin's ``version_history``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Mapping

from .spec_model import Component, FieldSchema, PluginSpec

__all__ = ["BreakingChangeResult", "classify_change", "is_breaking_change"]


@dataclass(frozen=True)
class BreakingChangeResult:
    """Outcome of comparing two specs.

    Attributes:
        is_breaking: ``True`` iff at least one breaking reason was found.
        reasons: One human-readable message per detected breaking change,
            in a stable order (connection first, then actions by name).
    """

    is_breaking: bool
    reasons: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.is_breaking


def is_breaking_change(old: PluginSpec, new: PluginSpec) -> bool:
    """Return ``True`` iff the change from ``old`` to ``new`` is breaking.

    Thin boolean wrapper around :func:`classify_change`; use that when the
    individual reasons are needed.
    """
    return classify_change(old, new).is_breaking


def classify_change(old: PluginSpec, new: PluginSpec) -> BreakingChangeResult:
    """Classify the change from ``old`` to ``new`` as breaking or not.

    Args:
        old: The previously exported spec (the baseline).
        new: The current draft spec.

    Returns:
        A :class:`BreakingChangeResult` whose ``is_breaking`` flag is ``True``
        iff any breaking condition from Req 12.2 is met, along with a reason
        for each condition detected.
    """
    reasons: List[str] = []

    # Connection: a single field map shared by the whole plugin.
    reasons.extend(_diff_field_map(old.connection, new.connection, location="connection"))

    # Actions: only actions that exist in the old spec are considered; brand
    # new actions are additions and never breaking.
    for name, old_action in old.actions.items():
        if name not in new.actions:
            reasons.append(f"action '{name}' was removed")
            continue
        reasons.extend(_diff_component(old_action, new.actions[name], action_name=name))

    return BreakingChangeResult(is_breaking=bool(reasons), reasons=reasons)


def _diff_component(old: Component, new: Component, *, action_name: str) -> List[str]:
    """Collect breaking reasons across an action's input and output schemas."""
    reasons: List[str] = []
    reasons.extend(_diff_field_map(old.input, new.input, location=f"action '{action_name}' input"))
    reasons.extend(_diff_field_map(old.output, new.output, location=f"action '{action_name}' output"))
    return reasons


def _diff_field_map(
    old_fields: Mapping[str, FieldSchema],
    new_fields: Mapping[str, FieldSchema],
    *,
    location: str,
) -> List[str]:
    """Collect breaking reasons between two field maps at ``location``.

    Only fields present in ``old_fields`` are inspected. A field is breaking
    when it is removed, its type changes, or it goes from optional to required.
    Fields that appear only in ``new_fields`` are additions and are ignored.
    """
    reasons: List[str] = []
    for name, old_field in old_fields.items():
        new_field = new_fields.get(name)
        if new_field is None:
            reasons.append(f"{location} field '{name}' was removed")
            continue
        if old_field.type != new_field.type:
            reasons.append(f"{location} field '{name}' type changed from '{old_field.type}' to '{new_field.type}'")
        if (not old_field.required) and new_field.required:
            reasons.append(f"{location} field '{name}' changed from optional to required")
    return reasons
