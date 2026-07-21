"""Typed data model for an InsightConnect ``plugin.spec.yaml`` (spec version ``v2``).

The model mirrors the on-disk spec as a typed tree so that later stages can
diff two specs exactly and classify a change as breaking or non-breaking
(design Property 23) without re-parsing YAML. The nodes are ``@dataclass``
values, so structural equality (``==``) compares the whole tree field by field.

Only the parts that breaking-change classification and view-model/documentation
generation reason about are given first-class attributes:

* :class:`SemVer` -- the ``version`` field, with a total ordering used by the
  version bumper.
* :class:`FieldSchema` -- a single input/output/connection/type field.
* :class:`Component` -- an action, trigger, or task (title/description plus
  ``input`` and ``output`` field maps).
* :class:`PluginSpec` -- the whole document.

Every key that is *not* modeled explicitly is preserved verbatim in an ``extra``
mapping on the enclosing node so that a load -> dump round trip through
:mod:`icplugin_builder.core.yaml_codec` does not silently drop data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

__all__ = [
    "SemVer",
    "FieldSchema",
    "Component",
    "PluginSpec",
]

_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# Field-level keys promoted to first-class attributes on FieldSchema; anything
# else a spec carries on a field is retained under FieldSchema.extra.
_FIELD_KNOWN_KEYS = (
    "title",
    "description",
    "type",
    "required",
    "default",
    "example",
    "placeholder",
    "tooltip",
    "order",
    "enum",
)

# Component-level keys promoted to first-class attributes; the rest are kept
# under Component.extra.
_COMPONENT_KNOWN_KEYS = ("title", "description", "input", "output")

# Top-level keys the PluginSpec models directly; every other top-level key is
# retained, in order, under PluginSpec.extra.
_SPEC_KNOWN_KEYS = (
    "plugin_spec_version",
    "name",
    "title",
    "description",
    "version",
    "vendor",
    "connection",
    "actions",
    "triggers",
    "tasks",
    "types",
)


@dataclass(frozen=True, order=True)
class SemVer:
    """A ``MAJOR.MINOR.PATCH`` semantic version with a total ordering.

    Instances are ordered by ``(major, minor, patch)`` so they can be compared
    and sorted directly; this ordering backs the schema-aware version bumper.
    """

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: Any) -> "SemVer":
        """Parse ``value`` into a :class:`SemVer`.

        Accepts a string in strict ``MAJOR.MINOR.PATCH`` form (no leading
        zeros, no pre-release/build metadata) or an existing :class:`SemVer`.

        Raises:
            ValueError: if ``value`` is not a valid semantic version string.
        """
        if isinstance(value, SemVer):
            return value
        if not isinstance(value, str):
            raise ValueError(f"invalid version field: expected a MAJOR.MINOR.PATCH string, got {type(value).__name__}")
        match = _SEMVER_RE.match(value.strip())
        if match is None:
            raise ValueError(f"invalid version field {value!r}: expected MAJOR.MINOR.PATCH format")
        major, minor, patch = (int(part) for part in match.groups())
        return cls(major, minor, patch)

    @classmethod
    def is_valid(cls, value: Any) -> bool:
        """Return ``True`` iff ``value`` is a valid ``MAJOR.MINOR.PATCH`` string."""
        if isinstance(value, SemVer):
            return True
        return isinstance(value, str) and _SEMVER_RE.match(value.strip()) is not None

    def bump_major(self) -> "SemVer":
        """Return a new version with MAJOR incremented and MINOR/PATCH reset to 0."""
        return SemVer(self.major + 1, 0, 0)

    def bump_patch(self) -> "SemVer":
        """Return a new version with PATCH incremented."""
        return SemVer(self.major, self.minor, self.patch + 1)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass
class FieldSchema:
    """A single input, output, connection, or custom-type field."""

    type: str = "string"
    required: bool = False
    title: Optional[str] = None
    description: Optional[str] = None
    default: Any = None
    example: Any = None
    placeholder: Optional[str] = None
    tooltip: Optional[str] = None
    order: Optional[int] = None
    enum: Optional[list] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "FieldSchema":
        """Build a :class:`FieldSchema` from a raw spec mapping."""
        known: Dict[str, Any] = {}
        extra: Dict[str, Any] = {}
        for key, value in data.items():
            if key in _FIELD_KNOWN_KEYS:
                known[key] = value
            else:
                extra[key] = _plain(value)
        enum = known.get("enum")
        return cls(
            type=known.get("type", "string"),
            required=bool(known.get("required", False)),
            title=known.get("title"),
            description=known.get("description"),
            default=_plain(known["default"]) if "default" in known else None,
            example=_plain(known["example"]) if "example" in known else None,
            placeholder=known.get("placeholder"),
            tooltip=known.get("tooltip"),
            order=known.get("order"),
            enum=list(enum) if enum is not None else None,
            extra=extra,
        )

    def to_mapping(self) -> Dict[str, Any]:
        """Serialize back to a plain ordered mapping (inverse of ``from_mapping``)."""
        out: Dict[str, Any] = {}
        if self.title is not None:
            out["title"] = self.title
        if self.description is not None:
            out["description"] = self.description
        out["type"] = self.type
        out["required"] = self.required
        if self.default is not None:
            out["default"] = self.default
        if self.example is not None:
            out["example"] = self.example
        if self.placeholder is not None:
            out["placeholder"] = self.placeholder
        if self.tooltip is not None:
            out["tooltip"] = self.tooltip
        if self.order is not None:
            out["order"] = self.order
        if self.enum is not None:
            out["enum"] = list(self.enum)
        out.update(self.extra)
        return out


@dataclass
class Component:
    """An action, trigger, or task: metadata plus input/output field maps."""

    title: Optional[str] = None
    description: Optional[str] = None
    input: Dict[str, FieldSchema] = field(default_factory=dict)
    output: Dict[str, FieldSchema] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Component":
        """Build a :class:`Component` from a raw spec mapping."""
        extra: Dict[str, Any] = {key: _plain(value) for key, value in data.items() if key not in _COMPONENT_KNOWN_KEYS}
        return cls(
            title=data.get("title"),
            description=data.get("description"),
            input=_fields_from_mapping(data.get("input")),
            output=_fields_from_mapping(data.get("output")),
            extra=extra,
        )

    def to_mapping(self) -> Dict[str, Any]:
        """Serialize back to a plain ordered mapping (inverse of ``from_mapping``)."""
        out: Dict[str, Any] = {}
        if self.title is not None:
            out["title"] = self.title
        if self.description is not None:
            out["description"] = self.description
        if self.input:
            out["input"] = {name: fs.to_mapping() for name, fs in self.input.items()}
        if self.output:
            out["output"] = {name: fs.to_mapping() for name, fs in self.output.items()}
        out.update(self.extra)
        return out


@dataclass
class PluginSpec:
    """A whole ``plugin.spec.yaml`` document as a typed tree."""

    name: str = ""
    title: str = ""
    description: str = ""
    version: SemVer = field(default_factory=lambda: SemVer(1, 0, 0))
    vendor: str = ""
    plugin_spec_version: str = "v2"
    connection: Dict[str, FieldSchema] = field(default_factory=dict)
    actions: Dict[str, Component] = field(default_factory=dict)
    triggers: Dict[str, Component] = field(default_factory=dict)
    tasks: Dict[str, Component] = field(default_factory=dict)
    types: Dict[str, Dict[str, FieldSchema]] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PluginSpec":
        """Build a :class:`PluginSpec` from a raw parsed spec mapping.

        Raises:
            ValueError: if the ``version`` field is not a valid semantic version.
        """
        extra: Dict[str, Any] = {key: _plain(value) for key, value in data.items() if key not in _SPEC_KNOWN_KEYS}
        return cls(
            name=data.get("name", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            version=SemVer.parse(data.get("version", "1.0.0")),
            vendor=data.get("vendor", ""),
            plugin_spec_version=data.get("plugin_spec_version", "v2"),
            connection=_fields_from_mapping(data.get("connection")),
            actions=_components_from_mapping(data.get("actions")),
            triggers=_components_from_mapping(data.get("triggers")),
            tasks=_components_from_mapping(data.get("tasks")),
            types=_types_from_mapping(data.get("types")),
            extra=extra,
        )

    def to_mapping(self) -> Dict[str, Any]:
        """Serialize back to a plain ordered mapping (inverse of ``from_mapping``).

        Key order follows the conventional spec layout; unmodeled keys from
        ``extra`` are appended so nothing is lost on a round trip.
        """
        out: Dict[str, Any] = {"plugin_spec_version": self.plugin_spec_version}
        out["name"] = self.name
        out["title"] = self.title
        out["description"] = self.description
        out["version"] = str(self.version)
        out["vendor"] = self.vendor
        if self.connection:
            out["connection"] = {name: fs.to_mapping() for name, fs in self.connection.items()}
        if self.types:
            out["types"] = {
                type_name: {name: fs.to_mapping() for name, fs in fields.items()}
                for type_name, fields in self.types.items()
            }
        if self.actions:
            out["actions"] = {name: comp.to_mapping() for name, comp in self.actions.items()}
        if self.triggers:
            out["triggers"] = {name: comp.to_mapping() for name, comp in self.triggers.items()}
        if self.tasks:
            out["tasks"] = {name: comp.to_mapping() for name, comp in self.tasks.items()}
        out.update(self.extra)
        return out


def _fields_from_mapping(data: Optional[Mapping[str, Any]]) -> Dict[str, FieldSchema]:
    """Convert a mapping of field-name -> raw field into a FieldSchema map."""
    if not data:
        return {}
    return {name: FieldSchema.from_mapping(value) for name, value in data.items()}


def _components_from_mapping(data: Optional[Mapping[str, Any]]) -> Dict[str, Component]:
    """Convert a mapping of component-name -> raw component into a Component map."""
    if not data:
        return {}
    return {name: Component.from_mapping(value) for name, value in data.items()}


def _types_from_mapping(data: Optional[Mapping[str, Any]]) -> Dict[str, Dict[str, FieldSchema]]:
    """Convert the ``types`` mapping into a nested FieldSchema map."""
    if not data:
        return {}
    return {type_name: _fields_from_mapping(fields) for type_name, fields in data.items()}


def _plain(value: Any) -> Any:
    """Recursively convert ruamel round-trip containers to plain Python values.

    ruamel's ``CommentedMap``/``CommentedSeq`` compare unequal to plain
    ``dict``/``list`` in ways that break structural equality of the typed tree,
    so preserved (``extra``) values are normalized to built-in containers.
    """
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value
