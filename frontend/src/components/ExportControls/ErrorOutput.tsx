// Displays a failing step's error output with truncated-plus-full access
// (Req 19.5). The backend bounds `displayed_output` to the first 10,000
// characters and always retains the complete `full_output`; this component
// shows the bounded view and lets the operator reveal the full output.

import { useState } from "react";
import type { FailureIndication } from "../../types";

export interface ErrorOutputProps {
  failure: Pick<FailureIndication, "displayed_output" | "full_output" | "truncated">;
}

/** Bounded error output with a toggle to reveal the complete text (Req 19.5). */
export function ErrorOutput({ failure }: ErrorOutputProps): JSX.Element {
  const [showFull, setShowFull] = useState(false);

  const displayed = failure.displayed_output ?? "";
  const full = failure.full_output ?? displayed;
  const truncated = failure.truncated;
  const omitted = Math.max(full.length - displayed.length, 0);

  return (
    <div data-testid="error-output">
      <pre data-testid="error-output-body">{showFull ? full : displayed}</pre>
      {truncated ? (
        <div data-testid="error-output-truncation">
          {showFull ? (
            <>
              <span>Showing full output ({full.length} characters).</span>
              <button
                type="button"
                onClick={() => setShowFull(false)}
                data-testid="error-output-collapse"
              >
                Show less
              </button>
            </>
          ) : (
            <>
              <span data-testid="error-output-truncated-note">
                Output truncated: showing first {displayed.length} of {full.length}{" "}
                characters ({omitted} hidden).
              </span>
              <button
                type="button"
                onClick={() => setShowFull(true)}
                data-testid="error-output-show-full"
              >
                Show full output
              </button>
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}
