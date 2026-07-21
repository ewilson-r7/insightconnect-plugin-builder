// Prior-version diff shown before an export (Req 16.3, 16.4). When a prior
// version exists in the registry the diff identifies added/removed/modified
// files; when none exists the draft is the first version and every file is
// presented as an addition.

import type { FileTreeDiff } from "../../types";

export interface DiffViewProps {
  diff: FileTreeDiff;
}

/** A diff between the prior exported version and the current draft (Req 16.3, 16.4). */
export function DiffView({ diff }: DiffViewProps): JSX.Element {
  if (diff.first_version) {
    // No prior version: indicate first version, present all files as additions
    // (Req 16.4). `added` already carries every file for a first-version diff.
    return (
      <section aria-label="Version diff" data-testid="diff-view">
        <h3>Changes</h3>
        <p data-testid="diff-first-version">
          This is the first version of the plugin. All files are new additions.
        </p>
        <DiffGroup label="Added" kind="added" files={diff.added} />
      </section>
    );
  }

  const unchanged =
    diff.added.length === 0 &&
    diff.removed.length === 0 &&
    diff.modified.length === 0;

  return (
    <section aria-label="Version diff" data-testid="diff-view">
      <h3>Changes since last export</h3>
      {unchanged ? (
        <p data-testid="diff-no-changes">No file changes since the last export.</p>
      ) : (
        <>
          <DiffGroup label="Added" kind="added" files={diff.added} />
          <DiffGroup label="Removed" kind="removed" files={diff.removed} />
          <DiffGroup label="Modified" kind="modified" files={diff.modified} />
        </>
      )}
    </section>
  );
}

interface DiffGroupProps {
  label: string;
  kind: "added" | "removed" | "modified";
  files: string[];
}

function DiffGroup({ label, kind, files }: DiffGroupProps): JSX.Element | null {
  if (files.length === 0) {
    return null;
  }
  const sorted = [...files].sort((a, b) => a.localeCompare(b));
  return (
    <div data-testid={`diff-${kind}`}>
      <h4>
        {label} ({sorted.length})
      </h4>
      <ul>
        {sorted.map((path) => (
          <li key={path} data-testid={`diff-${kind}-item`}>
            {path}
          </li>
        ))}
      </ul>
    </div>
  );
}
