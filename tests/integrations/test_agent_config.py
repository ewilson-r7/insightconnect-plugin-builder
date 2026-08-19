"""Tests for the generated Kiro agent config.

Two things matter here beyond "it writes a file". First, the config must not
clobber an operator-authored agent that happens to share our name. Second, the
config's whole purpose is to point the agent at the operator's real plugin skills
rather than at a paraphrase of them, so the resource wiring is asserted directly.
"""

import json

from pathlib import Path

from icplugin_builder.integrations.agent_config import (
    AGENT_NAME,
    BUNDLED_RULEBOOK_DIR,
    RULEBOOK_FILES,
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

    def test_default_resources_prefer_the_operators_skills_over_the_bundled_copy(self, tmp_path, monkeypatch):
        """The operator's own rulebook wins wherever they have one.

        This asserted ``resource.startswith("~/.kiro/")`` for every entry, when
        ``~/.kiro`` was the only place a rulebook could come from and a user without
        one simply got a degraded agent. A copy now ships with the package, because
        those files are in no public repository and a new user had no way to obtain
        them. The claim it was really making -- that editing a steering file changes
        the agent's behaviour, which a copy the operator cannot see would not --
        still holds, and is what is asserted here: the operator's file wins.
        """
        monkeypatch.setenv("KIRO_HOME", str(tmp_path))
        (tmp_path / "steering").mkdir(parents=True)
        mine = tmp_path / "steering" / "common-mistakes.md"
        mine.write_text("# my own rules\n", encoding="utf-8")

        resources = default_agent_config().resources

        assert str(mine) in resources, "the operator's own steering file was not preferred"
        assert any("plugin-dev" in resource for resource in resources)
        assert all(Path(resource).is_absolute() for resource in resources)

    def test_a_user_with_no_rulebook_gets_the_bundled_one(self, tmp_path, monkeypatch):
        """The whole point of bundling: no setup, and no degraded agent.

        Before this, the eleven files had to be obtained from a repository that does
        not publish them, so the realistic new-user outcome was an agent running with
        reduced guidance and no way to fix it.
        """
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "empty-home"))

        resources = default_agent_config().resources

        assert len(resources) == len(RULEBOOK_FILES)
        assert all(str(BUNDLED_RULEBOOK_DIR) in resource for resource in resources)
        assert missing_resources() == (), "a bundled rulebook must leave nothing missing"

    def test_every_named_rulebook_file_is_actually_bundled(self):
        """The package must carry all eleven, or the fallback is partial in silence."""
        absent = [name for name in RULEBOOK_FILES if not (BUNDLED_RULEBOOK_DIR / name).is_file()]
        assert absent == [], f"named in RULEBOOK_FILES but not bundled: {absent}"

    def test_a_file_in_neither_place_is_dropped_rather_than_breaking_the_run(self, tmp_path, monkeypatch):
        """An incomplete rulebook degrades the agent; it does not stop it."""
        monkeypatch.setenv("KIRO_HOME", str(tmp_path))
        monkeypatch.setattr("icplugin_builder.integrations.agent_config.BUNDLED_RULEBOOK_DIR", tmp_path / "no-bundle")

        resources = default_agent_config().resources

        assert resources == ()
        assert len(missing_resources()) == 0, "nothing is claimed missing when nothing is referenced"

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
