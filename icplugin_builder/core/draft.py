"""In-session Draft: a :class:`~icplugin_builder.core.spec_model.PluginSpec`
paired with its associated hand-written code files, plus targeted operations on
a single named component (design Property 1; Req 1.3, 2.3, 15.1, 15.2, 15.3,
22.1, 22.2).

The :class:`Draft` is the mutable-looking working state of a plugin during a
conversation session, but every operation on it is **non-mutating**: an
operation returns a *new* :class:`Draft` and leaves the receiver untouched. This
gives two guarantees for free:

* **Component preservation** -- an ``add``/``modify``/``remove`` targeting one
  named component leaves every *other* component in the spec, and every
  hand-written code file that does not belong to the target, byte-identical
  before and after (design Property 1).
* **A clean seam for atomicity and not-found handling** -- because operations
  never touch the receiver, the atomic apply wrapper (task 4.5) can commit a
  returned draft only on success, and not-found handling (task 4.3) can build
  on the :class:`ComponentNotFoundError` raised here without any risk that a
  rejected operation left a partial mutation behind.

A *named component* is an action, trigger, or task -- the three name-keyed
component collections in a spec. The plugin ``connection`` is a single field map
rather than a name-keyed collection and is out of scope for these per-name
operations.

Associated code files are modeled as a flat mapping of repository-relative path
to file content. A component "owns" the files under its conventional package
directory (``actions/<name>/``, ``triggers/<name>/``, ``tasks/<name>/``); those
owned files are the only files a targeted operation on that component may add,
replace, or delete.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, Optional

from .spec_model import Component, PluginSpec

__all__ = [
    "ComponentKind",
    "Draft",
    "DraftError",
    "ComponentNotFoundError",
    "ComponentExistsError",
]


class ComponentKind(Enum):
    """The three kinds of named component a :class:`Draft` operates on."""

    ACTION = "action"
    TRIGGER = "trigger"
    TASK = "task"


#: Conventional package sub-directory that holds each kind's code files. Used to
#: decide which code files a named component owns.
_KIND_DIRECTORY: Dict[ComponentKind, str] = {
    ComponentKind.ACTION: "actions",
    ComponentKind.TRIGGER: "triggers",
    ComponentKind.TASK: "tasks",
}


class DraftError(Exception):
    """Base class for draft operation errors."""


class ComponentNotFoundError(DraftError):
    """Raised when a modify/remove targets a component name absent from the draft.

    The operation raises *before* producing a new draft, so the receiver is
    left unchanged (Req 15.4). The error carries the rejected ``operation``
    verb, the component ``kind``, the missing ``name``, and the ``available``
    names of that kind so the caller can surface a clear, user-facing not-found
    message without re-deriving any of it. The exception's own string form
    *is* that user-facing message.

    Attributes:
        operation: the rejected operation verb (``"modify"`` or ``"remove"``).
        kind: the component kind that was targeted.
        name: the component name that was not found.
        available: the sorted names of existing components of ``kind``.
    """

    def __init__(
        self,
        operation: str,
        kind: "ComponentKind",
        name: str,
        available: Optional[List[str]] = None,
    ) -> None:
        self.operation = operation
        self.kind = kind
        self.name = name
        self.available = sorted(available) if available else []
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        """Compose the user-facing not-found message naming the component."""
        message = (
            f"Cannot {self.operation} {self.kind.value} '{self.name}': "
            f"no {self.kind.value} named '{self.name}' exists in the draft."
        )
        if self.available:
            existing = ", ".join(f"'{existing_name}'" for existing_name in self.available)
            message += f" Available {self.kind.value}s: {existing}."
        else:
            message += f" The draft has no {self.kind.value}s."
        return message


class ComponentExistsError(DraftError):
    """Raised when an add targets a component name already present in the draft."""


@dataclass
class Draft:
    """A plugin draft: a :class:`PluginSpec` plus its hand-written code files.

    Attributes:
        spec: the current typed plugin spec.
        code_files: a mapping of repository-relative path to file content
            (``bytes`` or ``str``) for hand-written code. Generated/derived
            files are not tracked here; only the hand-written surface that a
            component owns needs to be preserved across targeted operations.
    """

    spec: PluginSpec = field(default_factory=PluginSpec)
    code_files: Dict[str, bytes] = field(default_factory=dict)

    # --- Queries -----------------------------------------------------------

    def has_component(self, kind: ComponentKind, name: str) -> bool:
        """Return ``True`` iff a component of ``kind`` named ``name`` exists."""
        return name in self._component_map(self.spec, kind)

    def owned_code_files(self, kind: ComponentKind, name: str) -> Dict[str, bytes]:
        """Return the subset of ``code_files`` owned by the named component.

        Ownership is by convention: a path is owned when it lies under the
        component's package directory (e.g. ``.../actions/<name>/...``).
        """
        directory = _KIND_DIRECTORY[kind]
        return {path: content for path, content in self.code_files.items() if _owns(path, directory, name)}

    # --- Targeted operations (all non-mutating) ----------------------------

    def add_component(
        self,
        kind: ComponentKind,
        name: str,
        component: Component,
        code_files: Optional[Mapping[str, bytes]] = None,
    ) -> "Draft":
        """Return a new draft with a named component added.

        Args:
            kind: the component kind (action, trigger, or task).
            name: the new component's name; must not already exist for ``kind``.
            component: the component definition to add.
            code_files: optional hand-written code files to associate with the
                new component. They are added to the draft's code files as-is.

        Returns:
            A new :class:`Draft`. Every pre-existing component and every
            pre-existing code file is byte-identical in the returned draft.

        Raises:
            ValueError: if ``name`` is empty.
            ComponentExistsError: if a component of ``kind`` named ``name``
                already exists.
        """
        _require_name(name)
        if self.has_component(kind, name):
            raise ComponentExistsError(f"{kind.value} '{name}' already exists in the draft")
        new = self._clone()
        self._component_map(new.spec, kind)[name] = copy.deepcopy(component)
        if code_files:
            new.code_files.update(dict(code_files))
        return new

    def modify_component(
        self,
        kind: ComponentKind,
        name: str,
        component: Component,
        code_files: Optional[Mapping[str, bytes]] = None,
    ) -> "Draft":
        """Return a new draft with an existing named component replaced.

        Args:
            kind: the component kind (action, trigger, or task).
            name: the target component's name; must already exist for ``kind``.
            component: the replacement component definition.
            code_files: optional replacement hand-written code files for the
                component. When provided, the component's previously owned code
                files are removed and replaced with these; when ``None``, the
                component's existing code files are left unchanged.

        Returns:
            A new :class:`Draft` in which only the target component and (when
            ``code_files`` is provided) its owned code files differ; every other
            component and code file is byte-identical.

        Raises:
            ValueError: if ``name`` is empty.
            ComponentNotFoundError: if no component of ``kind`` named ``name``
                exists.
        """
        _require_name(name)
        self._require_existing("modify", kind, name)
        new = self._clone()
        self._component_map(new.spec, kind)[name] = copy.deepcopy(component)
        if code_files is not None:
            _drop_owned(new.code_files, kind, name)
            new.code_files.update(dict(code_files))
        return new

    def remove_component(self, kind: ComponentKind, name: str) -> "Draft":
        """Return a new draft with an existing named component removed.

        The component and every code file it owns are dropped; all other
        components and code files are byte-identical in the returned draft.

        Args:
            kind: the component kind (action, trigger, or task).
            name: the target component's name; must already exist for ``kind``.

        Returns:
            A new :class:`Draft` without the named component or its owned code.

        Raises:
            ValueError: if ``name`` is empty.
            ComponentNotFoundError: if no component of ``kind`` named ``name``
                exists.
        """
        _require_name(name)
        self._require_existing("remove", kind, name)
        new = self._clone()
        del self._component_map(new.spec, kind)[name]
        _drop_owned(new.code_files, kind, name)
        return new

    # --- Internals ---------------------------------------------------------

    def _require_existing(self, operation: str, kind: ComponentKind, name: str) -> None:
        """Reject ``operation`` on a missing named component (Req 15.4).

        Raises :class:`ComponentNotFoundError` *before* any clone/mutation when
        no component of ``kind`` named ``name`` exists, so the receiving draft
        is provably left unchanged. The raised error carries a clear,
        user-facing not-found message plus the available names of that kind.
        """
        if not self.has_component(kind, name):
            available = list(self._component_map(self.spec, kind))
            raise ComponentNotFoundError(operation, kind, name, available)

    def _clone(self) -> "Draft":
        """Return an independent deep copy of this draft.

        The spec is deep-copied so mutating the copy cannot reach back into the
        receiver's nested components/fields; code-file values are immutable
        ``bytes``/``str`` and so are shared, but the containing dict is fresh.
        """
        return Draft(spec=copy.deepcopy(self.spec), code_files=dict(self.code_files))

    @staticmethod
    def _component_map(spec: PluginSpec, kind: ComponentKind) -> Dict[str, Component]:
        """Return the name-keyed component map on ``spec`` for ``kind``."""
        if kind is ComponentKind.ACTION:
            return spec.actions
        if kind is ComponentKind.TRIGGER:
            return spec.triggers
        if kind is ComponentKind.TASK:
            return spec.tasks
        raise TypeError(f"unknown component kind: {kind!r}")


def _require_name(name: str) -> None:
    """Validate that ``name`` is a non-empty component name."""
    if not isinstance(name, str) or not name:
        raise ValueError("component name must be a non-empty string")


def _drop_owned(code_files: Dict[str, bytes], kind: ComponentKind, name: str) -> None:
    """Delete, in-place, every code file owned by the named component."""
    directory = _KIND_DIRECTORY[kind]
    owned: List[str] = [path for path in code_files if _owns(path, directory, name)]
    for path in owned:
        del code_files[path]


def _owns(path: str, directory: str, name: str) -> bool:
    """Return ``True`` iff ``path`` lies under ``<directory>/<name>/``.

    Path separators are normalized so both ``/`` and ``\\`` layouts match. A
    path owns a component when its segments contain the consecutive pair
    ``[directory, name]`` (e.g. ``icon_foo/actions/create/action.py`` is owned
    by the action ``create``).
    """
    segments = [segment for segment in path.replace("\\", "/").split("/") if segment]
    for index in range(len(segments) - 1):
        if segments[index] == directory and segments[index + 1] == name:
            return True
    return False
