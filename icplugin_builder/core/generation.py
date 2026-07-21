"""Generation classification and the template library (design Req 3.1-3.4).

The Generation_Engine splits every requested artifact into a **classification**
and dispatches it across the deterministic/LLM boundary that keeps token usage
bounded (design "Deterministic-vs-LLM Decision Boundary"). This module owns the
pure-logic half of that split -- the classification decision and the parameter-
ized :class:`TemplateLibrary` -- with **zero** LLM involvement and no I/O, so it
can be exhaustively property-tested (design Properties 7 and 8).

Every artifact is classified into exactly one :class:`ArtifactKind`:

* **Deterministic (zero LLM calls):**
  ``directory_structure``, ``spec_skeleton``, ``boilerplate`` are produced by
  the ``insight-plugin`` CLI (Req 3.1), and ``template_match`` is rendered here
  from a parameterized template (Req 3.3).
* **Reasoning (routed to the LLM):**
  ``action_logic``, ``field_description``, ``help_text`` require natural-language
  reasoning and are the *only* kinds the ``LLM_Generator`` is ever invoked for
  (Req 3.2). A reasoning request is routed to the LLM only when no matching
  template is available (Req 3.4).

The classifier therefore never routes a directory-structure, spec-skeleton, or
boilerplate artifact to the LLM, and any reasoning request that matches a
template is reclassified as ``template_match`` and rendered deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from string import Template as _StringTemplate
from typing import Any, Dict, List, Mapping, Optional

__all__ = [
    "ArtifactKind",
    "Route",
    "DETERMINISTIC_ARTIFACT_KINDS",
    "REASONING_ARTIFACT_KINDS",
    "REQUESTABLE_ARTIFACT_KINDS",
    "GenerationRequest",
    "Classification",
    "TemplateError",
    "Template",
    "TemplateLibrary",
    "classify_request",
    "default_template_library",
]


class ArtifactKind(str, Enum):
    """The kind an artifact is classified into.

    The three deterministic-source kinds and ``TEMPLATE_MATCH`` are produced
    with zero LLM calls; the three reasoning kinds are the only kinds the LLM is
    invoked for (Req 3.2).
    """

    DIRECTORY_STRUCTURE = "directory_structure"
    SPEC_SKELETON = "spec_skeleton"
    BOILERPLATE = "boilerplate"
    ACTION_LOGIC = "action_logic"
    FIELD_DESCRIPTION = "field_description"
    HELP_TEXT = "help_text"
    TEMPLATE_MATCH = "template_match"


class Route(str, Enum):
    """Where a classified artifact is dispatched.

    ``DETERMINISTIC`` artifacts consume zero LLM tokens (produced by the
    ``insight-plugin`` CLI or rendered from a template); ``LLM`` artifacts are
    served by the Kiro CLI subprocess.
    """

    DETERMINISTIC = "deterministic"
    LLM = "llm"


#: Kinds produced without any LLM call (Req 3.1, 3.3). ``TEMPLATE_MATCH`` is a
#: classification *result* rather than a request kind but is deterministic too.
DETERMINISTIC_ARTIFACT_KINDS = frozenset(
    {
        ArtifactKind.DIRECTORY_STRUCTURE,
        ArtifactKind.SPEC_SKELETON,
        ArtifactKind.BOILERPLATE,
        ArtifactKind.TEMPLATE_MATCH,
    }
)

#: The only kinds the ``LLM_Generator`` may be invoked for (Req 3.2).
REASONING_ARTIFACT_KINDS = frozenset(
    {
        ArtifactKind.ACTION_LOGIC,
        ArtifactKind.FIELD_DESCRIPTION,
        ArtifactKind.HELP_TEXT,
    }
)

#: Kinds a caller may submit as a request. ``TEMPLATE_MATCH`` is excluded
#: because it is only ever the *outcome* of classifying a reasoning request.
REQUESTABLE_ARTIFACT_KINDS = frozenset(
    {
        ArtifactKind.DIRECTORY_STRUCTURE,
        ArtifactKind.SPEC_SKELETON,
        ArtifactKind.BOILERPLATE,
        ArtifactKind.ACTION_LOGIC,
        ArtifactKind.FIELD_DESCRIPTION,
        ArtifactKind.HELP_TEXT,
    }
)


class TemplateError(Exception):
    """Raised when a template cannot be matched or rendered.

    Carries a message identifying the missing template or the parameters that
    were required by the template body but not supplied.
    """


@dataclass(frozen=True)
class GenerationRequest:
    """A request to produce one plugin artifact.

    Attributes:
        kind: the intrinsic kind of the requested artifact; must be one of
            :data:`REQUESTABLE_ARTIFACT_KINDS`.
        pattern: an optional identifier of a known pattern (e.g.
            ``"paginated_rest_list"``) used to look up a matching template.
            ``None`` means "no known pattern", so no template can match.
        parameters: values substituted into a matched template's body.
        name: an optional artifact name carried for context (e.g. the action
            name); it does not affect classification.
    """

    kind: ArtifactKind
    pattern: Optional[str] = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    name: Optional[str] = None


@dataclass(frozen=True)
class Classification:
    """The outcome of classifying a :class:`GenerationRequest`.

    Attributes:
        kind: the classified :class:`ArtifactKind`. For a matched reasoning
            request this is :attr:`ArtifactKind.TEMPLATE_MATCH`; otherwise it is
            the request's own kind.
        route: :attr:`Route.DETERMINISTIC` (zero LLM calls) or
            :attr:`Route.LLM`.
        template: the matched :class:`Template` when ``kind`` is
            ``TEMPLATE_MATCH``, else ``None``.
    """

    kind: ArtifactKind
    route: Route
    template: Optional["Template"] = None

    @property
    def requires_llm(self) -> bool:
        """Return ``True`` iff producing this artifact requires an LLM call."""
        return self.route is Route.LLM


@dataclass(frozen=True)
class Template:
    """A parameterized template that renders a reasoning artifact deterministically.

    A template satisfies a single reasoning :class:`ArtifactKind` (action logic,
    field description, or help text) for a named ``pattern``. Its ``body`` uses
    :class:`string.Template` ``$name`` placeholders, which do not collide with
    the ``{...}`` used by Python code and f-strings, so code bodies render
    cleanly.
    """

    name: str
    kind: ArtifactKind
    pattern: str
    body: str

    def __post_init__(self) -> None:
        if self.kind not in REASONING_ARTIFACT_KINDS:
            raise ValueError(
                f"template {self.name!r} has kind {self.kind.value!r}; templates may only satisfy reasoning kinds "
                f"({', '.join(sorted(k.value for k in REASONING_ARTIFACT_KINDS))})"
            )

    @property
    def required_parameters(self) -> List[str]:
        """The placeholder names the body requires, in stable sorted order."""
        return sorted(set(_StringTemplate(self.body).get_identifiers()))

    def render(self, parameters: Optional[Mapping[str, Any]] = None) -> str:
        """Render the template body with ``parameters`` substituted in.

        Args:
            parameters: values for the body's ``$name`` placeholders. Extra
                keys are ignored; every required placeholder must be present.

        Returns:
            The fully substituted artifact text. No LLM call is made.

        Raises:
            TemplateError: if a required placeholder has no supplied value.
        """
        values: Dict[str, Any] = dict(parameters or {})
        missing = [name for name in self.required_parameters if name not in values]
        if missing:
            raise TemplateError(
                f"template {self.name!r} is missing required parameter(s): {', '.join(sorted(missing))}"
            )
        try:
            return _StringTemplate(self.body).substitute(values)
        except (KeyError, ValueError) as error:  # pragma: no cover - guarded by the missing check
            raise TemplateError(f"failed to render template {self.name!r}: {error}") from error


class TemplateLibrary:
    """A registry of parameterized :class:`Template` objects keyed by kind+pattern.

    A reasoning request "matches a template" when a registered template shares
    both its kind and its ``pattern``; a match means the artifact is produced
    from the template with zero LLM calls (Req 3.3).
    """

    def __init__(self, templates: Optional[List[Template]] = None) -> None:
        """Create a library, optionally pre-populated with ``templates``."""
        self._templates: Dict[tuple, Template] = {}
        for template in templates or []:
            self.register(template)

    def register(self, template: Template) -> None:
        """Add ``template`` to the library.

        Raises:
            ValueError: if a template with the same kind and pattern already
                exists (patterns are unique per kind).
        """
        key = (template.kind, template.pattern)
        if key in self._templates:
            raise ValueError(f"a template for kind {template.kind.value!r} pattern {template.pattern!r} already exists")
        self._templates[key] = template

    def match(self, request: GenerationRequest) -> Optional[Template]:
        """Return the template matching ``request``, or ``None`` if none matches.

        A match requires the request to name a ``pattern`` whose (kind, pattern)
        pair is registered. Deterministic-kind requests never match a template
        because templates only satisfy reasoning kinds.
        """
        if request.pattern is None:
            return None
        return self._templates.get((request.kind, request.pattern))

    def render(self, request: GenerationRequest) -> str:
        """Match ``request`` to a template and render it with the request's parameters.

        Raises:
            TemplateError: if no template matches the request, or a required
                parameter is missing.
        """
        template = self.match(request)
        if template is None:
            raise TemplateError(f"no template matches kind {request.kind.value!r} pattern {request.pattern!r}")
        return template.render(request.parameters)

    def patterns(self, kind: Optional[ArtifactKind] = None) -> List[str]:
        """List registered patterns, optionally filtered to a single ``kind``."""
        return sorted(
            pattern for (registered_kind, pattern) in self._templates if kind is None or registered_kind is kind
        )

    def __contains__(self, request: GenerationRequest) -> bool:
        return self.match(request) is not None

    def __len__(self) -> int:
        return len(self._templates)


def classify_request(
    request: GenerationRequest,
    template_library: Optional[TemplateLibrary] = None,
) -> Classification:
    """Classify ``request`` and decide whether it needs an LLM call.

    Directory-structure, spec-skeleton, and boilerplate requests are always
    deterministic (Req 3.1). A reasoning request (action logic, field
    description, help text) is reclassified as :attr:`ArtifactKind.TEMPLATE_MATCH`
    and routed deterministically when a template matches (Req 3.3); otherwise it
    keeps its reasoning kind and is routed to the LLM (Req 3.2, 3.4).

    Args:
        request: the artifact request to classify.
        template_library: the templates to consult for reasoning requests; when
            ``None``, no template can match and reasoning requests route to the
            LLM.

    Returns:
        A :class:`Classification` with the resolved kind, route, and matched
        template (if any).

    Raises:
        ValueError: if ``request.kind`` is not a requestable kind.
    """
    kind = request.kind
    if kind not in REQUESTABLE_ARTIFACT_KINDS:
        raise ValueError(
            f"{kind.value!r} is not a requestable artifact kind; expected one of "
            f"{', '.join(sorted(k.value for k in REQUESTABLE_ARTIFACT_KINDS))}"
        )

    if kind in REASONING_ARTIFACT_KINDS:
        template = template_library.match(request) if template_library is not None else None
        if template is not None:
            return Classification(kind=ArtifactKind.TEMPLATE_MATCH, route=Route.DETERMINISTIC, template=template)
        return Classification(kind=kind, route=Route.LLM, template=None)

    # directory_structure | spec_skeleton | boilerplate
    return Classification(kind=kind, route=Route.DETERMINISTIC, template=None)


def default_template_library() -> TemplateLibrary:
    """Build the library of common parameterized patterns shipped with the tool.

    Covers the patterns called out in the design (paginated REST list action,
    single-resource GET, webhook trigger, plus stock field descriptions and
    connection help text). A request matching any of these renders here with
    zero LLM calls (Req 3.3).
    """
    return TemplateLibrary(
        [
            Template(
                name="single_resource_get_action",
                kind=ArtifactKind.ACTION_LOGIC,
                pattern="single_resource_get",
                body=(
                    "def run(self, params={}):\n"
                    '    resource_id = params.get("$id_param")\n'
                    '    endpoint = f"$base_path/{resource_id}"\n'
                    "    response = self.connection.client.get(endpoint)\n"
                    '    return {"$output_key": response}\n'
                ),
            ),
            Template(
                name="paginated_rest_list_action",
                kind=ArtifactKind.ACTION_LOGIC,
                pattern="paginated_rest_list",
                body=(
                    "def run(self, params={}):\n"
                    "    results = []\n"
                    "    page = 1\n"
                    "    while True:\n"
                    "        response = self.connection.client.get(\n"
                    '            "$base_path", params={"page": page, "per_page": $page_size}\n'
                    "        )\n"
                    '        items = response.get("$items_key", [])\n'
                    "        results.extend(items)\n"
                    "        if len(items) < $page_size:\n"
                    "            break\n"
                    "        page += 1\n"
                    '    return {"$output_key": results}\n'
                ),
            ),
            Template(
                name="webhook_trigger_action",
                kind=ArtifactKind.ACTION_LOGIC,
                pattern="webhook_trigger",
                body=(
                    "def run(self, params={}):\n"
                    "    while True:\n"
                    '        payload = self.receive_webhook(path="$webhook_path")\n'
                    "        if payload:\n"
                    '            self.send({"$output_key": payload})\n'
                ),
            ),
            Template(
                name="api_key_field_description",
                kind=ArtifactKind.FIELD_DESCRIPTION,
                pattern="api_key",
                body="An API key used to authenticate requests to the $service_name API.",
            ),
            Template(
                name="username_field_description",
                kind=ArtifactKind.FIELD_DESCRIPTION,
                pattern="username",
                body="The username for the $service_name account used to authenticate.",
            ),
            Template(
                name="connection_setup_help",
                kind=ArtifactKind.HELP_TEXT,
                pattern="connection_setup",
                body=(
                    "## Connection\n\n"
                    "This plugin connects to $service_name. Provide the credentials for your "
                    "$service_name account to establish the connection.\n"
                ),
            ),
        ]
    )
