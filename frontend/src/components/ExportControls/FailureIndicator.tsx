// A failure indication that distinguishes a build failure from an export
// failure (Req 19.4), names the failing step (Req 19.1), and shows its error
// output with truncated-plus-full access (Req 19.5). For a failed tenant export
// it also surfaces the retained artifact kept >=24h for retry (Req 19.2).

import type { ExportOutcome, FailureIndication } from "../../types";
import { ErrorOutput } from "./ErrorOutput";

export interface FailureIndicatorProps {
  outcome: ExportOutcome;
}

/** Distinguishes build vs export failures and renders the failing step (Req 19.1, 19.4). */
export function FailureIndicator({ outcome }: FailureIndicatorProps): JSX.Element {
  const failure: FailureIndication | null = outcome.failure;
  const isBuild = failure?.kind === "build" || outcome.status === "build_failed";
  const label = isBuild ? "Build failed" : "Export failed";

  return (
    <section
      aria-label="Failure"
      role="alert"
      data-testid="failure-indicator"
      data-failure-kind={isBuild ? "build" : "export"}
    >
      <h3 data-testid="failure-title">{label}</h3>
      {outcome.message ? (
        <p data-testid="failure-message">{outcome.message}</p>
      ) : null}
      {failure?.failing_step ? (
        <p data-testid="failure-step">
          Failing step: <strong>{failure.failing_step}</strong>
        </p>
      ) : null}
      {failure ? <ErrorOutput failure={failure} /> : null}
      {outcome.retained_artifact_path ? (
        <p data-testid="failure-retained-artifact">
          The built artifact was retained for retry:{" "}
          <code>{outcome.retained_artifact_path}</code>
        </p>
      ) : null}
    </section>
  );
}
