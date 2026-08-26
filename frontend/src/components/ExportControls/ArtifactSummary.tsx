// What the export will actually produce, shown before it happens (Req 16.2).
//
// A `.plg` is a container image archive, and a tenant identifies the plugin by the
// **image tag** it declares. That makes the tag the most consequential thing in the
// preview: an operator about to publish `rapid7_custom/jumpcloud:1.0.1` should be able to
// read that, not infer it from a version string and a vendor field elsewhere on the page.

import type { PlannedArtifact } from "../../types";

export interface ArtifactSummaryProps {
  artifact: PlannedArtifact | null;
}

/** The image tag and filename the export would produce (Req 16.2). */
export function ArtifactSummary({ artifact }: ArtifactSummaryProps): JSX.Element | null {
  if (artifact === null) {
    // The spec cannot yet form an identity -- a missing name or version. The
    // completeness findings beside this preview name the missing field, so staying
    // silent here is better than rendering a half-formed tag.
    return null;
  }
  return (
    <section aria-label="Artifact" data-testid="artifact-summary">
      <h3>Artifact</h3>
      <p data-testid="artifact-image-tag">
        Image: <code>{artifact.image_tag}</code>
      </p>
      <p data-testid="artifact-filename">
        File: <code>{artifact.filename}</code>
      </p>
    </section>
  );
}
