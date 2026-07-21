"""Spec_Validator: validate a ``plugin.spec.yaml`` against the InsightConnect
plugin-spec schema (design ``Spec_Validator``; Req 7.1, 7.2, 7.3, 7.5, 7.6).

The validator runs two independent checks and merges their findings into a
single :class:`ValidationReport`:

1. **Structural schema validation** -- the spec mapping is checked against a
   JSON Schema (Draft 2020-12) describing the ``plugin_spec_version: v2``
   document: required top-level metadata, the shape of ``connection``,
   ``actions``, ``triggers``, ``tasks``, ``types``, and the shape of each field.
   Every violation is collected (not just the first) so the report can list
   them all, each carrying the field path within the spec and a description of
   the violation (Req 7.2).

2. **Semantic-version check** -- the ``version`` field is verified against the
   strict ``MAJOR.MINOR.PATCH`` format independently of the schema, so a
   malformed version yields a clear, dedicated error naming the ``version``
   field and the expected format (Req 7.3, 7.5).

A report with no errors indicates success (Req 7.6); a report with any error
blocks export upstream (Req 7.4, enforced by the Orchestrator).

The validator accepts either a typed :class:`PluginSpec` or a raw parsed
mapping. A typed :class:`PluginSpec` always carries a valid :class:`SemVer`, so
raw-mapping input is what exercises the malformed-version path; both are
supported because callers validate specs at different stages (freshly parsed
YAML vs. an in-memory draft).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Union

from jsonschema import Draft202012Validator

from .spec_model import PluginSpec, SemVer

__all__ = [
    "SpecValidationError",
    "ValidationReport",
    "SpecValidator",
    "validate_spec",
    "PLUGIN_SPEC_SCHEMA",
]

# The expected semantic-version shape, surfaced in the version error message.
_SEMVER_FORMAT = "MAJOR.MINOR.PATCH"

# --- JSON Schema for plugin.spec.yaml (v2) ---------------------------------
#
# additionalProperties is left permissive (True) throughout: the InsightConnect
# spec carries many optional keys (sdk, support, tags, hub_tags, resources,
# requirements, ...) that this tool preserves verbatim but does not constrain.
# The schema pins the structural surface the validator must guarantee.

_FIELD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "minLength": 1},
        "required": {"type": "boolean"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "order": {"type": "integer"},
        "enum": {"type": "array"},
    },
    "required": ["type"],
    "additionalProperties": True,
}

_FIELD_MAP_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": _FIELD_SCHEMA,
}

_COMPONENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "input": _FIELD_MAP_SCHEMA,
        "output": _FIELD_MAP_SCHEMA,
    },
    "additionalProperties": True,
}

_COMPONENT_MAP_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": _COMPONENT_SCHEMA,
}

#: JSON Schema describing an InsightConnect ``plugin.spec.yaml`` (spec v2).
PLUGIN_SPEC_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "InsightConnect plugin.spec.yaml (v2)",
    "type": "object",
    "properties": {
        "plugin_spec_version": {"type": "string", "minLength": 1},
        "name": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_]*$",
        },
        "title": {"type": "string", "minLength": 1},
        "description": {"type": "string", "minLength": 1},
        # The strict semver shape is checked separately so the message can name
        # the expected format; here we only require it to be a string.
        "version": {"type": "string"},
        "vendor": {"type": "string", "minLength": 1},
        "connection": _FIELD_MAP_SCHEMA,
        "actions": _COMPONENT_MAP_SCHEMA,
        "triggers": _COMPONENT_MAP_SCHEMA,
        "tasks": _COMPONENT_MAP_SCHEMA,
        "types": {
            "type": "object",
            "additionalProperties": _FIELD_MAP_SCHEMA,
        },
    },
    "required": [
        "plugin_spec_version",
        "name",
        "title",
        "description",
        "version",
        "vendor",
    ],
    "additionalProperties": True,
}

# A single compiled validator instance is reusable and thread-safe for reads.
_SCHEMA_VALIDATOR = Draft202012Validator(PLUGIN_SPEC_SCHEMA)


@dataclass(frozen=True)
class SpecValidationError:
    """A single validation violation.

    Attributes:
        path: The location of the offending field within the spec, expressed as
            a dotted/bracketed path (e.g. ``actions.run.input.host.type``) or
            ``"(root)"`` for a document-level violation.
        message: A human-readable description of the violation.
    """

    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class ValidationReport:
    """The outcome of validating a spec.

    Attributes:
        errors: Every detected violation, ordered by field path then message so
            the report is deterministic. Empty when the spec is valid.
        duration_seconds: Wall-clock time the validation took, so callers can
            confirm the 5-second budget (Req 7.1).
    """

    errors: List[SpecValidationError] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def is_valid(self) -> bool:
        """Return ``True`` iff no violations were detected (Req 7.6)."""
        return not self.errors

    def summary(self) -> str:
        """Return a human-readable success/failure summary line."""
        if self.is_valid:
            return "Plugin spec is valid."
        count = len(self.errors)
        noun = "error" if count == 1 else "errors"
        return f"Plugin spec has {count} validation {noun}."


class SpecValidator:
    """Validates a plugin spec against the InsightConnect plugin-spec schema."""

    def __init__(self, schema_validator: Draft202012Validator = _SCHEMA_VALIDATOR) -> None:
        self._schema_validator = schema_validator

    def validate(self, spec: Union[PluginSpec, Mapping[str, Any]]) -> ValidationReport:
        """Validate ``spec`` and return a :class:`ValidationReport`.

        Args:
            spec: A typed :class:`PluginSpec` or a raw parsed mapping. A typed
                spec is serialized to its mapping form first.

        Returns:
            A :class:`ValidationReport` listing every schema violation and any
            semantic-version violation, each with a field path and description.
            The report is valid (``is_valid`` true) only when no violation was
            found.
        """
        start = time.monotonic()
        mapping = spec.to_mapping() if isinstance(spec, PluginSpec) else spec

        errors: List[SpecValidationError] = []
        errors.extend(self._schema_errors(mapping))
        errors.extend(self._version_errors(mapping))

        errors.sort(key=lambda err: (err.path, err.message))
        duration = time.monotonic() - start
        return ValidationReport(errors=errors, duration_seconds=duration)

    def _schema_errors(self, mapping: Any) -> List[SpecValidationError]:
        """Collect every structural schema violation in ``mapping``."""
        collected: List[SpecValidationError] = []
        for error in self._schema_validator.iter_errors(mapping):
            collected.append(
                SpecValidationError(
                    path=_format_path(error.absolute_path),
                    message=error.message,
                )
            )
        return collected

    def _version_errors(self, mapping: Any) -> List[SpecValidationError]:
        """Check the ``version`` field against the strict semver format.

        Skips the check when ``version`` is absent (the schema already reports
        it as required) or is present but not a string (the schema already
        reports the type error), avoiding a duplicate message for the same
        field.
        """
        if not isinstance(mapping, Mapping) or "version" not in mapping:
            return []
        value = mapping["version"]
        if not isinstance(value, str):
            return []
        if SemVer.is_valid(value):
            return []
        return [
            SpecValidationError(
                path="version",
                message=(f"version {value!r} is invalid; expected semantic-version format {_SEMVER_FORMAT}"),
            )
        ]


# A shared default instance for the common case.
_DEFAULT_VALIDATOR = SpecValidator()


def validate_spec(spec: Union[PluginSpec, Mapping[str, Any]]) -> ValidationReport:
    """Validate ``spec`` using the default :class:`SpecValidator` instance."""
    return _DEFAULT_VALIDATOR.validate(spec)


def _format_path(path: Any) -> str:
    """Format a jsonschema ``absolute_path`` deque into a readable field path.

    Mapping keys are joined with ``.`` and sequence indices are rendered as
    ``[i]``; an empty path (a document-level violation) becomes ``"(root)"``.
    """
    parts: List[str] = []
    for token in path:
        if isinstance(token, int):
            parts.append(f"[{token}]")
        elif parts:
            parts.append(f".{token}")
        else:
            parts.append(str(token))
    return "".join(parts) if parts else "(root)"
