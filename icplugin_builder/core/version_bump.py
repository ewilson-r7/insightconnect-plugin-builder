"""Schema-aware version bumper (design Property 24; Req 12.3, 12.4, 12.5, 12.7).

Given the current draft version, the set of versions previously exported for a
plugin (from the ``Plugin_Registry``), and whether the change relative to the
most recently exported spec is breaking, this module decides the version the
next export will carry.

The decision rules are:

* **No prior export** -> the plugin is exported at its current version
  unchanged (Req 12.7).
* **Breaking change** -> a MAJOR bump of the form ``(major + 1, 0, 0)``
  (Req 12.3).
* **Non-breaking change** -> a PATCH increment (Req 12.4).

On top of those rules the result is always guaranteed to be **strictly greater
than every previously exported version** under semantic-version ordering
(Req 12.5). To satisfy both the shape rules and monotonicity at once, the bump
is computed relative to the highest version known for the plugin -- the greater
of the current draft version and the maximum prior exported version -- so a
draft that lags behind an already-exported version can never produce a
colliding or lower version.

This module is pure arithmetic over :class:`~icplugin_builder.core.spec_model.SemVer`
and reuses the breaking-change classifier
(:mod:`icplugin_builder.core.classifier`) for the convenience entry point
:func:`bump_for_export`. It deliberately does not touch the draft's
``version_history``; that extension is layered on top (task 2.7) using the
``previous``/``new`` versions exposed here.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

from .classifier import classify_change
from .spec_model import PluginSpec, SemVer

__all__ = [
    "BUMP_NONE",
    "BUMP_MAJOR",
    "BUMP_PATCH",
    "VERSION_HISTORY_KEY",
    "VersionBump",
    "VersionHistoryUpdate",
    "bump_version",
    "bump_for_export",
    "version_history_entry",
    "apply_version_bump",
]

# Top-level spec key holding the ordered list of human-readable change notes.
# It is not a first-class :class:`PluginSpec` attribute, so it is carried in the
# spec's ``extra`` mapping.
VERSION_HISTORY_KEY = "version_history"

# Bump-kind labels describing which decision rule produced the new version.
BUMP_NONE = "none"
BUMP_MAJOR = "major"
BUMP_PATCH = "patch"


@dataclass(frozen=True)
class VersionBump:
    """Outcome of a version-bump decision.

    Attributes:
        previous: The version before the bump (the current draft version).
        new: The version the next export will carry. Equal to ``previous`` when
            no prior export exists.
        kind: One of :data:`BUMP_NONE`, :data:`BUMP_MAJOR`, or
            :data:`BUMP_PATCH`.
        breaking: ``True`` iff the change was classified as breaking.
        reasons: Human-readable reasons for a breaking classification (empty for
            non-breaking or no-prior-export outcomes); useful for the
            ``version_history`` entry recorded on a bump.
    """

    previous: SemVer
    new: SemVer
    kind: str
    breaking: bool = False
    reasons: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """``True`` iff the new version differs from the previous version."""
        return self.new != self.previous


def bump_version(
    current_version: SemVer,
    prior_versions: Iterable[SemVer],
    *,
    is_breaking: bool,
    reasons: Sequence[str] | None = None,
) -> VersionBump:
    """Decide the next export version from prior versions and a breaking flag.

    Args:
        current_version: The version on the current draft spec.
        prior_versions: Every version previously exported for this plugin
            (order irrelevant; may be empty).
        is_breaking: Whether the change relative to the most recently exported
            spec is a breaking schema change.
        reasons: Optional breaking-change reasons to carry on the result.

    Returns:
        A :class:`VersionBump`. When ``prior_versions`` is empty the current
        version is returned unchanged (Req 12.7). Otherwise a breaking change
        yields ``(major + 1, 0, 0)`` (Req 12.3) and a non-breaking change yields
        a patch increment (Req 12.4); in both cases the ``new`` version is
        strictly greater than every prior version (Req 12.5).
    """
    priors = list(prior_versions)
    reason_list = list(reasons or [])

    # Req 12.7: no prior export -> keep the current version unchanged.
    if not priors:
        return VersionBump(
            previous=current_version,
            new=current_version,
            kind=BUMP_NONE,
            breaking=is_breaking,
            reasons=reason_list,
        )

    # Base the bump on the highest version known for the plugin so the result
    # is monotone even if the draft version lags a prior export (Req 12.5).
    base = max([current_version, *priors])

    if is_breaking:
        new_version = SemVer(base.major + 1, 0, 0)  # Req 12.3
        kind = BUMP_MAJOR
    else:
        new_version = base.bump_patch()  # Req 12.4
        kind = BUMP_PATCH

    return VersionBump(
        previous=current_version,
        new=new_version,
        kind=kind,
        breaking=is_breaking,
        reasons=reason_list,
    )


def bump_for_export(
    current_spec: PluginSpec,
    last_exported_spec: PluginSpec | None,
    prior_versions: Iterable[SemVer],
) -> VersionBump:
    """Classify the change and decide the next export version in one step.

    Convenience wrapper around :func:`bump_version` for the orchestrator's
    export flow: it runs the breaking-change classifier against the most
    recently exported spec and feeds the result into the bump decision.

    Args:
        current_spec: The current draft spec being exported.
        last_exported_spec: The most recently exported spec, or ``None`` when
            the plugin has never been exported.
        prior_versions: Every version previously exported for this plugin.

    Returns:
        A :class:`VersionBump` describing the decision. When there is no prior
        export the current version is kept unchanged (Req 12.7); otherwise the
        change is classified and the version bumped accordingly.
    """
    priors = list(prior_versions)

    # Without a prior export there is nothing to classify against; keep as-is.
    if last_exported_spec is None or not priors:
        return bump_version(current_spec.version, priors, is_breaking=False)

    classification = classify_change(last_exported_spec, current_spec)
    return bump_version(
        current_spec.version,
        priors,
        is_breaking=classification.is_breaking,
        reasons=classification.reasons,
    )


@dataclass(frozen=True)
class VersionHistoryUpdate:
    """Result of extending a spec's ``version_history`` for a bump (Req 12.6).

    Attributes:
        spec: A copy of the input spec with its ``version`` set to the bumped
            version and exactly one new ``version_history`` entry recorded. The
            input spec is left untouched.
        entry: The single ``version_history`` entry that was added. It always
            references the new version (it is prefixed with ``str(new)``).
        previous: The version before the bump.
        new: The version after the bump (also the spec's new ``version``).
    """

    spec: PluginSpec
    entry: str
    previous: SemVer
    new: SemVer

    @property
    def display(self) -> str:
        """A ``"<previous> -> <new>"`` string to show the user before the build."""
        return f"{self.previous} -> {self.new}"


def _default_description(bump: VersionBump) -> str:
    """Derive a human-readable change note from a :class:`VersionBump`."""
    if bump.breaking and bump.reasons:
        return "; ".join(bump.reasons)
    if bump.kind == BUMP_MAJOR:
        return "Breaking changes"
    if bump.kind == BUMP_PATCH:
        return "Updates"
    return "Initial plugin"


def version_history_entry(bump: VersionBump, description: Optional[str] = None) -> str:
    """Build the single ``version_history`` entry for a bump.

    The entry follows the InsightConnect convention ``"<version> - <notes>"`` and
    always references the new version, so it can be matched back to the export.

    Args:
        bump: The version-bump decision the entry describes.
        description: Optional change note. When omitted, a note is derived from
            the bump (breaking-change reasons, or a generic label per kind).

    Returns:
        A ``version_history`` entry string prefixed with the new version.
    """
    notes = description if description is not None else _default_description(bump)
    return f"{bump.new} - {notes}"


def apply_version_bump(
    spec: PluginSpec,
    bump: VersionBump,
    *,
    description: Optional[str] = None,
) -> VersionHistoryUpdate:
    """Record a version bump on a spec's ``version_history`` (Req 12.6).

    Returns a copy of ``spec`` whose ``version`` is the bumped version and whose
    ``version_history`` has exactly one additional entry (design Property 25).
    The new entry is placed first, matching the newest-first ordering used in
    on-disk plugin specs, and references the new version. The ``previous`` and
    ``new`` versions are exposed on the result for display before the build
    begins.

    The input ``spec`` is not mutated.

    Args:
        spec: The draft spec being exported.
        bump: The version-bump decision (from :func:`bump_version` or
            :func:`bump_for_export`).
        description: Optional change note for the new entry; a sensible default
            is derived from ``bump`` when omitted.

    Returns:
        A :class:`VersionHistoryUpdate` with the updated spec, the added entry,
        and the previous/new versions.
    """
    entry = version_history_entry(bump, description)

    updated = copy.deepcopy(spec)
    updated.version = bump.new

    existing = updated.extra.get(VERSION_HISTORY_KEY)
    if existing is None:
        history: List = []
    elif isinstance(existing, list):
        history = list(existing)
    else:
        # Tolerate a malformed scalar by treating it as a single prior entry.
        history = [existing]

    updated.extra[VERSION_HISTORY_KEY] = [entry, *history]

    return VersionHistoryUpdate(
        spec=updated,
        entry=entry,
        previous=bump.previous,
        new=bump.new,
    )
