/**
 * Shared TypeScript mirrors of the backend's JSON serialization
 * (`icplugin_builder/api/app.py`, `icplugin_builder/orchestrator/session.py`).
 *
 * This is the single shared types module the whole UI imports from: the
 * conversation interface (session + chat + WebSocket frames), the
 * visualization view (view-model payload), and the app shell (entry modes).
 * Keeping these aligned with the server lets each panel render live state
 * without transforming the domain model.
 */

// ---------------------------------------------------------------------------
// Entry modes and session state
// ---------------------------------------------------------------------------

/** The three entry modes offered at session start (Req 24.1). */
export type EntryMode = "create_new" | "iterate_custom" | "enhance_production";

/**
 * A session's public state as serialized by `_serialize_session`.
 *
 * `spec` is the draft spec as a plain mapping (`PluginSpec.to_mapping()`), and
 * `visualization` is the current view-model payload the graph renders.
 */
export interface SessionState {
  session_id: string;
  entry_mode: EntryMode;
  plugin_name: string | null;
  /** Usage-restriction notice when forked from the private repo (Req 25.6). */
  private_source_notice: string | null;
  spec: Record<string, unknown> | null;
  /** Cumulative session token total (Req 3.6). */
  token_total: number;
  visualization: VisualizationPayload;
}

// ---------------------------------------------------------------------------
// Conversation turn results
// ---------------------------------------------------------------------------

/** The outcome category of a conversation turn (mirrors `TurnStatus`). */
export type TurnStatus = "applied" | "rejected_input" | "not_found" | "clarification" | "failed";

/** One artifact produced during a turn (mirrors `GeneratedArtifact`). */
export interface GeneratedArtifact {
  kind: string;
  content: string;
  from_llm: boolean;
  tokens: number;
}

/** The result of submitting one conversation turn (mirrors `TurnResult`). */
export interface TurnResult {
  status: TurnStatus;
  message: string;
  spec: Record<string, unknown> | null;
  generated: GeneratedArtifact[];
  refreshed: boolean;
  structural_reasons: string[];
  /** Cumulative session token total after the turn (Req 3.6). */
  token_total: number;
}

// ---------------------------------------------------------------------------
// Visualization view-model (Req 5)
// ---------------------------------------------------------------------------

/** The render outcome class produced by the backend view-model fallback layer. */
export type VisualizationState = "ok" | "empty" | "parse_error";

/** Node-kind discriminator: matches `CONNECTION`/`ACTION`/`TRIGGER`/`TASK`. */
export type NodeKind = "connection" | "action" | "trigger" | "task";

/** A single input/output/connection field prepared for display (Req 5.2, 5.4). */
export interface FieldView {
  name: string;
  type: string;
  required: boolean;
  title: string | null;
  description: string | null;
}

/** A single graph node: the connection, an action, a trigger, or a task (Req 5.1). */
export interface NodeView {
  node_id: string;
  kind: NodeKind;
  name: string;
  title: string | null;
  description: string | null;
  input: FieldView[];
  output: FieldView[];
}

/**
 * The whole visualization payload streamed by the backend (Req 5.1, 5.2, 5.5, 5.6).
 *
 * On a parse failure the backend sets `state === "parse_error"`, populates
 * `error`, and returns the *retained* last valid nodes so the UI keeps showing
 * the last good graph.
 */
export interface VisualizationPayload {
  state: VisualizationState;
  message: string | null;
  error: string | null;
  nodes: NodeView[];
}

// ---------------------------------------------------------------------------
// WebSocket frames (/ws/{session_id})
// ---------------------------------------------------------------------------

/** A file attached to a message (e.g. an API spec for the LLM to digest). */
export interface MessageAttachment {
  /** Original filename (e.g. "openapi.yaml"). */
  name: string;
  /** The full text content of the file. */
  content: string;
}

/** The single outbound frame the backend accepts on the session channel. */
export interface WsSubmitMessageFrame {
  type: "submit_message";
  text: string;
  /** Optional file attachments (API specs, schemas) sent alongside the message. */
  attachments?: MessageAttachment[];
}

/** Pushed on connect: the full session state (`_serialize_session`). */
export interface WsStateFrame {
  type: "state";
  state: SessionState;
}

/** Pushed after an applied turn: the turn result (`_serialize_turn_result`). */
export interface WsTurnFrame {
  type: "turn";
  result: TurnResult;
}

/** Pushed after a turn: the cumulative token total (Req 3.6). */
export interface WsTokensFrame {
  type: "tokens";
  token_total: number;
}

/** Pushed after a turn: the refreshed visualization payload (Req 5.3). */
export interface WsVisualizationFrame {
  type: "visualization";
  visualization: VisualizationPayload;
}

/** Pushed on an error (e.g. unknown session). */
export interface WsErrorFrame {
  type: "error";
  detail: string;
}

/** Pushed as a progress indicator while interpretation/generation is running. */
export interface WsStatusFrame {
  type: "status";
  message: string;
}

/** Every inbound frame the session channel can deliver. */
export type WsInboundFrame =
  | WsStateFrame
  | WsTurnFrame
  | WsTokensFrame
  | WsVisualizationFrame
  | WsErrorFrame
  | WsStatusFrame;

// ---------------------------------------------------------------------------
// Preview / diff / export (Req 12, 16, 19) -- serialized by
// `_serialize_export_plan`, `_serialize_export_outcome`, and `_serialize_failure`.
// ---------------------------------------------------------------------------

/** The added/removed/modified file partition versus a prior exported version. */
export interface FileTreeDiff {
  added: string[];
  removed: string[];
  modified: string[];
  /** True when no prior version exists; all files are additions (Req 16.4). */
  first_version: boolean;
}

/** A single spec-validation error (Req 7.2). */
export interface SpecError {
  path: string;
  message: string;
}

/**
 * The reviewable export preview computed by the backend before an export runs
 * (Req 12, 16). Serialized by `_serialize_export_plan`.
 */
export interface ExportPlan {
  /** True iff the spec is valid and all four code stages passed (Req 7.4, 8.6). */
  permitted: boolean;
  summary: string;
  /** The vendor-suffixed, version-bumped spec that would be exported (Req 16.1). */
  spec_preview: Record<string, unknown> | null;
  /** The exact files that would be included in the `.plg` (Req 16.2). */
  file_list: string[];
  /** The prior-version diff, or a first-version diff (Req 16.3, 16.4). */
  diff: FileTreeDiff;
  /** "<previous> -> <new>" when the version changed; empty when unchanged. */
  version_display: string;
  spec_errors: SpecError[];
  failed_stages: string[];
}

/** Whether a failure came from the build or the export phase (Req 19.4). */
export type FailureKind = "build" | "export";

/**
 * A build/export failure indication (Req 19.1, 19.4, 19.5). Serialized by
 * `_serialize_failure`.
 */
export interface FailureIndication {
  kind: FailureKind | null;
  failing_step: string | null;
  /** Bounded to the first 10,000 characters (Req 19.5). */
  displayed_output: string | null;
  /** The complete error output, always accessible (Req 19.5). */
  full_output: string | null;
  /** True when `displayed_output` is a truncated prefix of `full_output`. */
  truncated: boolean;
}

/** The outcome category of an export attempt (mirrors `ExportStatus`). */
export type ExportStatus =
  | "succeeded"
  | "aborted"
  | "blocked"
  | "build_failed"
  | "export_failed";

/** The result of confirming and running an export (`_serialize_export_outcome`). */
export interface ExportOutcome {
  status: ExportStatus;
  message: string;
  artifact_path: string | null;
  version: string | null;
  target: string | null;
  failure: FailureIndication | null;
  /** A failed tenant export's retained `.plg` path, kept >=24h (Req 19.2). */
  retained_artifact_path: string | null;
}

/** The export target selected by the operator. */
export type ExportTarget = "local" | "tenant";

/** Body for confirming or declining an export (matches ConfirmExportRequest). */
export interface ConfirmExportBody {
  confirmed: boolean;
  target: ExportTarget;
  output_dir?: string | null;
  region_base_url?: string | null;
  api_key?: string | null;
  force?: boolean;
}
