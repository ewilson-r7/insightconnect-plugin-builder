# Export Gate and Preview Fidelity Bugfix Design

## Overview

Three defects make the tool report a working plugin as broken. None of them is in
code generation: the JumpCloud plugin the run produced is correct, and this design
touches nothing that produced it (`bugfix.md` 3.1).

- **Bug 1**: the `Code_Validator`'s `test` stage runs `pytest` inside the built
  plugin image, where neither the tests nor `pytest` exist, so it fails for every
  plugin — while the `Quality_Gate` runs the same tests on the host and reports
  them passing. Two subsystems, opposite answers, one tree.
- **Bug 2**: the `lint` stage and the `formatted` condition judge files the
  `Agent_Rulebook` forbids editing, so a clean plugin fails on defects its author
  is not allowed to fix.
- **Bug 3**: `export/prepare` evaluates the in-session draft rather than the
  `plugin.spec.yaml` the delegated agent wrote, so the preview describes a spec
  that no longer exists and reports 16 completeness errors that are all false
  against the file on disk.

The shape of the fix is the same in all three cases: **one definition, consumed
by every subsystem that reports on it.** One definition of how a plugin's unit
tests are run, so the stage and the gate cannot disagree. One definition of which
files are generated, so lint, format, and packaging apply the same list. One
authoritative spec — the file on disk — so the preview describes what would be
packaged.

The three decisions in `bugfix.md` 2.1, 2.6, and 2.11 are already taken. This
design implements them as written and does not reopen them. Their consequences for
the parent specification are collected under [Specification amendments](#specification-amendments).

**Sequencing: Bug 3 first.** Bugs 1 and 2 block an export that `force` can get
past. Bug 3 tells the operator a correct plugin is defective, which is the defect
that makes the tool untrustworthy even after the two stages are repaired — and, as
[Root cause 3](#bug-3-the-preview-judges-a-stale-draft) records, the stale draft is
not only reported: `confirm_export` writes it back over the tree.

## Glossary

- **Bug_Condition (C)** — the condition under which a bug manifests. Three of
  them here, `C₁`–`C₃`, one per bug; a tree or session may satisfy more than one.
- **Property (P)** — the behavior required of the fixed system for inputs
  satisfying `C`.
- **Preservation** — the behavior required to be byte-for-byte unchanged for
  inputs satisfying none of `C₁`–`C₃`: a genuine hand-written defect, a genuinely
  failing test, a genuinely incomplete on-disk spec.
- **F / F′** — the tool at `e7726b7` / the tool after this fix.
- **Code_Validator** — the four-stage pre-export pipeline (lint, build, test,
  validate). Its conjunction is the export gate and only the export gate
  (parent Requirement 8, design Property 17).
- **Quality_Gate** — the fast, located, correctable checks over hand-written code
  (compile, format, prospector, tests, coverage). Advisory; feeds the repair loop.
- **Definition_Of_Done** — the twelve-condition report on whether the plugin is
  finished. Advisory (parent Requirement 27.6), presented beside the preview
  (27.7).
- **Draft** — the in-session `Plugin_Spec` plus hand-written code held on
  `SessionState`.
- **Project_Folder / the tree** — the on-disk working tree. Its
  `plugin.spec.yaml` is the source of truth (parent design "Project_Folder").
- **Generated file** — a file the `Insight_Plugin_CLI` emits and the
  `Agent_Rulebook` forbids editing (`schema.py`, `__init__.py`, `setup.py`,
  `Dockerfile`, `Makefile`, `help.md`, `.CHECKSUM`). Generated files are still
  **packaged**; they are only excluded from findings.
- **Byproduct** — a local build or test artifact (`.coverage`, `__pycache__`,
  `.pytest_cache`) that belongs in neither the findings nor the `.plg`.
- **Unit test run** — one execution of a plugin's `unit_test/` suite under a named
  interpreter. After this fix there is exactly one definition of it.

## Bug Details

### Bug 1 — the four-stage `test` stage can never pass

The `test` stage runs `docker run --rm <image> python -m pytest -q`
(`integrations/code_validator.py`, `_stage_specs`). The generated `.dockerignore`
excludes `unit_test/**/*` and the Dockerfile's `ADD . /python/src` respects it, so
the image contains no tests; the `rapid7/insightconnect-python-3-slim-plugin`
runtime image contains no `pytest`, and `requirements.txt` correctly does not add
one. The stage therefore fails for **every** plugin, and every fix available to
the plugin author is an edit to a generated file.

**Formal specification:**

```
FUNCTION isBugCondition_1(X)
  INPUT: X of type PluginWorkingTree
  OUTPUT: boolean

  RETURN hostUnitTestsPass(X)
     AND dockerignoreExcludes(X, 'unit_test')
     AND NOT imageHasPytest(X)
END FUNCTION
```

#### Examples

- JumpCloud plugin, tests present and exercised (coverage data for every module):
  expected `test` pass and `permitted: true`; actual `test` fail and
  `permitted: false`, exportable only with `force`.
- Same tree, same moment: `Quality_Gate` reports `unit_tests_pass` **met**, while
  the pipeline reports the `test` stage **failed**. Expected: one answer.
- A plugin whose `test_create_user` genuinely fails: expected `test` fail — and
  under F′ it must still fail, for the real reason, not for a missing `pytest`.
- Edge case, the tester's own host: the SDK interpreter
  (`/Library/Developer/CommandLineTools/usr/bin/python3`, 3.9) has no `pytest`;
  the project venv has `pytest` and no SDK. Expected: a stage fail that names the
  interpreter it used and why it could not run, never a quiet pass.

### Bug 2 — `lint` and `format` fail only on files the author must not edit

Measured findings sat entirely in generated files: `F401 imported but unused` in
`__init__.py`, `bad-super-call` in `schema.py`, and five files `black` would
reformat (four `schema.py`, one `setup.py`, the latter reproducing in the
pre-existing `abuseipdb` and `rapid7_velociraptor` projects).
`~/.kiro/steering/structure.md` forbids editing all three file kinds.

**Formal specification:**

```
FUNCTION isBugCondition_2(X)
  INPUT: X of type PluginWorkingTree
  OUTPUT: boolean

  RETURN findings(X) <> EMPTY
     AND FOR ALL f IN findings(X): isGenerated(path(f))
END FUNCTION
```

#### Examples

- JumpCloud plugin: 14 lint messages, 0 in hand-written files. Expected `lint`
  pass, `lint_clean` met; actual stage fail.
- `black --check` names four generated `schema.py` and one generated `setup.py`;
  the 22 other files, including every hand-written one, are clean. Expected
  `formatted` met; actual unmet.
- A hand-written `util/api.py` that uses `requests` without importing it:
  expected `undefined-variable` reported. This must keep working — the exclusion
  must make the bar measurable, not lower it (parent tasks.md task 37; `bugfix.md`
  3.3).
- Edge case, environment-dependent bar: `build_prep.resolve_lint_profile` prefers
  `~/Documents/GitHub/insightconnect-plugins/prospector.yaml` over the vendored
  copy, so the applied rules depend on the developer's home directory.

#### Two measurement corrections, to be made before fixing

`bugfix.md` already flags that its counts were taken with bare prospector and
black defaults. Reading the code raises two more discrepancies, and both change
what the fix has to do:

1. **The `lint` stage does not run prospector.** It runs `flake8 .`
   (`api/app.py` wires `lint_command=("flake8", ".")`). The 14 prospector messages
   in 1.4 come from a manual measurement, not from the stage. Worse, `flake8` is
   not in `build_prep.REQUIRED_TOOLS`, so on a host without it the stage fails
   because a tool nobody checks for is absent. And `flake8`'s defaults run
   `pycodestyle` at 79 columns, which the plugins repository never runs at all and
   whose `E501` fires on hand-written code formatted to 120 — so restricting the
   stage to hand-written files is *necessary but not sufficient* for 2.7. This is
   what makes the second half of decision 2 below (`lint` judged by the same
   linter and profile as the `Quality_Gate`) part of the fix rather than a
   refactor.
2. **The pre-existing test failure has a different cause than 1.6 states.** On
   this host both profiles — the repository's and the vendored fallback — are
   byte-identical, and
   `tests/integrations/test_quality_gate.py::TestFindingsTheRepositoryWouldNotRaise`
   **passes** (4 passed, prospector on `PATH`). It fails with an empty finding set
   when `prospector` is not on the `PATH` the suite inherits, because
   `_check_prospector` then records a skip and returns no findings while the test
   asserts a finding is present. So the defect the test exposes is that a
   content-dependent assertion cannot tell "the linter said nothing" from "the
   linter never ran". 2.8's remedy still applies — pin the profile — and it needs
   one addition: skip explicitly when the tool is absent, which is the same
   distinction parent design Property 58 already makes.

### Bug 3 — the preview judges a stale draft, not the plugin

`prepare_export` derives everything from `session.draft` (`atomic_apply(session.draft, _suffix_vendor)`).
After a delegated implementation turn the agent has written `plugin.spec.yaml` to
the tree and nothing re-reads it, so the draft and the tree diverge and the
preview describes the draft.

**Formal specification:**

```
FUNCTION isBugCondition_3(X)
  INPUT: X of type Session
  OUTPUT: boolean

  RETURN implementationDelegated(X)
     AND diskSpec(projectFolder(X)) <> draftSpec(X)
END FUNCTION
```

#### Examples

- Same session that implemented the plugin: 16 completeness errors — 11 absent
  required top-level fields plus `connection.api_key.type: 'credential_token' is
  not a valid credential type`. On disk: all 11 fields present,
  `credential_secret_key`, 12 `example:` entries. Expected 0 findings.
- Same plugin reopened as `iterate_custom` (which loads from disk): 0 completeness
  findings, 23 top-level keys, `spec_complete` met, 2 outstanding conditions
  instead of 3. Same code, same stages; only the spec read differs.
- `HTTP_ERROR_MAP` defined in `util/constants.py` and imported into `util/api.py`
  — the pattern `implementation.md` prescribes — reported as
  `icon_jumpcloud/util/api.py: no HTTP_ERROR_MAP`. Expected `api_client` met.
- Edge case, and the reason this is worse than a reporting defect:
  `confirm_export` → `_build_dir` calls
  `project_folder.save(export_spec, generated_files=session.draft.code_files)`,
  which **writes the stale draft spec back over the tree** before packaging. A
  forced export does not merely mis-report the plugin; it ships the spec the
  preview complained about.

## Expected Behavior

### Preservation Requirements

**Unchanged behaviors** (`bugfix.md` 3.1–3.12, binding):

- Code generation. Endpoints, methods, payload shapes, `connect()`/`test()`,
  central `_make_request`, per-action source citations: untouched.
- `.plg` contents. `.builder/` and all reference material stay excluded; every
  file the plugin needs stays included — the 39-entry baseline less the
  byproducts 2.15 names.
- The export gate is the four-stage conjunction and nothing else (Property 17).
  The `Definition_Of_Done` stays advisory (27.6) and is presented beside a
  permitted preview (27.7).
- `unverified` stays distinct from `met` (27.5); a skipped check stays
  distinguishable from a passing one (26.4).
- The `Quality_Gate` keeps compiling, format-checking, and **running** the files
  under `unit_test/`. The exclusion in decision 2 is lint-only (3.7).
- Exactly one `insight-plugin` validator stays excluded, with its own check
  performed by `core/version_bump.py` (3.4).
- Repair-loop termination stays finding-key arithmetic with a stall condition, a
  round limit, and honest labelling (3.8).
- Delegation keeps prompt-on-stdin, default-deny environment, enumerated tools,
  and untrusted content out of a shell-capable agent's prompt (3.9).
- Reference material keeps its verbatim storage, provenance, and naming to the
  agent (3.10).
- Version bumping, registry records, and audit entries are unchanged (3.11).
- A forced export past a blocked gate still succeeds and is still recorded as
  forced (3.12).

**Scope.** A tree or session satisfying none of `C₁`–`C₃` must be reported exactly
as F reports it: the same stage verdicts, the same findings, the same export
decision, the same packaged contents (less byproducts).

**One knowingly accepted exception, recorded rather than hidden.** For a tree with
**failing** tests on a host where **Docker is absent**, F reports the `test` stage
failed with the Docker-unavailable message; F′ reports it failed with the pytest
failures. The verdict is identical and the message is better, but it is not
byte-identical, so the preservation property below is stated over verdicts and
decisions, not over message text.

The behavior required for inputs where a bug condition *does* hold is stated once,
in [Correctness Properties](#correctness-properties).

## Hypothesized Root Cause

### Bug 1: the stage asks the wrong environment

1. **The image is not a test environment, by construction.** `_stage_specs` builds
   `docker run --rm <image> python -m pytest -q`. Confirmed by reading the module:
   the command is unconditional and the default is never overridden in `app.py`.
   The tests are excluded from the image by the generated `.dockerignore` and
   `pytest` is absent from the runtime base image. Both fixes are edits to
   generated files.
2. **Two independent test executions exist.** `QualityGate._check_tests` runs
   `<target_python> -m pytest unit_test -q` on the host with `--cov` when
   `pytest-cov` is importable. Nothing relates the two, so nothing prevents them
   disagreeing. This is the direct cause of 1.2.
3. **Interpreter resolution is a single guess.** `resolve_target_python()` returns
   a pyenv interpreter in the SDK's target series or falls back to `python3`,
   checking neither that the SDK is importable nor that `pytest` is. On the
   tester's host neither candidate satisfies both, and nothing says so.
4. **The stage has no third state.** `StageStatus` is pass / fail / timeout, so
   "could not be run" has to be expressed as a fail with a message. 2.3 requires
   exactly that; the message content is what is missing.

### Bug 2: three different notions of "which files count"

1. **The lint stage has no notion at all.** `flake8 .` walks the whole tree,
   generated files included, under `flake8`'s own defaults.
2. **The `Quality_Gate` has two, and they are correct but private to it.**
   `is_generated` and `is_lint_excluded` live in `quality_gate.py`; nothing else
   imports them.
3. **Packaging has a third.** `build_engine._EXCLUDED_DIRS` covers directories
   only, so files like `.coverage` are packaged (1.10), and the list overlaps but
   does not coincide with the gate's.
4. **`black` is invoked over hand-written files only, and still reports five.**
   `_check_format` passes `hand_written_python(root)`, which excludes
   `setup.py`/`schema.py` by name — so the observed `formatted` shortfall must
   have been measured with bare `black` outside the tool, or the reported paths
   reached the check by another route. **Re-measure through
   `QualityGate.run()` before changing `_check_format`;** the requirement is about
   which files are judged, and this particular count may already be satisfied.
5. **The bar is discovered per run and never reported.** `resolve_lint_profile`
   records `source` and `detail` on the `LintProfile`, and the gate surfaces the
   detail only when the profile is non-authoritative or unresolved. A run under
   the repository's profile says nothing about which profile it used or at what
   line length it formatted.

### Bug 3: the draft is authored in one place and written in another

1. **No re-read after delegation.** `_delegate_implementation` returns an
   `AgentRunResult` carrying a summary; the agent's actual output is the tree, and
   `session.draft` is never refreshed from it. Confirmed by reading
   `submit_message` steps 5–7: the draft is committed *before* delegation and not
   touched after.
2. **`prepare_export` reads only the draft.** `spec_preview`, `check_completeness`,
   and `_evaluate_done`'s `spec=` argument all come from `session.draft.spec`.
   `_file_tree` already reads the tree — which is why the file list was right
   while the spec was wrong.
3. **The tree is written from the draft at export.** `_build_dir` saves the draft
   spec (and the draft's code-file map) into the project folder before packaging,
   so the divergence is resolved in the wrong direction.
4. **The `api_client` detector requires a literal assignment.**
   `_api_client_condition` builds `assigned` from `ast.Assign`/`ast.AnnAssign`
   targets in `api.py` alone, so `from .constants import HTTP_ERROR_MAP` cannot
   satisfy it. This one is independent of the draft/disk split; it is grouped here
   because it is the other reason a correct plugin read as incomplete.

## Correctness Properties

Numbering **continues the parent specification**, which uses Properties 1–62
(`.kiro/specs/insightconnect-plugin-builder/design.md`). The properties below are
63–75 and belong to this bugfix; the parent's are unchanged except for the
note Property 17 gains under [Specification amendments](#specification-amendments).
`Validates: Requirements` references are clauses of this spec's `bugfix.md`.

### Property 63: The preview describes what would be packaged

*For any* session whose delegated agent has written a spec to the tree, the
previewed spec equals the vendor-suffixed, version-bumped **on-disk** spec, the
preview's completeness findings equal `check_completeness` of that same spec, and
a spec that is complete on disk yields zero completeness findings.

**Validates: Requirements 2.11, 2.12**

### Property 64: The draft and the tree do not diverge

*For any* turn that changes the draft's spec in a session with a project folder,
the draft's spec after the turn equals the spec on disk; and *for any*
implementation turn, the draft after the turn is a view of the tree — so no
subsequent read of either can disagree with the other, and no export-time write
can overwrite the agent's work with an older value.

**Validates: Requirements 2.11**

### Property 65: The API client's error map may be defined or imported

*For any* plugin whose `util/api.py` either defines `HTTP_ERROR_MAP` or imports it
from elsewhere within the plugin package, the `api_client` condition is met; it is
unmet only when the map is neither defined nor imported there.

**Validates: Requirements 2.13**

### Property 66: One definition of the unit test run, in both directions

*For any* plugin working tree, the `test` stage's verdict equals whether the
plugin's unit tests pass under the resolved interpreter — pass when they pass,
fail when they fail — the `Quality_Gate`'s test findings are derived from the same
execution definition and the same interpreter, and the two never report
contradictory outcomes for one tree.

**Validates: Requirements 2.1, 2.2, 2.4**

### Property 67: An unrunnable test run fails closed and says why

*For any* tree whose unit tests cannot be run — no `unit_test/` directory, or no
resolved interpreter that can import both the SDK and `pytest` — the `test` stage
records a fail carrying the interpreter it used and the reason, never a pass; and
the `unit_tests_pass` condition is reported **unverified** rather than unmet or
met.

**Validates: Requirements 2.3**

Also preserves clause 3.6.

### Property 68: Lint and format judge hand-written code, and state the bar

*For any* plugin working tree, every lint and format finding refers to a
hand-written file; the set of excluded files is computed by a single definition
shared by the `Quality_Gate`, the `lint` stage, and the packaging exclusion; a
tree whose only defects lie in generated files reports the `lint` stage passed and
`formatted`/`lint_clean` met; a genuine defect in hand-written code is still
reported; and every lint or format result names the profile applied, its source,
and the line length used.

**Validates: Requirements 2.6, 2.7, 2.8, 2.9**

Also preserves clauses 3.3 and 3.7.

### Property 69: Packaging excludes byproducts and nothing else new

*For any* plugin working tree, the packaged file set excludes `.builder/`, every
reference document, and every build/test byproduct including `.coverage` at any
depth, and contains every other file present in the tree.

**Validates: Requirements 2.15**

Also preserves clause 3.2.

### Property 70: A blocked export reports each failing stage's output

*For any* blocked export preview, the payload contains, for every stage that did
not pass, that stage's error output bounded by the existing
truncation-with-full-access rule, with the full text retained.

**Validates: Requirements 2.16**

### Property 71: Announced work is work performed, and a long run keeps reporting

*For any* turn, no progress message announces generation work the turn does not
perform; and *for any* delegated run, progress carrying the current step is
emitted at bounded intervals for the duration of the run.

**Validates: Requirements 2.17, 2.19**

### Property 72: Every paid invocation is counted

*For any* sequence of interpreter and agent invocations, the session token total
equals the sum of the successful ones — the interpreter included — and failed
invocations are excluded.

**Validates: Requirements 2.18**

Consistent with the parent specification's Property 9.

### Property 73: Truncation is disclosed

*For any* attachment truncated for the interpreter prompt, the user is told the
file's name, its full size, the size included, and that the delegated agent
receives the whole file.

**Validates: Requirements 2.20**

### Property 74: Credential types come from the toolchain's own schema

*For any* connection field whose declared type is a credential type defined by the
installed `Insight_Plugin_CLI` schema — including `credential_token` with its
`{token, domain}` shape — the completeness check reports no finding; a type the
schema does not define is still reported.

**Validates: Requirements 2.22, 1.17**

Consistent with the parent specification's Property 61.

### Property 75: Preservation — nothing else changes

*For any* tree or session satisfying none of `isBugCondition_1`,
`isBugCondition_2`, or `isBugCondition_3`, F′ produces the same stage verdicts,
the same findings, the same condition statuses, the same export decision, and the
same packaged contents as F, up to the byproducts 2.15 removes and the one stage
**message** difference recorded under Preservation Requirements.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12**

## Fix Implementation

### Sequencing

Nine changes, each landing on its own (SCOPE-7). Two are refactors carrying no
behavior change and land **before** the behavior they enable (SCOPE-4). Bug 3
leads, per `bugfix.md`'s fix order.

| # | Change | Kind | Serves |
|---|--------|------|--------|
| 1 | One helper reads a `Draft` from a tree | refactor | 2.11 |
| 2 | Disk is authoritative; the preview describes the tree | fix | 2.11, 2.12 |
| 3 | `api_client` accepts an imported error map | fix | 2.13 |
| 4 | Path predicates move to one module | refactor | 2.6 |
| 5 | Lint and format judge hand-written code, and state the bar | fix | 2.6–2.9 |
| 6 | One definition of the unit test run | refactor | 2.4 |
| 7 | The `test` stage runs the tests on the host | fix | 2.1–2.3 |
| 8 | Packaging excludes byproducts | fix | 2.15 |
| 9 | Reporting and accounting (2.16–2.22) | fixes | 2.16–2.22 |

Each spec amendment lands **in the same change as the code that makes it true**,
never ahead of it: a requirement stating that tests run on the host while they
still run in the image is a false statement in the authoritative document.

### Change 1 — one helper reads a `Draft` from a tree (refactor)

**File**: `orchestrator/orchestrator.py`.

`_start_iterate` and `_start_enhance` already do exactly this, twice:
`load_plugin_spec(folder.spec_path.read_text(...))` plus `_read_dir_tree(folder.path)`.
Add a module-level helper `_draft_from_folder(folder) -> Draft` and call it from
both. No new public surface, no cross-module abstraction — two existing callers
plus the one change 2 needs, so SCOPE-9's second-caller test is met by production
code that exists.

### Change 2 — disk is authoritative; the preview describes the tree

**Decision as recorded (2.11): disk wins**, because disk is what gets packaged.

**File**: `orchestrator/orchestrator.py`.

1. **Sync after the agent has run.** In `submit_message`, after the repair loop
   and *before* `_evaluate_done`, when a project folder exists and code was
   delegated: `session.draft = _draft_from_folder(folder)`, and
   `session.baseline_spec = deepcopy(draft.spec)` so the next turn's
   structural-change detection compares against what is actually on disk. Repairs
   can touch the spec too, which is why the sync follows the loop rather than the
   implementation run. `TurnResult.spec` then carries the real spec, and
   `_evaluate_done`'s `spec_complete` reads it.
2. **Fail safe, never clobber.** If the on-disk spec cannot be read or parsed, the
   draft is left as it is and the turn message reports the parse failure. A
   syntactically broken spec is a finding, not grounds for discarding the session.
3. **Write on every spec change, not only structural ones.** This is what makes
   "disk wins" safe. The tradeoff 2.11 states — that after an implementation turn
   the draft becomes a view of the tree, so an in-session edit must reach the tree
   to survive — has a concrete hole today: a *non-structural* edit (a description,
   say) triggers no refresh, so nothing writes it, and a later re-read would
   silently discard it. Closing it: when a turn changes the spec and a project
   folder exists, persist the spec (`ProjectFolder.save`) whether or not the change
   was structural. The refresh of derived files stays gated on structural change
   exactly as now. With this, Property 64 holds at every turn boundary and the
   re-read in step 1 and step 4 is a no-op when nothing else wrote.
4. **`prepare_export` derives from the tree.** Re-read the draft from the folder at
   the top of `prepare_export`, before `_suffix_vendor`, so `spec_preview`,
   `check_completeness`, and the `Definition_Of_Done`'s `spec=` all describe the
   spec that would be packaged. The preview stays non-mutating with respect to the
   draft (Req 16.6): the suffixed, bumped spec still rides on the returned
   `ExportPlan` only.
5. **Stop the export-time write-back from being a write-back.** `_build_dir`
   currently saves the draft spec *and* the draft's code-file map over the tree.
   With the draft a view of the tree the spec write is the version bump and vendor
   suffix only, which is correct and required. The code-file map write is not: when
   a project folder exists, the tree is already the source of those files, so pass
   no `generated_files` in that branch. The temporary-directory branch, which
   materializes an in-memory net-new draft, keeps writing them — that is the one
   case where the draft is the only copy.

**What this does not change**: the file list and diff already read the tree
(`_file_tree`), the gate's conjunction, and the advisory status of the definition
of done.

### Change 3 — `api_client` accepts an imported error map

**File**: `integrations/definition_of_done.py`, `_api_client_condition`.

Alongside the existing assigned-name check, treat `HTTP_ERROR_MAP` as present when
`api.py` imports it from within the plugin package: an `ast.ImportFrom` whose
`names` include `HTTP_ERROR_MAP` and whose module is relative (`level > 0`) or
begins with the package name. Report unmet only when it is neither defined nor
imported (2.13). A dangling import — imported from a module that does not define
it — is left to the linter and the compile check, which report it with a location;
duplicating that judgment here would report one defect twice.

`_make_request` and the "at least one public domain method" checks are unchanged.

### Change 4 — path predicates move to one module (refactor)

**New file**: `core/plugin_files.py`. Pure path logic, no I/O, so it belongs in
`core/` beside the other pure predicates.

Three predicates, kept distinct because they mean different things:

- `is_generated(path)` — emitted by the CLI, forbidden to edit, **still packaged**.
  Moved verbatim from `quality_gate.py` with `GENERATED_FILE_NAMES` /
  `GENERATED_DIR_NAMES`.
- `is_lint_excluded(path)` — everything generated, plus `unit_test/`. Moved
  verbatim. Lint only: `unit_test/` stays compiled, formatted, and executed (3.7).
- `is_packaging_excluded(path)` — `.builder/`, VCS and cache directories, and
  byproduct files. Replaces `build_engine._EXCLUDED_DIRS`.

`hand_written_python(root)` moves with them.

**Consumers, all task-required and all production**: `quality_gate.py` (findings),
`code_validator.py` (the `lint` stage's file set, change 5), `build_engine.py`
(`list_plugin_files`, change 8). Three callers, so SCOPE-9 is satisfied by need
rather than by anticipation; and per SCOPE-4 this lands first, as a move with no
behavior change — `quality_gate` re-exports nothing, its importers are updated in
the same commit.

**Decision as recorded (2.6)**: files are excluded **because the CLI generates them
and the rulebook forbids editing them**, not because they produced findings. The
tradeoff stands: a genuine defect inside a generated file will not be reported by
these checks, and cannot be fixed in the plugin if it were.

### Change 5 — lint and format judge hand-written code, and state the bar

**Files**: `integrations/code_validator.py`, `integrations/quality_gate.py`,
`integrations/build_prep.py`, `api/app.py`.

1. **The `lint` stage is judged by the same linter and profile as the
   `Quality_Gate`.** Replace `flake8 .` with prospector under the resolved
   profile, `LINT_TOOLS`, and the hand-written file set, deriving the stage verdict
   from that finding set: pass iff no finding refers to a hand-written file. This
   goes beyond "exclude generated files" deliberately, and 2.7 is why — `flake8`'s
   defaults include `pycodestyle`, which the plugins repository never runs, so a
   plugin a reviewer would call clean fails the stage on codes that are outside the
   bar no matter which files are excluded. It also removes the second
   two-subsystems-disagree case of the same shape as 2.4, and it removes a stage
   that could fail because `flake8` — absent from `REQUIRED_TOOLS` — is not
   installed.
   - **Line length**: `PLUGIN_LINE_LENGTH` (120) is applied wherever a width is
     applied, so the tool and the repository's CI agree.
   - **Empty file set**: a tree with no hand-written Python records a pass with a
     message naming zero files linted. Stated as a tradeoff, not hidden: the stage
     has no third state, and the honest report of that tree lives in the
     `Definition_Of_Done`, where `code_parses` is **unverified**.
2. **Format check**: re-measure first (see [root cause 2.4](#bug-2-three-different-notions-of-which-files-count)).
   `_check_format` already restricts to `hand_written_python`; if the re-measured
   run is clean, the code change here is nil and the finding is closed by
   measurement rather than by edit.
3. **Report the bar (2.8).** The applied profile path, its source
   (`repository` / `fallback`), and the applied line length are carried on
   `QualityReport` and on the `lint` stage's result, and serialized into the export
   payload. **Decision as recorded: runtime discovery is kept** — a vendored second
   copy of someone else's rules drifts, and then the two disagree about what clean
   means (parent tasks.md task 38). The tradeoff, now stated rather than removed:
   two operators with different checkouts can still see different findings; what
   changes is that the report says which bar produced them.
4. **Pin the profile in content-dependent tests (2.8), and skip when the tool is
   absent (2.9).** `QualityGate` already accepts `lint_profile`; the tests in
   `TestFindingsTheRepositoryWouldNotRaise` pass an explicit `LintProfile` pointing
   at a fixture copy, so the assertion no longer varies with the developer's home
   directory, and guard on `shutil.which("prospector")` so a missing linter is a
   skip rather than a false failure. That guard is the actual repair for the
   reported pre-existing failure, per the corrected diagnosis. The
   `undefined-variable` case in 2.9 stays as the proof the linter is still on.

### Change 6 — one definition of the unit test run (refactor)

**New file**: `integrations/plugin_tests.py`, moved out of
`QualityGate._check_tests`.

- `UnitTestRun` (frozen): `interpreter`, `ran`, `no_tests`, `failures`
  (`path`, `line`, `name`), `returncode`, `output`, `coverage_percent`,
  `skipped` notes, `message`.
- `async run_unit_tests(project_dir, *, python_executable, timeout_seconds) -> UnitTestRun`
  — the mechanics only: the `pytest` invocation, the `pytest-cov` probe, the
  failure and coverage parsing that `quality_gate` already implements. No
  findings, no verdicts.
- `resolve_test_interpreter()` joins `resolve_sdk_version`,
  `resolve_target_python`, and `resolve_lint_profile` in `build_prep.py`, which is
  where this tool already resolves what it depends on. It requires a candidate
  that can import **both** the SDK and `pytest`, tries the pyenv target-series
  interpreter, then `sys.executable`, then `python3` on `PATH`, and reports the
  candidate chosen and, for each rejection, which of the two imports failed. This
  exists because on the tester's host neither single candidate satisfies both, and
  a resolver that cannot say so produces exactly Bug 1's silence.

`QualityGate._check_tests` becomes a thin adapter from `UnitTestRun` to
`CodeFinding`s, preserving today's finding shapes, keys, and skip notes verbatim —
that equality is what makes this a refactor.

**SCOPE-12**: `pytest` is **not** added to this tool's dependencies. It has to be
present in the plugin's interpreter, and its absence is reported with remediation,
never installed silently.

### Change 7 — the `test` stage runs the tests on the host

**Decision as recorded (2.1): option (a), host-run.** The stage no longer
establishes that the tests pass *in the shipping environment*; that intent is
unreachable without edits to `.dockerignore` and the Dockerfile the rulebook
forbids. Option (b) (a test-only image layer) keeps the stronger property and is
rejected on cost; option (c) (dropping the stage) is rejected because Requirement
8 names four stages and Property 17 is a conjunction over four.

**Files**: `integrations/code_validator.py`, `orchestrator/orchestrator.py`,
`api/app.py`.

1. The `test` stage calls `run_unit_tests` instead of `docker run`. Its
   `StageResult` carries the pytest output as `stdout`, `returncode` from the run,
   and a `message` naming the interpreter used.
2. `requires_docker` becomes `False` for `test`. Consequence, recorded: on a host
   without Docker the pipeline now yields lint and test results rather than lint
   alone — more partial offline feedback, in line with the existing
   "Docker-optional" design note. The gate's conjunction is untouched, so export is
   still blocked while `build` and `validate` cannot run.
3. The 600s abort (Req 8.8) still applies, now to the host run.
4. **Unrunnable fails closed (2.3)**: no `unit_test/`, no interpreter satisfying
   both imports, or `pytest` absent → `StageStatus.FAILED` with a message naming
   the interpreter and the reason. Never a pass. The `Definition_Of_Done` reports
   `unit_tests_pass` **unverified** in the same situation, which is the distinction
   3.6 requires — the gate fails closed, the advisory report stays honest. The
   operator-facing consequence is stated plainly: on a host with no suitable
   interpreter, export requires `force`. That is what 2.3 asks for.
5. **One execution per export (2.4)**: in `prepare_export`, run the
   `Quality_Gate` first and pass its `UnitTestRun` into
   `run_pipeline(project, unit_test_run=...)`, which uses it instead of running the
   suite again. When no run is supplied — a caller holding only the validator — the
   stage runs its own. One execution, one interpreter, one verdict; and the export
   preview does one fewer test run than today. A genuinely flaky test can still
   differ between two executions; with a single execution per preview there is only
   one to report.

### Change 8 — packaging excludes byproducts

**File**: `integrations/build_engine.py`.

`list_plugin_files` filters through `is_packaging_excluded`, which adds `.coverage`
and `.coverage.*` at any depth to the existing directory exclusions. Because
`list_plugin_files` is the single source of truth the packager and the preview
both consume (parent Property 30), the preview file list changes with it. The
39-entry baseline less those byproducts is the expected result (3.2).

### Change 9 — reporting and accounting

Each of these is small and lands on its own; they are grouped only for reading.

- **2.16, blocked-export detail**: `api/app.py`'s `_serialize_export_plan` gains
  `failed_stages: [{name, status, returncode, message, displayed_output,
  full_output, truncated}]`, built from `plan.pipeline_report.failed_stages`
  through the existing `core/truncation.truncate_error_output`
  (`MAX_DISPLAY_CHARS` = 10,000, full text retained — Req 19.5). Every failing
  stage, not just the first, which is where `classify_build_failure` stops. No new
  dataclass: the data is already on the report. The preview UI renders the new
  field.
- **2.17 and 2.19, progress**: the "Generating logic for N action(s)…" frame moves
  out of the websocket handler's pre-submit path — where it is emitted from the
  plan before the orchestrator decides anything — and becomes a step reported by
  the orchestrator when it actually dispatches code requests. A minimal
  `ProgressReporter` protocol (`report(step: str)`) is passed into
  `submit_message`; the orchestrator reports at phase boundaries (applying
  operations, scaffolding, refreshing, implementing, repair round *n* of *m*,
  checking, evaluating done), and the websocket route wraps it to send a `status`
  frame plus a ticker task that re-emits the current step with elapsed seconds
  while a phase runs, so a 13-minute agent run is distinguishable from a hang. One
  implementation, one caller: not speculative generality but the only shape that
  lets the orchestrator report without importing the API layer.
- **2.18, interpreter usage**: `Interpreter.interpret` takes the `session_id` and
  the `CostController` and calls `record_usage(..., succeeded=True/False)` exactly
  as `LLMGenerator` does, so a paid call is counted where it happens. Parent
  Property 9 (total equals the sum of successful invocations) already required
  this; the interpreter was simply outside it. Gating the interpreter through
  `authorize()` is **not** in scope — a budget-exhausted session that cannot parse
  a message is a different decision — and is recorded here so the omission is
  visible.
- **2.20, truncation notice**: `interpret` records, per truncated attachment, the
  name, full size, included size, and that the delegated agent receives the whole
  file (it does — attachments are written verbatim into `.builder/reference/`).
  The notices ride the turn payload. The 60,000-character cap itself is unchanged.
- **2.21, `version_display`**: **diagnose before editing.** `apply_version_bump`
  produces `"<previous> -> <new>"` and `prepare_export` sets the display only when
  `bump.changed`. If the observed run had no prior export, an empty display is
  Req 12.7 behaving correctly and the defect is that the preview shows no version
  at all — fixed by populating the display with the version that would be
  exported, marked as unchanged (which Req 12.6 does not speak to and so does not
  contradict). If a bump did occur and the display was still empty, the defect is
  in the propagation and the fix is there. The task begins by reading the run's
  registry state; only then is the code change chosen.
- **2.22, credential types**: `credential_token` is defined by the installed
  toolchain — `insight_plugin/features/common/schema_util.py:109`, a required
  `token` (password-formatted) and an optional `domain`. Verified directly in the
  installed 1.9.20 package. Two edits follow: `VALID_CREDENTIAL_TYPES` in
  `core/spec_completeness.py` gains it, and the comment above it — which currently
  offers `credential_token` as the example of a type the platform does *not*
  define — is corrected. A test cross-checks the tuple against the installed
  schema and skips when `insight_plugin` is not importable, so the two cannot drift
  silently. The steering correction (`plugin-spec.md`) lands in the plugins
  repository, since `~/.kiro` is a symlink into it; it is outside this repo's test
  surface and is recorded as an accompanying change rather than a verified one.

### Specification amendments

Each lands with its change, and each carries a revision note in the parent
document saying what changed and why, matching the convention the parent's other
revision notes follow.

| Parent document | Amendment | Lands with |
|---|---|---|
| Requirement 8.3 | States that the plugin's unit tests are run **on the host under the resolved target interpreter**, and that an unrunnable test run records a fail naming the interpreter and the reason. | Change 7 |
| Design Property 17 | Unchanged as a conjunction over four stages; gains a note that the `test` stage is a host-run check and why the in-image intent was given up. | Change 7 |
| Requirement 26.3 | Names the single definition of generated files that the `Quality_Gate`, the `lint` stage, and the packaging exclusion all consume. | Changes 4, 5 |
| Requirement 27.1 | `lint_clean` and the formatting condition apply to **hand-written code only**; the API client's error map may be **defined in or imported into** the client module. | Changes 3, 5 |
| Requirement 16.1 | The previewed spec is the spec that would be packaged. | Change 2 |
| tasks.md "Remaining work" | Records this bugfix and the parts of it outstanding, replacing "Nothing outstanding" while it is. | every change |

The parent's Requirement 8 revision note (four stages are the export gate and only
the export gate) and Requirement 27's advisory note are unaffected: this design
changes *what a stage measures*, never *what gates an export*.

### Repository constraints observed

- **SCOPE-4**: changes 1, 4, and 6 are refactors with no behavior change and land
  before the behavior that needs them.
- **SCOPE-7**: nine changes, nine purposes. The two fixes already sitting in the
  working tree (the frozen-dataclass `force` crash, the missing-UI diagnostic) stay
  their own commits and are out of scope here.
- **SCOPE-9**: every extraction names its callers above. `core/plugin_files.py` has
  three; `_draft_from_folder` has three; `plugin_tests.run_unit_tests` has two;
  `resolve_test_interpreter` joins two existing resolvers in the module that owns
  resolution. Nothing is extracted for a caller this work does not require.
- **SCOPE-12**: no dependency is added, removed, or upgraded. `pytest` must exist
  in the plugin's interpreter; `flake8` stops being invoked but was never declared,
  and dropping its use is a wiring change in `app.py`, not a manifest change.
- **SCOPE-13, and an in-flight collision to expect**: `main` is not static and a PR
  is pending. **The lint path is the likely collision**: `quality_gate.py`,
  `build_prep.py` (`resolve_lint_profile`, `LINT_TOOLS`, `PLUGIN_LINE_LENGTH`),
  `tests/integrations/test_quality_gate.py`, and the `lint_command` wiring in
  `app.py` are the files most recently touched by lint work and the ones changes 4
  and 5 rewrite. Integrate current `origin/main` and re-run the affected gates
  before finalizing those two; if the pending PR has already moved the predicates
  or the profile plumbing, change 4 shrinks to updating imports rather than moving
  definitions. No git operations are performed as part of writing this design, and
  `origin/main` is not assumed reachable.

## Testing Strategy

### Validation approach

Two phases. First surface counterexamples on the **unfixed** code, to confirm or
refute each root cause — three of the hypotheses above are already partly refuted
by reading the code (the `lint` stage runs `flake8`, not prospector; the
pre-existing test failure is a missing tool rather than a divergent profile), so
this phase is not a formality. Then verify the fix and that everything else is
unchanged.

### Exploratory bug-condition checking

**Goal**: demonstrate each bug before fixing it, and re-measure the figures
`bugfix.md` flags as taken with bare tools.

**Test cases**

1. **Test stage on a healthy plugin** — build the JumpCloud tree's image, run the
   stage's exact command, assert a pass. Fails on unfixed code: no
   `/python/src/unit_test`, no `pytest` module.
2. **The contradiction** — one tree, `Quality_Gate` and pipeline together; assert
   the two agree about the unit tests. Fails on unfixed code (met vs. failed).
3. **Lint stage as the tool actually runs it** — `flake8 .` in the JumpCloud tree,
   partitioned into generated and hand-written paths. Expect generated-file
   findings *and* `E501` on hand-written lines at 120 columns, which is the
   measurement that decides how much of change 5 is required.
4. **Format check through the tool** — `QualityGate.run()` on the JumpCloud tree
   and on `abuseipdb`; record which paths `black` would reformat. If none are
   hand-written, 1.5 is closed by measurement.
5. **Profile provenance** — run the gate with the plugins checkout present and
   again with it hidden; assert the report states which profile was used. Fails on
   unfixed code for the authoritative case, which reports nothing.
6. **Preview fidelity** — in the session that delegated implementation, compare
   `plan.spec_preview` with the on-disk spec and the preview's completeness
   findings with `check_completeness(diskSpec)`. Fails on unfixed code: 16 findings
   versus 0.
7. **Write-back** — force an export from that session and read
   `plugin.spec.yaml` out of the produced `.plg`. Expect the stale draft spec,
   demonstrating that Bug 3 reaches the artifact and not only the report.
8. **Imported error map** — a fixture plugin defining `HTTP_ERROR_MAP` in
   `util/constants.py` and importing it into `util/api.py`; assert `api_client`
   met. Fails on unfixed code.
9. **Edge case, byproducts** — run the tests, package, list the members; expect
   `.coverage` and `unit_test/.coverage` present. Fails (i.e. reproduces) on
   unfixed code.
10. **Edge case, split interpreters** — two fake interpreters, one importing the
    SDK without `pytest`, one the reverse; assert the resolver rejects both with
    reasons and the stage fails closed naming what it tried. Nothing to run
    against on unfixed code; this is the case the tester's host produced and the
    one 2.3 exists for.

**Expected counterexamples**: the `test` stage failing for a reason no plugin edit
can address; lint findings located exclusively in files the rulebook protects; a
completeness report about a spec that is not the one on disk; and — new relative to
`bugfix.md` — a stage verdict that depends on whether `flake8` happens to be
installed.

### Fix checking

**Goal**: for all inputs where a bug condition holds, the fixed system produces
the required behavior.

```
FOR ALL X WHERE isBugCondition_1(X) DO
  report := runPipeline'(X)
  ASSERT stage(report, 'test').passed
  ASSERT stage(report, 'test').passed = hostUnitTestsPass(X)
  ASSERT decideExport'(specReport(X), report).permitted
END FOR

FOR ALL X WHERE isBugCondition_2(X) DO
  report := qualityGate'(X)
  ASSERT findings(report) = EMPTY
  ASSERT stage(runPipeline'(X), 'lint').passed
  ASSERT condition(doneReport'(X), 'formatted').met
  ASSERT condition(doneReport'(X), 'lint_clean').met
  ASSERT profileSource(report) IS REPORTED
END FOR

FOR ALL X WHERE isBugCondition_3(X) DO
  plan := prepareExport'(X)
  ASSERT plan.spec_preview = versionedVendorSuffixed(diskSpec(projectFolder(X)))
  ASSERT completenessFindings(plan) = checkCompleteness(diskSpec(projectFolder(X)))
  ASSERT isCompleteOnDisk(X) IMPLIES completenessFindings(plan) = EMPTY
END FOR

FOR ALL X WHERE errorMapImportedIntoClient(X) DO
  ASSERT condition(doneReport'(X), 'api_client').met
END FOR
```

### Preservation checking

**Goal**: for all inputs where no bug condition holds, F′ decides what F decides.

```
FOR ALL X WHERE NOT (isBugCondition_1(X) OR isBugCondition_2(X)
                     OR isBugCondition_3(X)) DO
  ASSERT verdicts(F(X)) = verdicts(F'(X))
END FOR
```

`verdicts` is deliberately the comparison unit rather than whole reports: stage
statuses, finding keys, condition statuses, the export decision, and the packaged
member set. One message differs by design (Docker-absent trees, recorded under
Preservation Requirements), and byproducts leave the member set by design.

Property-based testing is the right instrument here because the preservation claim
is universally quantified over trees and the interesting cases are combinatorial:
which tools are present, which files are generated, whether the spec is complete,
whether the tests pass. Generators produce trees along those axes.

**Test plan**: capture F's behavior on the unfixed code first for each axis, then
assert F′ reproduces it.

1. **Genuinely failing test** — a tree whose `test_suspend_user` fails: stage
   fails before and after, and the failure names the test.
2. **Genuine hand-written defect** — `requests` used and never imported: reported
   before and after, same path, same code, same key.
3. **Genuinely incomplete on-disk spec** — missing `sdk`, missing output examples:
   the same completeness findings before and after, now against the disk spec.
4. **Missing tool** — prospector absent: skipped, `lint_clean` unverified, before
   and after; a missing linter never reads as a clean lint (26.4, 27.5).
5. **Advisory boundary** — a tree clearing four stages with outstanding
   definition-of-done conditions: `permitted: true` with the conditions presented,
   before and after (27.6, 27.7).
6. **Packaged contents** — `.builder/` and every reference document absent from
   the `.plg`; every plugin file present; the member set equals the baseline less
   byproducts (3.2).
7. **Forced export** — still succeeds, still recorded as forced (3.12).
8. **Repair loop** — key arithmetic, stall, and round limit unchanged, with
   honest labelling (3.8).
9. **Delegation isolation** — stdin prompt, default-deny environment, enumerated
   tools (3.9); unchanged by any change here, asserted because change 9 touches
   the interpreter's call path.

### Unit tests

- `resolve_test_interpreter`: each candidate accepted, each rejected with the
  reason, none available.
- `run_unit_tests`: pass, fail with parsed failures, no `unit_test/`, no tests
  collected, collection error, coverage measured, `pytest-cov` absent.
- `test` stage: pass, fail, timeout at 600s, unrunnable-fails-closed, message
  names the interpreter, Docker-absent still runs.
- `is_generated` / `is_lint_excluded` / `is_packaging_excluded`: table-driven over
  each named file and directory, plus `.coverage` at depth, plus a `unit_test/`
  path that is lint-excluded and *not* generated.
- `_api_client_condition`: defined, relative import, absolute in-package import,
  import from outside the package, neither.
- `_draft_from_folder`: unreadable spec, unparseable spec, and the draft-preserved
  fallback.
- `prepare_export`: preview equals disk spec; a non-structural in-session edit
  made after an implementation turn survives (Property 64's hole, closed).
- Serialization: `failed_stages` carries every failing stage, truncation at
  10,000 characters with `full_output` retained.
- Interpreter: usage recorded on success, excluded on failure, truncation notice
  content.
- `VALID_CREDENTIAL_TYPES`: `credential_token` accepted, an invented type still
  reported, cross-check against the installed schema (skipped when absent).

### Property-based tests

One test per property, Hypothesis, minimum 100 examples, tagged
`# Feature: export-gate-and-preview-fidelity, Property {number}: {property_text}`
per the parent's convention.

- Property 63, 64: generate specs, mutate them on disk and in session, assert the
  preview and the draft track the tree.
- Property 66: generate trees with passing and failing suites; assert the stage
  verdict equals the host result and the two subsystems never disagree.
- Property 68: generate trees mixing generated and hand-written defects; assert
  every finding is hand-written and the bar is reported.
- Property 69: generate trees with arbitrary byproduct placement; assert the
  member set is exactly the tree less exclusions.
- Property 72: generate interleaved successful and failed interpreter and agent
  invocations; assert the total equals the successful sum.
- Property 75: the preservation comparison above, over the generated axes.

Properties 65, 67, 70, 71, 73, 74 describe behavior at process and filesystem
boundaries — interpreter probes, subprocess output, websocket framing — where
example-based tests over the real interfaces are more informative, and they are
covered accordingly. This follows the parent's own split at Properties 52–62.

### Integration tests

- **Whole preview, real toolchain, mocked Docker**: a delegated implementation
  turn followed by `export/prepare`; assert `permitted: true`, zero completeness
  findings, `spec_preview` equal to disk, and the profile and interpreter named.
- **Blocked preview**: a tree with a genuine hand-written defect and a failing
  test; assert both stages appear in `failed_stages` with their output, and that
  `force` is not required for anything that actually passes.
- **Split-interpreter host**: the tester's configuration reproduced with fake
  interpreters; assert the stage fails closed, the condition is unverified, and
  the message names the interpreter — this is the one Bug 1 case that only appears
  when the SDK and `pytest` live in different interpreters.
- **Progress and cancellation**: drive one long delegated run over the websocket;
  assert no gap between frames exceeds the reporting interval, that each frame
  names a step, and that no frame announces generation for a turn that ends in a
  clarification.

### Known gaps this design does not close

Recorded so they are not mistaken for covered. Each is from `bugfix.md`'s own list
and stays open:

- **Accessibility is unreviewed.** The preview gains a `failed_stages` region and
  new status frames; neither has been checked for keyboard operation, focus order,
  or screen-reader announcement. Full validation needs manual testing with
  assistive technology and expert review.
- No tenant import has been performed.
- The PDF and `reference_urls` paths remain unreachable from the browser
  (`MessageInput.tsx` accepts four text extensions; `MessageAttachment` carries no
  `encoding`/`media_type`). Closing that is its own task.
- How a built UI reaches a new user is a packaging decision, out of scope here.
