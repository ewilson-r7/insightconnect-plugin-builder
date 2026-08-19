"""Startup configuration loader and environment probes (task 21.1).

This module reads the local configuration file described in the design's
"Config File" section and turns it into a validated, typed :class:`AppConfig`.
It is the single source of truth for the tool's startup settings and enforces
Requirement 20:

* **Read local configuration** (Req 20.2) -- the loader parses the LLM provider,
  token budget, rate limit, network bind address, access-protection settings,
  filesystem paths, update settings, tenant defaults, and read-only production
  sources.
* **Halt on missing/invalid required settings** (Req 20.6, design Property 37)
  -- :func:`load_config` raises :class:`ConfigError` whose ``setting`` names the
  exact missing or invalid configuration key, so startup can halt with a message
  identifying the offending setting.
* **Probe external tooling** (Req 20.5) -- :func:`probe_kiro_cli` reports whether
  the Kiro CLI (the primary LLM provider) is available and, when it is not,
  identifies the remediation step. :func:`probe_docker` reports Docker
  availability; Docker is optional at startup and required only for building.

Numeric limits (token budget and rate limit) are validated by the shared
:mod:`icplugin_builder.core.limits` module so the accepted ranges match the
Cost_Controller exactly (Req 4.1, 4.4). The token budget defaults to 100,000
when unset (Req 4.6).

The loader accepts a mapping, a YAML string, or a path to a YAML file, so it can
be driven directly from tests without touching the filesystem.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from ruamel.yaml import YAML

from ..core.limits import (
    LimitOutOfRangeError,
    validate_rate_limit,
    validate_repair_rounds,
    validate_token_budget,
)
from ..orchestrator.repair_loop import DEFAULT_MAX_ROUNDS

__all__ = [
    "ConfigError",
    "LLMConfig",
    "CostConfig",
    "NetworkConfig",
    "AccessConfig",
    "PathsConfig",
    "UpdatesConfig",
    "TenantConfig",
    "ProductionSourceConfig",
    "AppConfig",
    "ProbeResult",
    "load_config",
    "ensure_config_file",
    "DEFAULT_CONFIG_TEMPLATE",
    "DEFAULT_KIRO_CLI_PATH",
    "probe_kiro_cli",
    "probe_docker",
]

#: Default per-session token budget applied when ``cost.token_budget`` is unset
#: (Req 4.6; design Config File comment).
DEFAULT_TOKEN_BUDGET = 100_000

#: Default per-user request rate when ``cost.rate_limit_per_min`` is unset.
DEFAULT_RATE_LIMIT_PER_MIN = 60

#: Default network bind address -- loopback so the tool is local-only by default
#: (Req 17.4; design Config File).
DEFAULT_BIND_ADDRESS = "127.0.0.1"

#: Default TCP port for the local web UI/API.
DEFAULT_PORT = 8787

#: Default filesystem locations (design Config File ``paths`` block).
DEFAULT_CONFIG_ROOT = "~/.icplugin-builder"
DEFAULT_PROJECTS_ROOT = "~/.icplugin-builder/projects"

#: Default update-check cadence and cache lifetime, in hours (Req 23.4).
DEFAULT_CHECK_INTERVAL_HOURS = 24
DEFAULT_CACHE_TTL_HOURS = 24

#: Provider identifier for the Kiro CLI, the primary LLM provider (Req 20.3).
KIRO_CLI_PROVIDER = "kiro_cli"

#: Remediation shown when the Kiro CLI cannot be used (Req 20.5).
_KIRO_REMEDIATION = (
    "Install the Kiro CLI and ensure it is on PATH or set 'llm.kiro_cli_path' to "
    "its absolute location, then authenticate it before starting the tool."
)

#: Remediation shown when the Docker engine is unavailable. Docker is optional at
#: startup and only required to build a plugin.
_DOCKER_REMEDIATION = "Install Docker and ensure the Docker daemon is running to build plugins."

#: The Kiro CLI executable written into a generated config. A bare name rather than
#: an absolute path, so it resolves on the operator's ``PATH`` wherever the CLI is
#: installed; :func:`probe_kiro_cli` reports it clearly when it is not found, and
#: ``llm.kiro_cli_path`` takes an absolute path for an install that is not on ``PATH``.
DEFAULT_KIRO_CLI_PATH = "kiro-cli"

#: The configuration a first start writes when no file exists.
#:
#: Only the settings without a default are written -- the ``llm`` section, which is
#: the one thing :func:`load_config` requires. Everything else is commented out at
#: the value the code already defaults to, so the file documents what can be changed
#: without silently pinning a default that later moves. Startup previously failed
#: with ``config_file: configuration file not found``, which told a first-time
#: operator what was missing but not what to write.
DEFAULT_CONFIG_TEMPLATE = f"""\
# InsightConnect Plugin Builder configuration.
#
# Written automatically on first start. Edit freely -- this file is never
# overwritten once it exists.

llm:
  provider: {KIRO_CLI_PROVIDER}
  # The Kiro CLI executable. A bare name is resolved on PATH; use an absolute
  # path if yours is installed somewhere else.
  kiro_cli_path: {DEFAULT_KIRO_CLI_PATH}

# cost:
#   token_budget: {DEFAULT_TOKEN_BUDGET}          # per-session token budget
#   rate_limit_per_min: {DEFAULT_RATE_LIMIT_PER_MIN}

# network:
#   bind_address: {DEFAULT_BIND_ADDRESS}     # loopback: the tool is local-only by default
#   port: {DEFAULT_PORT}

# paths:
#   config_root: {DEFAULT_CONFIG_ROOT}
#   projects_root: {DEFAULT_PROJECTS_ROOT}
"""


def ensure_config_file(path: Union[str, os.PathLike[str]]) -> bool:
    """Write :data:`DEFAULT_CONFIG_TEMPLATE` to ``path`` if nothing is there yet.

    A first start had no way to succeed: ``load_config`` requires an ``llm``
    section and ``llm.kiro_cli_path`` has no default, so an absent file halted
    startup and the operator had to discover the required shape from the source or
    the specification. Writing a working file is the smaller surprise.

    An existing file is left exactly as it is, including one that is empty or
    invalid -- the same rule the generated agent config follows (Req 20.7's
    "a config you wrote yourself is never overwritten"). Reporting the invalid
    setting is ``load_config``'s job and it does it better than a guess here would.

    Args:
        path: where the configuration file belongs.

    Returns:
        ``True`` if a file was written, ``False`` if one was already present.

    Raises:
        ConfigError: if the file cannot be written, naming the path -- startup
            cannot continue without a configuration and a silent failure here
            would resurface as the "not found" error this exists to prevent.
    """
    resolved = Path(path).expanduser()
    if resolved.exists():
        return False
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    except OSError as error:
        raise ConfigError("config_file", f"could not write a default configuration to {resolved}: {error}")
    return True


class ConfigError(ValueError):
    """Raised when a required configuration setting is missing or invalid.

    The offending setting's dotted key path (for example ``"llm.provider"`` or
    ``"cost.token_budget"``) is available as :attr:`setting` and is embedded in
    the message so startup can halt while naming exactly what is wrong
    (Req 20.6, design Property 37).
    """

    def __init__(self, setting: str, message: str) -> None:
        self.setting = setting
        super().__init__(f"{setting}: {message}")


@dataclass(frozen=True)
class LLMConfig:
    """LLM provider settings (Req 20.2, 20.3).

    Attributes:
        provider: The configured LLM provider identifier.
        kiro_cli_path: Path to the Kiro CLI executable used to reach the
            provider (design Config File ``llm.kiro_cli_path``).
    """

    provider: str
    kiro_cli_path: str


@dataclass(frozen=True)
class CostConfig:
    """Cost-control limits validated against the shared numeric ranges.

    Attributes:
        token_budget: Per-session token budget in ``1..10,000,000`` (Req 4.1),
            defaulting to :data:`DEFAULT_TOKEN_BUDGET` when unset (Req 4.6).
        rate_limit_per_min: Per-user request rate in ``1..1,000`` (Req 4.4).
        max_repair_rounds: How many fix attempts the ``Repair_Loop`` may make
            before it stops and reports reaching the limit, in ``1..10``
            (Req 26.8). Grouped with the cost limits because each round is a paid
            agent run, so raising it raises what a single turn can spend.
    """

    token_budget: int = DEFAULT_TOKEN_BUDGET
    rate_limit_per_min: int = DEFAULT_RATE_LIMIT_PER_MIN
    max_repair_rounds: int = DEFAULT_MAX_ROUNDS


@dataclass(frozen=True)
class NetworkConfig:
    """Network binding settings.

    Attributes:
        bind_address: Interface the local server binds to, defaulting to
            loopback (Req 17.4).
        port: TCP port for the local web UI/API.
    """

    bind_address: str = DEFAULT_BIND_ADDRESS
    port: int = DEFAULT_PORT


@dataclass(frozen=True)
class AccessConfig:
    """Optional local access-guard settings (Req 17).

    Attributes:
        protection_enabled: Whether the passphrase guard is active.
        passphrase_hash: The stored argon2/scrypt passphrase hash; required and
            non-empty when :attr:`protection_enabled` is ``True``.
    """

    protection_enabled: bool = False
    passphrase_hash: Optional[str] = None


@dataclass(frozen=True)
class PathsConfig:
    """Filesystem locations for persisted state (design Config File ``paths``).

    Attributes:
        config_root: Root directory for configuration and tool state.
        projects_root: Root directory holding per-plugin project folders.
    """

    config_root: str = DEFAULT_CONFIG_ROOT
    projects_root: str = DEFAULT_PROJECTS_ROOT


@dataclass(frozen=True)
class UpdatesConfig:
    """Managed-tooling update settings (Req 23.4, 23.5).

    Attributes:
        offline_mode: When ``True``, upstream update checks are skipped.
        check_interval_hours: Hours between startup/interval update checks.
        cache_ttl_hours: How long an upstream check result stays cached.
    """

    offline_mode: bool = False
    check_interval_hours: int = DEFAULT_CHECK_INTERVAL_HOURS
    cache_ttl_hours: int = DEFAULT_CACHE_TTL_HOURS


@dataclass(frozen=True)
class TenantConfig:
    """InsightConnect tenant defaults.

    Attributes:
        default_region_base_url: Optional default region base URL used to
            pre-fill tenant export; ``None`` when unset.
    """

    default_region_base_url: Optional[str] = None


@dataclass(frozen=True)
class ProductionSourceConfig:
    """A read-only production plugin source (Req 24.4, 25.1, 25.2).

    Attributes:
        id: Stable identifier for the source.
        repo: Repository name (for example ``rapid7/insightconnect-plugins``).
        visibility: ``"public"`` or ``"private"``.
        local_path: Preferred local clone path; ``None`` falls back to the
            remote (Req 25.2).
        remote_url: Remote git URL used when no local clone is available.
        git_credential_id: Credential_Store entry id for a private repo without a
            local clone; required in that case (Req 25.2, 25.9).
    """

    id: str
    repo: str
    visibility: str = "public"
    local_path: Optional[str] = None
    remote_url: Optional[str] = None
    git_credential_id: Optional[str] = None


@dataclass(frozen=True)
class AppConfig:
    """The fully-validated startup configuration (Req 20.2).

    Every field is present and valid once :func:`load_config` returns; invalid
    input never yields a partially-populated :class:`AppConfig` because loading
    raises :class:`ConfigError` before constructing it (Req 20.6).
    """

    llm: LLMConfig
    cost: CostConfig
    network: NetworkConfig
    access: AccessConfig
    paths: PathsConfig
    updates: UpdatesConfig
    tenant: TenantConfig
    production_sources: tuple[ProductionSourceConfig, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of probing an external dependency (Req 20.5).

    Attributes:
        name: Human-readable name of the probed dependency.
        available: ``True`` when the dependency is usable.
        detail: A short description of what was found (path, version, or error).
        remediation: The remediation step to make the dependency usable;
            ``None`` when :attr:`available` is ``True``.
    """

    name: str
    available: bool
    detail: str = ""
    remediation: Optional[str] = None


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------

_SAFE_YAML = YAML(typ="safe")


def _read_mapping(source: Union[Mapping[str, Any], str, os.PathLike[str]]) -> Mapping[str, Any]:
    """Coerce ``source`` into a mapping of configuration settings.

    ``source`` may be a mapping (used as-is), a filesystem path to a YAML file,
    or a YAML document string.

    Raises:
        ConfigError: if the resolved document is not a YAML mapping, or the
            referenced file does not exist.
    """
    if isinstance(source, Mapping):
        return source

    if isinstance(source, os.PathLike):
        path = Path(source)
        if not path.is_file():
            raise ConfigError("config_file", f"configuration file not found: {path}")
        text = path.read_text(encoding="utf-8")
    else:
        # A string that names an existing file is read as a file; otherwise it
        # is treated as an inline YAML document.
        candidate = Path(source)
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
        else:
            text = source

    loaded = _SAFE_YAML.load(text)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, Mapping):
        raise ConfigError("config_file", "configuration must be a YAML mapping")
    return loaded


def _require_section(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return the mapping at ``data[key]`` or raise naming the missing section."""
    if key not in data or data[key] is None:
        raise ConfigError(key, "required configuration section is missing")
    section = data[key]
    if not isinstance(section, Mapping):
        raise ConfigError(key, "configuration section must be a mapping")
    return section


def _optional_section(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return the mapping at ``data[key]`` or an empty mapping when absent."""
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(key, "configuration section must be a mapping")
    return value


def _require_str(section: Mapping[str, Any], parent: str, key: str) -> str:
    """Return a required non-empty string setting or raise naming its key."""
    setting = f"{parent}.{key}"
    if key not in section or section[key] is None:
        raise ConfigError(setting, "required setting is missing")
    value = section[key]
    if not isinstance(value, str) or value.strip() == "":
        raise ConfigError(setting, "setting must be a non-empty string")
    return value


def _load_cost(data: Mapping[str, Any]) -> CostConfig:
    """Build and validate the cost section, applying the token-budget default."""
    section = _optional_section(data, "cost")

    raw_budget = section.get("token_budget", DEFAULT_TOKEN_BUDGET)
    if raw_budget is None:
        raw_budget = DEFAULT_TOKEN_BUDGET
    try:
        token_budget = validate_token_budget(raw_budget)
    except LimitOutOfRangeError as exc:
        raise ConfigError("cost.token_budget", str(exc)) from exc

    raw_rate = section.get("rate_limit_per_min", DEFAULT_RATE_LIMIT_PER_MIN)
    if raw_rate is None:
        raw_rate = DEFAULT_RATE_LIMIT_PER_MIN
    try:
        rate_limit = validate_rate_limit(raw_rate)
    except LimitOutOfRangeError as exc:
        raise ConfigError("cost.rate_limit_per_min", str(exc)) from exc

    raw_rounds = section.get("max_repair_rounds", DEFAULT_MAX_ROUNDS)
    if raw_rounds is None:
        raw_rounds = DEFAULT_MAX_ROUNDS
    try:
        max_repair_rounds = validate_repair_rounds(raw_rounds)
    except LimitOutOfRangeError as exc:
        raise ConfigError("cost.max_repair_rounds", str(exc)) from exc

    return CostConfig(
        token_budget=token_budget,
        rate_limit_per_min=rate_limit,
        max_repair_rounds=max_repair_rounds,
    )


def _load_network(data: Mapping[str, Any]) -> NetworkConfig:
    """Build and validate the network section."""
    section = _optional_section(data, "network")

    bind_address = section.get("bind_address", DEFAULT_BIND_ADDRESS)
    if not isinstance(bind_address, str) or bind_address.strip() == "":
        raise ConfigError("network.bind_address", "setting must be a non-empty string")

    port = section.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int):
        raise ConfigError("network.port", "setting must be an integer")
    if not 1 <= port <= 65_535:
        raise ConfigError("network.port", "setting must be a TCP port in 1..65535")

    return NetworkConfig(bind_address=bind_address, port=port)


def _load_access(data: Mapping[str, Any]) -> AccessConfig:
    """Build and validate the access section (Req 17)."""
    section = _optional_section(data, "access")

    protection_enabled = section.get("protection_enabled", False)
    if not isinstance(protection_enabled, bool):
        raise ConfigError("access.protection_enabled", "setting must be a boolean")

    passphrase_hash = section.get("passphrase_hash")
    if protection_enabled:
        if not isinstance(passphrase_hash, str) or passphrase_hash.strip() == "":
            raise ConfigError(
                "access.passphrase_hash",
                "required when access protection is enabled",
            )
    elif passphrase_hash is not None and not isinstance(passphrase_hash, str):
        raise ConfigError("access.passphrase_hash", "setting must be a string when set")

    return AccessConfig(protection_enabled=protection_enabled, passphrase_hash=passphrase_hash)


def _load_paths(data: Mapping[str, Any]) -> PathsConfig:
    """Build the paths section, applying defaults for any unset location."""
    section = _optional_section(data, "paths")

    config_root = section.get("config_root", DEFAULT_CONFIG_ROOT)
    if not isinstance(config_root, str) or config_root.strip() == "":
        raise ConfigError("paths.config_root", "setting must be a non-empty string")

    projects_root = section.get("projects_root", DEFAULT_PROJECTS_ROOT)
    if not isinstance(projects_root, str) or projects_root.strip() == "":
        raise ConfigError("paths.projects_root", "setting must be a non-empty string")

    return PathsConfig(config_root=config_root, projects_root=projects_root)


def _load_updates(data: Mapping[str, Any]) -> UpdatesConfig:
    """Build and validate the updates section (Req 23.4, 23.5)."""
    section = _optional_section(data, "updates")

    offline_mode = section.get("offline_mode", False)
    if not isinstance(offline_mode, bool):
        raise ConfigError("updates.offline_mode", "setting must be a boolean")

    check_interval = section.get("check_interval_hours", DEFAULT_CHECK_INTERVAL_HOURS)
    if isinstance(check_interval, bool) or not isinstance(check_interval, int) or check_interval <= 0:
        raise ConfigError("updates.check_interval_hours", "setting must be a positive integer")

    cache_ttl = section.get("cache_ttl_hours", DEFAULT_CACHE_TTL_HOURS)
    if isinstance(cache_ttl, bool) or not isinstance(cache_ttl, int) or cache_ttl <= 0:
        raise ConfigError("updates.cache_ttl_hours", "setting must be a positive integer")

    return UpdatesConfig(
        offline_mode=offline_mode,
        check_interval_hours=check_interval,
        cache_ttl_hours=cache_ttl,
    )


def _load_tenant(data: Mapping[str, Any]) -> TenantConfig:
    """Build the tenant section."""
    section = _optional_section(data, "tenant")

    base_url = section.get("default_region_base_url")
    if base_url is not None and (not isinstance(base_url, str) or base_url.strip() == ""):
        raise ConfigError("tenant.default_region_base_url", "setting must be a non-empty string when set")

    return TenantConfig(default_region_base_url=base_url)


def _load_production_sources(data: Mapping[str, Any]) -> tuple[ProductionSourceConfig, ...]:
    """Build and validate the optional production-sources list (Req 24.4, 25.1, 25.2)."""
    raw = data.get("production_sources")
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ConfigError("production_sources", "setting must be a list of sources")

    sources: list[ProductionSourceConfig] = []
    for index, entry in enumerate(raw):
        prefix = f"production_sources[{index}]"
        if not isinstance(entry, Mapping):
            raise ConfigError(prefix, "each production source must be a mapping")

        source_id = _require_str(entry, prefix, "id")
        repo = _require_str(entry, prefix, "repo")

        visibility = entry.get("visibility", "public")
        if visibility not in ("public", "private"):
            raise ConfigError(f"{prefix}.visibility", "setting must be 'public' or 'private'")

        local_path = entry.get("local_path")
        remote_url = entry.get("remote_url")
        git_credential_id = entry.get("git_credential_id")

        # A private source with no local clone must name a git credential so the
        # remote fetch can authenticate (Req 25.2, 25.9).
        if visibility == "private" and not local_path and not git_credential_id:
            raise ConfigError(
                f"{prefix}.git_credential_id",
                "required for a private source without a local clone",
            )

        sources.append(
            ProductionSourceConfig(
                id=source_id,
                repo=repo,
                visibility=visibility,
                local_path=local_path,
                remote_url=remote_url,
                git_credential_id=git_credential_id,
            )
        )
    return tuple(sources)


def load_config(source: Union[Mapping[str, Any], str, os.PathLike[str]]) -> AppConfig:
    """Load and validate startup configuration, halting on any invalid setting.

    ``source`` may be a mapping, a path to a YAML file, or a YAML document
    string. The loader reads every startup setting group required by Req 20.2
    (LLM provider, token budget, rate limit, bind address, access protection,
    paths, updates, tenant, and production sources) and validates each one.

    Args:
        source: The configuration mapping, file path, or YAML text.

    Returns:
        A fully-populated, validated :class:`AppConfig`.

    Raises:
        ConfigError: if any required setting is missing or any setting is
            invalid. The exception's :attr:`~ConfigError.setting` names the
            offending key so startup halts identifying it (Req 20.6, Property
            37).
    """
    data = _read_mapping(source)

    llm_section = _require_section(data, "llm")
    provider = _require_str(llm_section, "llm", "provider")
    kiro_cli_path = _require_str(llm_section, "llm", "kiro_cli_path")
    llm = LLMConfig(provider=provider, kiro_cli_path=kiro_cli_path)

    return AppConfig(
        llm=llm,
        cost=_load_cost(data),
        network=_load_network(data),
        access=_load_access(data),
        paths=_load_paths(data),
        updates=_load_updates(data),
        tenant=_load_tenant(data),
        production_sources=_load_production_sources(data),
    )


# ---------------------------------------------------------------------------
# External dependency probes
# ---------------------------------------------------------------------------


def probe_kiro_cli(kiro_cli_path: str, *, timeout: float = 10.0) -> ProbeResult:
    """Probe whether the Kiro CLI is available, naming remediation when not.

    The probe resolves ``kiro_cli_path`` (an absolute path or a name on PATH)
    and runs ``<kiro> --version`` to confirm it is executable. A missing binary,
    a non-zero exit, or an execution error all produce an unavailable result
    carrying the remediation step (Req 20.5). This never raises for a missing or
    misbehaving CLI; it reports the condition so the caller can surface it.

    Args:
        kiro_cli_path: Configured path or command name for the Kiro CLI.
        timeout: Seconds to wait for the version probe before giving up.

    Returns:
        A :class:`ProbeResult` for the Kiro CLI.
    """
    resolved = shutil.which(kiro_cli_path) if os.sep not in kiro_cli_path else kiro_cli_path
    if not resolved or not Path(resolved).exists():
        return ProbeResult(
            name="Kiro CLI",
            available=False,
            detail=f"Kiro CLI not found at '{kiro_cli_path}'",
            remediation=_KIRO_REMEDIATION,
        )

    try:
        completed = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ProbeResult(
            name="Kiro CLI",
            available=False,
            detail=f"Kiro CLI could not be executed: {exc}",
            remediation=_KIRO_REMEDIATION,
        )

    if completed.returncode != 0:
        return ProbeResult(
            name="Kiro CLI",
            available=False,
            detail=f"Kiro CLI exited with status {completed.returncode}",
            remediation=_KIRO_REMEDIATION,
        )

    version = (completed.stdout or completed.stderr or "").strip()
    return ProbeResult(name="Kiro CLI", available=True, detail=version or resolved)


def probe_docker(*, docker_path: str = "docker", timeout: float = 10.0) -> ProbeResult:
    """Probe whether the Docker engine is reachable.

    Docker is optional at startup and required only to build a plugin, so an
    unavailable result is informational rather than a startup halt. The probe
    resolves the ``docker`` client and runs ``docker version`` to confirm the
    daemon is reachable.

    Args:
        docker_path: Path or command name for the Docker client.
        timeout: Seconds to wait for the version probe before giving up.

    Returns:
        A :class:`ProbeResult` for the Docker engine.
    """
    resolved = shutil.which(docker_path) if os.sep not in docker_path else docker_path
    if not resolved or not Path(resolved).exists():
        return ProbeResult(
            name="Docker",
            available=False,
            detail=f"Docker client not found at '{docker_path}'",
            remediation=_DOCKER_REMEDIATION,
        )

    try:
        completed = subprocess.run(
            [resolved, "version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ProbeResult(
            name="Docker",
            available=False,
            detail=f"Docker could not be executed: {exc}",
            remediation=_DOCKER_REMEDIATION,
        )

    if completed.returncode != 0:
        return ProbeResult(
            name="Docker",
            available=False,
            detail="Docker daemon is not reachable",
            remediation=_DOCKER_REMEDIATION,
        )

    return ProbeResult(name="Docker", available=True, detail=(completed.stdout or "").strip() or resolved)
