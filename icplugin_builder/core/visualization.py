"""Empty-state and parse-failure fallbacks for the visualization view-model.

The :mod:`~icplugin_builder.core.view_model` builder assumes a *parseable*,
already-typed :class:`~icplugin_builder.core.spec_model.PluginSpec` and always
produces a view-model. This module layers the two draft-quality fallbacks the
``Visualization_View`` needs *around* that builder without changing it:

* **Empty-state (Req 5.5).** When the draft defines no connection, actions,
  triggers, or tasks -- including a blank/whitespace-only draft that has no spec
  at all -- the render carries an empty-state indication rather than a blank
  view.
* **Parse failure (Req 5.6).** When the draft cannot be parsed into a
  :class:`PluginSpec`, the render carries an error indication that identifies
  the parse failure and *retains the most recently rendered valid
  visualization* so the UI keeps showing the last good graph.

Two entry points are provided:

* :func:`render_visualization` -- a pure function that classifies a single
  draft into a :class:`VisualizationRender`, given the caller's last valid
  view-model to fall back to on a parse failure.
* :class:`VisualizationRenderer` -- a small stateful wrapper that remembers the
  last valid view-model across successive renders so the retention in Req 5.6
  happens automatically.

A draft may be supplied either as raw ``plugin.spec.yaml`` text (the usual case,
since only text can fail to parse) or as an already-parsed :class:`PluginSpec`
(which can only be empty-state or OK, never a parse failure).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union

from ruamel.yaml.error import YAMLError

from .spec_model import PluginSpec
from .view_model import VisualizationViewModel, build_view_model
from .yaml_codec import load_plugin_spec

__all__ = [
    "EMPTY_STATE_MESSAGE",
    "VisualizationState",
    "VisualizationRender",
    "render_visualization",
    "VisualizationRenderer",
]

#: The human-readable empty-state indication shown for an empty draft (Req 5.5).
EMPTY_STATE_MESSAGE = "No plugin components yet. Describe a connection, action, trigger, or task to begin."

#: A draft is either raw ``plugin.spec.yaml`` text (or ``None``/blank for an
#: empty draft) or an already-parsed :class:`PluginSpec`.
Draft = Union[str, PluginSpec, None]


class VisualizationState(str, Enum):
    """The outcome class of rendering a draft into a visualization."""

    #: The draft parsed and defines at least one component.
    OK = "ok"
    #: The draft is empty -- no connection, actions, triggers, or tasks (Req 5.5).
    EMPTY = "empty"
    #: The draft could not be parsed; the last valid visualization is retained (Req 5.6).
    PARSE_ERROR = "parse_error"


@dataclass(frozen=True)
class VisualizationRender:
    """The result of rendering a draft, ready for the ``Visualization_View``.

    Attributes:
        state: which fallback (if any) applies -- see :class:`VisualizationState`.
        view_model: the view-model to render. For :attr:`VisualizationState.OK`
            and :attr:`VisualizationState.EMPTY` this is the freshly built model
            (empty in the empty-state case). For
            :attr:`VisualizationState.PARSE_ERROR` this is the *retained* last
            valid view-model (Req 5.6), which is ``None`` only when no valid
            visualization has been rendered yet.
        error: the parse-failure description identifying the failure (Req 5.6);
            populated only for :attr:`VisualizationState.PARSE_ERROR`.
        message: the empty-state indication (Req 5.5); populated only for
            :attr:`VisualizationState.EMPTY`.
    """

    state: VisualizationState
    view_model: Optional[VisualizationViewModel] = None
    error: Optional[str] = None
    message: Optional[str] = None

    @property
    def is_ok(self) -> bool:
        """Return ``True`` iff the draft rendered a non-empty view-model."""
        return self.state is VisualizationState.OK

    @property
    def is_empty_state(self) -> bool:
        """Return ``True`` iff the empty-state fallback applies (Req 5.5)."""
        return self.state is VisualizationState.EMPTY

    @property
    def is_parse_error(self) -> bool:
        """Return ``True`` iff the parse-failure fallback applies (Req 5.6)."""
        return self.state is VisualizationState.PARSE_ERROR


def render_visualization(
    draft: Draft,
    last_valid: Optional[VisualizationViewModel] = None,
) -> VisualizationRender:
    """Classify ``draft`` into a :class:`VisualizationRender`.

    Args:
        draft: the plugin draft. Either raw ``plugin.spec.yaml`` text
            (``None`` or a blank string counts as an empty draft) or an
            already-parsed :class:`PluginSpec`.
        last_valid: the most recently rendered valid view-model, returned as the
            retained visualization when ``draft`` cannot be parsed (Req 5.6).

    Returns:
        A :class:`VisualizationRender` whose :attr:`~VisualizationRender.state`
        indicates OK, empty-state (Req 5.5), or parse failure (Req 5.6).
    """
    if isinstance(draft, PluginSpec):
        return _render_spec(draft)

    if draft is None or not draft.strip():
        # A blank/whitespace-only or absent draft has no spec at all; treat it
        # as an empty draft (Req 5.5) rather than a parse failure.
        return _empty_render(build_view_model(PluginSpec()))

    try:
        spec = load_plugin_spec(draft)
    except (YAMLError, ValueError) as exc:
        # Unparseable draft: report the parse failure and retain the last valid
        # visualization (Req 5.6).
        return VisualizationRender(
            state=VisualizationState.PARSE_ERROR,
            view_model=last_valid,
            error=str(exc),
        )

    return _render_spec(spec)


def _render_spec(spec: PluginSpec) -> VisualizationRender:
    """Build the view-model for a parsed ``spec`` and pick OK vs empty-state."""
    view_model = build_view_model(spec)
    if view_model.is_empty:
        return _empty_render(view_model)
    return VisualizationRender(state=VisualizationState.OK, view_model=view_model)


def _empty_render(view_model: VisualizationViewModel) -> VisualizationRender:
    """Wrap an empty ``view_model`` in an empty-state render (Req 5.5)."""
    return VisualizationRender(
        state=VisualizationState.EMPTY,
        view_model=view_model,
        message=EMPTY_STATE_MESSAGE,
    )


class VisualizationRenderer:
    """Stateful renderer that retains the last valid visualization (Req 5.6).

    Each call to :meth:`render` classifies a draft via
    :func:`render_visualization`, passing the retained last valid view-model so
    a parse failure falls back to it. Any render that is not a parse failure
    (OK or empty-state -- both are valid visualizations) updates the retained
    model, so the next parse failure retains the *most recently* rendered valid
    visualization.
    """

    def __init__(self) -> None:
        self._last_valid: Optional[VisualizationViewModel] = None

    @property
    def last_valid(self) -> Optional[VisualizationViewModel]:
        """The most recently rendered valid view-model, or ``None`` if none yet."""
        return self._last_valid

    def render(self, draft: Draft) -> VisualizationRender:
        """Render ``draft``, updating and falling back to the retained model."""
        result = render_visualization(draft, last_valid=self._last_valid)
        if result.state is not VisualizationState.PARSE_ERROR:
            self._last_valid = result.view_model
        return result
