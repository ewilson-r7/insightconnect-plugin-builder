"""Property test for production-fork identity (task 17.3; Req 24.5, 25.4).

The unit tests in ``test_plugin_source_provider.py`` pin single examples of the
fork-identity behavior (``test_forks_into_new_project_folder_with_custom_vendor``
and ``test_records_provenance`` on the ``jira`` fixture); this module covers the
universal property across generated production plugin source trees: forking a
plugin with
:meth:`~icplugin_builder.integrations.plugin_source_provider.PluginSourceProvider.import_plugin`
always yields a fork whose vendor carries the ``_custom`` suffix, retains the
original plugin name, and records an ``enhance_production`` provenance carrying
the source repository, the original name, and the original version.

Each example builds a distinct production plugin in a throwaway local clone
(varied name/vendor/version, ``icon_``/``komand_`` package prefix, public or
private source repo), runs the import from the local clone (no network/git),
and asserts the three fork-identity facts against both the returned
:class:`~icplugin_builder.integrations.plugin_source_provider.ImportResult` and
the persisted ``plugin.spec.yaml``/``project.json``. The provider itself is not
mocked -- the real fork path exercises the filesystem.
"""

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from icplugin_builder.api.config import ProductionSourceConfig
from icplugin_builder.core.vendor import CUSTOM_VENDOR_SUFFIX
from icplugin_builder.core.yaml_codec import load_plugin_spec
from icplugin_builder.integrations.plugin_source_provider import (
    ENHANCE_PRODUCTION_ENTRY_MODE,
    PluginSourceProvider,
)
from tests import strategies as strat

# Package prefixes a production plugin package directory may carry (Req 25.7).
_PACKAGE_PREFIXES = ("icon", "komand")


def _spec_text(name: str, vendor: str, version: strat.SemVer) -> str:
    """Render a minimal schema-conforming ``plugin.spec.yaml`` body.

    Carries a recognizable name/vendor/version plus license/attribution
    ``resources`` so the import path runs end to end. The title is derived from
    the (snake_case) name so the body is always YAML-safe.
    """
    return (
        "plugin_spec_version: v2\n"
        f"name: {name}\n"
        f"title: {name.title()}\n"
        "description: A production plugin.\n"
        f"version: {version.major}.{version.minor}.{version.patch}\n"
        f"vendor: {vendor}\n"
        "resources:\n"
        "  source_url: https://github.com/rapid7/insightconnect-plugins\n"
        "  license_url: https://www.apache.org/licenses/LICENSE-2.0\n"
        "actions:\n"
        "  do_thing:\n"
        "    title: Do Thing\n"
        "    description: Does a thing.\n"
        "    input:\n"
        "      host:\n"
        "        type: string\n"
        "        required: true\n"
    )


@st.composite
def production_forks(draw: st.DrawFn):
    """Generate the makings of a production plugin plus its configured source.

    Returns ``(name, prefix, vendor, version, repo, visibility, spec_text)``.
    The vendor strategy mixes plain and already-``_custom`` values so the
    suffix's idempotency is exercised on the fork path.
    """
    name = draw(strat.snake_case_names())
    prefix = draw(st.sampled_from(_PACKAGE_PREFIXES))
    vendor = draw(strat.vendors())
    version = draw(strat.semvers())
    repo = f"{draw(strat.snake_case_names())}/{draw(strat.snake_case_names())}"
    visibility = draw(st.sampled_from(("public", "private")))
    return name, prefix, vendor, version, repo, visibility, _spec_text(name, vendor, version)


def _write_tree(clone_dir: Path, name: str, prefix: str, spec_text: str) -> None:
    """Materialize a minimal production plugin directory under ``clone_dir``."""
    plugin_dir = clone_dir / name
    package_dir = plugin_dir / f"{prefix}_{name}"
    package_dir.mkdir(parents=True)
    (plugin_dir / "plugin.spec.yaml").write_text(spec_text, encoding="utf-8")
    # An importable package module gives prefix detection a target.
    (package_dir / "__init__.py").write_text("", encoding="utf-8")


# Feature: insightconnect-plugin-builder, Property 48: Production-fork identity
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(fork=production_forks())
def test_fork_identity(fork):
    """Forking a production plugin yields the expected fork identity (Req 24.5, 25.4).

    For an arbitrary production plugin in a local clone, ``import_plugin`` must
    produce a fork whose vendor ends in ``_custom``, whose plugin name is the
    original (unchanged), and whose provenance is ``enhance_production`` carrying
    the source repository, the original plugin name, and the original version.
    Both the returned ``ImportResult`` and the persisted spec/metadata are
    checked so the identity holds on disk as well as in memory.

    **Validates: Requirements 24.5, 25.4**
    """
    name, prefix, _vendor, version, repo, visibility, spec_text = fork
    expected_version = f"{version.major}.{version.minor}.{version.patch}"

    with tempfile.TemporaryDirectory(prefix="icpb-fork-") as workspace:
        workspace_path = Path(workspace)
        clone_dir = workspace_path / "clone"
        clone_dir.mkdir()
        projects_root = workspace_path / "projects"

        _write_tree(clone_dir, name, prefix, spec_text)

        source = ProductionSourceConfig(
            id="prod_source",
            repo=repo,
            visibility=visibility,
            local_path=str(clone_dir),
            remote_url="https://github.com/rapid7/insightconnect-plugins.git",
        )
        provider = PluginSourceProvider([source], projects_root)
        result = provider.import_plugin("prod_source", name)

        folder = result.project_folder
        fork_spec = load_plugin_spec(folder.spec_path.read_text(encoding="utf-8"))
        provenance = result.provenance
        metadata = folder.metadata()

    # Vendor carries the _custom suffix (Req 25.4).
    assert fork_spec.vendor.endswith(CUSTOM_VENDOR_SUFFIX)

    # Original plugin name retained on the spec and the Project_Folder (Req 25.4).
    assert fork_spec.name == name
    assert folder.plugin_name == name

    # Provenance is enhance_production carrying repo/name/version (Req 24.5, 25.4).
    assert provenance.entry_mode == ENHANCE_PRODUCTION_ENTRY_MODE
    assert provenance.source_repo == repo
    assert provenance.original_plugin_name == name
    assert provenance.original_version == expected_version

    # The same provenance is persisted in the Project_Folder metadata (Req 24.5).
    assert metadata.provenance == provenance
