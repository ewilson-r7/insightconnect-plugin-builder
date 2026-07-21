// Spec preview shown before an export (Req 16.1). Renders the vendor-suffixed,
// version-bumped Plugin_Spec that would be exported so the operator can review
// exactly what leaves the tool.

export interface SpecPreviewProps {
  spec: Record<string, unknown> | null;
  /** "<previous> -> <new>" when the version changed; empty when unchanged (Req 12.6). */
  versionDisplay?: string;
}

/** A read-only, formatted preview of the plugin spec (Req 16.1). */
export function SpecPreview({ spec, versionDisplay }: SpecPreviewProps): JSX.Element {
  return (
    <section aria-label="Spec preview" data-testid="spec-preview">
      <h3>Spec preview</h3>
      {versionDisplay ? (
        <p data-testid="version-display">
          Version: <strong>{versionDisplay}</strong>
        </p>
      ) : null}
      {spec ? (
        <pre data-testid="spec-preview-body">{formatSpec(spec)}</pre>
      ) : (
        <p data-testid="spec-preview-empty">No spec to preview.</p>
      )}
    </section>
  );
}

function formatSpec(spec: Record<string, unknown>): string {
  try {
    return JSON.stringify(spec, null, 2);
  } catch {
    return String(spec);
  }
}
