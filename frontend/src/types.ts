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
  /**
   * True when this re-states the phase already announced rather than reporting a
   * new one (clause 2.19). A phase starting happens once and belongs in the
   * transcript; a re-statement is the same phase still running, and the backend
   * emits one every second, so appending them accumulates hundreds of entries.
   */
  progress?: boolean;
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
/** Whether a definition-of-done condition holds, or could not be checked. */
export type ConditionStatus = "met" | "unmet" | "unverified";

/**
 * One outstanding definition-of-done condition (Req 27.2). Serialized by
 * `_serialize_done_conditions`, which sends only the shortfalls.
 */
export interface DoneCondition {
  name: string;
  status: ConditionStatus;
  description: string;
  detail: string;
}

export interface ExportPlan {
  /** True iff the spec is valid and all four code stages passed (Req 7.4, 8.6). */
  permitted: boolean;
  summary: string;
  /**
   * Whether the plugin meets every definition-of-done condition (Req 27.1).
   * `null` means the definition of done was not evaluated, which is not the same
   * as `false`. Independent of `permitted`: the export gate weighs four stages,
   * this weighs whether the plugin is finished.
   */
  plugin_is_done: boolean | null;
  /** The conditions still outstanding, empty when the plugin is done. */
  done_conditions: DoneCondition[];
  /** The vendor-suffixed, version-bumped spec that would be exported (Req 16.1). */
  spec_preview: Record<string, unknown> | null;
  /** The exact files that would be included in the `.plg` (Req 16.2). */
  /**
   * The plugin files the artifact's image will be built from (Req 16.2). The build
   * context, not a manifest of the archive's members: a `.plg` is a `docker save` of the
   * image, and the plugin's own `.dockerignore` may keep some of these out of it.
   */
  file_list: string[];
  /** What the export would produce, or `null` when the spec cannot yet form an identity. */
  artifact: PlannedArtifact | null;
  /** The prior-version diff, or a first-version diff (Req 16.3, 16.4). */
  diff: FileTreeDiff;
  /** "<previous> -> <new>" when the version changed; empty when unchanged. */
  version_display: string;
  spec_errors: SpecError[];
  /**
   * The stages that failed, each with what it printed (clause 2.16). Serialized by
   * `_serialize_failed_stages`. A stage name alone is not actionable -- "lint
   * failed" does not say which finding in which file -- so each entry carries the
   * stage's message and its output under the same truncated-plus-full rule the
   * build/export failure path uses (Req 19.5).
   *
   * Only `name` is guaranteed: when a spec never reached the code stages there is
   * no pipeline report, and the backend falls back to the gate decision's names.
   */
  failed_stages: FailedStage[];
  /** Which linter profile and width judged this plugin (clause 2.8). */
  lint_bar?: LintBar | null;
}

/** One failing validation stage and what it printed (clause 2.16). */
export interface FailedStage {
  /** The stage's name: "lint", "build", "test" or "validate". */
  name: string;
  status?: string;
  returncode?: number | null;
  /** Why the stage failed, in one line. */
  message?: string;
  /** The stage's output, bounded to the first 10,000 characters (Req 19.5). */
  displayed_output?: string;
  /** The complete output, retained however long it is (Req 19.5). */
  full_output?: string;
  truncated?: boolean;
}

/**
 * The bar the lint stage applied (clause 2.8). Two operators with different
 * `insightconnect-plugins` checkouts can be held to different profiles, so the
 * preview says which one judged this plugin and at what width.
 */
export interface LintBar {
  profile_path: string | null;
  line_length: number | null;
  profile_is_authoritative?: boolean;
}

/**
 * What an export would produce (Req 16.2).
 *
 * A `.plg` is a gzipped `docker save` of the plugin's image, so the identity a tenant
 * reads is the image tag rather than the filename or the source file list.
 */
export interface PlannedArtifact {
  /** `<vendor>/<name>:<version>`, with the `_custom` vendor suffix applied. */
  image_tag: string;
  /** `<vendor>_<name>_<version>.plg`, matching what `insight-plugin export` writes. */
  filename: string;
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
