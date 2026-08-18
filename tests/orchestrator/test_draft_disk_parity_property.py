"""
# Feature: export-gate-and-preview-fidelity, Property 64: The draft and the tree do not diverge

Property 64 states that after any turn which changes the draft's spec in a session
with a project folder, the draft's spec equals the spec on disk; and that after any
implementation turn the draft is a *view of the tree*, so no later read can disagree
with it and no export-time write can overwrite the agent's work with an older value.

Two directions, both generated:

* **session -> disk.** Draw a spec, apply it as a turn's metadata edit, and assert
  the tree carries it. This is the direction that used to fail silently: the save
  was gated on *structural* change, so a description-only edit reached no file, and
  a later re-read of the tree would have discarded it.
* **disk -> session.** Draw a spec, write it to the tree behind the session's back
  (the delegated agent's write), run an implementation turn, and assert the draft
  now holds the tree's spec rather than the one the session authored.

No Docker and no toolchain: the agent is a stub that writes the drawn spec, which is
exactly what a delegated run does to the file this property is about.

**Validates: Requirements 2.11**
"""

import asyncio
from pathlib import Path

from hypothesis import HealthCheck, given, settings

from icplugin_builder.core.generation import ArtifactKind, GenerationRequest
from icplugin_builder.core.yaml_codec import dump_plugin_spec, load_plugin_spec
from icplugin_builder.integrations.plugin_agent import AgentRunResult
from icplugin_builder.orchestrator import Orchestrator, TurnPlan, UpdateMetadata
from icplugin_builder.persistence.project_folder import (
    ENTRY_MODE_ITERATE_CUSTOM,
    ProjectFolder,
)
from tests.strategies import plugin_specs

_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


class _SpecWritingAgent:
    """A delegated agent that writes a spec to the tree, as the real one does.

    Only the file matters to this property: what the agent puts in the *source*
    files is the subject of the generation requirements, not of draft/disk parity.
    """

    def __init__(self, spec_text: str) -> None:
        self._spec_text = spec_text
        self.calls = 0

    async def implement(self, project_dir, instruction, *, session_id, user_id):
        self.calls += 1
        (Path(project_dir) / "plugin.spec.yaml").write_text(self._spec_text, encoding="utf-8")
        return AgentRunResult(succeeded=True, summary="wrote the spec", transcript="")


def _open_session(projects_root, authored, *, agent=None):
    folder = ProjectFolder.create(projects_root, authored.name, authored)
    folder.save(authored)
    orch = Orchestrator(projects_root=projects_root, plugin_agent=agent)
    orch.start_session(
        ENTRY_MODE_ITERATE_CUSTOM,
        session_id="s",
        user_id="u",
        plugin_name=authored.name,
    )
    return orch, folder


@given(authored=plugin_specs(), edited=plugin_specs())
@_SETTINGS
def test_a_turn_that_changes_the_spec_writes_it_to_the_tree(tmp_path_factory, authored, edited):
    """Session -> disk, for a **non-structural** edit as well as a structural one."""
    root = tmp_path_factory.mktemp("parity")
    orch, folder = _open_session(root, authored)

    result = asyncio.run(
        orch.apply_turn(
            "s",
            TurnPlan(operations=[UpdateMetadata(title=edited.title, description=edited.description)]),
        )
    )
    assert result.status.name == "APPLIED"

    on_disk = load_plugin_spec(folder.spec_path.read_text(encoding="utf-8"))
    session_spec = orch.session("s").spec
    assert on_disk.title == session_spec.title
    assert on_disk.description == session_spec.description


@given(authored=plugin_specs(), by_agent=plugin_specs())
@_SETTINGS
def test_after_an_implementation_turn_the_draft_is_the_tree(tmp_path_factory, authored, by_agent):
    """Disk -> session: the agent's spec wins over the one the session authored."""
    root = tmp_path_factory.mktemp("parity")
    by_agent.name = authored.name  # the agent does not rename the folder it works in
    agent = _SpecWritingAgent(dump_plugin_spec(by_agent))
    orch, folder = _open_session(root, authored, agent=agent)

    result = asyncio.run(
        orch.apply_turn(
            "s",
            TurnPlan(
                reasoning=[GenerationRequest(kind=ArtifactKind.ACTION_LOGIC, parameters={"action": "an_action"})],
            ),
        )
    )
    assert result.status.name == "APPLIED"
    assert agent.calls == 1, "the reconstruction did not delegate, so this is not an implementation turn"

    on_disk = load_plugin_spec(folder.spec_path.read_text(encoding="utf-8"))
    session_spec = orch.session("s").spec
    assert session_spec.title == on_disk.title
    assert session_spec.description == on_disk.description
    assert sorted(session_spec.actions) == sorted(on_disk.actions)
    # And the turn reports the tree's spec, not the draft the session opened with.
    assert result.spec.title == on_disk.title
