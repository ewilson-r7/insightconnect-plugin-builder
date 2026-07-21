"""Unit tests for the schema-aware version bumper (task 2.5).

These cover specific examples and edge cases for Req 12.3, 12.4, 12.5, and 12.7
/ design Property 24. The universal monotonicity property across generated
inputs is covered separately by the property test (task 2.6).
"""

from icplugin_builder.core.classifier import classify_change
from icplugin_builder.core.spec_model import Component, FieldSchema, PluginSpec, SemVer
from icplugin_builder.core.version_bump import (
    BUMP_MAJOR,
    BUMP_NONE,
    BUMP_PATCH,
    VERSION_HISTORY_KEY,
    VersionBump,
    apply_version_bump,
    bump_for_export,
    bump_version,
    version_history_entry,
)


def _spec(version="1.0.0", **kwargs) -> PluginSpec:
    base = dict(name="example", title="Example", description="d", vendor="rapid7")
    base.update(kwargs)
    return PluginSpec(version=SemVer.parse(version), **base)


def _action(inputs=None, outputs=None) -> Component:
    return Component(title="Run", input=dict(inputs or {}), output=dict(outputs or {}))


class TestNoPriorExport:
    def test_keeps_current_version_unchanged(self):
        # Req 12.7: no prior export -> export at current version, no increment.
        result = bump_version(SemVer(2, 3, 4), [], is_breaking=False)
        assert result.new == SemVer(2, 3, 4)
        assert result.previous == SemVer(2, 3, 4)
        assert result.kind == BUMP_NONE
        assert result.changed is False

    def test_keeps_current_version_even_when_breaking(self):
        # With no prior export there is no baseline to break against.
        result = bump_version(SemVer(1, 0, 0), [], is_breaking=True)
        assert result.new == SemVer(1, 0, 0)
        assert result.kind == BUMP_NONE


class TestBreakingBump:
    def test_major_bump_resets_minor_and_patch(self):
        # Req 12.3: breaking -> (major+1, 0, 0).
        result = bump_version(SemVer(1, 4, 7), [SemVer(1, 4, 7)], is_breaking=True)
        assert result.new == SemVer(2, 0, 0)
        assert result.kind == BUMP_MAJOR
        assert result.breaking is True

    def test_major_bump_is_monotone_over_higher_priors(self):
        # Req 12.5: result strictly greater than every prior version.
        priors = [SemVer(1, 0, 0), SemVer(3, 2, 1), SemVer(2, 9, 9)]
        result = bump_version(SemVer(1, 0, 0), priors, is_breaking=True)
        assert result.new == SemVer(4, 0, 0)
        assert all(result.new > p for p in priors)


class TestNonBreakingBump:
    def test_patch_increment(self):
        # Req 12.4: non-breaking with prior export -> patch increment.
        result = bump_version(SemVer(1, 2, 3), [SemVer(1, 2, 3)], is_breaking=False)
        assert result.new == SemVer(1, 2, 4)
        assert result.kind == BUMP_PATCH
        assert result.breaking is False

    def test_patch_bump_is_monotone_over_higher_priors(self):
        # Req 12.5: patch bump must still exceed the highest prior version.
        priors = [SemVer(1, 2, 3), SemVer(1, 2, 9)]
        result = bump_version(SemVer(1, 2, 3), priors, is_breaking=False)
        assert result.new == SemVer(1, 2, 10)
        assert all(result.new > p for p in priors)

    def test_result_strictly_greater_than_all_priors(self):
        priors = [SemVer(0, 1, 0), SemVer(1, 0, 0), SemVer(1, 0, 5)]
        result = bump_version(SemVer(1, 0, 5), priors, is_breaking=False)
        assert all(result.new > p for p in priors)


class TestBumpForExport:
    def test_no_last_exported_spec_keeps_version(self):
        current = _spec(version="1.0.0", actions={"run": _action({"x": FieldSchema(type="string")})})
        result = bump_for_export(current, None, [])
        assert result.new == SemVer(1, 0, 0)
        assert result.kind == BUMP_NONE

    def test_non_breaking_change_patch_bumps(self):
        last = _spec(version="1.0.0", actions={"run": _action({"x": FieldSchema(type="string")})})
        current = _spec(
            version="1.0.0",
            actions={"run": _action({"x": FieldSchema(type="string"), "y": FieldSchema(type="string")})},
        )
        result = bump_for_export(current, last, [SemVer(1, 0, 0)])
        assert result.new == SemVer(1, 0, 1)
        assert result.kind == BUMP_PATCH
        assert result.breaking is False

    def test_breaking_change_major_bumps_with_reasons(self):
        last = _spec(version="1.0.0", actions={"run": _action({"x": FieldSchema(type="string")})})
        current = _spec(version="1.0.0", actions={"run": _action({"x": FieldSchema(type="integer")})})
        result = bump_for_export(current, last, [SemVer(1, 0, 0)])
        assert result.new == SemVer(2, 0, 0)
        assert result.kind == BUMP_MAJOR
        assert result.breaking is True
        assert result.reasons  # classifier reasons carried through
        # Reasons match the classifier's own output.
        assert result.reasons == classify_change(last, current).reasons


def _bump(previous="1.0.0", new="1.0.1", kind=BUMP_PATCH, breaking=False, reasons=None):
    return VersionBump(
        previous=SemVer.parse(previous),
        new=SemVer.parse(new),
        kind=kind,
        breaking=breaking,
        reasons=list(reasons or []),
    )


class TestVersionHistoryEntry:
    def test_entry_references_the_new_version(self):
        # Req 12.6: the entry describes the change and names the new version.
        entry = version_history_entry(_bump(new="2.0.0", kind=BUMP_MAJOR, breaking=True))
        assert entry.startswith("2.0.0 - ")

    def test_breaking_reasons_are_used_as_description(self):
        entry = version_history_entry(
            _bump(new="2.0.0", kind=BUMP_MAJOR, breaking=True, reasons=["removed input x", "type changed"])
        )
        assert entry == "2.0.0 - removed input x; type changed"

    def test_explicit_description_overrides_default(self):
        entry = version_history_entry(_bump(new="1.1.0"), description="Added List Users action")
        assert entry == "1.1.0 - Added List Users action"

    def test_no_change_defaults_to_initial_plugin(self):
        entry = version_history_entry(_bump(previous="1.0.0", new="1.0.0", kind=BUMP_NONE))
        assert entry == "1.0.0 - Initial plugin"


class TestApplyVersionBump:
    def test_adds_exactly_one_entry_referencing_new_version(self):
        # Design Property 25: exactly one additional entry referencing the new version.
        spec = _spec(version="1.0.0")
        spec.extra[VERSION_HISTORY_KEY] = ["1.0.0 - Initial plugin"]

        result = apply_version_bump(spec, _bump(previous="1.0.0", new="1.0.1"))

        history = result.spec.extra[VERSION_HISTORY_KEY]
        assert len(history) == 2  # one more than before
        assert history[0].startswith("1.0.1 - ")  # newest first, references new version

    def test_sets_spec_version_to_new_version(self):
        spec = _spec(version="1.0.0")
        result = apply_version_bump(spec, _bump(previous="1.0.0", new="2.0.0", kind=BUMP_MAJOR, breaking=True))
        assert result.spec.version == SemVer(2, 0, 0)

    def test_does_not_mutate_input_spec(self):
        spec = _spec(version="1.0.0")
        spec.extra[VERSION_HISTORY_KEY] = ["1.0.0 - Initial plugin"]

        apply_version_bump(spec, _bump(previous="1.0.0", new="1.0.1"))

        assert spec.version == SemVer(1, 0, 0)
        assert spec.extra[VERSION_HISTORY_KEY] == ["1.0.0 - Initial plugin"]

    def test_creates_history_when_absent(self):
        spec = _spec(version="1.0.0")
        assert VERSION_HISTORY_KEY not in spec.extra

        result = apply_version_bump(spec, _bump(previous="1.0.0", new="1.0.0", kind=BUMP_NONE))

        assert result.spec.extra[VERSION_HISTORY_KEY] == ["1.0.0 - Initial plugin"]

    def test_exposes_previous_and_new_for_display(self):
        # Req 12.6: previous and new versions surfaced before the build begins.
        spec = _spec(version="1.0.0")
        result = apply_version_bump(spec, _bump(previous="1.0.0", new="2.0.0", kind=BUMP_MAJOR, breaking=True))
        assert result.previous == SemVer(1, 0, 0)
        assert result.new == SemVer(2, 0, 0)
        assert result.display == "1.0.0 -> 2.0.0"
