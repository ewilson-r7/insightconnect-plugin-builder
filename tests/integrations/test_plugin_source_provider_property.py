"""Property test for the read-only production import invariant (task 17.2; Req 25.3).

The unit tests in ``test_plugin_source_provider.py`` pin a single example of the
read-only invariant (``test_does_not_modify_source`` on the ``jira`` fixture);
this module covers the universal property across generated production plugin
source trees: forking a plugin with
:meth:`~icplugin_builder.integrations.plugin_source_provider.PluginSourceProvider.import_plugin`
must leave **every** source file byte-identical -- no file added, removed, or
mutated in the source clone.

Each example builds a distinct production plugin in a throwaway local clone
(varied file set/content, ``icon_``/``komand_`` package prefix, a valid
``plugin.spec.yaml``), snapshots the raw bytes of every source file, runs the
import (local-clone resolution, no network/git), and asserts the snapshot is
unchanged afterwards. No mocking of the provider itself is used -- the real
copy path exercises the filesystem.
"""

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from icplugin_builder.api.config import ProductionSourceConfig
from icplugin_builder.integrations.plugin_source_provider import PluginSourceProvider
from tests import strategies as strat

# Package prefixes a production plugin package directory may carry (Req 25.7).
_PACKAGE_PREFIXES = ("icon", "komand")

# Filenames reserved by the plugin layout; generated extra files avoid them so
# they never clobber the spec or the package's own module.
_RESERVED_NAMES = frozenset({"plugin.spec.yaml", "__init__.py"})


def _spec_text(name: str, vendor: str, version: strat.SemVer) -> str:
    """Render a minimal but schema-conforming ``plugin.spec.yaml`` body.

    The spec content is orthogonal to the read-only invariant; it only needs to
    parse and carry a recognizable name/vendor/version plus license/attribution
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
def _extra_files(draw: st.DrawFn):
    """Generate a map of extra source files: ``{posix_relpath: bytes}``.

    Files land at the plugin root, inside the package directory, or in a nested
    subdirectory, with arbitrary (possibly binary) content, so the source tree
    varies in shape and bytes across examples. Reserved names are excluded so
    generated files never overwrite the spec or the package ``__init__``.
    """
    count = draw(st.integers(min_value=0, max_value=5))
    files = {}
    for _ in range(count):
        depth = draw(st.sampled_from(["root", "package", "nested"]))
        stem = draw(strat.snake_case_names())
        ext = draw(st.sampled_from([".py", ".md", ".json", ".txt", ".bin"]))
        filename = f"{stem}{ext}"
        if filename in _RESERVED_NAMES:
            filename = f"{stem}_x{ext}"
        if depth == "root":
            rel = filename
        elif depth == "package":
            rel = f"__PACKAGE__/{filename}"
        else:
            sub = draw(strat.snake_case_names())
            rel = f"__PACKAGE__/{sub}/{filename}"
        files[rel] = draw(st.binary(min_size=0, max_size=64))
    return files


@st.composite
def production_plugin_trees(draw: st.DrawFn):
    """Generate the makings of a production plugin directory.

    Returns ``(name, prefix, spec_text, extra_files)`` where ``extra_files``
    keys use the ``__PACKAGE__`` placeholder for the package directory so the
    concrete ``<prefix>_<name>`` path can be substituted at write time.
    """
    name = draw(strat.snake_case_names())
    prefix = draw(st.sampled_from(_PACKAGE_PREFIXES))
    vendor = draw(strat.vendors())
    version = draw(strat.semvers())
    spec_text = _spec_text(name, vendor, version)
    extra = draw(_extra_files())
    return name, prefix, spec_text, extra


def _write_tree(clone_dir: Path, name: str, prefix: str, spec_text: str, extra_files) -> Path:
    """Materialize the production plugin under ``clone_dir`` and return its path."""
    plugin_dir = clone_dir / name
    package_dir = plugin_dir / f"{prefix}_{name}"
    package_dir.mkdir(parents=True)

    (plugin_dir / "plugin.spec.yaml").write_text(spec_text, encoding="utf-8")
    # Guarantee an importable package module so prefix detection has a target.
    (package_dir / "__init__.py").write_text("", encoding="utf-8")

    for rel, content in extra_files.items():
        target = plugin_dir / rel.replace("__PACKAGE__", f"{prefix}_{name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return plugin_dir


def _snapshot(root: Path):
    """Return ``{posix_relpath: bytes}`` for every file under ``root``."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


# Feature: insightconnect-plugin-builder, Property 47: Production source is read-only under import
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(tree=production_plugin_trees())
def test_import_leaves_source_byte_identical(tree):
    """Importing a production plugin never mutates the source tree (Req 25.3).

    For an arbitrary production plugin in a local clone, the byte-for-byte
    snapshot of every source file taken before ``import_plugin`` must equal the
    snapshot taken afterwards: identical set of paths, identical contents. The
    fork is created in a separate ``projects_root``, so any change to the source
    snapshot would be a read-only violation.

    **Validates: Requirements 25.3**
    """
    name, prefix, spec_text, extra_files = tree

    with tempfile.TemporaryDirectory(prefix="icpb-prop-") as workspace:
        workspace_path = Path(workspace)
        clone_dir = workspace_path / "clone"
        clone_dir.mkdir()
        projects_root = workspace_path / "projects"

        plugin_dir = _write_tree(clone_dir, name, prefix, spec_text, extra_files)

        before = _snapshot(plugin_dir)

        source = ProductionSourceConfig(
            id="rapid7_public",
            repo="rapid7/insightconnect-plugins",
            visibility="public",
            local_path=str(clone_dir),
            remote_url="https://github.com/rapid7/insightconnect-plugins.git",
        )
        provider = PluginSourceProvider([source], projects_root)
        provider.import_plugin("rapid7_public", name)

        after = _snapshot(plugin_dir)

    assert after == before
