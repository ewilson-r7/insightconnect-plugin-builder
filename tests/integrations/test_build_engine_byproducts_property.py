"""
# Feature: export-gate-and-preview-fidelity, Property 69: Packaging excludes byproducts and nothing else new

Property 69 states that the packaged file set excludes ``.builder/``, every
reference document, and every build or test byproduct -- including ``.coverage`` at
any depth -- and contains **every other file present in the tree**.

Both halves are load-bearing, and the second is the one that catches a careless
fix. "Exclude the byproducts" is satisfiable by excluding too much: a predicate that
dropped every dotfile, or everything under any directory whose name it did not
recognise, would pass a test that only checked byproducts were gone and would ship
an artifact missing files the plugin needs to run. So the property is stated as an
*equality* over the packaged set, not as an absence.

The defect this closes: ``list_plugin_files`` filtered on a set of **directory
names**, which cannot express "a file called ``.coverage`` wherever it sits". An
operator who had run the plugin's tests once shipped their coverage database inside
the artifact -- twice over, since the suite writes one at the root and one under
``unit_test/``.

Byproducts are planted at generated placements rather than a fixed list, because the
hole was about *position*: the directory-name filter caught ``__pycache__/x.pyc`` and
missed ``icon_x/util/api.pyc``, and only varying the depth shows that.

**Validates: Requirements 2.15** -- also preserves 3.2
"""

from pathlib import Path, PurePosixPath
from typing import Dict, Tuple

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from icplugin_builder.core.plugin_files import COVERAGE_DATA_FILE, UNIT_TEST_DIR, is_packaging_excluded
from icplugin_builder.integrations.build_engine import BUILDER_METADATA_DIR, list_plugin_files, preview_export_files

PACKAGE = "icon_property"

#: The files a plugin needs, which must survive every exclusion. Deliberately a mix
#: of hand-written and generated: a generated ``schema.py`` is dropped from *lint*
#: and must still be packaged, because the plugin cannot run without it.
PLUGIN_FILES: Tuple[str, ...] = (
    "plugin.spec.yaml",
    "requirements.txt",
    "setup.py",
    "Dockerfile",
    "Makefile",
    "help.md",
    ".CHECKSUM",
    f"bin/icon_{PACKAGE}",
    f"{PACKAGE}/__init__.py",
    f"{PACKAGE}/util/api.py",
    f"{PACKAGE}/util/constants.py",
    f"{PACKAGE}/connection/connection.py",
    f"{PACKAGE}/actions/get_thing/action.py",
    f"{PACKAGE}/actions/get_thing/schema.py",
    f"{UNIT_TEST_DIR}/test_api.py",
    f"{UNIT_TEST_DIR}/responses/thing.json",
)

#: Directory prefixes a byproduct can be planted under, including none at all.
_DEPTHS: Tuple[str, ...] = (
    "",
    f"{UNIT_TEST_DIR}/",
    f"{PACKAGE}/",
    f"{PACKAGE}/util/",
    f"{PACKAGE}/actions/get_thing/",
)

_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@st.composite
def byproducts(draw: st.DrawFn) -> Tuple[str, ...]:
    """Draw a set of byproduct paths, positioned anywhere in the tree.

    Every kind change 8 names is represented, and each is placed at a drawn depth
    rather than a fixed one -- the original hole was that a directory-name filter
    caught the byproduct at one position and missed it at another.
    """
    planted = set()
    depth = lambda: draw(st.sampled_from(_DEPTHS))  # noqa: E731 - terse on purpose

    if draw(st.booleans()):
        planted.add(f"{depth()}{COVERAGE_DATA_FILE}")
    if draw(st.booleans()):
        suffix = draw(st.text(alphabet="abcdef0123456789.", min_size=1, max_size=12))
        planted.add(f"{depth()}{COVERAGE_DATA_FILE}.{suffix}")
    if draw(st.booleans()):
        planted.add(f"{depth()}orphan{draw(st.sampled_from(['.pyc', '.pyo']))}")
    if draw(st.booleans()):
        planted.add(f"{depth()}__pycache__/mod.cpython-313.pyc")
    if draw(st.booleans()):
        planted.add(f"build/lib/{PACKAGE}/util/api.py")
    if draw(st.booleans()):
        planted.add(f"{PACKAGE}_rapid7_plugin.egg-info/PKG-INFO")
    if draw(st.booleans()):
        planted.add(f"vendor-plugin-1.0.0{draw(st.sampled_from(['.tar.gz', '.tgz']))}")
    if draw(st.booleans()):
        planted.add(f"{depth()}.pytest_cache/CACHEDIR.TAG")
    if draw(st.booleans()):
        planted.add(".mypy_cache/missing_stubs.txt")
    if draw(st.booleans()):
        planted.add(".git/config")
    # Always planted: 3.2 requires the tool's own metadata and every reference
    # document stay out, whatever else is present.
    planted.add(f"{BUILDER_METADATA_DIR}/provenance.json")
    planted.add(f"{BUILDER_METADATA_DIR}/reference/vendor_swagger.yaml")
    return tuple(sorted(planted))


def _tree(root: Path, planted: Tuple[str, ...]) -> Dict[str, Tuple[str, ...]]:
    """Materialize a plugin tree with ``planted`` byproducts alongside the real files."""
    for relative in PLUGIN_FILES + planted:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"content of {relative}\n", encoding="utf-8")
    return {"plugin": PLUGIN_FILES, "planted": planted}


@given(planted=byproducts())
@_SETTINGS
def test_the_packaged_set_is_exactly_the_plugin_files(tmp_path_factory, planted):
    """Both halves at once, as an equality: no byproduct in, nothing needed out."""
    root = tmp_path_factory.mktemp("pkg")
    _tree(root, planted)

    packaged = set(list_plugin_files(root))

    surplus = sorted(packaged - set(PLUGIN_FILES))
    missing = sorted(set(PLUGIN_FILES) - packaged)
    assert (surplus, missing) == ([], []), (
        f"the packaged set is not the plugin's files: it adds {surplus} and is missing {missing}. "
        f"Planted byproducts were {list(planted)}"
    )


@given(planted=byproducts())
@_SETTINGS
def test_no_coverage_data_is_packaged_wherever_it_sits(tmp_path_factory, planted):
    """2.15's own words -- ``.coverage`` at *any* depth, which is what a directory set missed."""
    root = tmp_path_factory.mktemp("pkg")
    _tree(root, planted)

    coverage = [
        member
        for member in list_plugin_files(root)
        if PurePosixPath(member).name == COVERAGE_DATA_FILE
        or PurePosixPath(member).name.startswith(f"{COVERAGE_DATA_FILE}.")
    ]
    assert not coverage, f"the packaged set carries coverage data at {coverage}"


@given(planted=byproducts())
@_SETTINGS
def test_the_tools_own_metadata_and_reference_material_never_ship(tmp_path_factory, planted):
    """3.2's preservation constraint, restated over generated trees.

    Reference documents are vendor API specifications supplied by the operator. They
    are what the delegated agent reads and they have no business inside a plugin
    artifact -- this held before change 8 and must keep holding.
    """
    root = tmp_path_factory.mktemp("pkg")
    _tree(root, planted)

    leaked = [member for member in list_plugin_files(root) if member.startswith(f"{BUILDER_METADATA_DIR}/")]
    assert not leaked, f"the packaged set carries tool-only metadata at {leaked}"


@given(planted=byproducts())
@_SETTINGS
def test_the_preview_equals_what_would_be_packaged(tmp_path_factory, planted):
    """Design Property 30 -- one source of truth, so the preview cannot mislead.

    ``list_plugin_files`` is what both the packager and the export preview consume.
    Asserted here because change 8 alters that function, and a preview that disagreed
    with the archive would put the operator's confirmation on a different set of
    files than the one that shipped.
    """
    root = tmp_path_factory.mktemp("pkg")
    _tree(root, planted)

    assert tuple(preview_export_files(root).files) == tuple(list_plugin_files(root))


@given(planted=byproducts())
@_SETTINGS
def test_the_predicate_and_the_packager_agree(tmp_path_factory, planted):
    """The filter is the predicate, so nothing packaged may satisfy it."""
    root = tmp_path_factory.mktemp("pkg")
    _tree(root, planted)

    packaged = list_plugin_files(root)
    contradictions = [member for member in packaged if is_packaging_excluded(member)]
    assert not contradictions, f"packaged despite being excluded by the predicate: {contradictions}"

    for relative in planted:
        assert is_packaging_excluded(relative), f"a planted byproduct is not excluded by the predicate: {relative}"
