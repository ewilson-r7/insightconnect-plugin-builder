"""Round-trip-preserving YAML load/dump for ``plugin.spec.yaml``.

This module offers two complementary capabilities:

1. **Raw round trip** (:func:`load_document` / :func:`dump_document`) using
   ``ruamel.yaml`` in round-trip mode, so that comments, key ordering, quoting,
   and block/flow style survive an in-place edit of an existing spec file. This
   is what the design means by "comments/ordering survive".

2. **Typed model codec** (:func:`load_plugin_spec` / :func:`dump_plugin_spec`)
   that parses a spec into the typed :class:`~icplugin_builder.core.spec_model.PluginSpec`
   tree and serializes it back. This drives diffing, breaking-change
   classification, and the Plugin_Spec YAML round-trip property.

The two share a single configured ``YAML`` instance so both read the same
dialect (2-space indentation, no line wrapping, block style).
"""

from __future__ import annotations

import io
from typing import Any

from ruamel.yaml import YAML

from .spec_model import PluginSpec

__all__ = [
    "load_document",
    "dump_document",
    "load_plugin_spec",
    "dump_plugin_spec",
]


def _make_yaml() -> YAML:
    """Create a YAML handler configured to preserve spec formatting on round trip."""
    yaml = YAML()  # round-trip mode by default; retains comments and ordering.
    yaml.preserve_quotes = True
    yaml.width = 4096  # effectively disable line wrapping so long descriptions stay on one line.
    yaml.indent(mapping=2, sequence=2, offset=0)
    return yaml


# A module-level handler is safe here: ruamel YAML instances are reusable and
# this tool is single-operator/single-threaded per request path.
_YAML = _make_yaml()


def load_document(text: str) -> Any:
    """Load YAML ``text`` into a ruamel round-trip document.

    The returned object preserves comments and ordering, so dumping it again
    with :func:`dump_document` reproduces the original formatting.
    """
    return _YAML.load(text)


def dump_document(document: Any) -> str:
    """Serialize a ruamel round-trip ``document`` back to YAML text."""
    stream = io.StringIO()
    _YAML.dump(document, stream)
    return stream.getvalue()


def load_plugin_spec(text: str) -> PluginSpec:
    """Parse ``plugin.spec.yaml`` text into a typed :class:`PluginSpec`.

    Raises:
        ValueError: if the document is not a mapping or its ``version`` field is
            not a valid semantic version.
    """
    document = load_document(text)
    if document is None or not hasattr(document, "items"):
        raise ValueError("plugin spec must be a YAML mapping")
    return PluginSpec.from_mapping(document)


def dump_plugin_spec(spec: PluginSpec) -> str:
    """Serialize a typed :class:`PluginSpec` to ``plugin.spec.yaml`` text."""
    stream = io.StringIO()
    _YAML.dump(spec.to_mapping(), stream)
    return stream.getvalue()
