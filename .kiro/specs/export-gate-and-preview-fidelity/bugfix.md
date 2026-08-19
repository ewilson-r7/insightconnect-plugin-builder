# Bugfix Requirements Document

## Introduction

An end-to-end UI/API run on 2026-08-17 against `main` @ `e7726b7` built a real
JumpCloud user-provisioning plugin (`create_user`, `add_user_to_group`,
`suspend_user`) from a plain-language description with the JumpCloud v1 and v2
Swagger specs attached. The plugin lives at
`~/.icplugin-builder/projects/jumpcloud/`.

**The plugin is correct. The tool's account of it is not.** Every generated API
call was hand-checked against the supplied specs and is right
(`GET /api/systemusers?limit=1`, `POST /api/systemusers` with the required
`email`/`username`, `PUT /api/systemusers/{id}` with `{"suspended": true}`,
`POST /api/v2/usergroups/{group_id}/members` with `{op,type,id}` expecting 204).
There are no stubs or TODOs, `connect()`/`test()` are real, `_make_request` is
central, `HTTP_ERROR_MAP` is used, source citations are accurate, and reference
material does not leak into the `.plg` (39 entries, no `.builder/`, no swagger).

Every defect in this document is in the **gate and reporting layer**: the export
preview reported a stale draft instead of the plugin, the `test` stage cannot
pass for any plugin, and the `lint` stage fails only on files the plugin author
is forbidden to edit. The tool built a working plugin and then told the operator
it was broken, in three independent ways, and the only route to an export was
`force`.

Fix order is **Bug 3 first**. It is the one that reports a correct plugin as
incorrect, so it makes the tool untrustworthy even once the two gate stages are
repaired.

### Scope

Out of scope, deliberately:

- **Code generation.** Verified working. No requirement here touches it.
- Two defects already fixed in the working tree, which land as their own commits
  per SCOPE-7: the frozen-dataclass crash on a `force` export in `api/app.py`
  (fixed with `object.__setattr__`, two regression tests in
  `tests/api/test_app.py`), and the bare JSON 404 on a fresh clone when the built
  UI is absent (`main()` now prints a diagnostic with `flush=True`).
- **How a built UI reaches a new user** (prebuilt `icplugin_builder/ui` in the
  wheel, a build hook, or README reordering). That is the still-open half of the
  second item above and it is a packaging decision, not a gate defect. It is
  recorded here as out of scope so it is not mistaken for resolved.

Three of the defects below change what the product promises and each carries a
**recorded decision** with its tradeoff stated as a numbered clause, so the
choice is documented rather than buried in code. Requirements 8, 16, 26 and 27 of
`.kiro/specs/insightconnect-plugin-builder/requirements.md`, and design
Property 17, need amendment as a consequence; the clauses say which and why.
Task 37 in that spec's `tasks.md` already revised Requirement 8 once for a
closely related reason and is the precedent these decisions follow.

## Bug Analysis

### Current Behavior (Defect)

**Bug 1 -- the four-stage `test` gate can never pass.**

1.1 WHEN the export preview runs the four-stage pipeline against a generated
plugin whose unit tests exist and run, THEN the system records a fail for the
`test` stage, because it runs `docker run --rm <image> python -m pytest -q`
(`integrations/code_validator.py:322`) against an image in which
`/python/src/unit_test` does not exist -- the generated `.dockerignore` excludes
`unit_test/**/*` and the Dockerfile's `ADD . /python/src` respects it -- and in
which `python -m pytest` reports no module named pytest, because the
`rapid7/insightconnect-python-3-slim-plugin` runtime image has no pytest and
`requirements.txt` correctly carries no test dependencies.

1.2 WHEN the `test` stage fails for that reason, THEN the system returns
`permitted: false` from `export/prepare` for every plugin, while the
`Quality_Gate` reports `unit_tests_pass` as met for the same tree, so two
subsystems state opposite outcomes about the same tests.

1.3 WHEN a plugin author attempts to make the `test` stage pass, THEN the system
offers no route to do so, because the only available fixes are edits to
`.dockerignore` and the Dockerfile, both of which the `Agent_Rulebook` forbids
editing.

**Bug 2 -- `lint` fails only on files the user must not edit.**

1.4 WHEN the `lint` stage runs against the generated JumpCloud plugin, THEN the
system fails the stage on 14 prospector messages of which **zero** are in
hand-written files -- all are `F401 imported but unused` in generated
`__init__.py` and `bad-super-call` in generated `schema.py`.

1.5 WHEN the `formatted` definition-of-done condition is evaluated, THEN the
system reports it unmet on the strength of 5 files `black --check` would
reformat: four generated `schema.py` and a generated `setup.py`. All 22 other
files, including every hand-written file, are clean. This is systemic rather than
JumpCloud-specific: generated `setup.py` also fails `black` in the pre-existing
`abuseipdb` and `rapid7_velociraptor` projects. `~/.kiro/steering/structure.md`
forbids editing `schema.py`, `setup.py`, and `__init__.py`, so the failure is
real, accurately located, and unfixable by its audience.

> **Correction, established by task 7.2's re-measurement.** The second sentence is
> wrong, and the instrument is why: the five files were measured with **bare
> `black` at its own 88-column default**, while the tool formats at 120. At 120 no
> generated file is unformatted in any of the three trees -- so the five files are a
> measurement of the bar rather than of the plugins, and task 1.3's own tests record
> that. What *is* unformatted at 120 in the JumpCloud tree is two **hand-written**
> files, `icon_jumpcloud/connection/connection.py` and
> `unit_test/test_api_client.py`, which the report could not name because
> `_check_format` passed `--quiet` and then parsed the very lines `--quiet`
> suppresses. Both were confirmed with bare `black --line-length 120` outside the
> tool.
>
> The consequence for 2.7: `lint_clean` is met for that tree after change 5, and
> `formatted` is **correctly unmet**, now naming two files its author may fix. The
> defect 1.5 describes was the attribution, not the verdict.

1.6 WHEN the `lint` result is produced, THEN it depends on whether
`~/Documents/GitHub/insightconnect-plugins/prospector.yaml` exists on the host,
because `build_prep.resolve_lint_profile` prefers that clone over the vendored
fallback -- so the bar a plugin is held to is a function of the developer's home
directory. `tests/integrations/test_quality_gate.py::TestFindingsTheRepositoryWouldNotRaise::test_a_real_defect_is_still_reported`
already fails on this host before any change (it expects a prospector
`undefined-variable` and gets an empty set), confirmed pre-existing by stashing.

**Bug 3 -- the export preview judges a stale draft, not the plugin.**

1.7 WHEN `export/prepare` runs in the session that delegated implementation, THEN
the system evaluates the in-session draft (`plan.spec_preview`) rather than the
`plugin.spec.yaml` the agent wrote to the project tree, and reports 16
completeness errors -- 11 absent required top-level fields, plus
`connection.api_key.type: 'credential_token' is not a valid credential type` --
every one of which is false against the file on disk, which carries all 11
fields, uses `credential_secret_key`, and has 12 `example:` entries.

1.8 WHEN the same plugin is reopened in a fresh `iterate_custom` session, which
loads the spec from disk, THEN the system reports 0 completeness findings, 23
top-level keys, `spec_complete` met, and 2 rather than 3 outstanding conditions
-- the same plugin, the same code, the same stages, differing only in which spec
was read.

1.9 WHEN the `api_client` condition is evaluated on a plugin whose
`HTTP_ERROR_MAP` is defined in `util/constants.py` and imported into
`util/api.py`, THEN the system reports it unmet with the detail
`icon_jumpcloud/util/api.py: no HTTP_ERROR_MAP`, because the detector requires a
literal definition in `api.py` -- even though import-from-`constants` is the
pattern `~/.kiro/steering/implementation.md` prescribes.

**Lower-priority defects (no decision required).**

1.10 WHEN a plugin is packaged after a local test run, THEN the system includes
`.coverage` and `unit_test/.coverage` in the `.plg`.

1.11 WHEN an export is blocked, THEN the system reports only stage names
(`"failed code stages: lint, test"`) and carries no prospector or pytest output,
so the operator has to reproduce all four stages by hand to learn what failed.

1.12 WHEN a turn ends in a clarification request, THEN the system has already
emitted `"Generating logic for 3 action(s)..."`, because progress status is
derived from the plan before the orchestrator decides to ask a question.

1.13 WHEN the interpreter is invoked, THEN the system leaves `token_total` at 0
across two paid calls and then jumps to 53,836 (54% of the 100,000 budget) after
the agent run, so interpreter usage appears uncounted and cost is invisible until
it is spent.

1.14 WHILE the delegated agent runs, THE system emits no frame for 13 minutes
(last frame at 9.31s, next at 780.34s) with no heartbeat, no step name, and no
cancel, which is indistinguishable from a hang.

1.15 WHEN an attachment longer than 60,000 characters is sent to the interpreter,
THEN the system truncates it silently (`orchestrator/interpreter.py:245`). The
agent still receives the full file, so implementation was unaffected, but a
206KB OpenAPI spec has its `/systemusers` paths at roughly byte 65,000, outside
the interpreter's view, and nothing tells the user.

1.16 WHEN the export preview is returned, THEN `version_display` is empty.

> **Correction: not a defect. Closed by task 11.5's diagnosis with no code change.**
> The observed run had no prior export, so `bump_for_export` correctly reported no
> change and `prepare_export` left the display empty -- `"<previous> -> <new>"` has
> nothing to say when there is no previous version. Requirement 12.6, which clause
> 2.21 cites, is explicitly about the display *after* a version bump, and
> Requirement 12.7 requires the version stay unchanged when no prior export exists.
> Both were behaving correctly.
>
> The propagation was measured separately and works: a second preview, after the
> first export put a version in the registry, bumps and reports `1.0.0 -> 1.0.1`,
> and that string reaches the serialized payload.
>
> The remedy 2.21 proposes -- populate the display with the version that would be
> exported -- rests on the preview showing "no version at all", and it does not:
> `spec_preview.version` carries it and the UI renders the whole spec beside the
> display line. So what 1.16 describes is a presentation preference, not a lost
> value, and changing it would make a first preview claim a bump that did not
> happen. Recorded here rather than actioned.


1.17 WHEN `~/.kiro/steering/plugin-spec.md` is read as the authority on
credential types, THEN it lists three, omitting `credential_token`, which
`insight_plugin/features/common/schema_util.py` defines with shape
`{token, domain}` -- so a valid spec reads as a defect.

### Expected Behavior (Correct)

**Bug 1 -- what the `test` stage promises. Decision recorded.**

2.1 WHEN the `Code_Validator` runs the `test` stage, THE system SHALL run the
plugin's unit tests on the host under the resolved target interpreter, and SHALL
NOT run them inside the built plugin image. **Decision: option (a).** The
tradeoff is explicit and accepted: the stage no longer establishes that tests
pass *in the shipping environment*, which was the original intent of running them
in the image. It is chosen because that intent is currently unreachable without
edits to `.dockerignore` and the Dockerfile that the `Agent_Rulebook` forbids, and
because the `Quality_Gate` already runs these tests on the host successfully, so
the change removes a state in which two subsystems contradict each other about
one plugin. Option (b), a test-only image layer, preserves the stronger property
and is rejected for now on cost: another image build on a path that already takes
minutes. Option (c), dropping `test` from the four stages, is rejected because
Requirement 8 names four stages and design Property 17 encodes the conjunction
over four.

2.2 WHEN the `test` stage runs against a generated plugin whose unit tests pass,
THE system SHALL record a pass for that stage, and `export/prepare` SHALL return
`permitted: true` without `force`; and WHEN a plugin's unit tests fail, THE system
SHALL record a fail, so the stage's result reflects the plugin's real tests in
both directions.

2.3 IF the plugin's unit tests cannot be run at all -- no `unit_test/` directory,
or the resolved interpreter has no pytest -- THEN THE system SHALL record a fail
for the `test` stage carrying a message that names the interpreter used and the
reason, and SHALL NOT record a pass. The four-stage gate has no third state, so
an unrunnable check fails closed rather than passing quietly.

2.4 WHEN both the `test` stage and the `Quality_Gate` evaluate the same working
tree, THE system SHALL NOT report contradictory outcomes for the plugin's unit
tests, deriving both from one definition of how those tests are run.

2.5 WHEN this decision is applied, THE system's specification SHALL be amended to
match: Requirement 8.3 SHALL state where the plugin's unit tests are run, and
design Property 17 SHALL retain the four-stage conjunction while recording that
the `test` stage is a host-run check. The four stages remain the export gate and
only the export gate, per task 37.

**Bug 2 -- which files the quality bar applies to. Decision recorded.**

2.6 WHEN the `lint` stage and the `format` check run, THE system SHALL judge only
hand-written files, and SHALL determine which files are generated from a **single
definition** consumed by the `Quality_Gate`, the `Code_Validator`'s `lint` stage,
and the packaging exclusion, so the list is stated once. **Decision:** files are
excluded **because they are generated by the `Insight_Plugin_CLI` and the
`Agent_Rulebook` forbids editing them**, not because they produced findings --
the same standard task 37 applied to the excluded validator. The tradeoff: a
genuine defect inside a generated file will not be reported by these checks, which
is acceptable because such a defect is a defect in the CLI's templates and cannot
be fixed in the plugin. The precedent to follow rather than reinvent is
`build_engine._EXCLUDED_DIRS` together with `quality_gate.is_generated` /
`is_lint_excluded`, which already notes that the plugins repository's own
static-analysis job filters `unit_test/` before running.

2.7 WHEN a generated plugin that a human reviewer would call clean is checked, THE
system SHALL report the `lint` stage as passed and the `formatted` and
`lint_clean` definition-of-done conditions as met.

2.8 WHEN a lint or format result is produced, THE system SHALL report which
prospector profile was resolved, from which source, and which line length was
applied, so that a finding is attributable to the bar that produced it; and any
check whose expected outcome depends on profile content SHALL pin the profile
explicitly rather than relying on discovery. **Decision:** runtime discovery is
kept, because task 38 chose it deliberately -- a second copy of the repository's
rules drifts, and then the two disagree about what clean means. The tradeoff,
stated rather than removed: two operators with different checkouts can still see
different findings; what changes is that the report says so, and that the test
suite no longer varies with the developer's home directory.

2.9 WHEN the pre-existing failure in
`test_a_real_defect_is_still_reported` is addressed, THE system SHALL report a
genuine defect in hand-written code -- an `undefined-variable` for a `requests`
that is used and never imported -- under the profile the fix pins, so the
regression that motivated that test remains covered.

2.10 WHEN this decision is applied, THE system's specification SHALL be amended
to match: Requirement 26.3 SHALL name the single definition of generated files
that all checks consume, and Requirement 27.1's `lint_clean` and formatting
conditions SHALL state that they apply to hand-written code only.

**Bug 3 -- which artifact is authoritative. Decision recorded.**

2.11 WHEN the delegated agent has written to the project tree, THE system SHALL
treat the on-disk `plugin.spec.yaml` as authoritative over the in-session draft,
and SHALL re-read the draft's spec from the tree at the end of an implementation
turn so the two cannot diverge silently. **Decision: disk wins**, because disk is
what gets packaged and shipped. The tradeoff, which spans the orchestrator and is
the reason this is a decision rather than a patch: after an implementation turn
the draft stops being the authored source and becomes a view of the tree, so any
in-session edit made after that turn must be written to the tree to survive.

2.12 WHEN `export/prepare` computes the preview, THE system SHALL derive
`spec_preview` and the completeness findings from the spec that would actually be
packaged, and SHALL report zero completeness findings for a spec that is complete
on disk.

2.13 WHEN the `api_client` condition is evaluated, THE system SHALL treat an
`HTTP_ERROR_MAP` that `util/api.py` imports from elsewhere within the plugin
package as satisfying that condition, and SHALL report it unmet only when the map
is neither defined nor imported there -- matching the pattern
`implementation.md` prescribes.

2.14 WHEN this decision is applied, THE system's specification SHALL be amended
to match: Requirement 16.1 SHALL state that the previewed spec is the spec that
would be packaged, and Requirement 27.1's API-client condition SHALL state that
the error map may be defined in or imported into the client module.

**Lower-priority defects.**

2.15 WHEN a plugin is packaged, THE system SHALL exclude local build and test
byproducts, including `.coverage` at any depth, from the `.plg`.

2.16 WHEN an export is blocked, THE system SHALL report, for each stage that did
not pass, that stage's error output subject to the existing truncation-with-full-
access rule, so the operator can act without reproducing the pipeline by hand.

2.17 WHEN a turn ends in a clarification request, THE system SHALL NOT have
announced generation work it did not perform.

2.18 WHEN the interpreter is invoked, THE system SHALL record that invocation's
usage in the session total, so the displayed cost accounts for every paid call
rather than only the agent run.

2.19 WHILE the delegated agent runs, THE system SHALL emit periodic progress
carrying the current step, so a long run is distinguishable from a hang.

2.20 IF an attachment is truncated for the interpreter prompt, THEN THE system
SHALL tell the user which file was truncated and at what size, and SHALL state
that the agent receives the full file.

2.21 WHEN the export preview is returned after a version bump, THE system SHALL
populate `version_display` with the previous and new version, per Requirement
12.6.

2.22 WHEN `plugin-spec.md` steering lists valid credential types, THE system's
steering SHALL include `credential_token` with its `{token, domain}` shape, so a
valid spec does not read as a defect.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a plugin is generated against supplied reference material, THE system
SHALL CONTINUE TO produce correct endpoints, methods, and payload shapes with no
stubs or TODOs, a real `connect()` and `test()`, a central `_make_request`, and
accurate per-action source citations. Nothing in this fix touches generation.

3.2 WHEN a plugin is packaged, THE system SHALL CONTINUE TO exclude `.builder/`
and all reference material from the `.plg`, and SHALL CONTINUE TO include every
file the plugin needs -- the 39-entry baseline less the byproducts named in 2.15.

3.3 WHEN a check or validator is excluded, THE system SHALL CONTINUE TO justify
the exclusion by the excluded thing's own nature rather than by its having
complained, per task 37's standard that an exclusion must make the bar measurable
rather than lower it.

3.4 WHEN the `insight-plugin` validators run, THE system SHALL CONTINUE TO
exclude exactly one validator, for its hard dependency on the
`insightconnect-plugins` repository, and SHALL CONTINUE TO perform that
validator's own check itself through `core/version_bump.py` (Requirement 8.10).

3.5 WHEN export permission is decided, THE system SHALL CONTINUE TO permit export
if and only if the spec is valid and all four code stages passed (Property 17),
and SHALL CONTINUE TO treat the `Definition_Of_Done` as advisory rather than as a
term in that conjunction (Requirement 27.6), presenting outstanding conditions
alongside a permitted preview (Requirement 27.7).

3.6 WHEN a condition cannot be evaluated because a tool is unavailable, THE system
SHALL CONTINUE TO report it as unverified rather than met (Requirement 27.5), and
a skipped check SHALL CONTINUE TO be distinguishable from a passing one
(Requirement 26.4).

3.7 WHEN the `Quality_Gate` runs, THE system SHALL CONTINUE TO compile, format-
check, and execute the files under `unit_test/`; the exclusion in 2.6 applies to
lint only, because a unit test that does not parse is still a broken plugin.

3.8 WHEN the repair loop runs, THE system SHALL CONTINUE TO decide termination
from finding keys alone, with position-shift-stable identity, a stall condition,
and an explicit round limit, and SHALL CONTINUE TO label a stalled or
limit-reached outcome honestly (Requirement 26.6-26.11).

3.9 WHEN a delegated CLI is launched, THE system SHALL CONTINUE TO pass the
prompt on stdin, construct a default-deny environment, enumerate granted tools
explicitly, and keep untrusted content out of a shell-capable agent's prompt
(Requirement 29).

3.10 WHEN reference material is supplied, THE system SHALL CONTINUE TO store it
verbatim inside `.builder/reference/` with its provenance recorded, and to name
the stored files to the agent (Requirement 28).

3.11 WHEN a version is bumped, THE system SHALL CONTINUE TO apply the existing
breaking-change classification, monotonic bump, and `version_history` extension
(Requirement 12), and SHALL CONTINUE TO record registry and audit entries for
builds and exports (Requirements 11, 18).

3.12 WHEN an export is forced past a blocked gate, THE system SHALL CONTINUE TO
succeed without crashing and to record the export as forced, per the fix already
in the working tree.

### Bug Conditions and Properties

**Bug 1 -- the `test` stage.**

```pascal
FUNCTION isBugCondition_1(X)
  INPUT: X of type PluginWorkingTree
  OUTPUT: boolean

  // A tree whose own unit tests pass on the host, whose generated
  // .dockerignore excludes unit_test/, and whose runtime image has no pytest.
  RETURN hostUnitTestsPass(X)
     AND dockerignoreExcludes(X, 'unit_test')
     AND NOT imageHasPytest(X)
END FUNCTION
```

```pascal
// Property: Fix Checking -- a healthy plugin clears the gate
FOR ALL X WHERE isBugCondition_1(X) DO
  report <- runPipeline'(X)
  ASSERT stage(report, 'test').passed
  ASSERT decideExport'(specReport(X), report).permitted
  ASSERT stage(report, 'test').passed = hostUnitTestsPass(X)
END FOR
```

**Bug 2 -- the `lint` stage and the format condition.**

```pascal
FUNCTION isBugCondition_2(X)
  INPUT: X of type PluginWorkingTree
  OUTPUT: boolean

  // Every lint or format complaint is located in a generated file.
  RETURN findings(X) <> EMPTY
     AND FOR ALL f IN findings(X): isGenerated(path(f))
END FUNCTION
```

```pascal
// Property: Fix Checking -- generated files raise nothing, and the bar is stated
FOR ALL X WHERE isBugCondition_2(X) DO
  report <- qualityGate'(X)
  ASSERT findings(report) = EMPTY
  ASSERT stage(runPipeline'(X), 'lint').passed
  ASSERT condition(doneReport'(X), 'formatted').met
  ASSERT condition(doneReport'(X), 'lint_clean').met
  ASSERT profileSource(report) IS REPORTED
END FOR
```

Note on measurement: the counts in 1.4 and 1.5 were taken with **bare**
prospector and black defaults. The tool resolves a profile plus
`LINT_TOOLS` and formats at `--line-length 120`, so re-measure through the
tool's own resolved profile before fixing; the figures may differ, and the
requirement is about which files are judged, not about a particular count.

**Bug 3 -- preview fidelity.**

```pascal
FUNCTION isBugCondition_3(X)
  INPUT: X of type Session
  OUTPUT: boolean

  // The agent has written a spec to the tree that differs from the draft.
  RETURN implementationDelegated(X)
     AND diskSpec(projectFolder(X)) <> draftSpec(X)
END FUNCTION
```

```pascal
// Property: Fix Checking -- the preview describes what would be packaged
FOR ALL X WHERE isBugCondition_3(X) DO
  plan <- prepareExport'(X)
  ASSERT plan.spec_preview = versionedVendorSuffixed(diskSpec(projectFolder(X)))
  ASSERT completenessFindings(plan) = checkCompleteness(diskSpec(projectFolder(X)))
  ASSERT isCompleteOnDisk(X) IMPLIES completenessFindings(plan) = EMPTY
END FOR
```

```pascal
// Property: Fix Checking -- the API client detector accepts the prescribed pattern
FOR ALL X WHERE errorMapImportedIntoClient(X) DO
  ASSERT condition(doneReport'(X), 'api_client').met
END FOR
```

**Preservation, for all three.**

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT (isBugCondition_1(X) OR isBugCondition_2(X)
                     OR isBugCondition_3(X)) DO
  ASSERT F(X) = F'(X)
END FOR
```

Read as: for a tree with a genuine hand-written defect, a genuinely failing test,
or a genuinely incomplete on-disk spec, the fixed tool reports exactly what the
current one does. In particular `F(X) = F'(X)` covers packaged contents less the
byproducts of 2.15, the gate's conjunction, the advisory status of the definition
of done, and the unverified-versus-met distinction.

Where **F** is the tool as it behaves at `e7726b7` and **F'** is the tool after
this fix.

### Known Gaps (Unverified, Not Claimed Fixed)

Recorded so they are not mistaken for verified. Each is outstanding work, not a
finding.

- **Accessibility is entirely unreviewed.** Browser Mode was off for the whole
  run, so every finding above comes from timings and payloads. Export preview
  layout, twelve-condition presentation, empty states, keyboard operation, focus
  order, and screen-reader announcements were all unobserved.
- The `unverified` condition status was never exercised; the host had every tool
  installed. That a missing linter renders distinctly from a failing one is
  unverified.
- **No tenant import was performed** -- the one test that would prove the premise
  end to end.
- PDF and `reference_urls` paths are untested and unreachable from the browser:
  `MessageInput.tsx` accepts only `.json,.yaml,.yml,.txt,.md` and reads via
  `await file.text()`, and `MessageAttachment` in `types.ts` has no
  `encoding`/`media_type` field, so the base64 route the backend needs for PDFs
  cannot be populated by the UI. Closing that frontend gap is its own task.
- Whether the repair loop ran at all could not be determined; nothing records its
  rounds.
- The generated plugin's unit tests were verified to **exist and to have run**
  (coverage data present for every module), not to pass, because on this host the
  SDK interpreter (`/Library/Developer/CommandLineTools/usr/bin/python3`, 3.9) has
  no pytest while the project venv has pytest and no SDK. Bug 1's decision makes
  this the gate's problem rather than the tester's, so it needs to hold on a host
  where the two are separated.

### Reproduction Environment

Facts the run depended on, recorded because several defects read as environmental
without them.

- `insight-plugin` is **1.9.20**, not 1.11.0 as some notes claim.
- `insight-plugin` and `prospector` live in `~/Library/Python/3.9/bin`; `docker`
  in `/Applications/Docker.app/Contents/Resources/bin`. Neither is on a
  non-login shell `PATH`. Start the server with both prepended, or stages fail for
  environmental reasons and conditions read `unverified` misleadingly.
- `setsid` does not exist on macOS; detach with `( nohup ... & )`.
- `frontend/dist/` is stale by default and untracked; run
  `cd frontend && npm run build` first.
- `export/prepare` re-runs Docker: roughly 45s warm, several minutes cold.
- Harness from the run, reusable: `/tmp/icpb_drive.py` (one UI message per
  invocation over the websocket, timestamps frames, flags gaps over 10s),
  `/tmp/icpb_export.py`, `/tmp/icpb_conds.py`, `/tmp/icpb_force_and_audit.py`,
  `/tmp/jc_reference/*.yaml`, `/tmp/icpb_runlog/`.
