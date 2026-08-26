# Implementation Plan: the `.plg` is an image archive

Read `bugfix.md` first — it carries the measurements and the two decisions, one of
which (2.2) is still open and blocks task 3.

Each task is one purpose and one commit (SCOPE-7). Refactors land before the fix that
needs them (SCOPE-4). Specification amendments land in the **same commit** as the code
that makes them true, never ahead of it.

## Tasks

- [x] 1. Prove the defect, and pin what must not change
  - [x] 1.1 Test: the produced artifact is an image archive, not a source tree
    - Assert `oci-layout` and `index.json` are present at the root and that
      `manifest.json` carries `RepoTags == ["<vendor>/<name>:<version>"]`
    - **Expected to FAIL now**: the current artifact's root members are `Dockerfile`,
      `icon_<name>/`, `bin/` and so on
    - Compare against `/tmp/icpb_backup_working.plg`, the archive that actually
      imported, rather than against an idea of the format
    - _Requirements: 9.1, 9.2_

  - [x] 1.2 Test: the artifact is named `<vendor>_<name>_<version>.plg`
    - Currently `<name>-<version>.plg`. Expected to FAIL
    - _Requirements: 9.3_

  - [x] 1.3 Preservation: capture what the export path does today
    - Registry recording, audit entries, the tenant upload call shape, the failure
      indications, and the `permitted`/blocked gate decision must all survive
    - Compare **verdicts**, never message text, following the preservation approach in
      `.kiro/specs/export-gate-and-preview-fidelity/`
    - _Requirements: 9.3, 9.4, 9.5, 10.1-10.4_

  - [x] 1.4 Measure what a tenant actually requires
    - The import contract is inferred from one successful import. Before designing to
      it, record what is known and what is assumed: image archive **yes**, `RepoTags`
      identity **very likely**, filename **unverified**
    - A nil outcome is legitimate — the point is to stop assuming
    - Recorded as a confidence table in `bugfix.md` 1.8: two rows observed, one very
      likely, one unverified, one inferred. The weak rows are handled by matching the
      toolchain rather than guessing, and must not be mistaken for discovered
      requirements later
    - _Requirements: 9.2_

- [x] 2. Decide route A or route B for producing the image (`bugfix.md` 2.2)
  - **Decided: route B.** The tool drives `docker build`, `docker tag` and `docker save`
    itself. Route A was rejected because `insight-plugin export` cannot succeed on a
    host without working buildx (`bugfix.md` 1.5), and its failure there is silent,
    misleading and environmental
  - The departure from "wrap the real toolchain" is deliberate and recorded with its
    tradeoff in `bugfix.md` 2.2, including what would make route A viable again
  - Consequence carried into task 3: the tool now owns the image tag, so the tag is a
    thing that can be wrong where the toolchain would have got it right for free
  - _Requirements: 9.1, 9.2_

- [x] 3. Tag the plugin image with its published identity
  - The build stage tags `icplugin-validate/<name>:latest` for its own use. Add the
    published tag `<vendor>/<name>:<version>` with the `_custom`-suffixed vendor
  - Keep the validate tag: the validate stage depends on it, and the two tags have
    different jobs
  - Unit tests: the vendor suffix is applied once, a vendor already ending `_custom`
    is not doubled, and the version comes from the spec on disk
  - _Requirements: 9.2, 11.1, 11.2_

- [x] 4. Produce the artifact as a gzipped image archive
  - `BuildEngine.package` writes a gzipped `docker save` of the published tag, named
    `<vendor>_<name>_<version>.plg`
  - Preserve the atomic-write behaviour that already exists: a failure leaves no
    partial artifact and the source tree unchanged (Req 9.5)
  - Remove a stale `.plg` from the plugin directory first (`bugfix.md` 2.4), and say so
    in the report rather than deleting silently
  - Amend **Requirement 9.2** in the same commit to state that the artifact is a
    gzipped `docker save` of the image tagged `<vendor>/<name>:<version>`
  - _Requirements: 9.1, 9.2, 9.5_

- [x] 5. Report what the operator is about to get
  - The preview names the image tag, the version, the artifact filename and its size,
    in place of a list of source files that no longer describes the artifact
  - Amend **Requirement 16.2** in the same commit
  - **Property 69 is restated, not deleted**: its claim (the packaged set equals the
    plugin's files) is false for an image archive. Keep the historical record in the
    docstring and assert the new claim — the archive is an image carrying the expected
    identity
  - _Requirements: 16.1, 16.2_

- [x] 6. Make the failure legible when the image cannot be produced
  - Docker absent, daemon down, build failure, `docker save` failure, and — for route
    A — the SDK's stdout mis-detection all need to report what failed and what to do,
    not "packaging failed"
  - Fail closed: no artifact, no registry entry, no partial file
  - _Requirements: 9.4, 9.5, 19.1, 19.5_

- [x] 7. Integration test over the real export
  - Against `~/.icplugin-builder/projects/jumpcloud/`, with Docker up: produce a
    `.plg`, assert it is an image archive with the right `RepoTags`, and assert
    `docker load` accepts it
  - `docker load` is the closest available proxy for a tenant import and is the check
    that would have caught this on day one
  - Skip honestly when Docker is absent
  - _Requirements: 9.1, 9.2_

- [x] 8. Update what the tool tells a new user
  - Verified: the requirements table already listed Docker under "build, validate and
    package", so no requirement was missing. What *was* missing is the fact that explains
    it -- the README never said a `.plg` is a container image, so a reader had no reason to
    expect a daemon for an operation described as packaging
  - Added: what a `.plg` is and why Docker is needed to export; that the image is built
    from a staged copy so byproducts stay out; and two troubleshooting rows for the new
    failures (unreachable daemon, an untaggable vendor or name)
  - _Requirements: none — documentation accuracy_

- [x] 9. Checkpoint
  - Full suite. Confirm the three preservation axes from 1.3 hold over verdicts
  - Confirm against the JumpCloud tree that the produced artifact is byte-comparable
    in *structure* to `/tmp/icpb_backup_working.plg` — same top-level members, same
    `RepoTags`. Byte equality is not expected: layer digests differ per build
  - Integrate current `origin/main` and re-run the affected gates (SCOPE-13)
  - **Environment matters**: `insight-plugin` and `prospector` live in
    `~/Library/Python/3.9/bin`, `docker` in
    `/Applications/Docker.app/Contents/Resources/bin`, and neither is on a non-login
    shell's `PATH`. buildx is unavailable in the tool's shell — see `bugfix.md` 4
  - Ask the operator if questions arise; do not report a fix as complete with any
    failure open

  **Result.** 1,589 passed, 15 skipped, 0 failed -- the whole 1,604-test suite, run in
  serialized chunks because host memory was tight. All three preservation axes pass.

  The structural comparison holds. Built through `BuildEngine.package` against the real
  tree, into a temp directory so the operator's own artifact was untouched (confirmed
  byte-identical by digest afterwards):

  | | produced | reference (`insight-plugin export`) |
  |---|---|---|
  | top-level members | `oci-layout`, `index.json`, `manifest.json`, `blobs/` | identical |
  | `RepoTags` | `rapid7_custom/jumpcloud:1.0.1` | identical |
  | size | 81,201,124 B | 81,305,076 B |

  The 103,952-byte difference is the point rather than a discrepancy: the staged build
  context excludes the byproducts the plugin's own `.dockerignore` admits. Both `.coverage`
  (53,248 B) and `.DS_Store` (8,196 B) are present in the tree and absent from the image.

  **One real defect found by this checkpoint**, not tree drift:
  `test_end_to_end_integration.py` still asserted `plugin.spec.yaml` was a *top-level
  archive member* -- the source-tarball contract -- and task 5's migration missed it. It is
  the one place the whole flow runs through the real FastAPI app, so it was the last test
  still encoding the defect being fixed. Restated to assert an image archive with the
  expected tag, with the spec's presence in the layers left to the real-Docker contents
  test, which is the only thing that can prove it.

## Out of scope

- **The SDK's own bug** (`bugfix.md` 1.5). Reporting it upstream is worth doing and is
  not this change. Route B avoids it; route A works around it.
- **Whether a tenant validates the filename.** Recorded as unverified in 1.4. Matching
  the toolchain's convention is right regardless.
- **What the `.dockerignore` admits into the image.** The old leak check's question
  transposed to the new artifact — worth its own look, since `unit_test/**` being
  excluded is what caused the original test-stage defect, but not this change.
- **No dependency is added, removed, or upgraded** (SCOPE-12).

## Notes

- The correctness properties continue the parent specification's numbering at **76**.
- Property 69 is restated by task 5 rather than removed, per SCOPE-11: it encodes
  behaviour being intentionally changed, so the coupled update lands with the change.
- Docker is real in tasks 3, 4 and 7 — this defect is precisely the kind that a mock
  hides, since a mocked `docker save` would have returned whatever we told it to.
