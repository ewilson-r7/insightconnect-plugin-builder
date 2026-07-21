"""Documentation generation for a plugin draft (design "Documentation_Generator").

This module produces the plugin's ``help.md`` from a :class:`PluginSpec`
(Requirements 6.1-6.5). It is a pure-logic transform over the typed spec tree
with no I/O beyond the small :func:`write_help` file-writing wrapper.

The generated document guarantees:

* a distinct section for the plugin's connection, actions, triggers, and tasks
  (Req 6.1);
* for every action and trigger, each input and output field rendered with its
  name, data type, and required-or-optional status (Req 6.2);
* the plugin's title, description, version, and vendor (Req 6.3); and
* an empty component category renders its heading followed by a placeholder
  statement rather than being omitted (Req 6.4).

If any required metadata field (title, description, version, or vendor) is
absent, generation aborts by raising :class:`MissingMetadataError`; the
:func:`write_help` wrapper propagates that error and leaves any existing
``help.md`` unchanged (Req 6.5).
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from .spec_model import Component, FieldSchema, PluginSpec, SemVer

__all__ = [
    "DocumentationError",
    "MissingMetadataError",
    "REQUIRED_METADATA_FIELDS",
    "generate_help",
    "write_help",
]

#: The metadata fields that must be present for ``help.md`` generation (Req 6.3, 6.5).
REQUIRED_METADATA_FIELDS = ("title", "description", "version", "vendor")


class DocumentationError(Exception):
    """Base error for documentation generation failures."""


class MissingMetadataError(DocumentationError):
    """Raised when required plugin metadata is absent (Req 6.5).

    Attributes:
        missing_fields: the required metadata field names that were absent, in
            the canonical order of :data:`REQUIRED_METADATA_FIELDS`.
    """

    def __init__(self, missing_fields: List[str]) -> None:
        self.missing_fields = list(missing_fields)
        joined = ", ".join(self.missing_fields)
        super().__init__(f"cannot generate help.md: missing required metadata field(s): {joined}")


def generate_help(spec: PluginSpec) -> str:
    """Render the ``help.md`` document for ``spec``.

    Args:
        spec: The plugin draft to document.

    Returns:
        The full ``help.md`` content as a string.

    Raises:
        MissingMetadataError: If the title, description, version, or vendor is
            absent (Req 6.5). Generation aborts before producing any output.
    """
    missing = _missing_metadata(spec)
    if missing:
        raise MissingMetadataError(missing)

    lines: List[str] = []
    lines.append(f"# {spec.title.strip()}")
    lines.append("")
    lines.append("## Description")
    lines.append("")
    lines.append(spec.description.strip())
    lines.append("")

    # Metadata block (Req 6.3): title is the heading above; version and vendor
    # are surfaced explicitly here alongside a restatement of the title.
    lines.append("## Plugin Information")
    lines.append("")
    lines.append(f"- Title: {spec.title.strip()}")
    lines.append(f"- Version: {spec.version}")
    lines.append(f"- Vendor: {spec.vendor.strip()}")
    lines.append("")

    lines.extend(_render_connection(spec.connection))
    lines.append("")
    lines.extend(_render_components("Actions", "action", spec.actions))
    lines.append("")
    lines.extend(_render_components("Triggers", "trigger", spec.triggers))
    lines.append("")
    lines.extend(_render_components("Tasks", "task", spec.tasks))
    lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def write_help(spec: PluginSpec, path: str) -> str:
    """Generate ``help.md`` for ``spec`` and write it atomically to ``path``.

    The document is generated fully before any write occurs, so if generation
    aborts because required metadata is missing, an existing file at ``path`` is
    left unchanged (Req 6.5).

    Args:
        spec: The plugin draft to document.
        path: The destination ``help.md`` path.

    Returns:
        The ``path`` written.

    Raises:
        MissingMetadataError: If required metadata is absent; ``path`` is not
            touched.
    """
    content = generate_help(spec)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp_path, path)
    return path


def _missing_metadata(spec: PluginSpec) -> List[str]:
    """Return the required metadata fields absent from ``spec`` (Req 6.5)."""
    missing: List[str] = []
    if not _has_text(spec.title):
        missing.append("title")
    if not _has_text(spec.description):
        missing.append("description")
    if not _has_version(spec.version):
        missing.append("version")
    if not _has_text(spec.vendor):
        missing.append("vendor")
    return missing


def _has_text(value: Optional[str]) -> bool:
    """Return ``True`` iff ``value`` is a non-empty, non-whitespace string."""
    return isinstance(value, str) and value.strip() != ""


def _has_version(value: object) -> bool:
    """Return ``True`` iff ``value`` is a present semantic version."""
    return isinstance(value, SemVer)


def _render_connection(connection: Dict[str, FieldSchema]) -> List[str]:
    """Render the connection section (Req 6.1); placeholder when empty (Req 6.4)."""
    lines = ["## Connection", ""]
    if not connection:
        lines.append("_This plugin does not define a connection._")
        return lines
    lines.extend(_render_field_table(connection))
    return lines


def _render_components(heading: str, noun: str, components: Dict[str, Component]) -> List[str]:
    """Render an actions/triggers/tasks section.

    Args:
        heading: the section heading (e.g. ``"Actions"``).
        noun: the singular lowercase noun used in the empty-state placeholder
            (e.g. ``"action"``).
        components: the component map for this category.

    Empty categories render the heading followed by a placeholder rather than
    being omitted (Req 6.4). Each component renders its input and output fields
    with name, type, and required/optional status (Req 6.2).
    """
    lines = [f"## {heading}", ""]
    if not components:
        lines.append(f"_This plugin does not define any {noun}s._")
        return lines

    for name, component in components.items():
        title = component.title.strip() if _has_text(component.title) else name
        lines.append(f"### {title} (`{name}`)")
        lines.append("")
        if _has_text(component.description):
            lines.append(component.description.strip())
            lines.append("")

        lines.append("#### Input")
        lines.append("")
        if component.input:
            lines.extend(_render_field_table(component.input))
        else:
            lines.append(f"_This {noun} does not define any input fields._")
        lines.append("")

        lines.append("#### Output")
        lines.append("")
        if component.output:
            lines.extend(_render_field_table(component.output))
        else:
            lines.append(f"_This {noun} does not define any output fields._")
        lines.append("")

    # Drop the trailing blank line added after the final component.
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _render_field_table(fields: Dict[str, FieldSchema]) -> List[str]:
    """Render a markdown table of fields with name, type, required, description.

    Every field is listed with its name, data type, and required-or-optional
    status (Req 6.2). Fields are ordered by their ``order`` attribute when set
    (unordered fields sort last), then by name for a stable layout.
    """
    lines = [
        "|Name|Type|Required|Description|",
        "|----|----|--------|-----------|",
    ]
    for name, schema in _sorted_fields(fields):
        required = "Required" if schema.required else "Optional"
        description = _cell(schema.description)
        lines.append(f"|{_cell(name)}|{_cell(schema.type)}|{required}|{description}|")
    return lines


def _sorted_fields(fields: Dict[str, FieldSchema]) -> List[tuple]:
    """Return ``(name, schema)`` pairs ordered by ``order`` then name."""
    return sorted(
        fields.items(),
        key=lambda item: (item[1].order if item[1].order is not None else float("inf"), item[0]),
    )


def _cell(value: Optional[str]) -> str:
    """Escape a value for safe rendering inside a markdown table cell."""
    if value is None:
        return ""
    text = str(value).replace("\\", "\\\\").replace("|", "\\|")
    # Collapse any line breaks so a single row stays a single row.
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return text.strip()
