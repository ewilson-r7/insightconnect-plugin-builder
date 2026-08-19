// The preview/diff/confirm and build/export controls (task 23.3).
//
// This panel drives the two-step export flow the backend exposes:
//   1. Prepare  -> compute the reviewable preview (spec, packaged file list,
//      prior-version diff, version bump, and the validation gate decision)
//      without exporting (Req 16.1-16.4).
//   2. Confirm  -> only after the operator explicitly confirms the preview does
//      it build and export (Req 16.5). Declining/cancelling aborts with no
//      artifact and leaves the draft unchanged (Req 16.6).
//
// When a build or export fails the outcome is rendered by FailureIndicator,
// which distinguishes build from export failures (Req 19.4) and shows the
// failing step's error output with truncated-plus-full access (Req 19.5).

import { useCallback, useMemo, useState } from "react";
import {
  confirmExport as defaultConfirmExport,
  prepareExport as defaultPrepareExport,
  type ClientOptions,
} from "../../api/client";
import type {
  ConfirmExportBody,
  ExportOutcome,
  ExportPlan,
  ExportTarget,
} from "../../types";
import { DiffView } from "./DiffView";
import { ErrorOutput } from "./ErrorOutput";
import { FailureIndicator } from "./FailureIndicator";
import { FileList } from "./FileList";
import { SpecPreview } from "./SpecPreview";

/** Injectable client surface so the panel is testable without a live backend. */
export interface ExportClient {
  prepareExport(sessionId: string, options?: ClientOptions): Promise<ExportPlan>;
  confirmExport(
    sessionId: string,
    body: ConfirmExportBody,
    options?: ClientOptions,
  ): Promise<ExportOutcome>;
}

export interface ExportPanelProps {
  sessionId: string;
  /** Forwarded to the client for protected instances (Req 17.1). */
  passphrase?: string | null;
  /** Overridable for tests; defaults to the real fetch client. */
  client?: ExportClient;
}

type Phase = "idle" | "preparing" | "previewing" | "exporting" | "done";

const defaultClient: ExportClient = {
  prepareExport: defaultPrepareExport,
  confirmExport: defaultConfirmExport,
};

/** Preview/diff/confirm + build/export controls for a session (task 23.3). */
export function ExportPanel({
  sessionId,
  passphrase,
  client = defaultClient,
}: ExportPanelProps): JSX.Element {
  const [phase, setPhase] = useState<Phase>("idle");
  const [plan, setPlan] = useState<ExportPlan | null>(null);
  const [outcome, setOutcome] = useState<ExportOutcome | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Explicit preview confirmation is required before export (Req 16.5).
  const [confirmed, setConfirmed] = useState(false);
  const [target, setTarget] = useState<ExportTarget>("local");
  const [outputDir, setOutputDir] = useState("");
  const [regionBaseUrl, setRegionBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");

  const options: ClientOptions | undefined = useMemo(
    () => (passphrase ? { passphrase } : undefined),
    [passphrase],
  );

  const resetPreview = useCallback(() => {
    setPlan(null);
    setOutcome(null);
    setConfirmed(false);
    setError(null);
    setPhase("idle");
  }, []);

  const handlePrepare = useCallback(async () => {
    setError(null);
    setOutcome(null);
    setConfirmed(false);
    setPhase("preparing");
    try {
      const nextPlan = await client.prepareExport(sessionId, options);
      setPlan(nextPlan);
      setPhase("previewing");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("idle");
    }
  }, [client, sessionId, options]);

  const handleConfirm = useCallback(async () => {
    if (!plan || !plan.permitted || !confirmed) {
      return;
    }
    setError(null);
    setPhase("exporting");
    const body: ConfirmExportBody = {
      confirmed: true,
      target,
      output_dir: target === "local" ? outputDir || null : null,
      region_base_url: target === "tenant" ? regionBaseUrl || null : null,
      api_key: target === "tenant" ? apiKey || null : null,
    };
    try {
      const result = await client.confirmExport(sessionId, body, options);
      setOutcome(result);
      setPhase("done");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("previewing");
    }
  }, [
    plan,
    confirmed,
    target,
    outputDir,
    regionBaseUrl,
    apiKey,
    client,
    sessionId,
    options,
  ]);

  // Declining/cancelling aborts the export with no artifact (Req 16.6). Because
  // no confirm request is sent, no `.plg` is produced and the draft is untouched.
  const handleCancel = useCallback(() => {
    resetPreview();
  }, [resetPreview]);

  const exportSucceeded = outcome?.status === "succeeded";
  const exportFailed =
    outcome != null &&
    (outcome.status === "build_failed" || outcome.status === "export_failed");
  const canConfirm = Boolean(plan?.permitted && confirmed && phase === "previewing");

  const handleForceExport = useCallback(async () => {
    setError(null);
    setPhase("exporting");
    const body: ConfirmExportBody = {
      confirmed: true,
      target: "local",
      output_dir: null,
      force: true,
    };
    try {
      const result = await client.confirmExport(sessionId, body, options);
      setOutcome(result);
      setPhase("done");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("previewing");
    }
  }, [client, sessionId, options]);

  return (
    <div aria-label="Export controls" data-testid="export-panel">
      <div>
        <button
          type="button"
          onClick={handlePrepare}
          disabled={phase === "preparing" || phase === "exporting"}
          data-testid="prepare-export"
        >
          {phase === "preparing" ? "Preparing preview..." : "Review before export"}
        </button>
      </div>

      {error ? (
        <p role="alert" data-testid="request-error">
          {error}
        </p>
      ) : null}

      {plan && phase !== "done" ? (
        <div data-testid="export-preview">
          <SpecPreview
            spec={plan.spec_preview}
            versionDisplay={plan.version_display}
          />
          <FileList files={plan.file_list} />
          <DiffView diff={plan.diff} />

          {/* Shown on both branches. A permitted export says only that the four
              gate stages passed, so without this a plugin with no API client or a
              stubbed connection test would present as ready (Req 27.2, 27.3). */}
          <DefinitionOfDoneNotice plan={plan} />

          {plan.permitted ? (
            <ExportConfirmControls
              confirmed={confirmed}
              onConfirmedChange={setConfirmed}
              target={target}
              onTargetChange={setTarget}
              outputDir={outputDir}
              onOutputDirChange={setOutputDir}
              regionBaseUrl={regionBaseUrl}
              onRegionBaseUrlChange={setRegionBaseUrl}
              apiKey={apiKey}
              onApiKeyChange={setApiKey}
              canConfirm={canConfirm}
              exporting={phase === "exporting"}
              onConfirm={handleConfirm}
              onCancel={handleCancel}
            />
          ) : (
            <BlockedNotice plan={plan} onCancel={handleCancel} onForceExport={handleForceExport} />
          )}
        </div>
      ) : null}

      {outcome && phase === "done" ? (
        <div data-testid="export-outcome">
          {exportSucceeded ? (
            <section role="status" data-testid="export-success">
              <h3>Export succeeded</h3>
              {outcome.message ? <p>{outcome.message}</p> : null}
              {outcome.version ? <p>Version: {outcome.version}</p> : null}
              {outcome.artifact_path ? (
                <p data-testid="export-artifact-path">
                  Artifact: <code>{outcome.artifact_path}</code>
                </p>
              ) : null}
            </section>
          ) : exportFailed ? (
            <FailureIndicator outcome={outcome} />
          ) : (
            <section role="status" data-testid="export-aborted">
              <h3>Export not completed</h3>
              {outcome.message ? <p>{outcome.message}</p> : null}
            </section>
          )}
          <button type="button" onClick={resetPreview} data-testid="export-reset">
            Start over
          </button>
        </div>
      ) : null}
    </div>
  );
}

interface ExportConfirmControlsProps {
  confirmed: boolean;
  onConfirmedChange: (value: boolean) => void;
  target: ExportTarget;
  onTargetChange: (value: ExportTarget) => void;
  outputDir: string;
  onOutputDirChange: (value: string) => void;
  regionBaseUrl: string;
  onRegionBaseUrlChange: (value: string) => void;
  apiKey: string;
  onApiKeyChange: (value: string) => void;
  canConfirm: boolean;
  exporting: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

function ExportConfirmControls(props: ExportConfirmControlsProps): JSX.Element {
  return (
    <section aria-label="Confirm export" data-testid="confirm-controls">
      <fieldset>
        <legend>Export target</legend>
        <label>
          <input
            type="radio"
            name="export-target"
            value="local"
            checked={props.target === "local"}
            onChange={() => props.onTargetChange("local")}
            data-testid="target-local"
          />
          Local `.plg`
        </label>
        <label>
          <input
            type="radio"
            name="export-target"
            value="tenant"
            checked={props.target === "tenant"}
            onChange={() => props.onTargetChange("tenant")}
            data-testid="target-tenant"
          />
          InsightConnect tenant
        </label>
      </fieldset>

      {props.target === "local" ? (
        <label>
          Output directory
          <input
            type="text"
            value={props.outputDir}
            onChange={(e) => props.onOutputDirChange(e.target.value)}
            placeholder="user-accessible directory (optional)"
            data-testid="output-dir"
          />
        </label>
      ) : (
        <div data-testid="tenant-credentials">
          <label>
            Region base URL
            <input
              type="text"
              value={props.regionBaseUrl}
              onChange={(e) => props.onRegionBaseUrlChange(e.target.value)}
              data-testid="region-base-url"
            />
          </label>
          <label>
            API key
            <input
              type="password"
              value={props.apiKey}
              onChange={(e) => props.onApiKeyChange(e.target.value)}
              data-testid="api-key"
            />
          </label>
        </div>
      )}

      <label data-testid="confirm-checkbox-label">
        <input
          type="checkbox"
          checked={props.confirmed}
          onChange={(e) => props.onConfirmedChange(e.target.checked)}
          data-testid="confirm-checkbox"
        />
        I have reviewed the preview and confirm this export.
      </label>

      <div>
        <button
          type="button"
          onClick={props.onConfirm}
          disabled={!props.canConfirm || props.exporting}
          data-testid="confirm-export"
        >
          {props.exporting ? "Exporting..." : "Confirm & export"}
        </button>
        <button
          type="button"
          onClick={props.onCancel}
          disabled={props.exporting}
          data-testid="cancel-export"
        >
          Cancel
        </button>
      </div>
    </section>
  );
}

function DefinitionOfDoneNotice({ plan }: { plan: ExportPlan }): JSX.Element | null {
  if (plan.plugin_is_done === null) {
    return null;
  }
  if (plan.plugin_is_done) {
    return (
      <section role="status" data-testid="done-met">
        <h4>Definition of done: met</h4>
        <p>Every condition was checked and holds.</p>
      </section>
    );
  }
  const unmet = plan.done_conditions.filter((c) => c.status === "unmet");
  const unverified = plan.done_conditions.filter((c) => c.status === "unverified");
  return (
    // A labelled region rather than `role="alert"`. An alert is assertive and
    // atomic, so this section -- a heading, a summary, and up to two nested lists
    // of conditions -- was announced as one uninterruptible blob with its
    // structure flattened, and it interrupted whatever the operator was reading.
    // The short summary is what warrants announcing; the detail is what warrants
    // being navigable by heading and list, which a region gives and an alert
    // takes away.
    <section
      role="region"
      aria-labelledby="done-outstanding-heading"
      data-testid="done-outstanding"
    >
      <h4 id="done-outstanding-heading">Definition of done: not met</h4>
      <p role="status">
        This plugin is not finished. Exporting it now ships it in this state.
      </p>
      {unmet.length > 0 ? (
        <div data-testid="done-unmet">
          <h5>Unmet</h5>
          <ul>
            {unmet.map((condition) => (
              <li key={condition.name}>
                <code>{condition.name}</code>: {condition.description}
                {condition.detail ? <> &mdash; {condition.detail}</> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {unverified.length > 0 ? (
        <div data-testid="done-unverified">
          {/* Not failures. Nothing is known about these, which is why they are
              listed apart from the ones that were checked and did not hold. */}
          <h5>Could not be checked</h5>
          <ul>
            {unverified.map((condition) => (
              <li key={condition.name}>
                <code>{condition.name}</code>: {condition.description}
                {condition.detail ? <> &mdash; {condition.detail}</> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

function BlockedNotice({
  plan,
  onCancel,
  onForceExport,
}: {
  plan: ExportPlan;
  onCancel: () => void;
  onForceExport: () => void;
}): JSX.Element {
  return (
    // Same reasoning as the outstanding-conditions region, and more pressing here:
    // this section now carries each failing stage's output, which the backend
    // bounds at 10,000 characters. An assertive atomic region would read all of it
    // before the operator could do anything, so the summary is the announcement and
    // the stages are navigable structure.
    <section
      role="region"
      aria-labelledby="export-blocked-heading"
      data-testid="export-blocked"
    >
      <h3 id="export-blocked-heading">Export blocked</h3>
      <p role="status">{plan.summary || "This draft cannot be exported yet."}</p>
      {plan.spec_errors.length > 0 ? (
        <div data-testid="blocked-spec-errors">
          <h4>Spec validation errors</h4>
          <ul>
            {plan.spec_errors.map((err) => (
              <li key={`${err.path}:${err.message}`}>
                <code>{err.path}</code>: {err.message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {plan.failed_stages.length > 0 ? (
        <div data-testid="blocked-failed-stages">
          <h4>Failed validation stages</h4>
          {/* A stage name alone sends the operator back to a terminal to find out
              what happened. Each entry carries the stage's own message and the
              output it printed, under the same truncated-plus-full rule the
              build/export failure path uses (clause 2.16, Req 19.5). */}
          <ul>
            {plan.failed_stages.map((stage) => (
              <li key={stage.name} data-testid={`failed-stage-${stage.name}`}>
                <strong>{stage.name}</strong>
                {stage.message ? <> &mdash; {stage.message}</> : null}
                {stage.displayed_output ? (
                  <ErrorOutput
                    failure={{
                      displayed_output: stage.displayed_output,
                      full_output: stage.full_output ?? stage.displayed_output,
                      truncated: stage.truncated ?? false,
                    }}
                  />
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div style={{ display: "flex", gap: "8px", marginTop: "12px" }}>
        <button type="button" onClick={onCancel} data-testid="blocked-dismiss">
          Dismiss
        </button>
        <button
          type="button"
          onClick={onForceExport}
          data-testid="force-export"
          style={{ opacity: 0.8 }}
          title="Export the plugin spec without passing code validation"
        >
          Force Export (skip validation)
        </button>
      </div>
    </section>
  );
}
