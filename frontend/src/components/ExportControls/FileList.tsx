// The list of files that will be included in the PLG_Artifact, shown before an
// export (Req 16.2). This list is computed by the backend to equal the exact
// set of files packaged into the `.plg` (design Property 30).

export interface FileListProps {
  files: string[];
}

/** The exact set of files that will be packaged into the `.plg` (Req 16.2). */
export function FileList({ files }: FileListProps): JSX.Element {
  const sorted = [...files].sort((a, b) => a.localeCompare(b));
  return (
    <section aria-label="Packaged files" data-testid="file-list">
      <h3>Files in package ({sorted.length})</h3>
      {sorted.length === 0 ? (
        <p data-testid="file-list-empty">No files will be included.</p>
      ) : (
        <ul>
          {sorted.map((path) => (
            <li key={path} data-testid="file-list-item">
              {path}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
