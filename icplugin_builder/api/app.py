"""FastAPI application, HTTP routes, and the WebSocket channel (task 22.1).

This module wires the :class:`~icplugin_builder.orchestrator.orchestrator.Orchestrator`
behind a thin HTTP/WebSocket surface and serves the pre-built single-page UI as
static assets, so the operator launches one local process and opens
``http://127.0.0.1:<port>`` (design "Recommended Technology Stack").

What it exposes:

* **HTTP routes** (all under ``/api``) that drive the orchestrator: start a
  session by entry mode (net-new / iterate / enhance), submit a conversation
  message, read the cumulative token counter, read the visualization
  view-model, prepare an export preview, confirm/decline an export, list
  previously created plugins and their history, list read-only production
  sources and their plugins, and query managed-tooling updates.
* **A WebSocket channel** (``/ws/{session_id}``) that streams draft state, the
  token counter, and visualization updates on every applied turn, satisfying
  the push-based visualization refresh (Req 5.3) without polling.
* **Static assets** -- when a built UI directory is supplied it is mounted at
  ``/`` so the same process serves the app.

Security posture (Req 17.4, design "Be safe by default"):

* The server binds to the loopback interface (``127.0.0.1``) by default; the
  bind address is taken from :class:`~icplugin_builder.api.config.NetworkConfig`
  and only changes if the operator explicitly configures a different address.
* An optional local passphrase guard is enforced by
  :class:`~icplugin_builder.security.access_controller.AccessController`: when
  access protection is enabled every ``/api`` route and the WebSocket require a
  matching passphrase (supplied via the ``X-Access-Passphrase`` header or the
  ``passphrase`` query parameter for the WebSocket) and a mismatch is denied
  before any orchestrator function runs (Req 17.1, 17.2).

SECURITY NOTE: this app is intended for a single local operator on loopback.
The default configuration exposes **no** network-reachable endpoint beyond
loopback, and when access protection is disabled the loopback endpoints are
unauthenticated *by design* for local single-user use. Do **not** bind this app
to a non-loopback address (``0.0.0.0`` or a routable IP) without also enabling
access protection -- doing so would expose unauthenticated,
orchestrator-driving endpoints to the network.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..core.visualization import render_visualization
from ..integrations.code_validator import StageName
from ..integrations.definition_of_done import DoneReport
from ..integrations.export_manager import TenantCredentials
from ..orchestrator.orchestrator import (
    EntryModeError,
    Orchestrator,
    OrchestratorError,
    RegistryAccessError,
    SessionNotFoundError,
)
from ..orchestrator.session import ExportPlan, ExportOutcome, SessionState, TurnResult
from ..security.access_controller import AccessController
from .config import AppConfig, load_config

__all__ = [
    "create_app",
    "create_app_from_config",
    "main",
]

logger = logging.getLogger(__name__)

#: Header carrying the optional access passphrase for protected HTTP routes.
ACCESS_PASSPHRASE_HEADER = "X-Access-Passphrase"


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class StartSessionRequest(BaseModel):
    """Body for starting a session by entry mode (Req 24.1-24.4)."""

    entry_mode: str = Field(..., description="One of create_new / iterate_custom / enhance_production.")
    session_id: str = Field(..., description="Client-chosen session identifier.")
    user_id: str = Field("local-operator", description="Operator identifier governing the request rate.")
    plugin_name: Optional[str] = Field(None, description="Plugin to load for iterate mode.")
    source: Optional[str] = Field(None, description="Production source id for enhance mode.")
    production_plugin: Optional[str] = Field(None, description="Plugin within the source for enhance mode.")


class SubmitMessageRequest(BaseModel):
    """Body for submitting a conversation message (Req 1.1)."""

    text: str = Field(..., description="The raw user message (validated 1..10,000 chars).")


class ConfirmExportRequest(BaseModel):
    """Body for confirming or declining an export (Req 16.5, 9.3, 10)."""

    confirmed: bool = Field(..., description="Explicit confirmation of the preview (Req 16.5).")
    target: str = Field("local", description="'local' or 'tenant'.")
    output_dir: Optional[str] = Field(None, description="User-accessible directory for a local .plg.")
    region_base_url: Optional[str] = Field(None, description="Tenant region base URL (tenant target).")
    api_key: Optional[str] = Field(None, description="Tenant API key (tenant target).")
    force: bool = Field(False, description="Skip validation gate and force export even when blocked.")


# ---------------------------------------------------------------------------
# Serialization helpers (plain, JSON-ready dicts)
# ---------------------------------------------------------------------------


def _serialize_spec(spec: Any) -> Optional[Dict[str, Any]]:
    """Return the plugin spec as a JSON-ready mapping, or ``None``."""
    if spec is None:
        return None
    return spec.to_mapping()


def _serialize_visualization(spec: Any) -> Dict[str, Any]:
    """Build and serialize the visualization view-model for ``spec`` (Req 5.1, 5.2, 5.5)."""
    render = render_visualization(spec)
    view_model = render.view_model
    payload: Dict[str, Any] = {
        "state": render.state.value,
        "message": render.message,
        "error": render.error,
        "nodes": [],
    }
    if view_model is not None:
        payload["nodes"] = [_serialize_node(node) for node in view_model.nodes()]
    return payload


def _serialize_node(node: Any) -> Dict[str, Any]:
    """Serialize a single visualization node view."""
    return {
        "node_id": node.node_id,
        "kind": node.kind,
        "name": node.name,
        "title": node.title,
        "description": node.description,
        "input": [_serialize_field(f) for f in node.input],
        "output": [_serialize_field(f) for f in node.output],
    }


def _serialize_field(field_view: Any) -> Dict[str, Any]:
    """Serialize a single field view."""
    return {
        "name": field_view.name,
        "type": field_view.type,
        "required": field_view.required,
        "title": field_view.title,
        "description": field_view.description,
    }


def _serialize_turn_result(result: TurnResult) -> Dict[str, Any]:
    """Serialize a :class:`TurnResult` for HTTP/WebSocket responses (Req 1.4, 3.6)."""
    return {
        "status": result.status.value,
        "message": result.message,
        "spec": _serialize_spec(result.spec),
        "generated": [
            {
                "kind": getattr(artifact.kind, "value", str(artifact.kind)),
                "content": artifact.content,
                "from_llm": artifact.from_llm,
                "tokens": artifact.tokens,
            }
            for artifact in result.generated
        ],
        "refreshed": result.refreshed,
        "structural_reasons": list(result.structural_reasons),
        "token_total": result.token_total,
    }


def _serialize_done_conditions(report: Optional[DoneReport]) -> List[Dict[str, Any]]:
    """Serialize the definition-of-done conditions still outstanding (Req 27.2).

    Only the shortfalls are sent. A met condition needs no operator attention, and
    listing all eleven every time would bury the two that matter. Each entry keeps
    its status so an unverified condition is not presented as a failure.
    """
    if report is None:
        return []
    return [
        {
            "name": condition.name,
            "status": condition.status.value,
            "description": condition.description,
            "detail": condition.detail,
        }
        for condition in report.outstanding
    ]


def _serialize_export_plan(plan: ExportPlan) -> Dict[str, Any]:
    """Serialize an :class:`ExportPlan` preview (Req 12, 16, 7, 8)."""
    return {
        "permitted": plan.permitted,
        # The plan's own summary, not the gate's: "export permitted" speaks only
        # for the four stages, and on its own would let an unfinished plugin read
        # as ready (Req 27.3).
        "summary": plan.summary(),
        "plugin_is_done": plan.plugin_is_done,
        "done_conditions": _serialize_done_conditions(plan.done_report),
        "spec_preview": _serialize_spec(plan.spec_preview),
        "file_list": list(plan.file_list),
        "diff": {
            "added": sorted(plan.diff.added),
            "removed": sorted(plan.diff.removed),
            "modified": sorted(plan.diff.modified),
            "first_version": plan.diff.first_version,
        },
        "version_display": plan.version_display,
        "spec_errors": [
            {"path": err.path, "message": err.message} for err in (plan.spec_report.errors if plan.spec_report else ())
        ],
        "completeness_findings": [
            {
                "code": finding.code,
                "path": finding.path,
                "message": finding.message,
                "severity": finding.severity.value,
            }
            for finding in (plan.completeness.findings if plan.completeness else ())
        ],
        "failed_stages": [stage.name for stage in plan.decision.failed_stages],
        # Clause 2.8: a finding is attributable to the bar that produced it. Two
        # operators with different plugins checkouts can still be held to
        # different profiles; what the payload adds is that the preview says which
        # one judged this plugin, and at what width.
        "lint_bar": _serialize_lint_bar(plan),
    }


def _serialize_lint_bar(plan: ExportPlan) -> Optional[Dict[str, Any]]:
    """Serialize which prospector profile and line length judged the plugin.

    Read from the ``Quality_Gate``'s report when there is one and from the ``lint``
    stage's result otherwise, because either can be the thing that ran: a preview
    computed without a project folder has no gate report, and a caller holding only
    the validator has no quality report.
    """
    report = plan.quality_report
    if report is not None and (report.lint_profile is not None or report.line_length is not None):
        profile = report.lint_profile
        return {
            "profile_path": str(profile.path) if profile is not None and profile.path else None,
            "profile_source": profile.source if profile is not None else None,
            "line_length": report.line_length,
        }
    stage = plan.pipeline_report.stage(StageName.LINT) if plan.pipeline_report is not None else None
    if stage is None or (stage.lint_profile is None and stage.line_length is None):
        return None
    return {
        "profile_path": (
            str(stage.lint_profile.path) if stage.lint_profile is not None and stage.lint_profile.path else None
        ),
        "profile_source": stage.lint_profile.source if stage.lint_profile is not None else None,
        "line_length": stage.line_length,
    }


def _serialize_failure(failure: Any) -> Optional[Dict[str, Any]]:
    """Serialize a build/export ``FailureIndication`` for the UI (Req 19.1, 19.4, 19.5).

    The UI distinguishes a build failure from an export failure and shows the
    failing step's bounded error output (first 10,000 chars) while retaining
    access to the complete output. Returns ``None`` when no failure is attached.
    """
    if failure is None:
        return None
    kind = getattr(failure, "kind", None)
    return {
        "kind": getattr(kind, "value", str(kind)) if kind is not None else None,
        "failing_step": getattr(failure, "failing_step", None),
        "displayed_output": getattr(failure, "displayed_output", None),
        "full_output": getattr(failure, "full_output", None),
        "truncated": bool(getattr(failure, "truncated", False)),
    }


def _serialize_export_outcome(outcome: ExportOutcome) -> Dict[str, Any]:
    """Serialize an :class:`ExportOutcome` (Req 9, 10, 19)."""
    return {
        "status": outcome.status.value,
        "message": outcome.message,
        "artifact_path": outcome.artifact_path,
        "version": outcome.version,
        "target": outcome.target,
        "failure": _serialize_failure(outcome.failure),
        "retained_artifact_path": outcome.retained_artifact_path,
    }


def _serialize_session(state: SessionState, token_total: int) -> Dict[str, Any]:
    """Serialize a session's public state for the initial WS/HTTP payload."""
    return {
        "session_id": state.session_id,
        "entry_mode": state.entry_mode,
        "plugin_name": state.plugin_name,
        "private_source_notice": state.private_source_notice,
        "spec": _serialize_spec(state.spec),
        "token_total": token_total,
        "visualization": _serialize_visualization(state.spec),
    }


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    orchestrator: Orchestrator,
    config: Optional[AppConfig] = None,
    access_controller: Optional[AccessController] = None,
    registry: Optional[Any] = None,
    source_provider: Optional[Any] = None,
    update_manager: Optional[Any] = None,
    interpreter: Optional[Any] = None,
    static_dir: Optional[Any] = None,
) -> FastAPI:
    """Build the FastAPI app wiring the orchestrator behind HTTP + WebSocket.

    Every collaborator is injected so the app is fully testable with mocked
    externals. Only ``orchestrator`` is required; the routes that need the
    registry, the production-source provider, or the update manager return a
    clear error when their collaborator is absent.

    Args:
        orchestrator: the sequencing core every route drives.
        config: the loaded startup configuration; used to surface a safe config
            summary and, when ``access_controller`` is omitted, to build the
            access guard from ``config.access``/``config.network``.
        access_controller: the optional access guard; when omitted and ``config``
            is supplied one is constructed from the config.
        registry: the plugin registry for the plugins/history routes.
        source_provider: the read-only production source provider for the
            sources routes (Req 24.4, 25).
        update_manager: the managed-tooling update manager for the updates route.
        static_dir: a directory of pre-built UI assets to serve at ``/``; when
            omitted no static mount is added (the API still runs).

    Returns:
        The configured :class:`FastAPI` application.
    """
    if access_controller is None and config is not None:
        access_controller = AccessController(config.access, network_config=config.network)

    app = FastAPI(title="InsightConnect Plugin Builder", version="0.1.0")

    def require_access(x_access_passphrase: Optional[str] = Header(default=None)) -> None:
        """Deny a protected route unless access is granted (Req 17.1, 17.2)."""
        if access_controller is None or not access_controller.protection_enabled:
            return
        session = access_controller.authenticate(x_access_passphrase)
        if not session.granted:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=session.reason or "access denied",
            )

    protected = [Depends(require_access)]

    # Prepared export plans awaiting confirmation, scoped to this app instance so
    # the two-step prepare/confirm flow is stateless at the HTTP layer while the
    # orchestrator remains the source of truth for session state.
    export_plans: Dict[str, ExportPlan] = {}

    # -- health & config ----------------------------------------------------

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        """Liveness probe reporting the bind posture (unprotected)."""
        bind_address = access_controller.bind_address if access_controller is not None else "127.0.0.1"
        return {
            "status": "ok",
            "bind_address": bind_address,
            "access_protection": bool(access_controller and access_controller.protection_enabled),
        }

    @app.get("/api/config", dependencies=protected)
    async def get_config() -> Dict[str, Any]:
        """Return a non-secret summary of the active configuration (Req 20.2)."""
        if config is None:
            return {"configured": False}
        return {
            "configured": True,
            "llm_provider": config.llm.provider,
            "token_budget": config.cost.token_budget,
            "rate_limit_per_min": config.cost.rate_limit_per_min,
            "bind_address": config.network.bind_address,
            "port": config.network.port,
            "access_protection": config.access.protection_enabled,
            "production_sources": [src.id for src in config.production_sources],
        }

    # -- session lifecycle --------------------------------------------------

    @app.post("/api/session", dependencies=protected)
    async def start_session(body: StartSessionRequest) -> Dict[str, Any]:
        """Start a session by entry mode and return its initial state (Req 24)."""
        try:
            state = orchestrator.start_session(
                body.entry_mode,
                session_id=body.session_id,
                user_id=body.user_id,
                plugin_name=body.plugin_name,
                source=body.source,
                production_plugin=body.production_plugin,
            )
        except EntryModeError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
        return _serialize_session(state, orchestrator.token_total(body.session_id))

    @app.get("/api/session/{session_id}", dependencies=protected)
    async def get_session(session_id: str) -> Dict[str, Any]:
        """Return the current session state (Req 1.4, 3.6, 5)."""
        state = _lookup_session(orchestrator, session_id)
        return _serialize_session(state, orchestrator.token_total(session_id))

    @app.get("/api/session/{session_id}/tokens", dependencies=protected)
    async def get_tokens(session_id: str) -> Dict[str, Any]:
        """Return the session's cumulative usage (Req 3.6).

        Both figures are reported because they mean different things: the token
        total governs the budget but is an estimate, while credits are what the
        Kiro CLI actually measured. ``credits_reported`` says whether any figure
        was seen, so zero spend is distinguishable from unknown spend.
        """
        state = _lookup_session(orchestrator, session_id)
        return {
            "session_id": session_id,
            "token_total": orchestrator.token_total(session_id),
            "token_total_is_estimate": True,
            "credits_spent": round(state.credits_spent, 4),
            "credits_reported": state.credits_reported,
        }

    @app.get("/api/session/{session_id}/visualization", dependencies=protected)
    async def get_visualization(session_id: str) -> Dict[str, Any]:
        """Return the visualization view-model for the draft (Req 5.1, 5.2, 5.5)."""
        state = _lookup_session(orchestrator, session_id)
        return _serialize_visualization(state.spec)

    @app.post("/api/session/{session_id}/message", dependencies=protected)
    async def submit_message(session_id: str, body: SubmitMessageRequest) -> Dict[str, Any]:
        """Submit a conversation message and return the turn result (Req 1)."""
        _lookup_session(orchestrator, session_id)
        result = await orchestrator.submit_message(session_id, body.text)
        return _serialize_turn_result(result)

    # -- export -------------------------------------------------------------

    @app.post("/api/session/{session_id}/export/prepare", dependencies=protected)
    async def prepare_export(session_id: str) -> Dict[str, Any]:
        """Compute the reviewable export preview without exporting (Req 12, 16)."""
        _lookup_session(orchestrator, session_id)
        try:
            plan = await orchestrator.prepare_export(session_id)
        except RegistryAccessError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        export_plans[session_id] = plan
        return _serialize_export_plan(plan)

    @app.post("/api/session/{session_id}/export/confirm", dependencies=protected)
    async def confirm_export(session_id: str, body: ConfirmExportRequest) -> Dict[str, Any]:
        """Confirm/decline the preview and run the build + export (Req 16.5, 9, 10)."""
        _lookup_session(orchestrator, session_id)
        plan = export_plans.get(session_id)
        if plan is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No prepared export for this session; call export/prepare first.",
            )
        # Allow force-export to bypass the validation gate.
        if body.force:
            # ExportPlan is a frozen dataclass, so a plain attribute assignment
            # raises FrozenInstanceError. object.__setattr__ is the supported
            # way to set the override that confirm_export reads back with
            # getattr(plan, "_force", False).
            object.__setattr__(plan, "_force", True)
        credentials = None
        if body.target == "tenant":
            if not body.region_base_url or not body.api_key:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Tenant export requires region_base_url and api_key.",
                )
            credentials = TenantCredentials(region_base_url=body.region_base_url, api_key=body.api_key)
        try:
            outcome = await orchestrator.confirm_export(
                session_id,
                plan,
                confirmed=body.confirmed,
                target=body.target,
                credentials=credentials,
                output_dir=body.output_dir,
            )
        except OrchestratorError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
        return _serialize_export_outcome(outcome)

    # -- history / registry -------------------------------------------------

    @app.get("/api/plugins", dependencies=protected)
    async def list_plugins() -> Dict[str, Any]:
        """List previously created plugins from the registry (Req 11.4, 21.4)."""
        if registry is None:
            return {"plugins": []}
        return {
            "plugins": [
                {
                    "name": rec.plugin_name,
                    "vendor": rec.vendor,
                    "version": rec.version,
                    "created_utc": rec.created_utc,
                }
                for rec in registry.list_plugins()
            ]
        }

    @app.get("/api/plugins/{plugin_name}/history", dependencies=protected)
    async def plugin_history(plugin_name: str) -> Dict[str, Any]:
        """Return a plugin's version/export history, most-recent-first (Req 11.3)."""
        if registry is None:
            return {"plugin_name": plugin_name, "history": []}
        return {
            "plugin_name": plugin_name,
            "history": [
                {
                    "kind": entry.kind,
                    "version": entry.version,
                    "target": entry.target,
                    "result": entry.result,
                    "timestamp": entry.timestamp,
                }
                for entry in registry.history(plugin_name)
            ],
        }

    # -- production sources (Req 24.4, 25) ----------------------------------

    @app.get("/api/sources", dependencies=protected)
    async def list_sources() -> Dict[str, Any]:
        """List available read-only production sources (Req 25.1)."""
        if source_provider is None:
            return {"sources": []}
        return {
            "sources": [
                {"id": s.id, "available": getattr(s, "available", True)} for s in source_provider.list_sources()
            ]
        }

    @app.get("/api/sources/{source_id}/plugins", dependencies=protected)
    async def list_source_plugins(source_id: str) -> Dict[str, Any]:
        """List plugins available for import from a production source (Req 25.1)."""
        if source_provider is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No production source provider configured."
            )
        refs = source_provider.list_plugins(source_id)
        return {
            "source_id": source_id,
            "plugins": [{"name": ref.name, "version": getattr(ref, "version", None)} for ref in refs],
        }

    # -- managed-tooling updates (Req 23) -----------------------------------

    @app.get("/api/updates", dependencies=protected)
    async def get_updates() -> Dict[str, Any]:
        """Return a non-blocking, cached upstream update check (Req 23.3, 23.4)."""
        if update_manager is None:
            return {"available": False, "checked": False}
        result = update_manager.check_upstream()
        return {
            "checked": True,
            "performed": result.performed,
            "from_cache": result.from_cache,
            "skipped": result.skipped,
        }

    # -- WebSocket channel (Req 5.3) ----------------------------------------

    @app.websocket("/ws/{session_id}")
    async def session_channel(
        websocket: WebSocket, session_id: str, passphrase: Optional[str] = Query(default=None)
    ) -> None:
        """Stream draft state, the token counter, and visualization updates.

        On connect the current state is pushed; each ``submit_message`` frame
        applies a turn and pushes the updated draft state, token counter, and
        visualization (Req 1.4, 3.6, 5.3). When access protection is enabled the
        connection is rejected unless ``passphrase`` grants access (Req 17.2).
        """
        if access_controller is not None and access_controller.protection_enabled:
            if not access_controller.authenticate(passphrase).granted:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

        await websocket.accept()
        try:
            state = orchestrator.session(session_id)
        except SessionNotFoundError:
            await websocket.send_json({"type": "error", "detail": f"unknown session {session_id!r}"})
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await websocket.send_json(
            {"type": "state", "state": _serialize_session(state, orchestrator.token_total(session_id))}
        )

        try:
            while True:
                frame = await websocket.receive_json()
                if not isinstance(frame, dict) or frame.get("type") != "submit_message":
                    await websocket.send_json({"type": "error", "detail": "expected a submit_message frame"})
                    continue
                text = frame.get("text", "")
                # Interpret the user's natural-language message into a TurnPlan
                # via the Kiro CLI, then pass it to the orchestrator.
                plan = None
                attachments = frame.get("attachments") or []
                if attachments:
                    # Retained on the session so the delegated agent can read the
                    # real documents from the project tree during implementation,
                    # not just so the interpreter can derive spec structure now.
                    state = orchestrator.session(session_id)
                    for attachment in attachments:
                        if not isinstance(attachment, dict):
                            continue
                        state.attachments.append(
                            {
                                "name": str(attachment.get("name") or "reference"),
                                "content": str(attachment.get("content") or ""),
                                # Carried through so a PDF or other binary document
                                # survives the JSON hop and can have its text
                                # extracted before the agent reads it (Req 28.11).
                                "encoding": str(attachment.get("encoding") or ""),
                                "media_type": str(attachment.get("media_type") or ""),
                            }
                        )
                # Documentation URLs are retrieved by this process, never by the
                # agent: a fetched page is untrusted content and the agent runs
                # with shell access (Req 28.10).
                reference_urls = frame.get("reference_urls") or []
                if reference_urls:
                    state = orchestrator.session(session_id)
                    for url in reference_urls:
                        if isinstance(url, str) and url.strip():
                            state.reference_urls.append(url.strip())
                if interpreter is not None:
                    try:
                        current_spec = orchestrator.session(session_id).spec
                        await websocket.send_json({"type": "status", "message": "Interpreting your request..."})
                        plan = await interpreter.interpret(text, current_spec, attachments=attachments)
                    except Exception as interpret_err:
                        # Interpretation failure: surface as an error frame but
                        # don't crash the connection; fall through with plan=None
                        # which will ask for clarification.
                        await websocket.send_json({"type": "error", "detail": f"Interpretation error: {interpret_err}"})
                        continue
                if plan and plan.reasoning:
                    await websocket.send_json(
                        {"type": "status", "message": f"Generating logic for {len(plan.reasoning)} action(s)..."}
                    )
                elif plan and plan.operations:
                    await websocket.send_json(
                        {"type": "status", "message": f"Applying {len(plan.operations)} operation(s)..."}
                    )
                result = await orchestrator.submit_message(session_id, text, plan)
                await websocket.send_json({"type": "turn", "result": _serialize_turn_result(result)})
                await websocket.send_json({"type": "tokens", "token_total": result.token_total})
                current = orchestrator.session(session_id)
                await websocket.send_json(
                    {"type": "visualization", "visualization": _serialize_visualization(current.spec)}
                )
        except WebSocketDisconnect:
            return

    # -- static UI assets ---------------------------------------------------

    if static_dir is not None:
        static_path = Path(static_dir)
        if static_path.is_dir():
            app.mount("/", StaticFiles(directory=str(static_path), html=True), name="ui")

    return app


def _lookup_session(orchestrator: Orchestrator, session_id: str) -> SessionState:
    """Return the session state or raise a 404 :class:`HTTPException`."""
    try:
        return orchestrator.session(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


# ---------------------------------------------------------------------------
# Config-driven assembly and console entry point
# ---------------------------------------------------------------------------


def create_app_from_config(config: AppConfig, *, static_dir: Optional[Any] = None) -> FastAPI:
    """Assemble a self-contained app from a validated :class:`AppConfig`.

    Builds the persistence-backed collaborators the local tool can stand up
    without external tooling (the plugin registry and project-folder-backed
    orchestrator) and the optional access guard, then constructs the app. The
    costly externals (Kiro CLI, Docker, tenant API) are intentionally left
    unwired here -- the build/export path is gated until they are supplied --
    so the server starts self-contained on loopback (Req 20.1).

    Args:
        config: the loaded, validated startup configuration.
        static_dir: optional pre-built UI asset directory to serve at ``/``.

    Returns:
        A configured :class:`FastAPI` application.
    """
    from ..integrations.agent_config import AgentConfigError, missing_resources, write_agent_config
    from ..integrations.build_engine import BuildEngine
    from ..integrations.build_prep import resolve_target_python
    from ..integrations.code_validator import CodeValidator
    from ..integrations.export_manager import ExportManager
    from ..integrations.insight_plugin_cli import InsightPluginCli
    from ..integrations.llm_generator import LLMGenerator
    from ..integrations.plugin_agent import PluginAgent
    from ..integrations.quality_gate import QualityGate
    from ..integrations.refresh_coordinator import RefreshCoordinator
    from ..orchestrator.repair_loop import RepairLoop
    from ..orchestrator.interpreter import Interpreter
    from ..persistence.audit_log import AuditLog
    from ..persistence.registry import PluginRegistry

    projects_root = Path(config.paths.projects_root).expanduser()
    projects_root.mkdir(parents=True, exist_ok=True)
    config_root = Path(config.paths.config_root).expanduser()
    config_root.mkdir(parents=True, exist_ok=True)

    # Persistence
    registry = PluginRegistry(config_root / "registry.db")
    audit_log = AuditLog(config_root / "audit.log")

    # Cost control
    cost_controller = _build_cost_controller(config)

    # LLM generation for prose reasoning artifacts (field descriptions, help text)
    llm_generator = LLMGenerator(cost_controller, executable=config.llm.kiro_cli_path)

    # Delegated plugin implementation: the Kiro CLI run as an agent in the
    # plugin's working tree, with the operator's plugin skills as its rulebook.
    # Registering the agent config is best-effort -- a failure here must not stop
    # the server from starting, because everything except code implementation
    # still works without it.
    try:
        write_agent_config()
    except AgentConfigError as error:  # pragma: no cover - filesystem dependent
        logger.warning("could not register the plugin-builder agent config: %s", error)
    absent = missing_resources()
    if absent:
        logger.warning(
            "the plugin-builder agent will run with reduced guidance; "
            "these plugin skills/steering files are not installed: %s",
            ", ".join(absent),
        )
    plugin_agent = PluginAgent(cost_controller, executable=config.llm.kiro_cli_path)

    # A generated plugin imports the InsightConnect SDK at module scope, and the
    # SDK lives in the toolchain's target interpreter rather than in this tool's
    # environment. Running a plugin's tests with this process's own interpreter
    # would fail on the import instead of on anything about the plugin, so the
    # target is resolved (never hardcoded) per the build-prep workflow.
    resolved_python = resolve_target_python()
    target_python = resolved_python.executable or "python3"
    if not resolved_python.is_target_series:
        logger.warning("plugin unit tests will run under an unverified interpreter: %s", resolved_python.detail)

    # insight-plugin CLI: deterministic scaffolding for a net-new plugin, and
    # refresh of derived files after a structural edit. The same wrapper serves
    # both; it is passed as the scaffolder so a net-new tree is produced by
    # `insight-plugin create` (current icon_ prefix) rather than by refreshing a
    # bare directory (legacy komand_ prefix).
    insight_plugin_cli = InsightPluginCli(executable="insight-plugin")
    refresh_coordinator = RefreshCoordinator(cli=insight_plugin_cli)

    # Code validation (prospector + Docker build/test + insight-plugin validate).
    # The lint stage runs the same linter under the same profile as the
    # Quality_Gate, so the two cannot disagree about whether a plugin is clean.
    code_validator = CodeValidator(
        prospector_executable="prospector",
        docker_executable="docker",
        insight_plugin_executable="insight-plugin",
        # icon_validator lives with the plugin toolchain, not in this tool's
        # environment, so the validate stage runs under the same interpreter the
        # plugin's own tests do.
        validate_python_executable=target_python,
    )

    # One quality gate, shared by the repair loop (implementation path) and the
    # orchestrator (export path). It holds no state, and sharing it keeps both
    # paths judging the code by the same checks and the same interpreter.
    quality_gate = QualityGate(python_executable=target_python)

    # Build + export
    build_engine = BuildEngine()
    export_manager = ExportManager(build_engine=build_engine)

    # NL interpretation layer
    interpreter = Interpreter(executable=config.llm.kiro_cli_path)

    # Orchestrator with all collaborators
    orchestrator = Orchestrator(
        cost_controller=cost_controller,
        llm_generator=llm_generator,
        plugin_agent=plugin_agent,
        scaffolder=insight_plugin_cli,
        repair_loop=RepairLoop(quality_gate, max_rounds=config.cost.max_repair_rounds),
        quality_gate=quality_gate,
        refresh_coordinator=refresh_coordinator,
        code_validator=code_validator,
        build_engine=build_engine,
        export_manager=export_manager,
        registry=registry,
        audit_log=audit_log,
        projects_root=projects_root,
    )
    access_controller = AccessController(config.access, network_config=config.network)

    return create_app(
        orchestrator=orchestrator,
        config=config,
        access_controller=access_controller,
        registry=registry,
        interpreter=interpreter,
        static_dir=static_dir,
    )


def _build_cost_controller(config: AppConfig) -> Any:
    """Build a :class:`CostController` from the config's cost limits (Req 4)."""
    from ..core.cost_controller import CostController

    return CostController(
        token_budget=config.cost.token_budget,
        rate_limit=config.cost.rate_limit_per_min,
    )


def _default_config_path() -> Path:
    """Return the config file path from the environment or the default location."""
    override = os.environ.get("ICPLUGIN_BUILDER_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path("~/.icplugin-builder/config.yaml").expanduser()


def _default_ui_dir() -> Optional[Path]:
    """Resolve the pre-built UI asset directory to serve at ``/`` (Req 20.1).

    Resolution order, first existing wins:

    1. ``$ICPLUGIN_BUILDER_UI_DIR`` -- an explicit operator override.
    2. ``icplugin_builder/ui`` -- the UI bundled inside the installed package
       (the design ships the built UI inside the distribution).
    3. ``frontend/dist`` at the repository root -- the ``vite build`` output,
       used when running from a source checkout.

    Returns the first directory that exists, or ``None`` when no built UI is
    present (the API still runs; only the static mount is skipped).
    """
    override = os.environ.get("ICPLUGIN_BUILDER_UI_DIR")
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_dir() else None

    # Bundled-in-package location: icplugin_builder/ui (this module lives at
    # icplugin_builder/api/app.py, so the package root is two parents up).
    package_ui = Path(__file__).resolve().parent.parent / "ui"
    if package_ui.is_dir():
        return package_ui

    # Source-checkout location: <repo>/frontend/dist (vite build output).
    repo_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if repo_dist.is_dir():
        return repo_dist

    return None


def main() -> None:
    """Console entry point: load config, build the app, and serve on loopback.

    Reads the configuration from ``$ICPLUGIN_BUILDER_CONFIG`` (or the default
    ``~/.icplugin-builder/config.yaml``), locates the pre-built UI assets (see
    :func:`_default_ui_dir`), builds the self-contained app, and runs Uvicorn
    bound to the configured address -- loopback by default -- so the operator
    launches a single process serving both the API and the UI (Req 17.4, 20.1).
    """
    import uvicorn

    config = load_config(_default_config_path())

    ui_dir = _default_ui_dir()
    if ui_dir is None:
        # Without a built UI the API still runs, but "/" returns a bare 404 and
        # the printed URL looks broken. Say so, and name the fix.
        print(
            "icplugin-builder: no built UI found, so the web interface will not be served.\n"
            "                  The API is still available under /api.\n"
            "                  Build it with:  cd frontend && npm run build\n"
            "                  Or point $ICPLUGIN_BUILDER_UI_DIR at an existing bundle.",
            flush=True,
        )

    app = create_app_from_config(config, static_dir=ui_dir)
    uvicorn.run(app, host=config.network.bind_address, port=config.network.port)


if __name__ == "__main__":  # pragma: no cover - module executed as a script
    main()
