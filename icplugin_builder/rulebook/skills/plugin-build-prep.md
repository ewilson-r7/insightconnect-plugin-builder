# Build Prep — Before and After

The environment is already prepared for you: the toolchain is installed, the SDK
version is resolved and stamped into `plugin.spec.yaml`, and the plugin's directory
is your working directory. What is left for you is to confirm what you are working
with before you start, and to verify your own output before you report done.

## Before you start

1. **Read `plugin.spec.yaml`.** It may be complete, partial, or invalid. It is the
   source of truth for what to build, and correcting it is part of the job — not a
   reason to stop.
2. **Read the vendor documentation** under `.builder/reference/`. That is where the
   endpoints, payload shapes, auth model, pagination and error formats come from. You
   have no web access, and an inferred endpoint is a wrong endpoint.
3. **Look at what already exists.** A partly-built plugin is common. Read
   `util/api.py`, `connection/connection.py` and the existing actions before adding
   to them, so you extend the established pattern rather than introducing a second one.

Do not check tool versions or look up the SDK release. Both are settled before you
are invoked, and `sdk.version` in the spec is already correct.

## After you write code — verify it

Run all three from the plugin root. They are the same checks the export gate applies,
so a plugin that passes here is a plugin that can ship.

```bash
prospector icon_<plugin_name>/          # hand-written code must be clean
python -m pytest unit_test -q           # the tests must pass, not merely exist
insight-plugin validate                 # the spec and tree must validate
```

`python -m` puts the plugin root on `sys.path`, which is how
`from icon_<name>... import ...` resolves inside a test. No `conftest.py` and no
`sys.path` manipulation is needed.

## What "done" means

All of the following, together:

- `insight-plugin validate` passes.
- `prospector` reports nothing against hand-written code. Findings in generated files
  are ignored by the gate — do not edit a generated file to silence one.
- Every hand-written Python file parses and is `black`-formatted at 120 columns.
- `util/api.py` exists with a central `_make_request`, an `HTTP_ERROR_MAP`, and one
  domain method per action. Actions call those methods; they never import an HTTP
  library or build URLs.
- `connection.py` has a real `connect()` (state only) and a real `test()`. A `pass`
  or a `# TODO` in `test()` means not done.
- Unit tests exist per action against a mocked client **and pass**. A
  `self.fail("Unimplemented Test Case")` stub means not done.
- Statement coverage of the plugin package is at least 80%.
- `plugin.spec.yaml` is complete: every field `insight-plugin validate` needs, plus
  `version_history` and an `example` on every output.
- `requirements.txt` exists with exact pins, even if it has no dependencies.

If you cannot reach that bar, say so and name exactly what is failing. A report of
success with a known failure open is worse than no report.
