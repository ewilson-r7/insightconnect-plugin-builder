# Implementation Plan: InsightConnect Plugin Builder

## Overview

This plan implements the InsightConnect Plugin Builder as a Python 3.11+ FastAPI backend (bound to loopback) with a TypeScript/React + React Flow single-page UI served as static assets by the backend. The tool is a thin, token-efficient orchestration layer over the real InsightConnect toolchain: the `insight-plugin` CLI is the deterministic scaffolder and the Kiro CLI (subprocess) is the LLM provider; Docker is optional-at-startup and required-for-build.

The plan builds incrementally from the inside out. The large **pure-logic core** (spec model, semver/version-bump, breaking-change classification, `_custom` vendor suffixing, diffing, masking, token accounting, config validation, view-model and documentation generation) is implemented and property-tested first because it is the highest-value, most-regression-prone surface. **Persistence** (registry, credential store, audit log, project folders) comes next, then **external integrations** (insight-plugin CLI, Kiro CLI, Docker build/validate, tenant upload, production source provider, update manager), then the **orchestration** layer that sequences everything, then the **API and UI**, finishing with end-to-end wiring.

Property-based tests use [Hypothesis](https://hypothesis.readthedocs.io/) (min 100 examples per property). Each of the 51 correctness properties from the design is implemented by exactly one property-based test, tagged `# Feature: insightconnect-plugin-builder, Property {number}: {property_text}`. Costly externals (Kiro CLI, Docker, tenant API, `insight-plugin`, git remotes) are mocked in property tests and exercised by a small integration suite.

Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP; core implementation tasks are never optional.

## Tasks

- [ ] 1. Set up project structure and core spec model
  - [ ] 1.1 Scaffold the Python backend project and test tooling
    - Create the package layout (`icplugin_builder/` with `core/`, `persistence/`, `integrations/`, `orchestrator/`, `api/` subpackages) and a `tests/` tree
    - Configure `pyproject.toml` with FastAPI, Uvicorn, ruamel.yaml, jsonschema, cryptography, plus dev deps pytest and Hypothesis; add lint/format config (flake8/black consistent with repo `.flake8`)
    - Add a `Makefile`/task runner entry to run `pytest --run`-style single-shot test execution
    - _Requirements: 20.1, 20.3_

  - [ ] 1.2 Implement the PluginSpec typed data model and YAML codec
    - Define `PluginSpec`, `Component`, `FieldSchema`, and `SemVer` types mirroring `plugin.spec.yaml` (`plugin_spec_version: v2`) as a typed tree supporting exact diffing and classification
    - Implement round-trip-preserving load/dump using `ruamel.yaml` (comments/ordering survive)
    - _Requirements: 2.2, 21.5_

  - [ ]* 1.3 Write property test for Plugin_Spec YAML round trip
    - **Property 5: Plugin_Spec YAML round trip** — serialize then load yields an equivalent PluginSpec
    - **Validates: Requirements 2.2, 21.5**

  - [ ]* 1.4 Build shared Hypothesis strategies for PluginSpec generation
    - Compose valid `name`/semver/`vendor` and randomized `types`/`connection`/`actions`/`triggers`/`tasks` with `FieldSchema` across all field types (scalar, complex, credential)
    - Add labeled mutation strategies (add-optional-field, add-action, remove-field, change-type, optional→required, remove-component) for classifier and preservation tests
    - _Requirements: 2.2_

- [ ] 2. Implement version arithmetic and breaking-change classification (pure logic)
  - [ ] 2.1 Implement SemVer parsing, validation, and total ordering
    - Parse/validate `MAJOR.MINOR.PATCH`; expose a total order used throughout version bumping
    - _Requirements: 7.3, 7.5_

  - [ ]* 2.2 Write property test for semantic version validation
    - **Property 15: Semantic version validation** — accepts iff `MAJOR.MINOR.PATCH`; rejection names the version field and expected format
    - **Validates: Requirements 7.3, 7.5**

  - [ ] 2.3 Implement the breaking-change classifier
    - Compare two PluginSpecs; classify breaking iff an existing action/connection has a field removed, a type changed, an optional field made required, or the action/connection removed; adding a new optional field or new component is never breaking
    - _Requirements: 12.2_

  - [ ]* 2.4 Write property test for breaking-change classification
    - **Property 23: Breaking-change classification** — breaking iff a removal/type-change/optional→required on an existing action or connection; additions never breaking
    - **Validates: Requirements 12.2**

  - [ ] 2.5 Implement the schema-aware version bumper
    - Given prior exported versions and the current draft: no prior export → keep version; breaking → `(major+1, 0, 0)`; non-breaking → patch increment; guarantee result strictly greater than every prior exported version
    - _Requirements: 12.3, 12.4, 12.5, 12.7_

  - [ ]* 2.6 Write property test for version-bump monotonicity
    - **Property 24: Version-bump monotonicity** — selected version strictly greater than all prior; breaking → (major+1,0,0); non-breaking → patch bump; no prior → unchanged
    - **Validates: Requirements 12.3, 12.4, 12.5, 12.7**

  - [ ] 2.7 Implement version_history extension on bump
    - On a bump, append exactly one `version_history` entry referencing the new version and expose previous→new for display before build
    - _Requirements: 12.6_

  - [ ]* 2.8 Write property test for version-history extension
    - **Property 25: Version bump extends version_history** — exactly one additional entry referencing the new version
    - **Validates: Requirements 12.6**

- [ ] 3. Implement pure-logic utilities: vendor suffix, diff, masking, token accounting, limits, truncation
  - [ ] 3.1 Implement the custom vendor-suffix operation
    - Append literal `_custom` to the vendor with no separator; idempotent for values already ending in exact case-sensitive `_custom`; empty/missing/null vendor becomes `_custom`
    - _Requirements: 13.1, 13.2, 13.3, 13.4_

  - [ ]* 3.2 Write property test for custom vendor-suffix idempotency
    - **Property 26: Custom vendor suffix is idempotent** — result ends in `_custom` and `f(f(x)) == f(x)`
    - **Validates: Requirements 13.1, 13.2, 13.3**

  - [ ] 3.3 Implement the file-tree diff engine
    - Partition two file trees into added/removed/modified sets; when no prior tree exists, report every file as an addition
    - _Requirements: 16.3, 16.4_

  - [ ]* 3.4 Write property test for diff correctness
    - **Property 31: Diff correctness against prior version** — correct added/removed/modified partition; no-prior → all additions
    - **Validates: Requirements 16.3, 16.4**

  - [ ] 3.5 Implement the boundary secret-masking routine
    - Replace every character of a secret with a fixed placeholder; provide one masking function applied at all display/log/doc/package boundaries
    - _Requirements: 14.4_

  - [ ]* 3.6 Write property test for secret masking
    - **Property 28: Secret masking leaks no plaintext character** — emitted representation is absent or fully masked; no original character appears
    - **Validates: Requirements 14.3, 14.4, 18.3**

  - [ ] 3.7 Implement session token accounting
    - Maintain a cumulative non-negative integer total that sums only successful invocations; exclude failed invocations
    - _Requirements: 3.5, 3.6, 3.7_

  - [ ]* 3.8 Write property test for token accounting
    - **Property 9: Token accounting equals sum of successful invocations** — total equals sum of successful invocation token counts only
    - **Validates: Requirements 3.5, 3.6, 3.7**

  - [ ] 3.9 Implement configurable numeric-limit validation
    - Accept token budget iff within 1..10,000,000 and rate iff within 1..1,000; reject out-of-range values
    - _Requirements: 4.1, 4.4_

  - [ ]* 3.10 Write property test for numeric-limit ranges
    - **Property 10: Configurable numeric limits accept exactly their range** — accepted iff within inclusive range
    - **Validates: Requirements 4.1, 4.4**

  - [ ] 3.11 Implement error-output truncation utility
    - When output exceeds 10,000 characters, expose exactly the first 10,000 characters plus a handle to the full output; otherwise return the full output
    - _Requirements: 19.1, 19.5_

  - [ ]* 3.12 Write property test for error-output truncation
    - **Property 36: Error output truncation preserves full access** — first 10,000 chars shown with full access retained; otherwise full output shown
    - **Validates: Requirements 19.5, 19.1**

- [ ] 4. Implement draft operations, failure atomicity, and input validation (pure logic)
  - [ ] 4.1 Implement the in-session Draft with targeted component operations
    - Add / modify-by-name / remove-by-name on a named component while leaving all other components and their hand-written code byte-identical
    - _Requirements: 1.3, 2.3, 15.1, 15.2, 15.3, 22.1, 22.2_

  - [ ]* 4.2 Write property test for component preservation
    - **Property 1: Component preservation under targeted operations** — every non-target component and its code is byte-identical before/after
    - **Validates: Requirements 1.3, 2.3, 15.1, 15.2, 15.3, 22.1, 22.2**

  - [ ] 4.3 Implement not-found handling for named-component operations
    - Reject modify/remove of a name absent from the draft with a not-found message and leave the draft unchanged
    - _Requirements: 15.4_

  - [ ]* 4.4 Write property test for non-existent component rejection
    - **Property 29: Reject operations on non-existent named components** — rejected with not-found, draft unchanged
    - **Validates: Requirements 15.4**

  - [ ] 4.5 Implement the atomic apply wrapper
    - Structure generation/spec-edit/packaging/registry/credential operations so any failure leaves the draft, spec, and source files identical to their pre-step state (commit-on-success)
    - _Requirements: 1.7, 9.5, 11.6, 14.6, 19.3_

  - [ ]* 4.6 Write property test for failure atomicity
    - **Property 2: Failure atomicity (no partial mutation)** — on any failing step, draft/spec/source files are unchanged
    - **Validates: Requirements 1.7, 9.5, 11.6, 14.6, 19.3**

  - [ ] 4.7 Implement conversation input validation
    - Accept input for processing iff length is 1..10,000 characters; reject empty/whitespace-only leaving the draft unchanged
    - _Requirements: 1.1, 1.6_

  - [ ]* 4.8 Write property test for input length boundary
    - **Property 4: Input length acceptance boundary** — accepted iff length in 1..10,000 inclusive
    - **Validates: Requirements 1.1**

  - [ ]* 4.9 Write property test for empty/whitespace rejection
    - **Property 3: Empty/whitespace input rejection** — rejected and draft unchanged
    - **Validates: Requirements 1.6**

- [ ] 5. Implement spec validation, documentation, and visualization view-model
  - [ ] 5.1 Implement the Spec_Validator
    - Validate against the InsightConnect plugin-spec schema within 5s; run the semver check; return every error with a field path + description; indicate success when clean
    - _Requirements: 7.1, 7.2, 7.3, 7.5, 7.6_

  - [ ]* 5.2 Write property test for validation error completeness
    - **Property 16: Validation error report completeness** — one entry per violation, each carrying field path + description
    - **Validates: Requirements 7.2**

  - [ ] 5.3 Implement the Documentation_Generator
    - Produce `help.md` with distinct connection/actions/triggers/tasks sections; each action/trigger input+output field with name, type, required/optional; include title/description/version/vendor; empty categories render heading + placeholder; abort leaving existing `help.md` unchanged when required metadata missing
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ]* 5.4 Write property test for help.md completeness
    - **Property 14: help.md completeness** — required sections/fields/metadata present; empty categories render placeholder
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4**

  - [ ] 5.5 Implement the Visualization view-model builder
    - Build a view-model including every connection/action/trigger/task, the input+output schema of every action and trigger, and, on single selection, exactly that component's fields
    - _Requirements: 5.1, 5.2, 5.4_

  - [ ]* 5.6 Write property test for view-model completeness
    - **Property 13: Visualization view-model completeness** — includes all components, all action/trigger schemas, selected-component fields
    - **Validates: Requirements 5.1, 5.2, 5.4**

  - [ ] 5.7 Implement empty-state and parse-failure fallbacks for the view-model
    - Empty draft → empty-state indication; unparseable draft → error indication identifying the parse failure while retaining the last valid view-model
    - _Requirements: 5.5, 5.6_

  - [ ]* 5.8 Write unit tests for empty-state and parse-failure fallbacks
    - Cover empty-state rendering and retention of the last valid visualization on parse failure
    - _Requirements: 5.5, 5.6_

- [ ] 6. Checkpoint - pure-logic core
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Implement the Cost_Controller
  - [ ] 7.1 Implement authorize() with rate limiting and token budgeting
    - Enforce per-minute request rate (1..1,000) and per-session token budget (1..10,000,000); block LLM calls once budget reached while retaining completed output and persisting no partial result; return the budget-reached message
    - _Requirements: 4.2, 4.3, 4.4, 4.5_

  - [ ]* 7.2 Write property test for token-budget blocking
    - **Property 11: Token budget blocks once reached** — subsequent authorizations blocked, no partial output persisted, completed output retained
    - **Validates: Requirements 4.2**

  - [ ]* 7.3 Write property test for rate limiting
    - **Property 12: Rate limit rejects beyond threshold with retry-after** — excess requests rejected without invoking the LLM; retry-after in (0, 60]
    - **Validates: Requirements 4.5**

  - [ ] 7.4 Wire record_usage() and the default budget
    - Integrate token accounting into record_usage(); apply the 100,000-token default when no budget is configured
    - _Requirements: 3.5, 4.6_

  - [ ]* 7.5 Write unit test for the default token budget
    - Verify an unconfigured session applies the 100,000-token default
    - _Requirements: 4.6_

- [ ] 8. Implement the Plugin_Registry (SQLite)
  - [ ] 8.1 Implement registry storage and queries
    - Create SQLite `plugins`/`exports` schema; record creation (name/vendor/version/created_utc) and export (version/target/export_utc) in UTC; persist across restarts; history query returns versions + export events most-recent-first
    - _Requirements: 11.1, 11.2, 11.3, 11.4_

  - [ ]* 8.2 Write property test for registry round trip and ordering
    - **Property 22: Registry persistence round trip and ordering** — records survive reopen; history ordered most-recent-first
    - **Validates: Requirements 11.1, 11.2, 11.3, 11.4**

  - [ ]* 8.3 Write unit tests for empty history and write-failure preservation
    - Empty history returns an empty result (no error); a write failure returns an error and preserves prior history
    - _Requirements: 11.5, 11.6_

- [ ] 9. Implement the Credential_Store (Fernet)
  - [ ] 9.1 Implement encrypted store/retrieve/delete
    - Persist tenant-API and git credentials as Fernet ciphertext with a key derived (scrypt) from an OS keyring secret or the access passphrase; no plaintext at rest; reusable after restart; deletion removes plaintext and ciphertext
    - _Requirements: 14.1, 14.2, 14.5_

  - [ ]* 9.2 Write property test for credential round trip
    - **Property 27: Credential persistence round trip with no plaintext at rest** — on-disk blob contains no plaintext substring; decrypt returns original; deletion leaves neither plaintext nor ciphertext
    - **Validates: Requirements 14.1, 14.2, 14.5**

  - [ ]* 9.3 Write unit test for encryption-failure rejection
    - Verify a failed encryption rejects the store op leaving nothing partially written
    - _Requirements: 14.6_

- [ ] 10. Implement the Audit_Log (append-only, hash-chained)
  - [ ] 10.1 Implement append-only hash-chained records
    - Append records for auth success/failure, build, export, credential store/use with masked secrets and UTC timestamps (≥ second precision); chain each record's hash over the previous hash; reject in-place edits; ≥90-day retention
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 18.7_

  - [ ]* 10.2 Write property test for complete, append-only records
    - **Property 33: Audit records are complete and append-only** — each event appends a record with required fields + UTC timestamp; appending never alters prior records
    - **Validates: Requirements 18.1, 18.2, 18.4, 18.5, 18.6**

  - [ ]* 10.3 Write property test for tamper detection
    - **Property 34: Audit tamper detection** — any alter/delete of a prior record is detected via hash-chain verification and rejected
    - **Validates: Requirements 18.7**

- [ ] 11. Implement Project_Folder history and reuse
  - [ ] 11.1 Implement the Project_Folder layout and metadata
    - Save spec/code/docs/artifacts and `.builder/` metadata (`project.json`, `tooling.json`, `history/<version>/`); list previously created plugins with name, current version, last-modification timestamp
    - _Requirements: 21.1, 21.2, 21.4_

  - [ ]* 11.2 Write property test for save/list fidelity
    - **Property 38: Project-folder save/list fidelity** — stored spec/code/docs/artifacts match the draft; listing returns name/version/last-modified
    - **Validates: Requirements 21.1, 21.2, 21.4**

  - [ ]* 11.3 Write property test for per-version history retention
    - **Property 39: Per-version history retention** — each version's spec snapshot and export outcome is independently retrievable and equals what was exported
    - **Validates: Requirements 21.3**

  - [ ]* 11.4 Write unit test for missing project-folder content
    - Verify loading a project with missing content returns a missing-content error
    - _Requirements: 21.6_

- [ ] 12. Checkpoint - persistence layer
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. Implement the Deterministic_Scaffolder and generation classification
  - [ ] 13.1 Implement the insight-plugin CLI wrapper
    - Wrap `insight-plugin create` and `insight-plugin refresh` via `asyncio.create_subprocess_exec` (regenerating `schema.py`/`__init__.py`/`Dockerfile`/`Makefile`/`setup.py`/`help.md`/`.CHECKSUM`) with zero LLM calls
    - _Requirements: 3.1, 22.3_

  - [ ] 13.2 Implement generation classification and TemplateLibrary
    - Classify each requested artifact (`directory_structure|spec_skeleton|boilerplate|action_logic|field_description|help_text|template_match`) and render template matches from parameterized templates with zero LLM calls
    - _Requirements: 3.1, 3.2, 3.3_

  - [ ]* 13.3 Write property test for zero-LLM deterministic scaffolding
    - **Property 7: Deterministic scaffolding makes zero LLM calls** — directory/skeleton/boilerplate/template artifacts produce zero LLM_Generator invocations
    - **Validates: Requirements 3.1, 3.3**

  - [ ]* 13.4 Write property test for LLM restriction to reasoning content
    - **Property 8: LLM invocations are restricted to reasoning content** — every invocation is action logic / field description / help text only
    - **Validates: Requirements 3.2**

  - [ ] 13.5 Implement structural-change refresh triggering
    - Invoke `insight-plugin refresh` whenever an action/trigger/task/connection changes so derived files equal the refresh output (never hand-edited)
    - _Requirements: 22.3_

  - [ ]* 13.6 Write property test for structural-change refresh
    - **Property 40: Structural spec change triggers refresh, not hand-editing** — refresh is invoked and derived files equal refresh output
    - **Validates: Requirements 22.3**

- [ ] 14. Implement the LLM_Generator (Kiro CLI)
  - [ ] 14.1 Implement scoped Kiro CLI dispatch with cost gating
    - Dispatch `generate(kind, scoped_context)` for reasoning kinds only through the Kiro CLI subprocess; route every call through `Cost_Controller.authorize()`; measure tokens by reported figure with tokenizer-estimate fallback; halt the step and exclude the invocation from the total on failure
    - _Requirements: 3.4, 3.7, 4.2, 20.3_

  - [ ]* 14.2 Write integration test for Kiro CLI dispatch (mocked)
    - Verify reasoning-kind dispatch, cost gating, and token recording against a mocked Kiro CLI subprocess
    - _Requirements: 20.3, 3.4_

- [ ] 15. Implement the Code_Validator, Build_Engine, and PLG packaging
  - [ ] 15.1 Implement the four-stage validation pipeline
    - Run lint, Docker build, unit tests, and `insight-plugin validate`, recording pass/fail per stage; abort build/test stages at 600s with a timeout fail; on any fail retain code unchanged; probe Docker availability and return an actionable error when absent
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.6, 8.8_

  - [ ]* 15.2 Write property test for failing-stage identification
    - **Property 18: Failing stage is identified in the report** — each failing stage and its error output is reported
    - **Validates: Requirements 8.5**

  - [ ] 15.3 Implement the export-gating decision
    - Permit export iff the spec is valid and all four code stages passed; otherwise block and indicate remaining validation errors
    - _Requirements: 7.4, 8.6, 8.7, 22.4_

  - [ ]* 15.4 Write property test for export gating
    - **Property 17: Export gating equals validation conjunction** — export permitted iff spec valid AND all four stages passed
    - **Validates: Requirements 7.4, 8.6, 8.7, 9.1, 9.4, 22.4**

  - [ ] 15.5 Implement PLG packaging in the Build_Engine
    - Package a validated project into a single gzipped-tarball `.plg` only when validation passed; on packaging failure produce no partial artifact and leave sources unchanged
    - _Requirements: 9.1, 9.2, 9.4, 9.5_

  - [ ]* 15.6 Write property test for PLG round trip
    - **Property 6: PLG artifact round trip** — package then extract yields the same files with identical contents; artifact carries gzip format
    - **Validates: Requirements 2.1, 9.2**

  - [ ] 15.7 Implement the export preview file list
    - Compute the preview file list to equal the exact set of files that will be included in the `.plg`
    - _Requirements: 16.1, 16.2_

  - [ ]* 15.8 Write property test for preview/package consistency
    - **Property 30: Preview file list matches packaged contents** — preview list equals actual `.plg` contents
    - **Validates: Requirements 16.1, 16.2**

  - [ ] 15.9 Implement build/export failure classification and retention
    - Distinguish build vs export failures; display failing step name + full error output within 5s (truncated per 3.11); retain failed-export `.plg` ≥24h for retry
    - _Requirements: 19.1, 19.2, 19.4, 19.5_

  - [ ]* 15.10 Write property test for failure-type distinction
    - **Property 35: Failure indication distinguishes build from export** — classifies each as build vs export failure
    - **Validates: Requirements 19.4**

  - [ ]* 15.11 Write integration test for the Docker pipeline (mocked)
    - Cover lint/build/test/validate stages and the 600s timeout abort against a mocked Docker engine and `insight-plugin`
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.8_

- [ ] 16. Implement the Export_Manager (local + tenant)
  - [ ] 16.1 Implement export_local and export_tenant
    - Write `.plg` to a user-accessible location and report the path; validate non-empty region base URL + API key before any network call; reject when no built artifact exists; upload via the InsightConnect API with a 60s timeout
    - _Requirements: 9.3, 10.1, 10.4, 10.5_

  - [ ]* 16.2 Write property test for missing-credential rejection
    - **Property 20: Missing credentials rejected before any network call** — empty region URL or API key rejected pre-network, naming the missing credential
    - **Validates: Requirements 10.4**

  - [ ]* 16.3 Write property test for build-before-export
    - **Property 21: Export requires a built artifact** — export rejected when no built artifact exists
    - **Validates: Requirements 10.5**

  - [ ] 16.4 Implement export outcome recording
    - On success record the export in the registry with region + timestamp; on failure/timeout record a failed-attempt audit entry, leave the registry unchanged, and retain the artifact ≥24h
    - _Requirements: 10.2, 10.3_

  - [ ]* 16.5 Write property test for export recording semantics
    - **Property 19: Successful upload records export; failure leaves registry unchanged** — success adds exactly one export record with region+UTC; failure leaves registry unchanged and adds a failed-attempt audit record
    - **Validates: Requirements 10.2, 10.3**

  - [ ]* 16.6 Write integration test for tenant upload success/failure (mocked)
    - Cover success and failure/timeout paths against a mocked InsightConnect tenant API
    - _Requirements: 10.1_

- [ ] 17. Implement the Plugin_Source_Provider (read-only production forks)
  - [ ] 17.1 Implement source listing and read-only import
    - `list_sources`/`list_plugins` (local clone first, remote GitHub fallback with git creds for the private repo); `import_plugin` copies into a new Project_Folder without writing to the source, applies `_custom` while retaining the original name, records a Provenance_Record (repo/name/version), preserves license/attribution in `resources`, detects `icon_`/`komand_` prefix, and stores a `.builder/baseline/` snapshot; flag the private-source notice
    - _Requirements: 24.5, 25.1, 25.2, 25.3, 25.4, 25.5, 25.6, 25.7_

  - [ ]* 17.2 Write property test for read-only import
    - **Property 47: Production source is read-only under import** — every source file byte-identical before/after import
    - **Validates: Requirements 25.3**

  - [ ]* 17.3 Write property test for fork identity
    - **Property 48: Production-fork identity** — vendor ends in `_custom`, original name retained, provenance = enhance_production with repo/name/version
    - **Validates: Requirements 24.5, 25.4**

  - [ ]* 17.4 Write property test for package-prefix handling
    - **Property 49: Package-prefix handling for both eras** — `icon_` and `komand_` imports succeed; recorded prefix equals the source's actual prefix
    - **Validates: Requirements 25.7**

  - [ ] 17.5 Implement baseline_diff for forks
    - Compute the diff between the current draft and the stored `.builder/baseline/` production baseline, independent of the exported-version diff
    - _Requirements: 25.8_

  - [ ]* 17.6 Write property test for baseline-diff correctness
    - **Property 50: Baseline diff correctness for forks** — baseline diff equals the added/removed/modified set-difference vs the stored baseline
    - **Validates: Requirements 25.8**

  - [ ]* 17.7 Write unit tests for import error paths
    - Missing private-repo git credential rejected before any network call; unreadable/non-conforming plugin reports a specific error with no partial draft
    - _Requirements: 25.9, 25.10_

- [ ] 18. Implement the Update_Manager
  - [ ] 18.1 Implement version snapshot and cached upstream checks
    - Snapshot installed Managed_Tooling versions at startup; perform non-blocking upstream checks cached for a configurable TTL; skip checks in offline mode / no network
    - _Requirements: 23.1, 23.3, 23.4, 23.5_

  - [ ]* 18.2 Write property test for update-check caching
    - **Property 42: Update-check caching honored** — no new upstream check within the cache TTL
    - **Validates: Requirements 23.4**

  - [ ]* 18.3 Write property test for notification correctness
    - **Property 43: Update notification iff newer version available** — notify iff latest newer than installed, including component/installed/available/changelog
    - **Validates: Requirements 23.6**

  - [ ] 18.4 Implement apply_update, tooling stamping, and SDK-bump offer
    - Apply updates only on approval; install → smoke-test a known-good sample → record new version on pass, roll back with reason on fail; stamp per-build `insight-plugin`/SDK versions into the Project_Folder; offer an SDK bump when a loaded plugin's pinned SDK is behind, leaving the pin unless approved
    - _Requirements: 23.2, 23.7, 23.8, 23.9, 23.10_

  - [ ]* 18.5 Write property test for no-upgrade-without-approval
    - **Property 44: No upgrade without approval** — with no approval, every installed version unchanged
    - **Validates: Requirements 23.7**

  - [ ]* 18.6 Write property test for smoke-test-gated recording/rollback
    - **Property 45: Approved update records version iff smoke test passes; rollback otherwise** — recorded version becomes new iff smoke test passes; otherwise pre-update version with a reason
    - **Validates: Requirements 23.8, 23.9**

  - [ ]* 18.7 Write property test for SDK-bump offer
    - **Property 46: SDK bump offered but not applied without approval** — offered on next refresh; pin unchanged unless approved
    - **Validates: Requirements 23.10**

  - [ ]* 18.8 Write property test for per-build tooling stamp
    - **Property 41: Per-build tooling version stamp accuracy** — stamped CLI/SDK versions equal those actually used
    - **Validates: Requirements 23.2**

  - [ ]* 18.9 Write integration test for non-blocking update check (mocked)
    - Verify the startup/interval check does not block and honors offline mode against mocked upstream sources
    - _Requirements: 23.3_

- [ ] 19. Checkpoint - external integrations
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 20. Implement provenance and the Orchestrator
  - [ ] 20.1 Implement the Provenance_Record model and persistence
    - Define Provenance_Record (entry mode + fork fields) and persist it in `.builder/project.json` for every created draft
    - _Requirements: 24.5_

  - [ ]* 20.2 Write property test for provenance across entry modes
    - **Property 51: Provenance recorded for every entry mode** — persisted provenance entry mode equals the mode used to create the draft
    - **Validates: Requirements 24.5**

  - [ ] 20.3 Implement the Orchestrator sequencing
    - Establish entry mode (net-new/iterate/enhance) and route accordingly; hold the draft; dispatch turns across the deterministic/LLM boundary; run refresh after structural edits; enforce validate-before-export and version-bump-before-build; apply `_custom` and drive preview/diff/confirm; emit audit events; surface clarification prompts on ambiguity leaving the draft unchanged
    - _Requirements: 1.4, 1.5, 3.6, 12.1, 12.6, 13.3, 15.1, 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 22.4, 22.5, 24.1, 24.2, 24.3, 24.4_

  - [ ]* 20.4 Write integration test for orchestration flows (mocked externals)
    - Cover create/iterate/enhance flows including refresh ordering, export gating, version bump, and clarification handling
    - _Requirements: 22.4, 24.2, 24.3, 24.4_

- [ ] 21. Implement the Access_Controller and startup configuration
  - [ ] 21.1 Implement the config loader with startup validation
    - Read LLM provider, token budget, rate limit, bind address, access, paths, update, tenant, and production-source settings; halt startup naming any missing/invalid required setting; probe Kiro CLI (report remediation if unavailable) and Docker availability
    - _Requirements: 20.2, 20.5, 20.6_

  - [ ]* 21.2 Write property test for startup config validation
    - **Property 37: Missing required configuration halts startup naming the setting** — startup halts and the error names the missing/invalid setting
    - **Validates: Requirements 20.6**

  - [ ] 21.3 Implement the Access_Controller
    - When protection enabled require the configured passphrase (stored as an argon2/scrypt hash) and run no protected function on mismatch; when disabled grant access without prompting; bind to a configurable address defaulting to loopback; record auth success/failure to the audit log
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 18.1, 18.5_

  - [ ]* 21.4 Write property test for wrong-passphrase denial
    - **Property 32: Wrong passphrase denies access and runs nothing** — mismatch denies access and executes no protected function
    - **Validates: Requirements 17.1, 17.2**

  - [ ]* 21.5 Write unit test for disabled access protection
    - Verify access is granted without a prompt when protection is disabled
    - _Requirements: 17.3_

- [ ] 22. Implement the API layer (FastAPI + WebSocket)
  - [ ] 22.1 Implement the FastAPI app, routes, and WebSocket channel
    - Wire the Orchestrator behind HTTP routes (entry-mode selection, message submission, build, export, history, production-source import, updates) and a WebSocket channel streaming draft state, token counter, and visualization updates; bind to loopback by default; serve the built UI as static assets
    - _Requirements: 1.4, 3.6, 5.3, 17.4, 20.1_

  - [ ]* 22.2 Write integration test for API endpoints and startup
    - Verify the app starts self-contained on loopback and the core routes/WebSocket wire through to the Orchestrator (mocked externals)
    - _Requirements: 20.1_

- [ ] 23. Implement the frontend UI (TypeScript + React + React Flow)
  - [ ] 23.1 Implement the Conversation_Interface chat UI
    - Message-list + input wired to the WebSocket; entry-mode selection; live cumulative token counter; clarification prompts; private-source usage-restriction notice
    - _Requirements: 1.4, 3.6, 24.1, 25.6_

  - [ ] 23.2 Implement the Visualization_View with React Flow
    - Render connection/actions/triggers/tasks as nodes with input/output schemas; single-selection detail panel; empty-state and parse-error fallbacks; update within 2s of draft changes
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [ ] 23.3 Implement the preview/diff/confirm and build/export controls
    - Show spec preview, packaged file list, and prior-version diff; require explicit confirmation before export; surface build vs export failures with truncated-plus-full error access
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 19.4, 19.5_

  - [ ]* 23.4 Write frontend component tests
    - Test the chat token counter/notices and the visualization node/selection/empty/error states
    - _Requirements: 5.4, 5.5, 5.6_

- [ ] 24. Final integration and wiring
  - [ ] 24.1 Wire all components end-to-end and package the UI
    - Connect Orchestrator, persistence, integrations, cost/access controls, and API; build the React UI to static assets served by FastAPI so the operator launches a single process
    - _Requirements: 20.1, 20.3, 20.4_

  - [ ]* 24.2 Write an end-to-end integration test (mocked externals)
    - Exercise create → generate → validate → build → export (local and tenant) with mocked Kiro CLI, `insight-plugin`, Docker, and tenant API
    - _Requirements: 20.1, 20.4_

- [ ] 25. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP; core implementation tasks are never optional.
- Each of the 51 correctness properties is implemented by exactly one property-based test (Hypothesis, min 100 examples), tagged `# Feature: insightconnect-plugin-builder, Property {number}: {property_text}`.
- Costly externals (Kiro CLI, Docker, tenant API, `insight-plugin`, git remotes) are mocked in property tests; a small integration suite exercises real dispatch paths.
- Property tests are placed close to the implementation they validate so regressions surface early; unit tests cover specific branches/defaults/messages (empty-state, default budget, disabled access, clarification prompts).
- Each task references specific requirement sub-clauses and, where applicable, the design property it validates for traceability.
- Checkpoints (tasks 6, 12, 19, 25) provide incremental validation gates.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.4", "2.1", "2.3", "3.1", "3.3", "3.5", "3.7", "3.9", "3.11", "4.1", "4.3", "4.5", "4.7", "5.1", "5.3", "5.5", "5.7", "8.1", "9.1", "10.1", "11.1", "13.1", "21.1"] },
    { "id": 3, "tasks": ["1.3", "2.2", "2.4", "2.5", "3.2", "3.4", "3.6", "3.8", "3.10", "3.12", "4.2", "4.4", "4.6", "4.8", "4.9", "5.2", "5.4", "5.6", "5.8", "7.1", "8.2", "8.3", "9.2", "9.3", "10.2", "10.3", "11.2", "11.3", "11.4", "13.2", "13.5", "21.3"] },
    { "id": 4, "tasks": ["2.6", "2.7", "7.2", "7.3", "7.4", "7.5", "13.3", "13.4", "13.6", "14.1", "15.1", "17.1", "18.1", "21.2", "21.4", "21.5"] },
    { "id": 5, "tasks": ["2.8", "14.2", "15.2", "15.3", "15.5", "15.9", "17.2", "17.3", "17.4", "17.5", "18.2", "18.3", "18.4", "20.1"] },
    { "id": 6, "tasks": ["15.4", "15.6", "15.7", "15.10", "15.11", "16.1", "17.6", "17.7", "18.5", "18.6", "18.7", "18.8", "18.9", "20.2"] },
    { "id": 7, "tasks": ["15.8", "16.2", "16.3", "16.4", "20.3"] },
    { "id": 8, "tasks": ["16.5", "16.6", "20.4", "22.1"] },
    { "id": 9, "tasks": ["22.2", "23.1", "23.2", "23.3"] },
    { "id": 10, "tasks": ["23.4", "24.1"] },
    { "id": 11, "tasks": ["24.2"] }
  ]
}
```
