"""Property-based test for startup configuration validation (task 21.2).

# Feature: insightconnect-plugin-builder, Property 37: Missing required configuration halts startup naming the setting

The unit tests in ``test_config.py`` pin specific missing/invalid-setting
examples; this module covers the universal property with Hypothesis: for any
configuration in which a required setting is missing or an existing setting
carries an out-of-range value, :func:`load_config` halts by raising
:class:`ConfigError` whose ``setting`` names the offending key. As the
complementary direction, any fully-valid configuration loads into an
:class:`AppConfig` without error.

Validates: Requirement 20.6.
"""

import copy

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.api.config import AppConfig, ConfigError, load_config
from icplugin_builder.core.limits import (
    RATE_LIMIT_MAX,
    RATE_LIMIT_MIN,
    TOKEN_BUDGET_MAX,
    TOKEN_BUDGET_MIN,
)


def _out_of_range() -> st.SearchStrategy[int]:
    """Integers strictly outside every accepted numeric-limit range.

    Values below the smallest lower bound or above the largest upper bound are
    invalid for both the token budget (``1..10,000,000``) and the rate limit
    (``1..1,000``), so a single generator serves both settings.
    """
    return st.one_of(
        st.integers(max_value=TOKEN_BUDGET_MIN - 1),
        st.integers(min_value=TOKEN_BUDGET_MAX + 1),
    )


@st.composite
def valid_configs(draw: st.DrawFn) -> dict:
    """Generate a fully-valid configuration mapping.

    Only the ``llm`` section is strictly required; the optional sections are
    included with in-range values so mutation strategies can break exactly one
    setting and attribute the resulting failure to it.
    """
    return {
        "llm": {
            "provider": draw(st.sampled_from(["kiro_cli", "custom_provider"])),
            "kiro_cli_path": draw(st.sampled_from(["/usr/local/bin/kiro", "/bin/kiro"])),
        },
        "cost": {
            "token_budget": draw(st.integers(min_value=TOKEN_BUDGET_MIN, max_value=TOKEN_BUDGET_MAX)),
            "rate_limit_per_min": draw(st.integers(min_value=RATE_LIMIT_MIN, max_value=RATE_LIMIT_MAX)),
        },
        "network": {"port": draw(st.integers(min_value=1, max_value=65_535))},
    }


@st.composite
def defective_configs(draw: st.DrawFn) -> tuple:
    """Generate ``(config, expected_setting)`` where exactly one setting is bad.

    Half the defects omit a required setting (the ``llm`` section or one of its
    keys); the other half leave every required setting present but give an
    existing setting an out-of-range or otherwise invalid value. In both cases
    the expected offending key is the dotted setting name that
    :attr:`ConfigError.setting` must report.
    """
    config = draw(valid_configs())
    defect = draw(
        st.sampled_from(
            [
                "missing-llm",
                "missing-provider",
                "missing-kiro-path",
                "bad-token-budget",
                "bad-rate-limit",
                "bad-port",
                "protection-without-passphrase",
            ]
        )
    )

    if defect == "missing-llm":
        del config["llm"]
        return config, "llm"

    if defect == "missing-provider":
        del config["llm"]["provider"]
        return config, "llm.provider"

    if defect == "missing-kiro-path":
        del config["llm"]["kiro_cli_path"]
        return config, "llm.kiro_cli_path"

    if defect == "bad-token-budget":
        config["cost"]["token_budget"] = draw(_out_of_range())
        return config, "cost.token_budget"

    if defect == "bad-rate-limit":
        config["cost"]["rate_limit_per_min"] = draw(
            st.one_of(st.integers(max_value=RATE_LIMIT_MIN - 1), st.integers(min_value=RATE_LIMIT_MAX + 1))
        )
        return config, "cost.rate_limit_per_min"

    if defect == "bad-port":
        config["network"]["port"] = draw(st.one_of(st.integers(max_value=0), st.integers(min_value=65_536)))
        return config, "network.port"

    # protection-without-passphrase: enable the access guard but omit the hash.
    config["access"] = {"protection_enabled": True}
    return config, "access.passphrase_hash"


# Feature: insightconnect-plugin-builder, Property 37: Missing required configuration halts startup naming the setting
@settings(max_examples=200)
@given(case=defective_configs())
def test_missing_or_invalid_setting_halts_naming_the_setting(case: tuple):
    """Loading halts with a ``ConfigError`` naming the offending setting.

    **Validates: Requirement 20.6**
    """
    config, expected_setting = case

    try:
        load_config(config)
    except ConfigError as exc:
        assert exc.setting == expected_setting
        # The offending key is embedded in the message so startup can surface it.
        assert expected_setting in str(exc)
    else:
        raise AssertionError(f"expected ConfigError naming {expected_setting!r} for {config!r}")


# Feature: insightconnect-plugin-builder, Property 37: Missing required configuration halts startup naming the setting
@settings(max_examples=200)
@given(config=valid_configs())
def test_fully_valid_configuration_loads(config: dict):
    """A configuration with all required settings present loads successfully.

    **Validates: Requirement 20.6**
    """
    loaded = load_config(copy.deepcopy(config))

    assert isinstance(loaded, AppConfig)
    assert loaded.llm.provider == config["llm"]["provider"]
    assert loaded.llm.kiro_cli_path == config["llm"]["kiro_cli_path"]
    assert loaded.cost.token_budget == config["cost"]["token_budget"]
    assert loaded.cost.rate_limit_per_min == config["cost"]["rate_limit_per_min"]
    assert loaded.network.port == config["network"]["port"]
