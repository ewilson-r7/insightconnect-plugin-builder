"""Property test for structural-change refresh triggering (task 13.6).

The unit tests in ``test_refresh_coordinator.py`` pin specific examples; this
module covers the universal property across generated inputs: an *iteration*
that structurally changes the spec (adds/changes/removes an action, trigger,
task, or connection field) must drive a deterministic ``insight-plugin
refresh`` -- so the derived files equal the refresh output -- while a
metadata-only edit must never trigger a refresh (the derived files are left
untouched, never hand-edited).

The CLI is *mocked* by :class:`FakeCli`, which records every ``refresh`` call
and returns a fixed :class:`ProjectTree`; the real ``insight-plugin`` binary is
never required.
"""

import asyncio
import copy
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.spec_model import PluginSpec, SemVer
from icplugin_builder.integrations.insight_plugin_cli import DERIVED_FILE_NAMES, ProjectTree
from icplugin_builder.integrations.refresh_coordinator import RefreshCoordinator, detect_structural_change
from tests import strategies as strat

_DERIVED = sorted(DERIVED_FILE_NAMES)


class FakeCli:
    """A stand-in for :class:`InsightPluginCli` that records refresh calls.

    It never touches the filesystem or hand-edits derived files; it simply
    returns the :class:`ProjectTree` it was configured with, standing in for the
    deterministic output of ``insight-plugin refresh``.
    """

    def __init__(self, tree: ProjectTree) -> None:
        self.refresh_calls: list = []
        self._tree = tree

    async def refresh(self, project_dir) -> ProjectTree:
        self.refresh_calls.append(project_dir)
        return self._tree


@st.composite
def project_trees(draw: st.DrawFn) -> ProjectTree:
    """Generate a refresh-output :class:`ProjectTree` with derived files.

    Always includes at least one derived file (``schema.py``, ``Dockerfile``,
    ...) so ``derived_files()`` is non-empty, plus the spec file that is not a
    derived artifact, exercising the derived/non-derived split.
    """
    names = draw(st.lists(st.sampled_from(_DERIVED), min_size=1, max_size=len(_DERIVED), unique=True))
    files = {name: draw(st.text(max_size=20)) for name in names}
    files["plugin.spec.yaml"] = draw(st.text(max_size=20))
    return ProjectTree(root=Path("/plugins/generated"), files=files)


def metadata_only_edit(base: PluginSpec) -> PluginSpec:
    """Return a copy of ``base`` with only metadata changed.

    Title/description/version/vendor differ, but the four structural sections
    (connection/actions/triggers/tasks) are left byte-identical, so the edit is
    non-structural by construction.
    """
    spec = copy.deepcopy(base)
    spec.title = (base.title or "") + " edited"
    spec.description = (base.description or "") + " more"
    spec.version = SemVer(base.version.major + 1, base.version.minor, base.version.patch)
    if not base.vendor.endswith("_custom"):
        spec.vendor = base.vendor + "_custom"
    return spec


# Feature: insightconnect-plugin-builder, Property 40: Structural spec change triggers refresh, not hand-editing
@settings(max_examples=200)
@given(data=st.data())
def test_structural_change_triggers_refresh(data):
    """Structural edits refresh (derived files == refresh output); metadata edits do not.

    A base spec is edited two ways: a labeled structural mutation (which always
    touches an action/trigger/task/connection) and a metadata-only edit (which
    never does). ``RefreshCoordinator.refresh_if_structural`` must invoke the
    CLI ``refresh`` exactly once for the structural edit and return the CLI's
    refresh output as the derived files, and must skip refresh entirely for the
    metadata-only edit.

    **Validates: Requirements 22.3**
    """
    base = data.draw(strat.plugin_specs())
    structural_new = data.draw(strat.labeled_mutations(base)).spec
    metadata_new = metadata_only_edit(base)
    tree = data.draw(project_trees())
    project_dir = Path("/plugins") / data.draw(strat.snake_case_names())

    # The two edits sit on opposite sides of the structural boundary.
    assert detect_structural_change(base, structural_new).is_structural is True
    assert detect_structural_change(base, metadata_new).is_structural is False

    # Structural change -> refresh invoked once and the derived files equal the refresh output.
    structural_cli = FakeCli(tree)
    structural_result = asyncio.run(
        RefreshCoordinator(cli=structural_cli).refresh_if_structural(base, structural_new, project_dir)
    )
    assert structural_cli.refresh_calls == [project_dir]
    assert structural_result is tree
    assert structural_result.derived_files() == tree.derived_files()

    # Metadata-only change -> no refresh, so no derived file is (re)generated or hand-edited.
    metadata_cli = FakeCli(tree)
    metadata_result = asyncio.run(
        RefreshCoordinator(cli=metadata_cli).refresh_if_structural(base, metadata_new, project_dir)
    )
    assert metadata_result is None
    assert metadata_cli.refresh_calls == []
