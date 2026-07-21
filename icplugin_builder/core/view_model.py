"""Visualization view-model builder (design "Visualization_View"; Req 5.1, 5.2, 5.4).

This module turns a :class:`~icplugin_builder.core.spec_model.PluginSpec` into a
flat, immutable **view-model** the ``Visualization_View`` renders as a graph
(connection, actions, triggers, tasks as nodes; input/output schema fields shown
on node expansion/selection). It is a pure-logic transform over the typed spec
tree with no I/O.

The built view-model guarantees (design Property 13):

* it includes every defined connection, action, trigger, and task (Req 5.1);
* it includes the input schema and output schema of every action and trigger
  (Req 5.2); and
* selecting a single node exposes exactly that node's fields (Req 5.4).

Every node carries a stable :attr:`~NodeView.node_id` (``"connection"``,
``"action:<name>"``, ``"trigger:<name>"``, ``"task:<name>"``) so the UI can
address a node for selection and so :func:`VisualizationViewModel.select` can
return exactly one node's detail.

This builder assumes a *parseable* spec and always produces a view-model. The
empty-state and parse-failure fallbacks (task 5.7, Req 5.5/5.6) are layered on
*around* this builder rather than baked into it: :attr:`VisualizationViewModel.is_empty`
lets the fallback layer detect the empty draft, and because the builder is a
pure function of a :class:`PluginSpec`, a wrapper can catch a parse failure and
retain the last successfully built model without this module knowing about
either concern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .spec_model import Component, FieldSchema, PluginSpec

__all__ = [
    "CONNECTION",
    "ACTION",
    "TRIGGER",
    "TASK",
    "FieldView",
    "NodeView",
    "VisualizationViewModel",
    "NodeNotFoundError",
    "build_view_model",
]

#: Node-kind discriminators. The connection is a single node; actions, triggers,
#: and tasks are name-keyed collections of nodes.
CONNECTION = "connection"
ACTION = "action"
TRIGGER = "trigger"
TASK = "task"

#: The fixed node id of the (single) connection node.
_CONNECTION_NODE_ID = CONNECTION


class NodeNotFoundError(KeyError):
    """Raised when :func:`VisualizationViewModel.select` is given an unknown id."""

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        super().__init__(f"no node with id {node_id!r} exists in the view-model")


@dataclass(frozen=True)
class FieldView:
    """A single input/output/connection field prepared for display.

    Mirrors the display-relevant surface of a
    :class:`~icplugin_builder.core.spec_model.FieldSchema`; the ``required``
    flag drives the required-or-optional badge in the UI.
    """

    name: str
    type: str
    required: bool
    title: Optional[str] = None
    description: Optional[str] = None
    default: object = None
    enum: Optional[Tuple[object, ...]] = None

    @classmethod
    def from_schema(cls, name: str, schema: FieldSchema) -> "FieldView":
        """Build a :class:`FieldView` from a named :class:`FieldSchema`."""
        return cls(
            name=name,
            type=schema.type,
            required=bool(schema.required),
            title=schema.title,
            description=schema.description,
            default=schema.default,
            enum=tuple(schema.enum) if schema.enum is not None else None,
        )


@dataclass(frozen=True)
class NodeView:
    """A single graph node: the connection, an action, a trigger, or a task.

    Attributes:
        node_id: stable id used for selection (e.g. ``"action:list_things"``).
        kind: one of :data:`CONNECTION`, :data:`ACTION`, :data:`TRIGGER`,
            :data:`TASK`.
        name: the component name; for the connection node this is
            :data:`CONNECTION`.
        title: the component's display title, if any.
        description: the component's description, if any.
        input: the ordered input-schema fields. For the connection node this
            holds the connection's fields; for a task it holds the task's input
            fields.
        output: the ordered output-schema fields. Empty for the connection node.
    """

    node_id: str
    kind: str
    name: str
    title: Optional[str] = None
    description: Optional[str] = None
    input: Tuple[FieldView, ...] = ()
    output: Tuple[FieldView, ...] = ()

    @property
    def fields(self) -> Tuple[FieldView, ...]:
        """Return exactly this node's fields (input followed by output).

        Backs the single-selection detail view (Req 5.4): the returned tuple
        contains every field the node defines and no field belonging to any
        other node.
        """
        return self.input + self.output


@dataclass(frozen=True)
class VisualizationViewModel:
    """The whole visualization view-model for a plugin draft.

    Attributes:
        connection: the connection node. Always present as a node (its
            ``input`` is empty when the draft defines no connection fields) so
            the graph can render a connection slot consistently; use
            :attr:`has_connection` to distinguish a defined connection.
        actions: the action nodes, in spec order.
        triggers: the trigger nodes, in spec order.
        tasks: the task nodes, in spec order.
    """

    connection: NodeView
    actions: Tuple[NodeView, ...] = ()
    triggers: Tuple[NodeView, ...] = ()
    tasks: Tuple[NodeView, ...] = ()
    # Index of node_id -> node, built once for O(1) selection.
    _index: Dict[str, NodeView] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        index: Dict[str, NodeView] = {}
        for node in self.nodes():
            index[node.node_id] = node
        # frozen dataclass: assign the index via object.__setattr__.
        object.__setattr__(self, "_index", index)

    @property
    def has_connection(self) -> bool:
        """Return ``True`` iff the draft defines at least one connection field."""
        return bool(self.connection.input)

    @property
    def is_empty(self) -> bool:
        """Return ``True`` iff the draft has no connection, actions, triggers, or tasks.

        The empty-state fallback (task 5.7, Req 5.5) consults this to decide
        whether to render an empty-state indication instead of an empty graph.
        """
        return not (self.has_connection or self.actions or self.triggers or self.tasks)

    def component_nodes(self) -> Tuple[NodeView, ...]:
        """Return the action, trigger, and task nodes (excluding the connection)."""
        return self.actions + self.triggers + self.tasks

    def nodes(self) -> Tuple[NodeView, ...]:
        """Return every node, connection first, then actions, triggers, tasks."""
        return (self.connection,) + self.component_nodes()

    def select(self, node_id: str) -> NodeView:
        """Return exactly the node identified by ``node_id`` (Req 5.4).

        Args:
            node_id: the id of the node to select (e.g. ``"trigger:on_thing"``
                or :data:`CONNECTION`).

        Returns:
            The single matching :class:`NodeView`, whose :attr:`~NodeView.fields`
            are exactly that component's fields.

        Raises:
            NodeNotFoundError: if no node has the given id.
        """
        try:
            return self._index[node_id]
        except KeyError:
            raise NodeNotFoundError(node_id) from None


def build_view_model(spec: PluginSpec) -> VisualizationViewModel:
    """Build the visualization view-model for a parseable ``spec``.

    Includes every connection field, action, trigger, and task (Req 5.1) with
    the input and output schema of every action and trigger (Req 5.2); the
    resulting model supports single-node selection exposing exactly that node's
    fields (Req 5.4).

    Args:
        spec: the plugin draft to visualize. Assumed already parsed into the
            typed model; empty-state and parse-failure handling are the
            responsibility of the fallback layer (task 5.7).

    Returns:
        The :class:`VisualizationViewModel`.
    """
    connection = NodeView(
        node_id=_CONNECTION_NODE_ID,
        kind=CONNECTION,
        name=CONNECTION,
        input=_field_views(spec.connection),
    )
    return VisualizationViewModel(
        connection=connection,
        actions=_component_nodes(ACTION, spec.actions),
        triggers=_component_nodes(TRIGGER, spec.triggers),
        tasks=_component_nodes(TASK, spec.tasks),
    )


def _component_nodes(kind: str, components: Dict[str, Component]) -> Tuple[NodeView, ...]:
    """Build the ordered node views for a component collection of ``kind``."""
    nodes: List[NodeView] = []
    for name, component in components.items():
        nodes.append(
            NodeView(
                node_id=_node_id(kind, name),
                kind=kind,
                name=name,
                title=component.title,
                description=component.description,
                input=_field_views(component.input),
                output=_field_views(component.output),
            )
        )
    return tuple(nodes)


def _field_views(fields: Dict[str, FieldSchema]) -> Tuple[FieldView, ...]:
    """Build ordered :class:`FieldView`s from a field map.

    Fields are ordered by their ``order`` attribute when set (unordered fields
    sort last), then by name, matching the documentation renderer for a stable,
    predictable layout.
    """
    ordered = sorted(
        fields.items(),
        key=lambda item: (item[1].order if item[1].order is not None else float("inf"), item[0]),
    )
    return tuple(FieldView.from_schema(name, schema) for name, schema in ordered)


def _node_id(kind: str, name: str) -> str:
    """Return the stable node id for a named component of ``kind``."""
    return f"{kind}:{name}"
