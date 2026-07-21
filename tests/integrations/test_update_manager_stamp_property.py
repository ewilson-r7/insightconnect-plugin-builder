"""Property test for per-build tooling version stamps (task 18.8).

When a build completes, :meth:`UpdateManager.stamp_build` writes the tool
versions actually used into the plugin's ``Project_Folder`` (``.builder/
tooling.json``). This module covers the universal property across generated
inputs: the :class:`ToolingStamp` recorded for the build version equals exactly
the versions used for that build -- whether those come from the manager's
installed snapshot or from an explicit ``used_versions`` override (Req 23.2).

Every collaborator is injected: the installed probe reports a generated
snapshot, and the Project_Folder lives in a per-example temporary directory, so
the test is deterministic with no real binaries, network, or wall clock.
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from icplugin_builder.api.config import UpdatesConfig
from icplugin_builder.core.spec_model import PluginSpec, SemVer
from icplugin_builder.integrations.update_manager import (
    COMPONENT_INSIGHT_PLUGIN_CLI,
    COMPONENT_INSIGHTCONNECT_SDK,
    COMPONENT_KIRO_CLI,
    COMPONENT_PLUGIN_SPEC_SCHEMA,
    UpdateManager,
)
from icplugin_builder.persistence.project_folder import ProjectFolder, ToolingStamp

version_strategy = st.from_regex(r"\A[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,2}\Z", fullmatch=True)


def tooling_versions():
    """Generate a ``component -> version`` map over a subset of Managed_Tooling.

    Each of the four managed-tooling components is independently present or
    absent, so partial snapshots (for example when a binary is not installed)
    are exercised alongside complete ones.
    """
    optional_version = st.one_of(st.none(), version_strategy)
    return st.fixed_dictionaries(
        {
            COMPONENT_INSIGHT_PLUGIN_CLI: optional_version,
            COMPONENT_INSIGHTCONNECT_SDK: optional_version,
            COMPONENT_KIRO_CLI: optional_version,
            COMPONENT_PLUGIN_SPEC_SCHEMA: optional_version,
        }
    ).map(lambda mapping: {component: value for component, value in mapping.items() if value is not None})


def _expected_stamp(used: dict) -> ToolingStamp:
    """The :class:`ToolingStamp` that exactly records ``used``'s versions."""
    return ToolingStamp(
        insight_plugin_cli=used.get(COMPONENT_INSIGHT_PLUGIN_CLI),
        sdk_version=used.get(COMPONENT_INSIGHTCONNECT_SDK),
        kiro_cli=used.get(COMPONENT_KIRO_CLI),
        spec_schema=used.get(COMPONENT_PLUGIN_SPEC_SCHEMA),
    )


# Feature: insightconnect-plugin-builder, Property 41: Per-build tooling version stamp accuracy
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    installed=tooling_versions(),
    override=st.one_of(st.none(), tooling_versions()),
    build_version=version_strategy,
)
def test_stamped_versions_equal_versions_used(tmp_path_factory, installed, override, build_version):
    """The stamped CLI/SDK versions equal the versions used for the build.

    A Project_Folder is created in a fresh temp directory and the manager is
    given a generated installed snapshot. ``stamp_build`` is then invoked either
    with an explicit ``used_versions`` override or without one (defaulting to the
    installed snapshot). Whichever source supplies the versions, the
    :class:`ToolingStamp` recorded in ``tooling.json`` under the build version
    must equal exactly a stamp built from those used versions -- so the stamp
    always reflects the tooling actually in effect for the build.

    **Validates: Requirements 23.2**
    """
    project_root = tmp_path_factory.mktemp("stamp")
    spec = PluginSpec(name="my_plugin", title="My Plugin", version=SemVer(1, 0, 0), vendor="acme_custom")
    folder = ProjectFolder.create(project_root, "my_plugin", spec)

    manager = UpdateManager(
        UpdatesConfig(offline_mode=False, cache_ttl_hours=24),
        installed_probe=lambda: dict(installed),
        upstream_source=lambda: {},
    )
    manager.snapshot_installed()

    # The versions actually used for the build: the explicit override when given,
    # otherwise the installed snapshot the manager falls back to.
    used = override if override is not None else installed
    expected = _expected_stamp(used)

    if override is not None:
        returned = manager.stamp_build(folder, build_version, used_versions=override)
    else:
        returned = manager.stamp_build(folder, build_version)

    # The returned stamp reflects exactly the versions used.
    assert returned == expected

    # The stamp persisted under the build version equals the versions used.
    recorded = folder.tooling()[build_version]
    assert recorded == expected
    assert recorded.insight_plugin_cli == used.get(COMPONENT_INSIGHT_PLUGIN_CLI)
    assert recorded.sdk_version == used.get(COMPONENT_INSIGHTCONNECT_SDK)
    assert recorded.kiro_cli == used.get(COMPONENT_KIRO_CLI)
    assert recorded.spec_schema == used.get(COMPONENT_PLUGIN_SPEC_SCHEMA)
