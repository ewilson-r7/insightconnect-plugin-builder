# Implementation Plan: Export Gate and Preview Fidelity

## Overview

This plan fixes three defects in the gate and reporting layer. **Nothing here
touches code generation** — the JumpCloud plugin the run produced is correct
(`bugfix.md` 3.1), and no task in this plan edits a generation path.

The spine of the plan is the nine-change sequencing table in `design.md`
"Fix Implementation", used in its own order rather than a re-derived one:

| # | Change | Kind | Serves | Task |
|---|--------|------|--------|------|
| 1 | One helper reads a `Draft` from a tree | refactor | 2.11 | 3 |
| 2 | Disk is authoritative; the preview describes the tree | fix | 2.11, 2.12 | 4 |
| 3 | `api_client` accepts an imported error map | fix | 2.13 | 5 |
| 4 | Path predicates move to one module | refactor | 2.6 | 6 |
| 5 | Lint and format judge hand-written code, and state the bar | fix | 2.6–2.9 | 7 |
| 6 | One definition of the unit test run | refactor | 2.4 | 8 |
| 7 | The `test` stage runs the tests on the host | fix | 2.1–2.3 | 9 |
| 8 | Packaging excludes byproducts | fix | 2.15 | 10 |
| 9 | Reporting and accounting | fixes | 2.16–2.22 | 11 |

**Bug 3 leads** (changes 1–3, tasks 3–5): it is the defect that reports a correct
plugin as broken, so it is what makes the tool untrustworthy even once the two
gate stages are repaired. The three refactors (changes 1, 4, 6 — tasks 3, 6, 8)
carry no behavior change and each lands **before** the fix that needs it, per
SCOPE-4. Their SCOPE-9 second-caller justifications are already written in the
design and are carried into the task text so nobody has to re-derive them.

**Two numbering schemes are in play, deliberately.** Tasks 1 and 2 use the
bug-condition workflow's `**Property 1: Bug Condition**` / `**Property 2:
Preservation**` labels, which are the exploration and preservation tests for the
bugfix as a whole. The design's own correctness properties continue the parent
specification's numbering at **63–75** and are referenced by those numbers in the
test tasks, matching the parent `tasks.md` convention
(`**Property N** — **Validates: Requirements X.Y**`). `_Requirements:_`
annotations refer to clauses of this spec's `bugfix.md`.

**Measurement precedes several edits.** The design defers four decisions to
measurement, because reading the code already partly refuted the reported
diagnosis: the `lint` stage runs `flake8 .` and not prospector, the format check
already restricts to hand-written files, the pre-existing test failure is a
missing tool rather than a divergent profile, and `version_display` may be
Requirement 12.7 behaving correctly. Those measurements are tasks 1.x and 11.5,
and they come before the code changes they inform. A nil code change is a legitimate
outcome for two of them.

**Out of scope, recorded so it is not mistaken for missing.** Code generation
(3.1). The two fixes already sitting in the working tree — the frozen-dataclass
crash on a `force` export and the missing-UI diagnostic — which land as their own
two commits per SCOPE-7. How a built UI reaches a new user, which is a packaging
decision. No tasks exist for any of these.

Property-based tests use Hypothesis at a minimum of 100 examples, tagged
`# Feature: export-gate-and-preview-fidelity, Property {number}: {property_text}`.

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** — A Correct Plugin Reported As Broken
  - **CRITICAL**: These tests MUST FAIL on unfixed code — failure confirms the bugs exist
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: These tests encode the expected behavior; they validate the fix when they pass after implementation (tasks 5.4, 7.5, 9.6)
  - **GOAL**: Surface counterexamples that demonstrate each bug, and re-measure the figures `bugfix.md` flags as taken with bare tools
  - **Scoped PBT Approach**: The three bug conditions are deterministic against the JumpCloud tree at `~/.icplugin-builder/projects/jumpcloud/`, so scope each property to that concrete tree (plus the `abuseipdb` and `rapid7_velociraptor` trees for the systemic claims) rather than generating trees; the generated-tree generalization arrives with Properties 66, 68, 69 in tasks 7.6, 9.7, 10.2
  - Place these in `tests/integrations/test_export_gate_bug_conditions.py` and `tests/orchestrator/test_preview_fidelity_bug_conditions.py`
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct — it proves the bugs exist)
  - Document every counterexample found, since three root-cause hypotheses are already partly refuted by reading the code and this phase decides how much of changes 5 and 9 is required
  - Mark complete when the tests are written, run, and the failures are documented
  - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.7, 1.9, 1.10_

  - [x] 1.1 `isBugCondition_1` — the `test` stage can never pass
    - Build the JumpCloud tree's image and run the stage's exact command
      (`docker run --rm <image> python -m pytest -q`, `code_validator._stage_specs`); assert a pass
    - **Expected counterexample**: no `/python/src/unit_test` (the generated `.dockerignore` excludes `unit_test/**/*`) and no `pytest` module in `rapid7/insightconnect-python-3-slim-plugin`
    - Assert `hostUnitTestsPass(X) AND dockerignoreExcludes(X, 'unit_test') AND NOT imageHasPytest(X)` holds for this tree
    - **The contradiction**: run `Quality_Gate` and the pipeline over the one tree and assert they agree about the unit tests; expect `unit_tests_pass` met versus `test` stage failed
    - **Edge case, split interpreters**: two fake interpreters, one importing the SDK without `pytest`, one the reverse; record that neither candidate satisfies both and that nothing in `resolve_target_python()` says so. This is the tester's own host configuration and the case 2.3 exists for
    - _Requirements: 1.1, 1.2, 1.3_

  - [x] 1.2 `isBugCondition_2` — re-measure the `lint` stage as the tool actually runs it
    - **This is a measurement task whose outcome branches, not a formality.** The design found the stage runs `flake8 .` (wired at `api/app.py:767`, defaulted at `code_validator.py:203`), **not** prospector — so the 14 prospector messages in `bugfix.md` 1.4 came from a manual measurement and not from the stage
    - Run `flake8 .` in the JumpCloud tree and partition the findings into generated and hand-written paths using `quality_gate.is_generated`
    - **Expect both**: findings in generated `__init__.py`/`schema.py`, *and* `E501` on hand-written code formatted to 120 columns, because `flake8`'s defaults run `pycodestyle` at 79 and the plugins repository never runs `pycodestyle` at all
    - Record whether `flake8` is on the host `PATH` at all; it is absent from `build_prep.REQUIRED_TOOLS`, so the stage can fail because a tool nobody checks for is missing
    - **Outcome decides task 7's size**: if `E501` fires on hand-written lines, excluding generated files is necessary but not sufficient for 2.7, and replacing the linter is part of the fix rather than a refactor
    - _Requirements: 1.4, 1.6, 2.7_

  - [x] 1.3 Re-measure the format check **through `QualityGate.run()`**
    - `_check_format` already passes `hand_written_python(root)`, which excludes `setup.py` and `schema.py` by name, so the five-file shortfall in 1.5 must have been measured with bare `black` outside the tool
    - Run `QualityGate.run()` against the JumpCloud tree and against `abuseipdb`; record exactly which paths `black --check --line-length 120` reports
    - **A nil code change is an acceptable and expected outcome.** If no reported path is hand-written, 1.5 is closed by measurement rather than by edit, and task 7.2 shrinks to recording that
    - _Requirements: 1.5, 2.7_

  - [x] 1.4 Confirm the corrected diagnosis of the pre-existing test failure
    - `bugfix.md` 1.6 attributes `TestFindingsTheRepositoryWouldNotRaise::test_a_real_defect_is_still_reported` to profile divergence. The design found otherwise: on this host the repository profile and the vendored fallback are **byte-identical** and the test **passes** with prospector on `PATH`
    - Run the test with `prospector` on `PATH` and again with it removed from `PATH`; assert the outcomes differ
    - **Expected counterexample**: with the tool absent, `_check_prospector` records a skip and returns no findings while the test asserts a finding is present — the assertion cannot distinguish "the linter said nothing" from "the linter never ran", which is the same distinction parent design Property 58 already makes
    - Record both profile paths and their content hashes, so the divergence hypothesis is refuted with evidence rather than by assertion
    - **Outcome**: the repair is pinning the profile (2.8) **plus** an explicit tool guard (2.9), not profile reconciliation
    - _Requirements: 1.6, 2.8, 2.9_

  - [x] 1.5 Profile provenance is unreported
    - Run the gate with `~/Documents/GitHub/insightconnect-plugins/` present, then again with it hidden; assert the report states which profile was used, from which source, and at what line length
    - **Expected counterexample**: the authoritative case reports nothing — `resolve_lint_profile` records `source` and `detail` on the `LintProfile` and the gate surfaces the detail only when the profile is non-authoritative or unresolved
    - _Requirements: 1.6, 2.8_

  - [x] 1.6 `isBugCondition_3` — the preview judges a stale draft
    - In a session that delegated implementation, compare `plan.spec_preview` with the on-disk `plugin.spec.yaml`, and the preview's completeness findings with `check_completeness(diskSpec)`
    - **Expected counterexample**: 16 findings versus 0 — 11 absent required top-level fields plus `credential_token is not a valid credential type`, every one false against a file carrying all 11 fields, `credential_secret_key`, and 12 `example:` entries
    - Assert the control: the same plugin reopened as `iterate_custom` (which loads from disk) yields 0 completeness findings, 23 top-level keys, `spec_complete` met, 2 rather than 3 outstanding conditions
    - _Requirements: 1.7, 1.8_

  - [x] 1.7 The write-back — Bug 3 reaches the artifact, not only the report
    - **This is the counterexample that changes Bug 3's severity**, so it is a test of its own rather than a note on 1.6
    - Force an export from the stale session, then read `plugin.spec.yaml` back **out of the produced `.plg`**
    - **Expected counterexample**: the stale draft spec, because `confirm_export` → `_build_dir` calls `project_folder.save(export_spec, generated_files=session.draft.code_files)` and writes the draft over the tree before packaging. A forced export ships the spec the preview complained about
    - _Requirements: 1.7_

  - [x] 1.8 The `api_client` detector rejects the prescribed pattern
    - Fixture plugin defining `HTTP_ERROR_MAP` in `util/constants.py` and importing it into `util/api.py` — the pattern `~/.kiro/steering/implementation.md` prescribes; assert `api_client` met
    - **Expected counterexample**: unmet with `<pkg>/util/api.py: no HTTP_ERROR_MAP`, because `_api_client_condition` builds `assigned` from `ast.Assign`/`ast.AnnAssign` targets in `api.py` alone
    - _Requirements: 1.9_

  - [x] 1.9 Byproducts reach the `.plg`
    - Run the plugin's tests, package the tree, list the members; expect `.coverage` and `unit_test/.coverage` present, because `build_engine._EXCLUDED_DIRS` covers directories only
    - _Requirements: 1.10_

  - [x] 1.10 Reporting and accounting counterexamples
    - Blocked export carries stage names only (`"failed code stages: lint, test"`) and no prospector or pytest output
    - A turn ending in a clarification request has already emitted `"Generating logic for N action(s)…"`
    - `token_total` stays 0 across two interpreter calls, then jumps to 53,836 after the agent run
    - No websocket frame for 13 minutes during a delegated run (last at 9.31s, next at 780.34s)
    - An attachment over 60,000 characters is truncated silently (`orchestrator/interpreter.py:245`)
    - `version_display` is empty in the returned preview
    - `credential_token` is rejected by `core/spec_completeness.VALID_CREDENTIAL_TYPES` though the installed toolchain defines it
    - _Requirements: 1.11, 1.12, 1.13, 1.14, 1.15, 1.16, 1.17_

- [x] 2. Write preservation property tests (BEFORE implementing any fix)
  - **Property 2: Preservation** — Genuine Defects Are Still Reported
  - **IMPORTANT**: Follow observation-first methodology — run the **unfixed** code first, record what it actually does, then assert that
  - **This task captures F.** The preservation property is stated as F′ reproducing F, so F's behavior has to be recorded before anything changes. Serialize the observed baselines to fixtures under `tests/fixtures/preservation_baseline/` so the post-fix comparison is against recorded fact rather than re-derived expectation
  - **Compare verdicts, not whole reports**: stage statuses, finding keys, condition statuses, the export decision, and the packaged member set. Message text is deliberately excluded — one message differs by design (see the exception below)
  - Property-based testing is the right instrument because the claim is universally quantified over trees and the interesting cases are combinatorial: which tools are present, which files are generated, whether the spec is complete, whether the tests pass. Build generators along those axes
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - **One knowingly accepted exception, recorded not hidden**: for a tree with failing tests on a host with no Docker, F reports the `test` stage failed with the Docker-unavailable message and F′ reports it failed with the pytest failures. Same verdict, better message, not byte-identical — which is why the property is over verdicts
  - Mark complete when the baselines are captured, the tests are written, and they pass on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12_

  - [x] 2.1 Observe and pin the nine preservation axes on unfixed code
    - **Genuinely failing test** — a tree whose `test_suspend_user` fails: stage fails, and the failure names the test
    - **Genuine hand-written defect** — `util/api.py` using `requests` without importing it: reported with its path, code, and finding key
    - **Genuinely incomplete on-disk spec** — missing `sdk`, missing output examples: record the completeness findings
    - **Missing tool** — prospector absent: skipped, `lint_clean` unverified; a missing linter never reads as a clean lint (26.4, 27.5)
    - **Advisory boundary** — a tree clearing four stages with outstanding definition-of-done conditions: `permitted: true` with the conditions presented (27.6, 27.7)
    - **Packaged contents** — `.builder/` and every reference document absent; every plugin file present; record the 39-entry baseline member set
    - **Forced export** — succeeds, and is recorded as forced
    - **Repair loop** — finding-key arithmetic, stall condition, round limit, honest labelling
    - **Delegation isolation** — prompt on stdin, default-deny environment, enumerated tools; asserted because change 9 touches the interpreter's call path
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12_

  - [x] 2.2 Property test: nothing else changes
    - **Property 75** — for any tree or session satisfying none of `isBugCondition_1`, `isBugCondition_2`, `isBugCondition_3`, F′ produces the same stage verdicts, findings, condition statuses, export decision, and packaged contents as F, up to the byproducts 2.15 removes and the one stage message recorded above
    - Generators cover the axes in 2.1: tool presence, generated-versus-hand-written defect placement, spec completeness, test outcome
    - `tests/integrations/test_export_gate_preservation_property.py`
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12**

- [x] 3. Change 1 — one helper reads a `Draft` from a tree (refactor)
  - **SCOPE-4: this is a pure move with no behavior change, and lands before change 2 which needs it.** Its own commit
  - `_start_iterate` and `_start_enhance` in `orchestrator/orchestrator.py` already do this twice: `load_plugin_spec(folder.spec_path.read_text(...))` plus `_read_dir_tree(folder.path)`
  - Add a module-level `_draft_from_folder(folder) -> Draft` and call it from both
  - **SCOPE-9 callers, already named in the design and not to be re-derived**: `_start_iterate`, `_start_enhance`, and the two call sites change 2 adds (`submit_message`, `prepare_export`). Production callers that exist or that this work requires — no anticipated ones
  - No new public surface, no cross-module abstraction
  - Assert the refactor is behavior-preserving by re-running the existing orchestrator suite unchanged
  - _Requirements: 2.11_

  - [x] 3.1 Unit tests for `_draft_from_folder`
    - Unreadable spec, unparseable spec, and the draft-preserved fallback change 4.2 depends on
    - `tests/orchestrator/test_orchestrator.py`
    - _Requirements: 2.11_

- [x] 4. Change 2 — disk is authoritative; the preview describes the tree
  - **Decision as recorded (2.11): disk wins**, because disk is what gets packaged and shipped. The tradeoff, which is why this is a decision rather than a patch: after an implementation turn the draft stops being the authored source and becomes a view of the tree, so an in-session edit made after that turn must reach the tree to survive
  - **File**: `orchestrator/orchestrator.py`
  - _Requirements: 2.11, 2.12_

  - [x] 4.1 Sync the draft from the tree after the agent has run
    - In `submit_message`, after the repair loop and **before** `_evaluate_done`, when a project folder exists and code was delegated: `session.draft = _draft_from_folder(folder)`
    - Set `session.baseline_spec = deepcopy(draft.spec)` so the next turn's structural-change detection compares against what is on disk
    - The sync follows the repair loop rather than the implementation run, because repairs can touch the spec too
    - `TurnResult.spec` then carries the real spec, and `_evaluate_done`'s `spec_complete` reads it
    - _Bug_Condition: isBugCondition_3(X) — implementationDelegated(X) AND diskSpec(projectFolder(X)) <> draftSpec(X)_
    - _Requirements: 2.11_

  - [x] 4.2 Fail safe, never clobber
    - If the on-disk spec cannot be read or parsed, leave the draft as it is and report the parse failure in the turn message
    - A syntactically broken spec is a finding, not grounds for discarding the session
    - _Requirements: 2.11_

  - [x] 4.3 Write on every spec change, not only structural ones
    - **This is what makes "disk wins" safe, and it closes a concrete hole**: a non-structural edit (a description, say) triggers no refresh today, so nothing writes it and a later re-read would silently discard it
    - When a turn changes the spec and a project folder exists, persist it via `ProjectFolder.save` whether or not the change was structural
    - The refresh of derived files stays gated on structural change, exactly as now
    - With this, Property 64 holds at every turn boundary and the re-reads in 4.1 and 4.4 are no-ops when nothing else wrote
    - _Requirements: 2.11_

  - [x] 4.4 `prepare_export` derives from the tree
    - Re-read the draft from the folder at the top of `prepare_export`, before `_suffix_vendor`, so `spec_preview`, `check_completeness`, and the `Definition_Of_Done`'s `spec=` all describe the spec that would be packaged
    - The preview stays non-mutating with respect to the draft (parent Req 16.6): the suffixed, bumped spec still rides on the returned `ExportPlan` only
    - _Expected_Behavior: plan.spec_preview = versionedVendorSuffixed(diskSpec(projectFolder(X)))_
    - _Requirements: 2.12_

  - [x] 4.5 Stop the export-time write-back from being a write-back
    - `_build_dir` currently saves the draft spec **and** the draft's code-file map over the tree
    - With the draft a view of the tree, the spec write is the version bump and vendor suffix only — correct and required, so keep it
    - The code-file map write is not: when a project folder exists the tree is already the source of those files, so pass no `generated_files` in that branch
    - The temporary-directory branch, which materializes an in-memory net-new draft, keeps writing them — the one case where the draft is the only copy
    - Verify against task 1.7: the spec read back out of the `.plg` is now the agent's
    - _Requirements: 2.11, 2.12_

  - [x] 4.6 Amend parent Requirement 16.1
    - **Lands in this task, not ahead of it**: the previewed spec is the spec that would be packaged
    - Carry a revision note in `.kiro/specs/insightconnect-plugin-builder/requirements.md` matching that document's existing revision-note convention, saying what changed and why
    - _Requirements: 2.14_

  - [x] 4.7 Unit tests for preview fidelity
    - `prepare_export`: the preview equals the disk spec; a **non-structural** in-session edit made after an implementation turn survives (Property 64's hole, closed by 4.3)
    - `tests/orchestrator/test_orchestrator.py`
    - _Requirements: 2.11, 2.12_

  - [x] 4.8 Property test: the preview describes what would be packaged
    - **Property 63** — the previewed spec equals the vendor-suffixed, version-bumped on-disk spec; the preview's completeness findings equal `check_completeness` of that spec; a spec complete on disk yields zero completeness findings
    - Generate specs, mutate them on disk, assert the preview tracks the tree
    - `tests/orchestrator/test_preview_fidelity_property.py`
    - **Validates: Requirements 2.11, 2.12**

  - [x] 4.9 Property test: the draft and the tree do not diverge
    - **Property 64** — after any turn that changes the draft's spec in a session with a project folder, the draft's spec equals the spec on disk; after any implementation turn the draft is a view of the tree, so no later read can disagree and no export-time write can overwrite the agent's work with an older value
    - Generate specs, mutate them in session and on disk, assert both directions
    - `tests/orchestrator/test_draft_disk_parity_property.py`
    - **Validates: Requirements 2.11**

- [x] 5. Change 3 — `api_client` accepts an imported error map
  - **File**: `integrations/definition_of_done.py`, `_api_client_condition`
  - Alongside the existing assigned-name check, treat `HTTP_ERROR_MAP` as present when `api.py` imports it from within the plugin package: an `ast.ImportFrom` whose `names` include `HTTP_ERROR_MAP` and whose module is relative (`level > 0`) or begins with the package name
  - Report unmet only when the map is neither defined nor imported there
  - **A dangling import is deliberately left alone** — imported from a module that does not define it is reported by the linter and the compile check, with a location; duplicating that judgment here would report one defect twice
  - `_make_request` and the "at least one public domain method" checks are unchanged
  - _Bug_Condition: errorMapImportedIntoClient(X)_
  - _Expected_Behavior: condition(doneReport'(X), 'api_client').met_
  - _Preservation: 3.3 — the exclusion makes the bar measurable, it does not lower it_
  - _Requirements: 2.13_

  - [x] 5.1 Unit tests for `_api_client_condition`
    - Defined in `api.py`; relative import; absolute in-package import; import from outside the package; neither
    - `tests/integrations/test_definition_of_done.py`
    - _Requirements: 2.13_

  - [x] 5.2 Property test: the error map may be defined or imported
    - **Property 65** — covered **example-based**, per the design: this is behavior at a source-parsing boundary where enumerated import forms are more informative than generated ones
    - Recorded here so its absence from the property suite is a choice and not an omission; follows the parent's own split at Properties 52–62
    - **Validates: Requirements 2.13**

  - [x] 5.3 Amend parent Requirement 27.1 — API client clause
    - The API client's error map may be **defined in or imported into** the client module
    - Revision note in the parent `requirements.md`, per that document's convention
    - _Requirements: 2.14_

  - [x] 5.4 Verify the Bug 3 exploration tests now pass
    - **Property 1: Expected Behavior** — A Correct Plugin Reported As Broken
    - **IMPORTANT**: Re-run the SAME tests from tasks 1.6, 1.7, 1.8 — do NOT write new ones
    - **EXPECTED OUTCOME**: Tests PASS — 0 completeness findings, the `.plg` carries the agent's spec, `api_client` met
    - _Requirements: Expected Behavior for isBugCondition_3, clauses 2.11, 2.12, 2.13_

  - [x] 5.5 Verify preservation tests still pass
    - **Property 2: Preservation** — Genuine Defects Are Still Reported
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new ones
    - In particular: a genuinely incomplete **on-disk** spec produces the same completeness findings as before, now read from disk
    - **EXPECTED OUTCOME**: Tests PASS (no regressions)

- [x] 6. Change 4 — path predicates move to one module (refactor)
  - **SCOPE-4: a move with no behavior change, landing before change 5 which needs it.** Its own commit
  - **SCOPE-13, integrate before finalizing.** The lint path is the named collision point with the pending PR: `quality_gate.py`, `build_prep.py` (`resolve_lint_profile`, `LINT_TOOLS`, `PLUGIN_LINE_LENGTH`), `tests/integrations/test_quality_gate.py`, and the `lint_command` wiring in `app.py`. Integrate current `origin/main` and re-run the affected gates before finalizing this task and task 7. **If the pending PR has already moved the predicates or the profile plumbing, this task shrinks to updating imports rather than moving definitions** — check first, then size the change. GitHub is under an outage as of writing: do not perform git operations and do not assume `origin/main` is reachable; integrate when it is, and treat that integration as part of finalizing rather than as optional
  - **New file**: `core/plugin_files.py`. Pure path logic, no I/O, so it belongs in `core/` beside the other pure predicates
  - Three predicates, kept distinct because they mean different things:
    - `is_generated(path)` — emitted by the CLI, forbidden to edit, **still packaged**. Moved verbatim from `quality_gate.py` with `GENERATED_FILE_NAMES` / `GENERATED_DIR_NAMES`
    - `is_lint_excluded(path)` — everything generated, plus `unit_test/`. Moved verbatim. **Lint only**: `unit_test/` stays compiled, formatted, and executed (3.7)
    - `is_packaging_excluded(path)` — `.builder/`, VCS and cache directories, and byproduct files. Replaces `build_engine._EXCLUDED_DIRS`
  - `hand_written_python(root)` moves with them
  - **SCOPE-9 callers, already named in the design**: `quality_gate.py` (findings), `code_validator.py` (the `lint` stage's file set, change 5), `build_engine.py` (`list_plugin_files`, change 8). Three task-required production callers, so the extraction is justified by need rather than anticipation
  - `quality_gate` re-exports nothing; its importers are updated in the same commit
  - **Decision as recorded (2.6)**: files are excluded **because the CLI generates them and the rulebook forbids editing them**, not because they produced findings — the standard task 37 applied to the excluded validator. The tradeoff stands: a genuine defect inside a generated file will not be reported by these checks, and could not be fixed in the plugin if it were
  - _Requirements: 2.6_

  - [x] 6.1 Unit tests for the three predicates
    - Table-driven over each named file and directory, plus `.coverage` at depth, plus a `unit_test/` path that is lint-excluded and **not** generated
    - `tests/core/test_plugin_files.py`
    - _Requirements: 2.6_

- [x] 7. Change 5 — lint and format judge hand-written code, and state the bar
  - **Depends on task 1.2 and 1.3**: those measurements decide how much of this task is required
  - **SCOPE-13**: same collision point and same integrate-before-finalizing condition as task 6
  - **Files**: `integrations/code_validator.py`, `integrations/quality_gate.py`, `integrations/build_prep.py`, `api/app.py`
  - _Bug_Condition: isBugCondition_2(X) — findings(X) <> EMPTY AND every finding is in a generated file_
  - _Expected_Behavior: lint stage passed, formatted met, lint_clean met, profile source reported_
  - _Preservation: 3.3, 3.7 — a genuine hand-written defect is still reported; unit_test/ stays compiled, formatted, and run_
  - _Requirements: 2.6, 2.7, 2.8, 2.9_

  - [x] 7.1 The `lint` stage is judged by the same linter and profile as the `Quality_Gate`
    - Replace `flake8 .` with prospector under the resolved profile, `LINT_TOOLS`, and the hand-written file set from `core/plugin_files`; derive the stage verdict from that finding set — pass iff no finding refers to a hand-written file
    - **This goes beyond "exclude generated files" deliberately, and 2.7 is why**: `flake8`'s defaults include `pycodestyle`, which the plugins repository never runs, so a plugin a reviewer would call clean fails the stage on codes outside the bar no matter which files are excluded
    - It also removes the second two-subsystems-disagree case of the same shape as 2.4, and removes a stage that could fail because `flake8` — absent from `REQUIRED_TOOLS` — is not installed
    - **Line length**: apply `PLUGIN_LINE_LENGTH` (120) wherever a width is applied, so the tool and the repository's CI agree
    - **Empty file set, stated as a tradeoff rather than hidden**: a tree with no hand-written Python records a pass with a message naming zero files linted, because the stage has no third state. The honest report of that tree lives in the `Definition_Of_Done`, where `code_parses` is **unverified**
    - Remove the `lint_command=("flake8", ".")` wiring from `api/app.py` and the default from `code_validator.py`
    - _Requirements: 2.6, 2.7_

  - [x] 7.2 Format check — re-measure first, edit only if the measurement says so
    - `_check_format` already restricts to `hand_written_python`. **If task 1.3's re-measured run is clean, the code change here is nil and the finding is closed by measurement rather than by edit** — record that outcome in this task
    - If it is not clean, route the file set through `core/plugin_files.hand_written_python` and fix whatever admitted the generated paths
    - _Requirements: 2.7_

  - [x] 7.3 Report the bar
    - Carry the applied profile path, its source (`repository` / `fallback`), and the applied line length on `QualityReport` and on the `lint` stage's result, and serialize them into the export payload
    - **Decision as recorded: runtime discovery is kept**, because a vendored second copy of someone else's rules drifts and then the two disagree about what clean means (parent task 38)
    - The tradeoff, now stated rather than removed: two operators with different checkouts can still see different findings; what changes is that the report says which bar produced them
    - _Requirements: 2.8_

  - [x] 7.4 Pin the profile in content-dependent tests, and skip when the tool is absent
    - **This is the actual repair for the pre-existing failure, per task 1.4's corrected diagnosis** — not profile reconciliation
    - `QualityGate` already accepts `lint_profile`: have `TestFindingsTheRepositoryWouldNotRaise` pass an explicit `LintProfile` pointing at a fixture copy, so the assertion no longer varies with the developer's home directory
    - Guard on `shutil.which("prospector")` so a missing linter is a **skip** rather than a false failure — the same distinction parent design Property 58 makes
    - Keep the `undefined-variable` case as the proof the linter is still on: a `requests` that is used and never imported is still reported under the pinned profile
    - `tests/integrations/test_quality_gate.py`
    - _Requirements: 2.8, 2.9_

  - [x] 7.5 Verify the Bug 2 exploration tests now pass
    - **Property 1: Expected Behavior** — Generated Files Raise Nothing, And The Bar Is Stated
    - **IMPORTANT**: Re-run the SAME tests from tasks 1.2, 1.3, 1.5 — do NOT write new ones
    - **EXPECTED OUTCOME**: Tests PASS — `lint` stage passed, `formatted` and `lint_clean` met, profile source and line length reported
    - Re-run task 2's preservation tests: the hand-written `undefined-variable` case is still reported with the same key, and prospector-absent still reads unverified rather than clean
    - _Requirements: 2.6, 2.7, 2.8, 2.9_

  - [x] 7.6 Property test: lint and format judge hand-written code, and state the bar
    - **Property 68** — every lint and format finding refers to a hand-written file; the excluded set is computed by a single definition shared by the `Quality_Gate`, the `lint` stage, and the packaging exclusion; a tree whose only defects lie in generated files reports `lint` passed and `formatted`/`lint_clean` met; a genuine hand-written defect is still reported; every result names the profile applied, its source, and the line length
    - Generate trees mixing generated and hand-written defects
    - `tests/integrations/test_quality_gate_hand_written_property.py`
    - **Validates: Requirements 2.6, 2.7, 2.8, 2.9** — also preserves 3.3, 3.7

  - [x] 7.7 Amend parent Requirements 26.3 and 27.1
    - 26.3 names the single definition of generated files that the `Quality_Gate`, the `lint` stage, and the packaging exclusion all consume
    - 27.1's `lint_clean` and formatting conditions apply to **hand-written code only**
    - Revision notes in the parent `requirements.md`, per that document's convention. Lands with the code that makes them true, per task 6 and this task
    - _Requirements: 2.10_

- [x] 8. Change 6 — one definition of the unit test run (refactor)
  - **SCOPE-4: a move with no behavior change, landing before change 7 which needs it.** Its own commit
  - **SCOPE-12: `pytest` is NOT added to this tool's dependencies.** It has to be present in the plugin's interpreter, and its absence is reported with remediation, **never installed silently**
  - **New file**: `integrations/plugin_tests.py`, moved out of `QualityGate._check_tests`
  - `UnitTestRun` (frozen): `interpreter`, `ran`, `no_tests`, `failures` (`path`, `line`, `name`), `returncode`, `output`, `coverage_percent`, `skipped` notes, `message`
  - `async run_unit_tests(project_dir, *, python_executable, timeout_seconds) -> UnitTestRun` — mechanics only: the `pytest` invocation, the `pytest-cov` probe, the failure and coverage parsing `quality_gate` already implements. **No findings, no verdicts**
  - `QualityGate._check_tests` becomes a thin adapter from `UnitTestRun` to `CodeFinding`s, preserving today's finding shapes, keys, and skip notes **verbatim** — that equality is what makes this a refactor, so assert it against task 2's captured baseline
  - **SCOPE-9 callers, already named in the design**: `run_unit_tests` has two (`quality_gate._check_tests`, the `test` stage in change 7); `resolve_test_interpreter` joins two existing resolvers in the module that already owns resolution
  - _Requirements: 2.4_

  - [x] 8.1 `resolve_test_interpreter` in `build_prep.py`
    - Joins `resolve_sdk_version`, `resolve_target_python`, and `resolve_lint_profile`, which is where this tool already resolves what it depends on
    - Requires a candidate that can import **both** the SDK and `pytest`; tries the pyenv target-series interpreter, then `sys.executable`, then `python3` on `PATH`
    - Reports the candidate chosen and, for each rejection, **which of the two imports failed**
    - **This exists because on the tester's host neither single candidate satisfies both** — the SDK interpreter (`/Library/Developer/CommandLineTools/usr/bin/python3`, 3.9) has no `pytest`, the project venv has `pytest` and no SDK — and a resolver that cannot say so produces exactly Bug 1's silence
    - _Requirements: 2.3, 2.4_

  - [x] 8.2 Unit tests for the resolver and the runner
    - `resolve_test_interpreter`: each candidate accepted, each rejected with its reason, none available
    - `run_unit_tests`: pass, fail with parsed failures, no `unit_test/`, no tests collected, collection error, coverage measured, `pytest-cov` absent
    - `tests/integrations/test_plugin_tests.py`, `tests/integrations/test_build_prep.py`
    - _Requirements: 2.3, 2.4_

  - [x] 8.3 Assert the adapter is finding-for-finding identical
    - Compare `QualityGate` output before and after the move against task 2's captured baseline: same finding keys, same shapes, same skip notes
    - A difference here means this is not a refactor and the change has to be re-scoped
    - _Requirements: 2.4_

- [x] 9. Change 7 — the `test` stage runs the tests on the host
  - **Decision as recorded (2.1): option (a), host-run.** The stage no longer establishes that the tests pass *in the shipping environment*; that intent is unreachable without edits to `.dockerignore` and the Dockerfile the `Agent_Rulebook` forbids. Option (b), a test-only image layer, keeps the stronger property and is rejected on cost — another image build on a path that already takes minutes. Option (c), dropping the stage, is rejected because Requirement 8 names four stages and Property 17 is a conjunction over four
  - **Files**: `integrations/code_validator.py`, `orchestrator/orchestrator.py`, `api/app.py`
  - _Bug_Condition: isBugCondition_1(X) — hostUnitTestsPass(X) AND dockerignoreExcludes(X, 'unit_test') AND NOT imageHasPytest(X)_
  - _Expected_Behavior: stage(report, 'test').passed = hostUnitTestsPass(X); decideExport'(...).permitted_
  - _Preservation: 3.5, 3.6 — the gate stays the four-stage conjunction; an unverifiable condition stays unverified_
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 9.1 The stage calls `run_unit_tests` instead of `docker run`
    - `StageResult` carries the pytest output as `stdout`, `returncode` from the run, and a `message` naming the interpreter used
    - `requires_docker` becomes `False` for `test`
    - **Consequence, recorded**: on a host without Docker the pipeline now yields lint **and** test results rather than lint alone — more partial offline feedback, in line with the existing Docker-optional design note. The gate's conjunction is untouched, so export is still blocked while `build` and `validate` cannot run
    - The 600s abort (parent Req 8.8) still applies, now to the host run
    - _Requirements: 2.1, 2.2_

  - [x] 9.2 Unrunnable fails closed and says why
    - No `unit_test/`, no interpreter satisfying both imports, or `pytest` absent → `StageStatus.FAILED` with a message naming the interpreter and the reason. **Never a pass**
    - The `Definition_Of_Done` reports `unit_tests_pass` **unverified** in the same situation, which is the distinction 3.6 requires — the gate fails closed, the advisory report stays honest
    - **The operator-facing consequence, stated plainly**: on a host with no suitable interpreter, export requires `force`. That is what 2.3 asks for
    - `pytest`'s absence is reported with remediation and never remedied by installing it (SCOPE-12)
    - _Requirements: 2.3_

  - [x] 9.3 One execution per export
    - In `prepare_export`, run the `Quality_Gate` first and pass its `UnitTestRun` into `run_pipeline(project, unit_test_run=...)`, which uses it instead of running the suite again
    - When no run is supplied — a caller holding only the validator — the stage runs its own
    - One execution, one interpreter, one verdict; and the export preview does one fewer test run than today
    - **A genuinely flaky test can still differ between two executions**; with a single execution per preview there is only one to report
    - _Requirements: 2.4_

  - [x] 9.4 Unit tests for the `test` stage
    - Pass, fail, timeout at 600s, unrunnable-fails-closed, message names the interpreter, Docker-absent still runs
    - `tests/integrations/test_code_validator.py`
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 9.5 Amend parent Requirement 8.3 and design Property 17
    - **Lands in this task and not before it**: a requirement stating that tests run on the host while they still run in the image is a false statement in the authoritative document
    - Requirement 8.3 states that the plugin's unit tests are run **on the host under the resolved target interpreter**, and that an unrunnable test run records a fail naming the interpreter and the reason
    - Design Property 17 is **unchanged as a conjunction over four stages**, and gains a note that the `test` stage is a host-run check and why the in-image intent was given up
    - Revision notes in the parent `requirements.md` and `design.md`, per those documents' conventions. The four stages remain the export gate and only the export gate, per parent task 37
    - _Requirements: 2.5_

  - [x] 9.6 Verify the Bug 1 exploration tests now pass
    - **Property 1: Expected Behavior** — A Healthy Plugin Clears The Gate
    - **IMPORTANT**: Re-run the SAME tests from tasks 1.1 and 1.2's contradiction check — do NOT write new ones
    - **EXPECTED OUTCOME**: Tests PASS — the `test` stage passes for a healthy plugin, `export/prepare` returns `permitted: true` without `force`, and the two subsystems agree
    - Re-run task 2's preservation tests: a genuinely failing `test_suspend_user` still fails the stage, and the failure still names the test
    - _Requirements: 2.1, 2.2, 2.4_

  - [x] 9.7 Property test: one definition of the unit test run, in both directions
    - **Property 66** — the `test` stage's verdict equals whether the plugin's unit tests pass under the resolved interpreter, pass when they pass and fail when they fail; the `Quality_Gate`'s test findings derive from the same execution definition and the same interpreter; the two never report contradictory outcomes for one tree
    - Generate trees with passing and failing suites
    - `tests/integrations/test_unit_test_run_agreement_property.py`
    - **Validates: Requirements 2.1, 2.2, 2.4**

  - [x] 9.8 Property test: an unrunnable test run fails closed and says why
    - **Property 67** — covered **example-based**, per the design: interpreter probes and subprocess output are process-boundary behavior where tests over the real interfaces are more informative than generated ones
    - Covered by 9.4 plus the split-interpreter integration test in 12.3; recorded here so the choice is visible
    - **Validates: Requirements 2.3** — also preserves 3.6

- [x] 10. Change 8 — packaging excludes byproducts
  - **File**: `integrations/build_engine.py`
  - `list_plugin_files` filters through `core/plugin_files.is_packaging_excluded`, which adds `.coverage` and `.coverage.*` at any depth to the existing directory exclusions
  - Because `list_plugin_files` is the single source of truth the packager and the preview both consume (parent Property 30), the preview file list changes with it
  - **Expected result: the 39-entry baseline less those byproducts** (3.2) — compare against task 2.1's captured member set
  - _Bug_Condition: byproducts present in the tree after a local test run_
  - _Expected_Behavior: packaged set excludes .builder/, every reference document, and every byproduct; contains every other file_
  - _Preservation: 3.2_
  - _Requirements: 2.15_

  - [x] 10.1 Verify against the exploration test
    - Re-run task 1.9: `.coverage` and `unit_test/.coverage` are absent from the `.plg`, and every other member is still present
    - _Requirements: 2.15_

  - [x] 10.2 Property test: packaging excludes byproducts and nothing else new
    - **Property 69** — the packaged file set excludes `.builder/`, every reference document, and every build/test byproduct including `.coverage` at any depth, and contains every other file present in the tree
    - Generate trees with arbitrary byproduct placement
    - `tests/integrations/test_build_engine_byproducts_property.py`
    - **Validates: Requirements 2.15** — also preserves 3.2

- [ ] 11. Change 9 — reporting and accounting
  - Each of these is small and lands **on its own commit** (SCOPE-7); they are grouped only for reading
  - _Requirements: 2.16, 2.17, 2.18, 2.19, 2.20, 2.21, 2.22_

  - [x] 11.1 Blocked-export detail
    - `api/app.py`'s `_serialize_export_plan` gains `failed_stages: [{name, status, returncode, message, displayed_output, full_output, truncated}]`, built from `plan.pipeline_report.failed_stages` through the existing `core/truncation.truncate_error_output` (`MAX_DISPLAY_CHARS` = 10,000, full text retained — parent Req 19.5)
    - **Every failing stage, not just the first**, which is where `classify_build_failure` stops
    - No new dataclass: the data is already on the report. The preview UI renders the new field
    - Unit test: `failed_stages` carries every failing stage; truncation at 10,000 characters with `full_output` retained
    - _Requirements: 2.16_

  - [ ] 11.2 Progress reports work actually performed, and keeps reporting
    - Move the `"Generating logic for N action(s)…"` frame out of the websocket handler's pre-submit path — where it is emitted from the plan before the orchestrator decides anything — and make it a step the orchestrator reports when it actually dispatches code requests
    - A minimal `ProgressReporter` protocol (`report(step: str)`) is passed into `submit_message`; the orchestrator reports at phase boundaries (applying operations, scaffolding, refreshing, implementing, repair round *n* of *m*, checking, evaluating done)
    - The websocket route wraps it to send a `status` frame plus a ticker task that re-emits the current step with elapsed seconds while a phase runs, so a 13-minute agent run is distinguishable from a hang
    - **One implementation, one caller — not speculative generality but the only shape that lets the orchestrator report without importing the API layer** (SCOPE-9)
    - _Requirements: 2.17, 2.19_

  - [x] 11.3 Interpreter usage is counted
    - `Interpreter.interpret` takes the `session_id` and the `CostController` and calls `record_usage(..., succeeded=True/False)` exactly as `LLMGenerator` does, so a paid call is counted where it happens
    - Parent Property 9 already required this; the interpreter was simply outside it
    - **Gating the interpreter through `authorize()` is NOT in scope** — a budget-exhausted session that cannot parse a message is a different decision — and is recorded here so the omission is visible rather than silent
    - Unit test: usage recorded on success, excluded on failure
    - _Requirements: 2.18_

  - [x] 11.4 Truncation is disclosed
    - `interpret` records, per truncated attachment, the file name, its full size, the size included, and that the delegated agent receives the whole file — it does, since attachments are written verbatim into `.builder/reference/`
    - The notices ride the turn payload. **The 60,000-character cap itself is unchanged**
    - Unit test: notice content
    - _Requirements: 2.20_

  - [ ] 11.5 `version_display` — diagnose before editing
    - **Read the run's registry state first.** `apply_version_bump` produces `"<previous> -> <new>"` and `prepare_export` sets the display only when `bump.changed`
    - **If the observed run had no prior export**, an empty display is parent Req 12.7 behaving correctly, and the defect is that the preview shows no version at all — fixed by populating the display with the version that would be exported, marked as unchanged (which Req 12.6 does not speak to, so this does not contradict it)
    - **If a bump did occur and the display was still empty**, the defect is in the propagation and the fix is there
    - The code change is chosen only after the diagnosis, not before
    - _Requirements: 2.21_

  - [x] 11.6 Credential types come from the toolchain's own schema
    - `credential_token` is defined by the installed toolchain — `insight_plugin/features/common/schema_util.py:109`, a required `token` (password-formatted) and an optional `domain`. Verified directly in the installed 1.9.20 package
    - `VALID_CREDENTIAL_TYPES` in `core/spec_completeness.py` gains it, and the comment above it — which currently offers `credential_token` as the example of a type the platform does *not* define — is corrected
    - A test cross-checks the tuple against the installed schema and **skips when `insight_plugin` is not importable**, so the two cannot drift silently
    - Unit tests: `credential_token` accepted, an invented type still reported, cross-check against the installed schema
    - **The steering correction (`plugin-spec.md`) lands in the plugins repository**, since `~/.kiro` is a symlink into it. It is outside this repo's test surface and is recorded as an accompanying change rather than a verified one
    - _Requirements: 2.22, 1.17_

  - [ ] 11.7 Property test: every paid invocation is counted
    - **Property 72** — the session token total equals the sum of the successful invocations, the interpreter included; failed invocations are excluded
    - Generate interleaved successful and failed interpreter and agent invocations
    - Consistent with parent Property 9
    - `tests/orchestrator/test_interpreter_usage_property.py`
    - **Validates: Requirements 2.18**

  - [ ] 11.8 Example-based coverage for Properties 70, 71, 73, 74
    - **Property 70** — a blocked export reports each failing stage's output, bounded by the truncation-with-full-access rule, full text retained. Covered by 11.1
    - **Property 71** — no progress message announces generation work the turn does not perform; progress carrying the current step is emitted at bounded intervals for a delegated run's duration. Covered by 11.2 and the websocket integration test in 12.4
    - **Property 73** — truncation is disclosed with name, full size, included size, and the statement that the agent receives the whole file. Covered by 11.4
    - **Property 74** — credential types defined by the installed `Insight_Plugin_CLI` schema report no finding; a type the schema does not define is still reported. Covered by 11.6, and consistent with parent Property 61
    - **These are behavior at process, filesystem, and websocket boundaries, where example-based tests over the real interfaces are more informative than generated ones.** Recorded as a task so the choice is visible; follows the parent's own split at Properties 52–62
    - **Validates: Requirements 2.16, 2.17, 2.19, 2.20, 2.22**

- [ ] 12. Integration tests over the whole preview
  - _Requirements: 2.2, 2.3, 2.7, 2.12, 2.16, 2.17, 2.19_

  - [ ] 12.1 Whole preview, real toolchain, mocked Docker
    - A delegated implementation turn followed by `export/prepare`; assert `permitted: true`, zero completeness findings, `spec_preview` equal to disk, and the profile and interpreter named
    - _Requirements: 2.2, 2.7, 2.8, 2.12_

  - [ ] 12.2 Blocked preview
    - A tree with a genuine hand-written defect and a failing test; assert both stages appear in `failed_stages` with their output, and that `force` is not required for anything that actually passes
    - _Requirements: 2.16_

  - [ ] 12.3 Split-interpreter host
    - Reproduce the tester's configuration with fake interpreters; assert the stage fails closed, `unit_tests_pass` is **unverified**, and the message names the interpreter
    - **This is the one Bug 1 case that only appears when the SDK and `pytest` live in different interpreters**, and it is why 2.3 exists
    - _Requirements: 2.3_

  - [ ] 12.4 Progress and cancellation
    - Drive one long delegated run over the websocket; assert no gap between frames exceeds the reporting interval, that each frame names a step, and that no frame announces generation for a turn that ends in a clarification
    - _Requirements: 2.17, 2.19_

- [ ] 13. Record this bugfix in the parent plan
  - Replace the parent `tasks.md` "Remaining work" section's "Nothing outstanding" with this bugfix and the parts of it still open, for as long as any are
  - That section exists so the distance between the plan and the code is visible rather than discovered; an empty list is a claim worth being able to check, so it should not read empty while this work is in flight
  - Update it again when the work lands, rather than leaving it stale
  - _Requirements: 2.5, 2.10, 2.14_

- [ ] 14. Accessibility review of the new preview surface
  - **This work creates the surface, so the review belongs to it.** `failed_stages` is a new region in the export preview and 11.2 adds new status frames; neither has been checked
  - Review keyboard operation, focus order, and screen-reader announcement for the `failed_stages` region, the outstanding-conditions presentation beside a permitted preview, and the periodic status frames — a frame that re-announces every few seconds is a live-region decision, not a default
  - **Full validation requires manual testing with assistive technologies and expert accessibility review**; this task is that testing, not a substitute for it
  - Browser Mode was off for the entire originating run, so every finding in `bugfix.md` came from timings and payloads and no part of the preview UI has been observed
  - _Requirements: Known Gaps in bugfix.md and design.md_

- [ ] 15. Checkpoint — ensure all tests pass
  - Run the full suite: the exploration tests from task 1 now pass, the preservation tests from task 2 still pass, every property test at 100+ examples, and the integration suite
  - Confirm the three bug conditions are closed against the JumpCloud tree at `~/.icplugin-builder/projects/jumpcloud/`: `permitted: true` without `force`, zero completeness findings, `spec_preview` equal to the on-disk spec
  - Confirm the preservation comparison over **verdicts** — stage statuses, finding keys, condition statuses, the export decision, the packaged member set — against task 2.1's captured baselines, allowing only the byproducts 2.15 removes and the one Docker-absent stage message
  - **Environment matters and several defects read as environmental without it**: `insight-plugin` 1.9.20 and `prospector` live in `~/Library/Python/3.9/bin`, `docker` in `/Applications/Docker.app/Contents/Resources/bin`, and neither is on a non-login shell `PATH`. Prepend both or stages fail for environmental reasons and conditions read `unverified` misleadingly. `frontend/dist/` is stale by default — run `cd frontend && npm run build` first
  - **SCOPE-13 before finalizing**: integrate current `origin/main` and re-run the affected gates, particularly for tasks 6 and 7. GitHub is under an outage as of writing, so this waits until the remote is reachable rather than being skipped
  - Ask the user if questions arise; do not report a fix as complete with any failure open

## Out of scope

Recorded so none of it is mistaken for missing work.

- **Code generation.** Verified correct against the supplied JumpCloud v1 and v2
  Swagger specs — every endpoint, method, and payload shape hand-checked, real
  `connect()`/`test()`, central `_make_request`, accurate per-action citations. No
  task above touches a generation path (3.1).
- **The two fixes already in the working tree**: the frozen-dataclass crash on a
  `force` export in `api/app.py` (fixed with `object.__setattr__`, two regression
  tests in `tests/api/test_app.py`), and the bare JSON 404 on a fresh clone when
  the built UI is absent (`main()` prints a diagnostic with `flush=True`). They
  land as their own two commits per SCOPE-7.
- **How a built UI reaches a new user** — prebuilt `icplugin_builder/ui` in the
  wheel, a build hook, or README reordering. A packaging decision, not a gate
  defect.
- **No dependency is added, removed, or upgraded** (SCOPE-12). `pytest` must exist
  in the plugin's interpreter and its absence is reported with remediation, never
  installed silently. `flake8` stops being invoked but was never declared, so
  dropping its use is a wiring change in `app.py` and not a manifest change.

## Known gaps this plan does not close

Each is from `bugfix.md`'s own list and stays open. Task 14 closes the one this
work creates; these remain.

- **No tenant import has been performed** — the one test that would prove the
  premise end to end.
- **The PDF and `reference_urls` paths are unreachable from the browser.**
  `MessageInput.tsx` accepts only `.json,.yaml,.yml,.txt,.md` and reads via
  `await file.text()`; `MessageAttachment` in `types.ts` carries no
  `encoding`/`media_type` field, so the base64 route the backend needs for PDFs
  cannot be populated by the UI. Closing that frontend gap is its own task, not a
  sub-task of any change here.
- **Whether the repair loop ran at all could not be determined**; nothing records
  its rounds. Task 11.2 makes rounds *visible while running*, which is not the
  same as recording them.
- The generated plugin's unit tests were verified to **exist and to have run**
  (coverage data present for every module), not to pass. Task 12.3 is what makes
  that the gate's problem rather than the tester's.

## Notes

- Tasks 1 and 2 use the bug-condition workflow's `**Property 1: Bug Condition**`
  and `**Property 2: Preservation**` labels; the design's correctness properties
  continue the parent specification's numbering at **63–75** and are referenced by
  those numbers. Both schemes appear on purpose — see the Overview.
- Properties 63, 64, 66, 68, 69, 72, and 75 are implemented as property-based
  tests (Hypothesis, min 100 examples), tagged
  `# Feature: export-gate-and-preview-fidelity, Property {number}: {property_text}`.
  Properties 65, 67, 70, 71, 73, and 74 are covered example-based at process,
  filesystem, and websocket boundaries, per the design's explicit choice and the
  parent's own split at Properties 52–62. Tasks 5.2, 9.8, and 11.8 record those
  choices rather than leaving them as gaps.
- Each of the nine changes is one purpose and one commit (SCOPE-7). Refactors
  (tasks 3, 6, 8) land before the fixes that need them (SCOPE-4) and each names its
  task-required production callers (SCOPE-9).
- Measurement tasks (1.2, 1.3, 1.4, 11.5) precede the code they inform, and a nil
  code change is a legitimate outcome for 1.3/7.2 and possibly 11.5.
- Specification amendments (tasks 4.6, 5.3, 7.7, 9.5, 13) land **in the same task
  as the code that makes them true**, never ahead of it, each with a revision note
  in the parent document matching that document's convention.
- Docker is mocked in property tests; the real toolchain is exercised by the
  integration suite in task 12.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9", "1.10"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2"] },
    { "id": 3, "tasks": ["3", "3.1"] },
    { "id": 4, "tasks": ["4.1", "4.2", "4.3", "4.4", "4.5"] },
    { "id": 5, "tasks": ["4.6", "4.7", "4.8", "4.9"] },
    { "id": 6, "tasks": ["5", "5.1", "5.2", "5.3"] },
    { "id": 7, "tasks": ["5.4", "5.5"] },
    { "id": 8, "tasks": ["6", "6.1"] },
    { "id": 9, "tasks": ["7.1", "7.2", "7.3", "7.4"] },
    { "id": 10, "tasks": ["7.5", "7.6", "7.7"] },
    { "id": 11, "tasks": ["8.1", "8.2", "8.3"] },
    { "id": 12, "tasks": ["9.1", "9.2", "9.3", "9.4"] },
    { "id": 13, "tasks": ["9.5", "9.6", "9.7", "9.8"] },
    { "id": 14, "tasks": ["10", "10.1", "10.2"] },
    { "id": 15, "tasks": ["11.1", "11.2", "11.3", "11.4", "11.5", "11.6"] },
    { "id": 16, "tasks": ["11.7", "11.8"] },
    { "id": 17, "tasks": ["12.1", "12.2", "12.3", "12.4"] },
    { "id": 18, "tasks": ["13", "14"] },
    { "id": 19, "tasks": ["15"] }
  ]
}
```
