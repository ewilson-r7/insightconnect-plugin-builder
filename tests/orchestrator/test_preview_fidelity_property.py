"""
# Feature: export-gate-and-preview-fidelity, Property 63: The preview describes what would be packaged

Property 63 states that the export preview's spec is the vendor-suffixed,
version-bumped spec **on disk**; that the preview's completeness findings are
``check_completeness`` of that spec; and that a spec complete on disk yields zero
completeness findings.

The defect this closes: ``prepare_export`` evaluated the in-session draft. Once
implementation was delegated to an agent that writes ``plugin.spec.yaml`` into the
project tree, the draft and the tree diverged, and the preview reported findings
about a spec that no longer existed -- 16 of them, against a file that had none.

Generators draw a whole spec, write it to the tree *behind the session's back* (the
agent's write, modelled without invoking an agent), and assert the preview tracks
the tree rather than the draft. Nothing here needs Docker or the plugin toolchain:
no code validator or quality gate is attached, because neither contributes to
``spec_preview`` or to the completeness findings.

**Validates: Requirements 2.11, 2.12**
"""

import asyncio

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from icplugin_builder.core.spec_completeness import check_completeness
from icplugin_builder.core.vendor import apply_custom_vendor_suffix
from icplugin_builder.core.yaml_codec import dump_plugin_spec, load_plugin_spec
from icplugin_builder.orchestrator import Orchestrator
from icplugin_builder.persistence.project_folder import (
    ENTRY_MODE_ITERATE_CUSTOM,
    ProjectFolder,
)
from icplugin_builder.persistence.registry import PluginRegistry
from tests.strategies import plugin_specs

_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def _session_over(projects_root, authored, on_disk):
    """Open an ``iterate_custom`` session on ``authored``, then rewrite the tree.

    Returns ``(orchestrator, folder)``. The rewrite stands in for the delegated
    agent: it changes the spec on the tree only, leaving the session holding the
    draft it read at open time.
    """
    folder = ProjectFolder.create(projects_root, authored.name, authored)
    folder.save(authored)
    orch = Orchestrator(
        projects_root=projects_root,
        registry=PluginRegistry(str(projects_root / "registry.db")),
    )
    orch.start_session(
        ENTRY_MODE_ITERATE_CUSTOM,
        session_id="s",
        user_id="u",
        plugin_name=authored.name,
    )
    on_disk.name = authored.name  # the agent does not rename the folder it works in
    folder.spec_path.write_text(dump_plugin_spec(on_disk), encoding="utf-8")
    return orch, folder


@given(authored=plugin_specs(), on_disk=plugin_specs())
@_SETTINGS
def test_the_previewed_spec_is_the_vendor_suffixed_disk_spec(tmp_path_factory, authored, on_disk):
    """``plan.spec_preview = versionedVendorSuffixed(diskSpec(projectFolder(X)))``."""
    root = tmp_path_factory.mktemp("preview")
    orch, folder = _session_over(root, authored, on_disk)

    plan = asyncio.run(orch.prepare_export("s"))

    expected = load_plugin_spec(folder.spec_path.read_text(encoding="utf-8"))
    assert plan.spec_preview.title == expected.title
    assert plan.spec_preview.description == expected.description
    assert plan.spec_preview.vendor == apply_custom_vendor_suffix(expected.vendor)
    assert sorted(plan.spec_preview.actions) == sorted(expected.actions)
    assert sorted(plan.spec_preview.connection) == sorted(expected.connection)


@given(authored=plugin_specs(), on_disk=plugin_specs())
@_SETTINGS
def test_the_completeness_findings_are_the_disk_specs(tmp_path_factory, authored, on_disk):
    """``completenessFindings(plan) = checkCompleteness(diskSpec(projectFolder(X)))``."""
    root = tmp_path_factory.mktemp("preview")
    orch, _ = _session_over(root, authored, on_disk)

    plan = asyncio.run(orch.prepare_export("s"))

    expected = check_completeness(plan.spec_preview)
    assert tuple(finding.key for finding in plan.completeness.findings) == tuple(
        finding.key for finding in expected.findings
    )


@given(authored=plugin_specs(min_actions=1), extra=st.text(alphabet="abcdef", min_size=1, max_size=8))
@_SETTINGS
def test_a_spec_complete_on_disk_yields_no_completeness_findings(tmp_path_factory, authored, extra):
    """``isCompleteOnDisk(X) IMPLIES completenessFindings(plan) = EMPTY``.

    The complete spec is built by taking a generated one and filling in every
    field the completeness check requires, so "complete" is established by the
    checker's own definition rather than by a hand-written fixture that could
    drift from it.
    """
    root = tmp_path_factory.mktemp("preview")
    complete = _make_complete(authored, extra)
    orch, _ = _session_over(root, authored, complete)

    plan = asyncio.run(orch.prepare_export("s"))

    findings = check_completeness(complete).findings
    if findings:
        # The generated spec could not be completed (a case the checker flags for
        # a reason unrelated to the fields filled below); assert the weaker claim
        # that still bites -- the preview agrees with the disk spec, not the draft.
        assert tuple(f.key for f in plan.completeness.findings) == tuple(
            f.key for f in check_completeness(plan.spec_preview).findings
        )
    else:
        assert not plan.completeness.findings


def _make_complete(spec, extra):
    """Return a copy of ``spec`` carrying every field the completeness check wants."""
    complete = load_plugin_spec(dump_plugin_spec(spec))
    complete.extra.update(
        {
            "extension": "plugin",
            "products": ["insightconnect"],
            "cloud_ready": False,
            "support": "rapid7",
            "status": [],
            "supported_versions": ["2026-01-01"],
            "key_features": [f"does {extra}"],
            "requirements": ["API credentials"],
            "version_history": [f"{complete.version} - Initial plugin release"],
            "sdk": {"type": "full", "version": "6.6.0", "user": "nobody"},
            "resources": {
                "source_url": "https://example.invalid/src",
                "license_url": "https://example.invalid/license",
                "vendor_url": "https://example.invalid",
            },
            "links": ["[Vendor](https://example.invalid)"],
            "references": ["[API](https://example.invalid/api)"],
            "tags": [extra],
            "hub_tags": {"use_cases": ["threat_detection_and_response"], "keywords": [extra], "features": []},
        }
    )
    for component in list(complete.actions.values()) + list(complete.triggers.values()) + list(complete.tasks.values()):
        for field in component.output.values():
            if not field.example:
                field.example = "example"
    return complete
