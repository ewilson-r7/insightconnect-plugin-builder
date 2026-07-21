"""Draft operations a single conversation turn may apply (task 20.3 support).

The :class:`~icplugin_builder.orchestrator.orchestrator.Orchestrator` is the only
component that mutates the in-session draft, and it does so through the small,
declarative operation objects defined here. Each operation is a pure,
**non-mutating** transformation ``apply(draft) -> Draft`` that either returns a
new :class:`~icplugin_builder.core.draft.Draft` or raises, so a sequence of
operations composes cleanly under the atomic apply wrapper: if any operation in
a turn raises, the wrapper preserves the pre-turn draft byte-for-byte (design
Property 2; Req 1.7, 15.1-15.4).

Two families of operation exist:

* **Named-component operations** (:class:`AddComponent`, :class:`ModifyComponent`,
  :class:`RemoveComponent`) delegate straight to the corresponding
  :class:`Draft` method, inheriting its component-preservation guarantee (every
  non-target component and its code is left byte-identical, Req 15.1-15.3) and
  its not-found rejection (Req 15.4).
* **Whole-spec operations** (:class:`SetConnection`, :class:`UpdateMetadata`)
  edit fields the :class:`Draft` does not expose per-name -- the plugin
  ``connection`` field map and the top-level metadata (name/title/description/
  version/vendor). They rebuild a new draft around a deep-copied spec so the
  receiver is never touched.

The classification of *which* operations a natural-language turn maps to is the
job of the upstream planner/LLM and is intentionally out of scope here; these
objects are the deterministic vocabulary the planner emits and the orchestrator
applies.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

from ..core.draft import ComponentKind, Draft
from ..core.spec_model import Component, FieldSchema, PluginSpec, SemVer

__all__ = [
    "DraftOperation",
    "AddComponent",
    "ModifyComponent",
    "RemoveComponent",
    "SetConnection",
    "UpdateMetadata",
]


class DraftOperation:
    """Base class for a single non-mutating draft operation.

    Subclasses implement :meth:`apply`, returning a new :class:`Draft`. They must
    never mutate the draft they are given, which is what lets the orchestrator
    run a whole turn's operations against an isolated copy and commit only on
    full success (design Property 2).
    """

    def apply(self, draft: Draft) -> Draft:  # pragma: no cover - abstract
        """Return a new :class:`Draft` with this operation applied."""
        raise NotImplementedError


@dataclass(frozen=True)
class AddComponent(DraftOperation):
    """Add a named action/trigger/task to the draft (Req 15.1, 22.1)."""

    kind: ComponentKind
    name: str
    component: Component
    code_files: Optional[Mapping[str, bytes]] = None

    def apply(self, draft: Draft) -> Draft:
        return draft.add_component(self.kind, self.name, self.component, self.code_files)


@dataclass(frozen=True)
class ModifyComponent(DraftOperation):
    """Replace an existing named component, preserving all others (Req 15.2).

    Rejects a name absent from the draft with the draft's own
    :class:`~icplugin_builder.core.draft.ComponentNotFoundError`, leaving the
    draft unchanged (Req 15.4).
    """

    kind: ComponentKind
    name: str
    component: Component
    code_files: Optional[Mapping[str, bytes]] = None

    def apply(self, draft: Draft) -> Draft:
        return draft.modify_component(self.kind, self.name, self.component, self.code_files)


@dataclass(frozen=True)
class RemoveComponent(DraftOperation):
    """Remove an existing named component, preserving all others (Req 15.3).

    Rejects a name absent from the draft, leaving it unchanged (Req 15.4).
    """

    kind: ComponentKind
    name: str

    def apply(self, draft: Draft) -> Draft:
        return draft.remove_component(self.kind, self.name)


@dataclass(frozen=True)
class SetConnection(DraftOperation):
    """Replace the plugin's ``connection`` field map (Req 22.1).

    The connection is a single field map rather than a name-keyed collection, so
    it is edited whole here rather than through the per-name draft operations.
    """

    connection: Dict[str, FieldSchema] = field(default_factory=dict)

    def apply(self, draft: Draft) -> Draft:
        new_spec = copy.deepcopy(draft.spec)
        new_spec.connection = copy.deepcopy(dict(self.connection))
        return Draft(spec=new_spec, code_files=dict(draft.code_files))


@dataclass(frozen=True)
class UpdateMetadata(DraftOperation):
    """Update top-level plugin metadata, leaving components untouched.

    Only the provided fields are changed; ``None`` fields are left as-is. This
    is a non-structural edit (it does not touch connection/actions/triggers/
    tasks), so it never triggers an ``insight-plugin refresh``.
    """

    name: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    version: Optional[SemVer] = None
    vendor: Optional[str] = None

    def apply(self, draft: Draft) -> Draft:
        new_spec: PluginSpec = copy.deepcopy(draft.spec)
        if self.name is not None:
            new_spec.name = self.name
        if self.title is not None:
            new_spec.title = self.title
        if self.description is not None:
            new_spec.description = self.description
        if self.version is not None:
            new_spec.version = self.version
        if self.vendor is not None:
            new_spec.vendor = self.vendor
        return Draft(spec=new_spec, code_files=dict(draft.code_files))
