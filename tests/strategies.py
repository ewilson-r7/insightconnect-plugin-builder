"""Shared Hypothesis strategies for generating and mutating ``PluginSpec`` trees.

This module is the single source of generators used by the property-based
tests. It composes:

* valid ``name`` (snake_case), :class:`SemVer`, and ``vendor`` values;
* :class:`FieldSchema` values across every field family the spec supports --
  scalar (``string``/``integer``/``float``/``boolean``/``bytes``/``date``/
  ``password``), complex (``object``, ``[]string``, ``[]<type>``), and
  credential (``credential_secret_key``/``credential_username_password``/
  ``credential_asymmetric_key``);
* randomized ``types``/``connection``/``actions``/``triggers``/``tasks`` maps;
* whole :func:`plugin_specs` documents.

It also exposes **labeled mutation strategies** (:func:`labeled_mutations`) that
apply one of six edits to an existing spec and report the edit's label and
whether it is a breaking change. These back the breaking-change classifier
test (design Property 23) and the component-preservation test (design
Property 1), each of which needs to know the mutation that was applied.

The strategies deliberately generate *structurally* valid specs (unique keys,
valid semver, snake_case names); they do not attempt to satisfy every semantic
rule of the InsightConnect schema, which is the concern of the Spec_Validator.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from hypothesis import strategies as st

from icplugin_builder.core.spec_model import (
    Component,
    FieldSchema,
    PluginSpec,
    SemVer,
)

__all__ = [
    "SCALAR_TYPES",
    "COMPLEX_TYPES",
    "CREDENTIAL_TYPES",
    "ALL_FIELD_TYPES",
    "MUTATION_LABELS",
    "BREAKING_LABELS",
    "NON_BREAKING_LABELS",
    "LabeledMutation",
    "snake_case_names",
    "semvers",
    "vendors",
    "field_types",
    "field_schemas",
    "components",
    "plugin_specs",
    "mutatable_plugin_specs",
    "labeled_mutations",
]

# --- Field-type vocabulary -------------------------------------------------

#: Scalar field types (single primitive values).
SCALAR_TYPES: Tuple[str, ...] = (
    "string",
    "integer",
    "float",
    "boolean",
    "bytes",
    "date",
    "password",
)

#: Complex field types (objects and typed arrays).
COMPLEX_TYPES: Tuple[str, ...] = (
    "object",
    "[]string",
    "[]integer",
    "[]object",
)

#: Credential field types (InsightConnect credential unions).
CREDENTIAL_TYPES: Tuple[str, ...] = (
    "credential_secret_key",
    "credential_username_password",
    "credential_asymmetric_key",
)

#: Every field type a generated ``FieldSchema`` may take.
ALL_FIELD_TYPES: Tuple[str, ...] = SCALAR_TYPES + COMPLEX_TYPES + CREDENTIAL_TYPES

# --- Mutation labels -------------------------------------------------------

#: Non-breaking edits: adding new optional surface never breaks consumers.
NON_BREAKING_LABELS: Tuple[str, ...] = ("add-optional-field", "add-action")

#: Breaking edits applied to an *existing* action or connection.
BREAKING_LABELS: Tuple[str, ...] = (
    "remove-field",
    "change-type",
    "optional-to-required",
    "remove-component",
)

#: Every mutation label this module can produce.
MUTATION_LABELS: Tuple[str, ...] = NON_BREAKING_LABELS + BREAKING_LABELS


@dataclass
class LabeledMutation:
    """The result of applying one labeled edit to a base :class:`PluginSpec`.

    Attributes:
        label: which edit was applied (one of :data:`MUTATION_LABELS`).
        breaking: whether the edit is a breaking schema change per design
            Property 23 (removal/type-change/optional->required on an existing
            action or connection, or removing an existing action/connection).
        spec: the mutated spec (a deep copy of the base with the edit applied).
        target_kind: the surface the edit touched -- ``"action"``,
            ``"connection"``, or ``None`` for edits that only add new surface.
        target_name: the name of the affected component/field, or ``None`` when
            the edit only adds new surface.
    """

    label: str
    breaking: bool
    spec: PluginSpec
    target_kind: Optional[str] = None
    target_name: Optional[str] = None


# --- Scalar building blocks ------------------------------------------------


def _safe_text(max_size: int = 40) -> st.SearchStrategy[str]:
    """Generate round-trip-safe free text for titles/descriptions.

    Restricted to printable ASCII with leading/trailing whitespace stripped so
    that values survive the YAML codec unchanged (control characters such as
    U+0085 and Unicode line separators are treated as line breaks by YAML and
    would not round trip).
    """
    return st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        min_size=0,
        max_size=max_size,
    ).map(str.strip)


def snake_case_names() -> st.SearchStrategy[str]:
    """Generate valid snake_case identifiers (letters/digits/underscores).

    Names start with a lowercase letter and contain only lowercase letters,
    digits, and single underscores, matching the plugin/component/field naming
    convention used throughout ``plugin.spec.yaml``.
    """
    first = st.sampled_from("abcdefghijklmnopqrstuvwxyz")
    rest = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=0, max_size=11)
    return st.builds(lambda head, tail: (head + tail).strip("_"), first, rest).map(lambda s: s or "x")


def semvers() -> st.SearchStrategy[SemVer]:
    """Generate :class:`SemVer` values with small, comparable components."""
    part = st.integers(min_value=0, max_value=50)
    return st.builds(SemVer, part, part, part)


def vendors() -> st.SearchStrategy[str]:
    """Generate vendor strings, including some already carrying ``_custom``.

    Mixing plain and ``_custom``-suffixed vendors exercises the idempotent
    suffixing logic downstream.
    """
    base = snake_case_names()
    return st.one_of(base, base.map(lambda v: f"{v}_custom"))


def field_types() -> st.SearchStrategy[str]:
    """Generate a field ``type`` drawn from every supported family."""
    return st.sampled_from(list(ALL_FIELD_TYPES))


@st.composite
def field_schemas(draw: st.DrawFn, required: Optional[bool] = None) -> FieldSchema:
    """Generate a :class:`FieldSchema` across all field families.

    Args:
        required: when given, forces the field's ``required`` flag; otherwise
            it is generated. Fixing it lets mutation strategies produce fields
            with a known optionality.
    """
    ftype = draw(field_types())
    is_required = draw(st.booleans()) if required is None else required
    optional_text = st.one_of(st.none(), _safe_text(max_size=20))
    return FieldSchema(
        type=ftype,
        required=is_required,
        title=draw(optional_text),
        description=draw(optional_text),
        order=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=20))),
    )


@st.composite
def _field_maps(draw: st.DrawFn, min_size: int = 0, max_size: int = 4) -> Dict[str, FieldSchema]:
    """Generate a ``{field_name: FieldSchema}`` map with unique names."""
    names = draw(st.lists(snake_case_names(), min_size=min_size, max_size=max_size, unique=True))
    return {name: draw(field_schemas()) for name in names}


@st.composite
def components(draw: st.DrawFn) -> Component:
    """Generate a :class:`Component` (action/trigger/task) with I/O fields."""
    optional_text = st.one_of(st.none(), _safe_text(max_size=30))
    return Component(
        title=draw(optional_text),
        description=draw(optional_text),
        input=draw(_field_maps(min_size=0, max_size=4)),
        output=draw(_field_maps(min_size=0, max_size=4)),
    )


@st.composite
def _component_maps(draw: st.DrawFn, min_size: int = 0, max_size: int = 3) -> Dict[str, Component]:
    """Generate a ``{component_name: Component}`` map with unique names."""
    names = draw(st.lists(snake_case_names(), min_size=min_size, max_size=max_size, unique=True))
    return {name: draw(components()) for name in names}


@st.composite
def _type_maps(draw: st.DrawFn, min_size: int = 0, max_size: int = 2) -> Dict[str, Dict[str, FieldSchema]]:
    """Generate the ``types`` map: ``{type_name: {field_name: FieldSchema}}``."""
    names = draw(st.lists(snake_case_names(), min_size=min_size, max_size=max_size, unique=True))
    return {name: draw(_field_maps(min_size=1, max_size=3)) for name in names}


@st.composite
def plugin_specs(
    draw: st.DrawFn,
    min_actions: int = 0,
    min_connection_fields: int = 0,
) -> PluginSpec:
    """Generate a structurally valid :class:`PluginSpec`.

    Args:
        min_actions: minimum number of actions to include.
        min_connection_fields: minimum number of connection fields to include.

    The ``types``, ``triggers``, and ``tasks`` maps are always randomized.
    """
    return PluginSpec(
        name=draw(snake_case_names()),
        title=draw(_safe_text(max_size=40)),
        description=draw(_safe_text(max_size=80)),
        version=draw(semvers()),
        vendor=draw(vendors()),
        connection=draw(_field_maps(min_size=min_connection_fields, max_size=4)),
        actions=draw(_component_maps(min_size=min_actions, max_size=3)),
        triggers=draw(_component_maps(min_size=0, max_size=2)),
        tasks=draw(_component_maps(min_size=0, max_size=2)),
        types=draw(_type_maps(min_size=0, max_size=2)),
    )


def mutatable_plugin_specs() -> st.SearchStrategy[PluginSpec]:
    """Generate specs guaranteed to have a mutation target.

    Every spec has at least one action and at least one connection field so the
    breaking mutations (remove-field, change-type, optional->required,
    remove-component) always have a valid target.
    """
    return plugin_specs(min_actions=1, min_connection_fields=1)


# --- Mutation helpers ------------------------------------------------------


def _fresh_name(existing: List[str], candidate: str) -> str:
    """Return ``candidate`` made unique against ``existing`` names."""
    name = candidate
    suffix = 0
    while name in existing:
        suffix += 1
        name = f"{candidate}_{suffix}"
    return name


def _existing_field_locations(spec: PluginSpec) -> List[Tuple[str, str, str]]:
    """Enumerate ``(kind, owner, field_name)`` for actions' inputs and connection.

    ``kind`` is ``"action"`` or ``"connection"``; ``owner`` is the action name
    (or ``""`` for the connection). Only actions and the connection are
    enumerated because those are the surfaces the breaking-change classifier
    reasons about.
    """
    locations: List[Tuple[str, str, str]] = []
    for field_name in spec.connection:
        locations.append(("connection", "", field_name))
    for action_name, action in spec.actions.items():
        for field_name in action.input:
            locations.append(("action", action_name, field_name))
        for field_name in action.output:
            locations.append(("action", action_name, field_name))
    return locations


def _get_field(spec: PluginSpec, kind: str, owner: str, field_name: str) -> FieldSchema:
    """Return the :class:`FieldSchema` at a location produced by the enumerator."""
    if kind == "connection":
        return spec.connection[field_name]
    action = spec.actions[owner]
    if field_name in action.input:
        return action.input[field_name]
    return action.output[field_name]


def _remove_field(spec: PluginSpec, kind: str, owner: str, field_name: str) -> None:
    """Delete a field at the given location in-place."""
    if kind == "connection":
        del spec.connection[field_name]
        return
    action = spec.actions[owner]
    if field_name in action.input:
        del action.input[field_name]
    else:
        del action.output[field_name]


_OTHER_TYPE = {t: (ALL_FIELD_TYPES[(ALL_FIELD_TYPES.index(t) + 1) % len(ALL_FIELD_TYPES)]) for t in ALL_FIELD_TYPES}


@st.composite
def labeled_mutations(draw: st.DrawFn, base: PluginSpec) -> LabeledMutation:
    """Apply one applicable labeled edit to ``base`` and report it.

    The set of applicable edits depends on ``base``: field-targeting breaking
    edits require an existing field, ``optional-to-required`` requires an
    existing optional field, and ``remove-component`` requires an existing
    action. Non-breaking additions are always applicable. The returned
    :class:`LabeledMutation` carries a deep copy of the mutated spec so the base
    is never touched.
    """
    locations = _existing_field_locations(base)
    optional_locations = [loc for loc in locations if not _get_field(base, *loc).required]

    applicable: List[str] = ["add-optional-field", "add-action"]
    if locations:
        applicable += ["remove-field", "change-type"]
    if optional_locations:
        applicable.append("optional-to-required")
    if base.actions:
        applicable.append("remove-component")

    label = draw(st.sampled_from(applicable))
    spec = copy.deepcopy(base)

    if label == "add-optional-field":
        new_field = _fresh_name(list(spec.connection), draw(snake_case_names()))
        spec.connection[new_field] = draw(field_schemas(required=False))
        return LabeledMutation(label=label, breaking=False, spec=spec)

    if label == "add-action":
        new_action = _fresh_name(list(spec.actions), draw(snake_case_names()))
        spec.actions[new_action] = draw(components())
        return LabeledMutation(label=label, breaking=False, spec=spec)

    if label == "remove-field":
        kind, owner, field_name = draw(st.sampled_from(locations))
        _remove_field(spec, kind, owner, field_name)
        return LabeledMutation(label=label, breaking=True, spec=spec, target_kind=kind, target_name=owner or field_name)

    if label == "change-type":
        kind, owner, field_name = draw(st.sampled_from(locations))
        target = _get_field(spec, kind, owner, field_name)
        target.type = _OTHER_TYPE[target.type]
        return LabeledMutation(label=label, breaking=True, spec=spec, target_kind=kind, target_name=owner or field_name)

    if label == "optional-to-required":
        kind, owner, field_name = draw(st.sampled_from(optional_locations))
        _get_field(spec, kind, owner, field_name).required = True
        return LabeledMutation(label=label, breaking=True, spec=spec, target_kind=kind, target_name=owner or field_name)

    # remove-component: drop an existing action.
    action_name = draw(st.sampled_from(list(spec.actions)))
    del spec.actions[action_name]
    return LabeledMutation(label=label, breaking=True, spec=spec, target_kind="action", target_name=action_name)


# A callable alias so tests can pass ``labeled_mutations`` to ``flatmap``.
_MutationFactory = Callable[[PluginSpec], st.SearchStrategy[LabeledMutation]]
