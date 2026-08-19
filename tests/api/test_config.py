"""Unit tests for the startup config loader and probes (task 21.1; Req 20.2, 20.5, 20.6).

These cover a fully-populated load, default application (token budget, bind
address, paths), and the specific-setting-naming behaviour when a required
setting is missing or invalid (Req 20.6). The universal "missing required
configuration halts startup naming the setting" property is covered separately
by the property test (task 21.2, Property 37).
"""

import subprocess
from types import SimpleNamespace

import pytest

from icplugin_builder.api.config import (
    AppConfig,
    ConfigError,
    DEFAULT_BIND_ADDRESS,
    DEFAULT_PORT,
    DEFAULT_RATE_LIMIT_PER_MIN,
    DEFAULT_TOKEN_BUDGET,
    DEFAULT_CONFIG_TEMPLATE,
    DEFAULT_KIRO_CLI_PATH,
    ProbeResult,
    ensure_config_file,
    load_config,
    probe_docker,
    probe_kiro_cli,
)
from icplugin_builder.orchestrator.repair_loop import DEFAULT_MAX_ROUNDS


def _minimal_config() -> dict:
    """A minimal valid configuration: only the required LLM section present."""
    return {"llm": {"provider": "kiro_cli", "kiro_cli_path": "/usr/local/bin/kiro"}}


class TestLoadValid:
    def test_loads_full_configuration(self):
        data = {
            "llm": {"provider": "kiro_cli", "kiro_cli_path": "/usr/local/bin/kiro"},
            "cost": {"token_budget": 250_000, "rate_limit_per_min": 120},
            "network": {"bind_address": "0.0.0.0", "port": 9000},
            "access": {"protection_enabled": True, "passphrase_hash": "scrypt$abc"},
            "paths": {"config_root": "/tmp/cfg", "projects_root": "/tmp/projects"},
            "updates": {"offline_mode": True, "check_interval_hours": 12, "cache_ttl_hours": 6},
            "tenant": {"default_region_base_url": "https://us.api.insight.rapid7.com"},
            "production_sources": [
                {
                    "id": "rapid7_public",
                    "repo": "rapid7/insightconnect-plugins",
                    "visibility": "public",
                    "local_path": "~/src/insightconnect-plugins",
                },
                {
                    "id": "komand_private",
                    "repo": "komand-plugins",
                    "visibility": "private",
                    "git_credential_id": "komand_git",
                },
            ],
        }

        config = load_config(data)

        assert isinstance(config, AppConfig)
        assert config.llm.provider == "kiro_cli"
        assert config.llm.kiro_cli_path == "/usr/local/bin/kiro"
        assert config.cost.token_budget == 250_000
        assert config.cost.rate_limit_per_min == 120
        assert config.network.bind_address == "0.0.0.0"
        assert config.network.port == 9000
        assert config.access.protection_enabled is True
        assert config.access.passphrase_hash == "scrypt$abc"
        assert config.paths.config_root == "/tmp/cfg"
        assert config.updates.offline_mode is True
        assert config.tenant.default_region_base_url == "https://us.api.insight.rapid7.com"
        assert len(config.production_sources) == 2
        assert config.production_sources[1].git_credential_id == "komand_git"

    def test_applies_defaults_when_sections_omitted(self):
        config = load_config(_minimal_config())

        # Req 4.6: unconfigured budget applies the 100,000-token default.
        assert config.cost.token_budget == DEFAULT_TOKEN_BUDGET
        assert config.cost.rate_limit_per_min == DEFAULT_RATE_LIMIT_PER_MIN
        # Req 17.4: default bind address is loopback.
        assert config.network.bind_address == DEFAULT_BIND_ADDRESS
        assert config.network.port == DEFAULT_PORT
        assert config.access.protection_enabled is False
        assert config.access.passphrase_hash is None
        assert config.production_sources == ()

    def test_none_budget_falls_back_to_default(self):
        data = _minimal_config()
        data["cost"] = {"token_budget": None}
        assert load_config(data).cost.token_budget == DEFAULT_TOKEN_BUDGET

    def test_repair_rounds_are_configurable(self):
        # Req 26.8 speaks of a "configured" maximum; it was a constant.
        data = _minimal_config()
        data["cost"] = {"max_repair_rounds": 5}
        assert load_config(data).cost.max_repair_rounds == 5

    def test_repair_rounds_default_to_the_loop_s_own_default(self):
        # One definition of the default, in the loop that enforces it, so config
        # and loop cannot drift apart.
        assert load_config(_minimal_config()).cost.max_repair_rounds == DEFAULT_MAX_ROUNDS

    def test_loads_from_yaml_string(self):
        text = (
            "llm:\n" "  provider: kiro_cli\n" "  kiro_cli_path: /usr/local/bin/kiro\n" "cost:\n" "  token_budget: 500\n"
        )
        config = load_config(text)
        assert config.cost.token_budget == 500

    def test_loads_from_file_path(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("llm:\n  provider: kiro_cli\n  kiro_cli_path: /bin/kiro\n", encoding="utf-8")
        config = load_config(cfg)
        assert config.llm.kiro_cli_path == "/bin/kiro"


class TestLoadInvalidNamesSetting:
    def test_missing_llm_section_named(self):
        with pytest.raises(ConfigError) as exc:
            load_config({})
        assert exc.value.setting == "llm"
        assert "llm" in str(exc.value)

    def test_missing_provider_named(self):
        with pytest.raises(ConfigError) as exc:
            load_config({"llm": {"kiro_cli_path": "/bin/kiro"}})
        assert exc.value.setting == "llm.provider"

    def test_missing_kiro_cli_path_named(self):
        with pytest.raises(ConfigError) as exc:
            load_config({"llm": {"provider": "kiro_cli"}})
        assert exc.value.setting == "llm.kiro_cli_path"

    def test_blank_provider_rejected(self):
        with pytest.raises(ConfigError) as exc:
            load_config({"llm": {"provider": "   ", "kiro_cli_path": "/bin/kiro"}})
        assert exc.value.setting == "llm.provider"

    def test_out_of_range_token_budget_named(self):
        data = _minimal_config()
        data["cost"] = {"token_budget": 0}
        with pytest.raises(ConfigError) as exc:
            load_config(data)
        assert exc.value.setting == "cost.token_budget"

    def test_out_of_range_rate_limit_named(self):
        data = _minimal_config()
        data["cost"] = {"rate_limit_per_min": 100_000}
        with pytest.raises(ConfigError) as exc:
            load_config(data)
        assert exc.value.setting == "cost.rate_limit_per_min"

    def test_out_of_range_repair_rounds_named(self):
        data = _minimal_config()
        data["cost"] = {"max_repair_rounds": 50}
        with pytest.raises(ConfigError) as exc:
            load_config(data)
        assert exc.value.setting == "cost.max_repair_rounds"

    def test_zero_repair_rounds_is_rejected(self):
        # A loop permitted no fix attempts is just a check, and the repair loop
        # itself refuses to be built that way.
        data = _minimal_config()
        data["cost"] = {"max_repair_rounds": 0}
        with pytest.raises(ConfigError) as exc:
            load_config(data)
        assert exc.value.setting == "cost.max_repair_rounds"

    def test_invalid_port_named(self):
        data = _minimal_config()
        data["network"] = {"port": 70_000}
        with pytest.raises(ConfigError) as exc:
            load_config(data)
        assert exc.value.setting == "network.port"

    def test_protection_enabled_without_passphrase_named(self):
        data = _minimal_config()
        data["access"] = {"protection_enabled": True}
        with pytest.raises(ConfigError) as exc:
            load_config(data)
        assert exc.value.setting == "access.passphrase_hash"

    def test_private_source_without_clone_or_credential_named(self):
        data = _minimal_config()
        data["production_sources"] = [{"id": "komand", "repo": "komand-plugins", "visibility": "private"}]
        with pytest.raises(ConfigError) as exc:
            load_config(data)
        assert exc.value.setting == "production_sources[0].git_credential_id"

    def test_non_mapping_config_rejected(self):
        with pytest.raises(ConfigError) as exc:
            load_config("- just\n- a\n- list\n")
        assert exc.value.setting == "config_file"

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(ConfigError) as exc:
            load_config(tmp_path / "does-not-exist.yaml")
        assert exc.value.setting == "config_file"


class TestProbeKiroCli:
    def test_reports_remediation_when_missing(self, monkeypatch):
        monkeypatch.setattr("icplugin_builder.api.config.shutil.which", lambda _: None)
        result = probe_kiro_cli("kiro")
        assert isinstance(result, ProbeResult)
        assert result.available is False
        assert result.remediation is not None

    def test_available_on_successful_version(self, monkeypatch, tmp_path):
        fake = tmp_path / "kiro"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr("icplugin_builder.api.config.shutil.which", lambda _: str(fake))
        monkeypatch.setattr(
            "icplugin_builder.api.config.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="kiro 1.2.3", stderr=""),
        )
        result = probe_kiro_cli("kiro")
        assert result.available is True
        assert "1.2.3" in result.detail
        assert result.remediation is None

    def test_unavailable_on_nonzero_exit(self, monkeypatch, tmp_path):
        fake = tmp_path / "kiro"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr("icplugin_builder.api.config.shutil.which", lambda _: str(fake))
        monkeypatch.setattr(
            "icplugin_builder.api.config.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        )
        result = probe_kiro_cli("kiro")
        assert result.available is False
        assert result.remediation is not None

    def test_unavailable_on_execution_error(self, monkeypatch, tmp_path):
        fake = tmp_path / "kiro"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr("icplugin_builder.api.config.shutil.which", lambda _: str(fake))

        def _raise(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="kiro", timeout=1.0)

        monkeypatch.setattr("icplugin_builder.api.config.subprocess.run", _raise)
        result = probe_kiro_cli("kiro")
        assert result.available is False


class TestProbeDocker:
    def test_reports_remediation_when_missing(self, monkeypatch):
        monkeypatch.setattr("icplugin_builder.api.config.shutil.which", lambda _: None)
        result = probe_docker()
        assert result.available is False
        assert result.remediation is not None

    def test_available_when_daemon_reachable(self, monkeypatch, tmp_path):
        fake = tmp_path / "docker"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr("icplugin_builder.api.config.shutil.which", lambda _: str(fake))
        monkeypatch.setattr(
            "icplugin_builder.api.config.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="Docker version 25", stderr=""),
        )
        result = probe_docker()
        assert result.available is True
        assert result.remediation is None


class TestEnsureConfigFile:
    """A first start has to be able to succeed.

    ``load_config`` requires an ``llm`` section and ``llm.kiro_cli_path`` has no
    default, so an absent file used to halt startup with "configuration file not
    found" -- accurate, and no help to anyone who had never seen the file's shape.
    """

    def test_a_first_start_writes_a_config_that_loads(self, tmp_path):
        target = tmp_path / "config.yaml"

        assert ensure_config_file(target) is True
        assert target.is_file()

        # The point is not that a file appeared but that startup can proceed.
        config = load_config(target)
        assert config.llm.kiro_cli_path == DEFAULT_KIRO_CLI_PATH
        assert config.network.bind_address == DEFAULT_BIND_ADDRESS
        assert config.network.port == DEFAULT_PORT

    def test_missing_parent_directories_are_created(self, tmp_path):
        """The default path is under ``~/.icplugin-builder``, which may not exist."""
        target = tmp_path / "absent" / "nested" / "config.yaml"
        assert ensure_config_file(target) is True
        assert load_config(target).llm.provider == "kiro_cli"

    def test_an_existing_config_is_never_overwritten(self, tmp_path):
        """The operator's own settings survive every later start (Req 20.7)."""
        target = tmp_path / "config.yaml"
        target.write_text("llm:\n  provider: kiro_cli\n  kiro_cli_path: /opt/custom/kiro\n", encoding="utf-8")

        assert ensure_config_file(target) is False
        assert load_config(target).llm.kiro_cli_path == "/opt/custom/kiro"

    def test_an_empty_file_is_left_for_the_loader_to_report(self, tmp_path):
        """Present but unusable is not the same as absent.

        Overwriting an empty file would discard whatever the operator was part-way
        through writing, and ``load_config`` already names the missing section more
        precisely than a guess here could.
        """
        target = tmp_path / "config.yaml"
        target.write_text("", encoding="utf-8")

        assert ensure_config_file(target) is False
        with pytest.raises(ConfigError) as caught:
            load_config(target)
        assert caught.value.setting == "llm"

    def test_the_commented_defaults_match_the_code(self, tmp_path):
        """The template documents defaults, so it must not drift from them.

        A commented value that no longer matches is worse than no comment: it reads
        as documentation while misinforming.
        """
        for value in (
            str(DEFAULT_TOKEN_BUDGET),
            str(DEFAULT_RATE_LIMIT_PER_MIN),
            DEFAULT_BIND_ADDRESS,
            str(DEFAULT_PORT),
        ):
            assert value in DEFAULT_CONFIG_TEMPLATE, f"the template does not mention {value}"

    def test_a_path_that_cannot_be_written_names_itself(self, tmp_path):
        """Startup cannot continue without a config, so this fails loudly."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("", encoding="utf-8")

        with pytest.raises(ConfigError) as caught:
            ensure_config_file(blocker / "config.yaml")
        assert caught.value.setting == "config_file"
        assert "config.yaml" in str(caught.value)
