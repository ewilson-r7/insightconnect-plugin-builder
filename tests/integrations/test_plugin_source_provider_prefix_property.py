"""Property test for package-prefix handling across both plugin eras (task 17.4; Req 25.7).

Requirement 25.7 says the builder must import production plugins that use either
the current ``icon_`` package prefix or the legacy ``komand_`` prefix. The unit
tests in ``test_plugin_source_provider.py`` pin single examples; this module
covers the universal property across generated production plugin source trees:
regardless of which of the two prefixes a plugin package carries, forking it with
:meth:`~icplugin_builder.integrations.plugin_source_provider.PluginSourceProvider.import_plugin`

* succeeds (no exception, a project folder is created), and
* records the source's *actual* prefix both on the returned
  :class:`~icplugin_builder.integrations.plugin_source_provider.ImportResult`
  and in the persisted project metadata (``.builder/project.json``).

Each example builds a distinct production plugin in a throwaway local clone
(varied name/vendor/version and either an ``icon_`` or ``komand_`` package
directory, a valid ``plugin.spec.yaml``), runs the import via local-clone
resolution (no network/git), and asserts both recorded prefixes equal the
prefix the source tree was written with. No mocking of the provider is used --
the real detection/copy path exercises the filesystem.
"""

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from icplugin_builder.api.config import ProductionSourceConfig
from icplugin_builder.integrations.plugin_source_provider import PluginSourceProvider
from tests import strategies as strat

# The two package prefixes a production plugin package directory may carry: the
# current ``icon_`` and the legacy ``komand_`` (Req 25.7).
_PACKAGE_PREFIXES = ("icon", "komand")


def _spec_text(name: str, vendor: str, version: strat.SemVer) -> str:
    """Render a minimal but schema-conforming ``plugin.spec.yaml`` body.

    The spec content is orthogonal to prefix handling; it only needs to parse
    and carry a recognizable name/vendor/version plus license/attribution
    ``resources`` so the import path runs end to end. The title derives from the
    (snake_case) name so the body is always YAML-safe.
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
def prefixed_plugin_trees(draw: st.DrawFn):
    """Generate the makings of a production plugin directory with a chosen prefix.

    Returns ``(name, prefix, spec_text)`` where ``prefix`` is drawn from both
    eras (``icon``/``komand``) so import is exercised against each.
    """
    name = draw(strat.snake_case_names())
    prefix = draw(st.sampled_from(_PACKAGE_PREFIXES))
    vendor = draw(strat.vendors())
    version = draw(strat.semvers())
    return name, prefix, _spec_text(name, vendor, version)


def _write_tree(clone_dir: Path, name: str, prefix: str, spec_text: str) -> Path:
    """Materialize the production plugin under ``clone_dir`` and return its path."""
    plugin_dir = clone_dir / name
    package_dir = plugin_dir / f"{prefix}_{name}"
    package_dir.mkdir(parents=True)

    (plugin_dir / "plugin.spec.yaml").write_text(spec_text, encoding="utf-8")
    # An importable package module gives prefix detection a concrete target.
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    return plugin_dir


# Feature: insightconnect-plugin-builder, Property 49: Package-prefix handling for both eras
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(tree=prefixed_plugin_trees())
def test_import_records_actual_prefix_for_both_eras(tree):
    """Importing an ``icon_`` or ``komand_`` plugin succeeds and records its prefix (Req 25.7).

    For an arbitrary production plugin whose package directory carries either the
    current ``icon_`` or the legacy ``komand_`` prefix, ``import_plugin`` must
    complete successfully and the recorded package prefix -- both on the returned
    ``ImportResult`` and in the persisted project metadata -- must equal the
    prefix the source tree actually used.

    **Validates: Requirements 25.7**
    """
    name, prefix, spec_text = tree

    with tempfile.TemporaryDirectory(prefix="icpb-prefix-prop-") as workspace:
        workspace_path = Path(workspace)
        clone_dir = workspace_path / "clone"
        clone_dir.mkdir()
        projects_root = workspace_path / "projects"

        _write_tree(clone_dir, name, prefix, spec_text)

        source = ProductionSourceConfig(
            id="rapid7_public",
            repo="rapid7/insightconnect-plugins",
            visibility="public",
            local_path=str(clone_dir),
            remote_url="https://github.com/rapid7/insightconnect-plugins.git",
        )
        provider = PluginSourceProvider([source], projects_root)

        result = provider.import_plugin("rapid7_public", name)

        # Import succeeded for both eras: a project folder was created ...
        assert result.project_folder.path.is_dir()
        # ... and the recorded prefix (ImportResult) equals the source's actual prefix.
        assert result.package_prefix == prefix
        # ... as does the persisted project metadata.
        assert result.project_folder.metadata().package_prefix == prefix
