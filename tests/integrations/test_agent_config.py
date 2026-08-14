"""Tests for the generated Kiro agent config.

Two things matter here beyond "it writes a file". First, the config must not
clobber an operator-authored agent that happens to share our name. Second, the
config's whole purpose is to point the agent at the operator's real plugin skills
rather than at a paraphrase of them, so the resource wiring is asserted directly.
"""

import json

from icplugin_builder.integrations.agent_config import (
    AGENT_NAME,
    DEFAULT_TOOLS,
    GENERATED_MARKER,
    AgentConfig,
    agent_config_path,
    default_agent_config,
    missing_resources,
    resolve_kiro_home,
    write_agent_config,
)


class TestRendering:
    def test_tools_are_trusted_because_the_run_is_non_interactive(self):
        # An available-but-untrusted tool would stall a --no-interactive run
        # waiting for a confirmation nobody can give, so allowedTools mirrors
        # tools. Narrowing is done by not listing a tool at all.
        data = default_agent_config().to_dict()
        assert data["tools"] == list(DEFAULT_TOOLS)
        assert data["allowedTools"] == list(DEFAULT_TOOLS)

    def test_broad_tools_are_not_granted(self):
        # Only what building a plugin needs. Each of these would widen what a
        # prompt-injected instruction could reach.
        granted = set(default_agent_config().to_dict()["tools"])
        assert granted.isdisjoint({"aws", "delegate", "knowledge", "report", "introspect"})

    def test_shell_is_granted_because_the_workflow_requires_the_toolchain(self):
        # The plugin workflow is defined in terms of running insight-plugin and
        # the linters; an agent that cannot run them cannot verify its own work.
        assert "shell" in default_agent_config().to_dict()["tools"]

    def test_operator_mcp_servers_are_excluded(self):
        assert default_agent_config().to_dict()["includeMcpJson"] is False

    def test_model_is_unset_so_the_operators_configured_default_wins(self):
        assert default_agent_config().to_dict()["model"] is None

    def test_resources_are_rendered_as_file_uris(self):
        config = AgentConfig(resources=("~/.kiro/skills/plugin-dev.md",))
        assert config.to_dict()["resources"] == ["file://~/.kiro/skills/plugin-dev.md"]

    def test_default_resources_reference_the_operators_skills_not_a_copy(self):
        # The rulebook must be the operator's real files: editing a steering file
        # has to change the agent's behavior, which a vendored copy would not do.
        resources = default_agent_config().resources
        assert any("plugin-dev" in resource for resource in resources)
        assert any("common-mistakes" in resource for resource in resources)
        assert all(resource.startswith("~/.kiro/") for resource in resources)

    def test_json_round_trips(self):
        parsed = json.loads(default_agent_config().to_json())
        assert parsed["name"] == AGENT_NAME
        assert GENERATED_MARKER in parsed["description"]


class TestKiroHomeResolution:
    def test_env_override_is_honored(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "custom"))
        assert resolve_kiro_home() == tmp_path / "custom"

    def test_falls_back_to_home_dot_kiro(self, monkeypatch):
        monkeypatch.delenv("KIRO_HOME", raising=False)
        assert resolve_kiro_home().name == ".kiro"

    def test_config_path_follows_the_harness_convention(self, tmp_path):
        path = agent_config_path(kiro_home=tmp_path, name="thing")
        assert path == tmp_path / "agents" / "thing.json"


class TestWriting:
    def test_writes_a_discoverable_config(self, tmp_path):
        path = write_agent_config(AgentConfig(resources=()), kiro_home=tmp_path, prune_missing_resources=False)
        assert path == tmp_path / "agents" / f"{AGENT_NAME}.json"
        assert json.loads(path.read_text(encoding="utf-8"))["name"] == AGENT_NAME

    def test_is_idempotent(self, tmp_path):
        first = write_agent_config(AgentConfig(resources=()), kiro_home=tmp_path, prune_missing_resources=False)
        before = first.read_text(encoding="utf-8")
        second = write_agent_config(AgentConfig(resources=()), kiro_home=tmp_path, prune_missing_resources=False)
        assert second == first
        assert second.read_text(encoding="utf-8") == before

    def test_overwrites_its_own_previous_config(self, tmp_path):
        # So a fixed prompt or a new skill reference actually takes effect.
        path = agent_config_path(kiro_home=tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"name": AGENT_NAME, "description": f"old {GENERATED_MARKER}", "prompt": "stale"}),
            encoding="utf-8",
        )
        write_agent_config(AgentConfig(resources=()), kiro_home=tmp_path, prune_missing_resources=False)
        assert json.loads(path.read_text(encoding="utf-8"))["prompt"] != "stale"

    def test_never_clobbers_an_operator_authored_config(self, tmp_path):
        path = agent_config_path(kiro_home=tmp_path)
        path.parent.mkdir(parents=True)
        mine = json.dumps({"name": AGENT_NAME, "description": "my own agent", "prompt": "keep me"})
        path.write_text(mine, encoding="utf-8")
        returned = write_agent_config(AgentConfig(resources=()), kiro_home=tmp_path, prune_missing_resources=False)
        assert returned == path
        assert path.read_text(encoding="utf-8") == mine

    def test_unparseable_existing_file_is_treated_as_operator_owned(self, tmp_path):
        # Fail safe: on any doubt about provenance, leave the file alone.
        path = agent_config_path(kiro_home=tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")
        write_agent_config(AgentConfig(resources=()), kiro_home=tmp_path, prune_missing_resources=False)
        assert path.read_text(encoding="utf-8") == "{ not json"

    def test_missing_resources_are_pruned_so_the_agent_still_runs(self, tmp_path):
        real = tmp_path / "present.md"
        real.write_text("# present\n", encoding="utf-8")
        config = AgentConfig(resources=(str(real), str(tmp_path / "absent.md")))
        path = write_agent_config(config, kiro_home=tmp_path)
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["resources"] == [f"file://{real}"]


class TestMissingResources:
    def test_reports_absent_paths_for_warning(self, tmp_path):
        real = tmp_path / "present.md"
        real.write_text("x", encoding="utf-8")
        absent = str(tmp_path / "absent.md")
        config = AgentConfig(resources=(str(real), absent))
        assert missing_resources(config) == (absent,)

    def test_empty_when_all_present(self, tmp_path):
        real = tmp_path / "present.md"
        real.write_text("x", encoding="utf-8")
        assert missing_resources(AgentConfig(resources=(str(real),))) == ()
