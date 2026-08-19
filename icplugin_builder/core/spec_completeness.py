"""Spec completeness: the fields and conventions ``insight-plugin validate`` needs.

:mod:`.spec_validator` answers "is this document structurally well-formed?" This
module answers a different and, in practice, more useful question: "will the
InsightConnect toolchain accept it, and does it follow the plugin conventions?"

The two are kept apart deliberately. A missing field ``type`` is a malformed
document; a missing ``sdk`` block is a well-formed document that `insight-plugin
validate` will reject. Folding the second into the first would make every
in-progress draft structurally invalid, which is not what a schema error should
mean.

The checks here are not speculative. Each one corresponds to a field the
toolchain requires or a rule stated in the operator's own plugin steering, and
every one of them was actually missing from plugins this tool produced: specs
went out with no ``sdk`` block, no ``version_history``, no ``supported_versions``,
no ``resources``, and no ``example`` on any output, while using a credential type
that does not exist.

Each finding carries a stable :attr:`Finding.code`. That is what lets a repair
loop tell "the same problem is still here" from "a new problem appeared" without
asking a model to judge it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Tuple, Union

from .spec_model import PluginSpec

__all__ = [
    "Severity",
    "Finding",
    "CompletenessReport",
    "REQUIRED_TOP_LEVEL",
    "REQUIRED_SDK_KEYS",
    "REQUIRED_RESOURCE_KEYS",
    "VALID_CREDENTIAL_TYPES",
    "check_completeness",
    "with_sdk_version",
]


class Severity(str, Enum):
    """How much a finding matters.

    ``ERROR`` means the toolchain will reject the plugin or the generated code
    will not work. ``WARNING`` means it violates a documented convention but will
    still build.
    """

    ERROR = "error"
    WARNING = "warning"


#: Top-level keys a plugin spec needs beyond the six the structural schema
#: requires. Sourced from the ``plugin-spec`` steering's reference document.
REQUIRED_TOP_LEVEL: Tuple[str, ...] = (
    "extension",
    "products",
    "support",
    "status",
    "cloud_ready",
    "sdk",
    "supported_versions",
    "key_features",
    "requirements",
    "version_history",
    "resources",
    "hub_tags",
)

#: Keys required inside the ``sdk`` block.
REQUIRED_SDK_KEYS: Tuple[str, ...] = ("type", "version", "user")

#: Keys required inside ``resources``.
REQUIRED_RESOURCE_KEYS: Tuple[str, ...] = ("source_url", "license_url")

#: The credential field types the platform defines, and therefore the ones a plugin
#: may declare. A type outside this set will not bind its credential at runtime.
#: This tuple is the installed toolchain's own ``SchemaUtil.BASE_TYPES``, and a test
#: cross-checks it against that schema so the two cannot drift in silence.
VALID_CREDENTIAL_TYPES: Tuple[str, ...] = (
    "credential_secret_key",
    "credential_username_password",
    "credential_asymmetric_key",
    "credential_token",
)
# Why the cross-check exists rather than trust: this tuple listed only the first
# three and its own comment offered the fourth as the example of a type "the platform
# does not define". The toolchain had defined it all along -- a required
# password-formatted `token` and an optional `domain` -- so a spec the toolchain
# would have accepted was reported as a defect, and it was one of sixteen findings a
# real run raised against a plugin whose every endpoint had been verified by hand.
# The set is read from the schema now, not maintained by taste.

#: Component sections whose outputs the toolchain expects examples on.
_COMPONENT_SECTIONS: Tuple[str, ...] = ("actions", "triggers", "tasks")

#: Required keys whose empty value is conventional rather than a mistake.
#: ``status: []`` is how a plugin with no special status is written.
_EMPTY_ALLOWED = frozenset({"status"})


@dataclass(frozen=True)
class Finding:
    """One completeness or convention violation.

    Attributes:
        code: a stable identifier for the *kind* of problem (e.g.
            ``missing_field``, ``output_missing_example``). Stable across runs so
            a repair loop can compare findings round over round.
        path: where in the spec the problem is, as a dotted path.
        message: what is wrong, and what is expected instead.
        severity: :class:`Severity`.
    """

    code: str
    path: str
    message: str
    severity: Severity = Severity.ERROR

    @property
    def key(self) -> str:
        """A stable identity for this finding, for round-over-round comparison."""
        return f"{self.code}:{self.path}"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"[{self.severity.value}] {self.path}: {self.message}"


@dataclass(frozen=True)
class CompletenessReport:
    """The result of a completeness check.

    Attributes:
        findings: every violation, ordered by path then code for determinism.
    """

    findings: List[Finding] = field(default_factory=list)

    @property
    def errors(self) -> Tuple[Finding, ...]:
        """Findings that will cause the toolchain to reject the plugin."""
        return tuple(f for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warnings(self) -> Tuple[Finding, ...]:
        """Findings that violate a convention without breaking the build."""
        return tuple(f for f in self.findings if f.severity is Severity.WARNING)

    @property
    def is_complete(self) -> bool:
        """Return ``True`` iff there are no error-severity findings."""
        return not self.errors

    def keys(self) -> Tuple[str, ...]:
        """The stable keys of every finding, sorted."""
        return tuple(sorted(f.key for f in self.findings))

    def summary(self) -> str:
        """Return a human-readable one-line summary."""
        if not self.findings:
            return "Plugin spec is complete."
        return f"Plugin spec has {len(self.errors)} completeness error(s) and {len(self.warnings)} warning(s)."


def check_completeness(spec: Union[PluginSpec, Mapping[str, Any]]) -> CompletenessReport:
    """Check ``spec`` for the fields and conventions the toolchain requires.

    Args:
        spec: a typed :class:`PluginSpec` or a raw parsed mapping.

    Returns:
        A :class:`CompletenessReport`. Deterministically ordered so two runs over
        the same spec produce identical output.
    """
    mapping: Mapping[str, Any] = spec.to_mapping() if isinstance(spec, PluginSpec) else spec
    findings: List[Finding] = []

    findings.extend(_missing_top_level(mapping))
    findings.extend(_nested_requirements(mapping))
    findings.extend(_output_examples(mapping))
    findings.extend(_credential_types(mapping))
    findings.extend(_encoding_conventions(mapping))

    findings.sort(key=lambda f: (f.path, f.code))
    return CompletenessReport(findings=findings)


def _missing_top_level(mapping: Mapping[str, Any]) -> List[Finding]:
    """Report absent or empty required top-level keys."""
    findings: List[Finding] = []
    for key in REQUIRED_TOP_LEVEL:
        if key not in mapping:
            findings.append(
                Finding(
                    code="missing_field",
                    path=key,
                    message=f"required top-level field {key!r} is absent; insight-plugin validate requires it",
                )
            )
        elif _is_empty(mapping[key]) and key not in _EMPTY_ALLOWED:
            findings.append(
                Finding(
                    code="empty_field",
                    path=key,
                    message=f"required top-level field {key!r} is present but empty",
                )
            )
    return findings


def _is_empty(value: Any) -> bool:
    """Return ``True`` for values that are present but carry no content.

    ``False`` and ``0`` are *not* empty -- ``cloud_ready: false`` is a real
    setting, so a plain falsiness test would wrongly report it.
    """
    if value is None:
        return True
    if isinstance(value, (str, list, tuple, dict, set)):
        return len(value) == 0
    return False


def _nested_requirements(mapping: Mapping[str, Any]) -> List[Finding]:
    """Report missing keys inside ``sdk`` and ``resources``."""
    findings: List[Finding] = []

    sdk = mapping.get("sdk")
    if isinstance(sdk, Mapping):
        for key in REQUIRED_SDK_KEYS:
            if not sdk.get(key):
                findings.append(
                    Finding(
                        code="missing_field",
                        path=f"sdk.{key}",
                        message=(
                            f"sdk.{key} is required; resolve the SDK version from the SDK "
                            "changelog rather than hardcoding it"
                        ),
                    )
                )

    resources = mapping.get("resources")
    if isinstance(resources, Mapping):
        for key in REQUIRED_RESOURCE_KEYS:
            if not resources.get(key):
                findings.append(
                    Finding(
                        code="missing_field",
                        path=f"resources.{key}",
                        message=f"resources.{key} is required",
                    )
                )
    return findings


def _output_examples(mapping: Mapping[str, Any]) -> List[Finding]:
    """Report output fields with no ``example`` value.

    The toolchain's ExampleInputValidator expects them, and ``help.md`` renders
    them into the published documentation.
    """
    findings: List[Finding] = []
    for section in _COMPONENT_SECTIONS:
        components = mapping.get(section)
        if not isinstance(components, Mapping):
            continue
        for component_name, component in components.items():
            if not isinstance(component, Mapping):
                continue
            outputs = component.get("output")
            if not isinstance(outputs, Mapping):
                continue
            for field_name, schema in outputs.items():
                if not isinstance(schema, Mapping):
                    continue
                if "example" not in schema:
                    findings.append(
                        Finding(
                            code="output_missing_example",
                            path=f"{section}.{component_name}.output.{field_name}",
                            message="output field has no 'example'; every output needs one",
                        )
                    )
    return findings


def _credential_types(mapping: Mapping[str, Any]) -> List[Finding]:
    """Report connection fields using a credential type the platform does not define."""
    findings: List[Finding] = []
    connection = mapping.get("connection")
    if not isinstance(connection, Mapping):
        return findings
    for field_name, schema in connection.items():
        if not isinstance(schema, Mapping):
            continue
        declared = str(schema.get("type", ""))
        if not declared.startswith("credential"):
            continue
        if declared not in VALID_CREDENTIAL_TYPES:
            findings.append(
                Finding(
                    code="invalid_credential_type",
                    path=f"connection.{field_name}.type",
                    message=(
                        f"{declared!r} is not a valid credential type; expected one of "
                        f"{', '.join(VALID_CREDENTIAL_TYPES)}"
                    ),
                )
            )
    return findings


def _encoding_conventions(mapping: Mapping[str, Any]) -> List[Finding]:
    """Report text that the toolchain or code generation rejects.

    Two specific hazards, both from the plugin steering: the EncodingValidator
    rejects em dashes anywhere in the spec, and a nested double quote inside a
    description produces a syntax error in the generated ``schema.py``.
    """
    findings: List[Finding] = []
    for path, value in _walk_strings(mapping):
        if "\u2014" in value:
            findings.append(
                Finding(
                    code="em_dash",
                    path=path,
                    message="contains an em dash, which the EncodingValidator rejects; use a hyphen",
                )
            )
        if path.endswith("description") and '"' in value:
            findings.append(
                Finding(
                    code="nested_quotes",
                    path=path,
                    message=(
                        "description contains a double quote, which breaks generated schema.py; "
                        "rephrase or use single quotes"
                    ),
                )
            )
    return findings


def _walk_strings(node: Any, prefix: str = "") -> List[Tuple[str, str]]:
    """Yield ``(dotted_path, value)`` for every string in a nested structure."""
    found: List[Tuple[str, str]] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            found.extend(_walk_strings(value, child))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            found.extend(_walk_strings(value, f"{prefix}[{index}]"))
    elif isinstance(node, str):
        found.append((prefix or "(root)", node))
    return found


def with_sdk_version(spec: PluginSpec, version: str, *, sdk_type: str = "slim", user: str = "nobody") -> PluginSpec:
    """Return a copy of ``spec`` whose ``sdk`` block records ``version``.

    ``sdk`` is not a modeled field, so it lives in :attr:`PluginSpec.extra`
    alongside the other keys the spec carries verbatim.

    Only absent values are filled: an ``sdk`` block that the operator or the
    agent already populated is left as-is, so a deliberate pin is never silently
    overwritten. The input spec is not mutated.

    Args:
        spec: the spec to copy.
        version: the SDK version to record. Resolve it with
            :func:`~icplugin_builder.integrations.build_prep.resolve_sdk_version`
            rather than hardcoding it.
        sdk_type: the SDK packaging type to default to.
        user: the container user to default to.

    Returns:
        A new :class:`PluginSpec` with the ``sdk`` block filled in.
    """
    updated = copy.deepcopy(spec)
    existing = updated.extra.get("sdk")
    sdk: Dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}
    sdk.setdefault("type", sdk_type)
    sdk.setdefault("version", version)
    sdk.setdefault("user", user)
    updated.extra["sdk"] = sdk
    return updated
