"""Tests for the default-deny environment built for delegated CLI subprocesses.

The point of :mod:`icplugin_builder.integrations.env_guard` is that a delegated
LLM CLI cannot read secrets out of this process's environment. These tests assert
the *deny* half specifically -- it is easy to write an allowlist that quietly
admits everything, and the failure mode is silent credential exposure rather than
a broken test.
"""

from icplugin_builder.integrations.env_guard import (
    BASE_NAMES,
    KIRO_ALLOW_PREFIXES,
    guarded_env,
    redacted_names,
)


class TestDefaultDeny:
    def test_unlisted_variables_are_dropped(self):
        source = {
            "PATH": "/usr/bin",
            "GITHUB_TOKEN": "ghp_secret",
            "ANTHROPIC_API_KEY": "sk-secret",
            "DATABASE_URL": "postgres://user:pw@host/db",
            "MY_APP_SECRET": "hunter2",
        }
        env = guarded_env(KIRO_ALLOW_PREFIXES, source=source)
        assert env == {"PATH": "/usr/bin"}

    def test_no_secret_value_survives_anywhere_in_the_result(self):
        # Guards against an allowlist bug that admits a name whose value is a
        # secret: assert on values, not just on names.
        secrets = {
            "GITHUB_TOKEN": "ghp_secret",
            "ANTHROPIC_API_KEY": "sk-secret",
            "AZURE_CLIENT_SECRET": "azure-secret",
            "ICPLUGIN_TENANT_API_KEY": "tenant-secret",
        }
        source = {"PATH": "/usr/bin", "HOME": "/home/e", **secrets}
        env = guarded_env(KIRO_ALLOW_PREFIXES, source=source)
        for value in secrets.values():
            assert value not in env.values()

    def test_empty_allowlist_admits_only_base_names(self):
        source = {"PATH": "/usr/bin", "KIRO_TOKEN": "k", "AWS_SECRET_ACCESS_KEY": "s"}
        env = guarded_env(source=source)
        assert env == {"PATH": "/usr/bin"}


class TestAllowlist:
    def test_kiro_auth_prefixes_are_admitted(self):
        # Kiro authenticates through AWS/Amazon identity, so these must reach it
        # or the delegated run cannot log in at all.
        source = {
            "KIRO_HOME": "/home/e/.kiro",
            "AWS_REGION": "us-east-1",
            "AMAZON_THING": "x",
            "CODEWHISPERER_FOO": "y",
            "UNRELATED": "z",
        }
        env = guarded_env(KIRO_ALLOW_PREFIXES, source=source)
        assert set(env) == {"KIRO_HOME", "AWS_REGION", "AMAZON_THING", "CODEWHISPERER_FOO"}

    def test_base_names_and_benign_prefixes_pass(self):
        source = {
            "TMPDIR": "/tmp",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "C",
            "XDG_CONFIG_HOME": "/home/e/.config",
            "SSL_CERT_FILE": "/etc/ssl/cert.pem",
            "https_proxy": "http://proxy:8080",
            "SOMETHING_ELSE": "no",
        }
        env = guarded_env(source=source)
        assert "SOMETHING_ELSE" not in env
        assert set(env) == {"TMPDIR", "LANG", "LC_ALL", "XDG_CONFIG_HOME", "SSL_CERT_FILE", "https_proxy"}

    def test_every_declared_base_name_is_actually_admitted(self):
        source = {name: f"value-of-{name}" for name in BASE_NAMES}
        env = guarded_env(source=source)
        assert set(env) == set(BASE_NAMES)


class TestIsolationAndExtras:
    def test_source_mapping_is_not_mutated_and_result_is_a_new_object(self):
        source = {"PATH": "/usr/bin", "SECRET": "s"}
        snapshot = dict(source)
        env = guarded_env(KIRO_ALLOW_PREFIXES, source=source)
        assert source == snapshot
        assert env is not source

    def test_extra_is_applied_after_filtering(self):
        # `extra` is the single deliberate bypass, so that a value this tool
        # chooses itself is set at the call site rather than hidden in a list.
        env = guarded_env(source={"PATH": "/usr/bin"}, extra={"KIRO_HOME": "/custom/kiro"})
        assert env["KIRO_HOME"] == "/custom/kiro"
        assert env["PATH"] == "/usr/bin"


class TestRedactedNames:
    def test_reports_dropped_names_sorted(self):
        source = {"PATH": "/usr/bin", "ZED": "z", "ALPHA": "a"}
        assert redacted_names(KIRO_ALLOW_PREFIXES, source=source) == ("ALPHA", "ZED")

    def test_returns_names_only_so_logging_it_cannot_leak_a_value(self):
        source = {"PATH": "/usr/bin", "GITHUB_TOKEN": "ghp_supersecret"}
        reported = redacted_names(KIRO_ALLOW_PREFIXES, source=source)
        assert reported == ("GITHUB_TOKEN",)
        assert "ghp_supersecret" not in " ".join(reported)
