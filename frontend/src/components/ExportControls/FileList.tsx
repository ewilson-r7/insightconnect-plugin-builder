// The plugin files the `.plg`'s image will be built from, shown before an export
// (Req 16.2).
//
// This used to be described as "the exact set of files packaged into the `.plg`", and
// the backend guaranteed the list equalled the archive's members (design Property 30).
// A `.plg` is now a gzipped `docker save` of the plugin's image, so the archive's members
// are `oci-layout`, `index.json` and layer blobs -- this list is the **build context**
// instead, and the plugin's own `.dockerignore` may keep some of it out of the image
// (the unit tests, for instance). The heading says so rather than overstating it.

export interface FileListProps {
  files: string[];
}

/** The plugin files the artifact's image will be built from (Req 16.2). */
export function FileList({ files }: FileListProps): JSX.Element {
  const sorted = [...files].sort((a, b) => a.localeCompare(b));
  return (
    <section aria-label="Packaged files" data-testid="file-list">
      <h3>Files the image is built from ({sorted.length})</h3>
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
