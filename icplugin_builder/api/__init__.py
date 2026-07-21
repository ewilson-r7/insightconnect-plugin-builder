"""API layer.

FastAPI application, HTTP routes, and the WebSocket channel that streams draft
state, the token counter, and visualization updates. Serves the pre-built UI as
static assets and binds to loopback by default.

Also exposes the startup configuration loader and environment probes used to
read local configuration and verify external tooling before the server starts.
"""

from .config import (
    AccessConfig,
    AppConfig,
    ConfigError,
    CostConfig,
    DEFAULT_BIND_ADDRESS,
    DEFAULT_PORT,
    DEFAULT_RATE_LIMIT_PER_MIN,
    DEFAULT_TOKEN_BUDGET,
    KIRO_CLI_PROVIDER,
    LLMConfig,
    NetworkConfig,
    PathsConfig,
    ProbeResult,
    ProductionSourceConfig,
    TenantConfig,
    UpdatesConfig,
    load_config,
    probe_docker,
    probe_kiro_cli,
)
from .app import create_app, create_app_from_config, main

__all__ = [
    "AccessConfig",
    "AppConfig",
    "ConfigError",
    "CostConfig",
    "DEFAULT_BIND_ADDRESS",
    "DEFAULT_PORT",
    "DEFAULT_RATE_LIMIT_PER_MIN",
    "DEFAULT_TOKEN_BUDGET",
    "KIRO_CLI_PROVIDER",
    "LLMConfig",
    "NetworkConfig",
    "PathsConfig",
    "ProbeResult",
    "ProductionSourceConfig",
    "TenantConfig",
    "UpdatesConfig",
    "create_app",
    "create_app_from_config",
    "load_config",
    "main",
    "probe_docker",
    "probe_kiro_cli",
]
