# Plugin Development — Core Rules

You build Rapid7 InsightConnect plugins: Python packages that expose **actions**,
**triggers**, and **tasks** to the InsightConnect SOAR platform.

The plugin's own directory is your working directory. There is no enclosing
repository to choose, no branch to pick, and nothing to push — your job is to leave
the plugin finished in place. The detailed patterns live in the other rulebook files;
this one is the summary.

## Plugin concepts

- **Actions** — one-shot operations ("Get Agent Details", "Quarantine Device")
- **Triggers** — event-driven polling loops that emit events via `self.send()`
- **Tasks** — scheduled background operations
- **Connection** — auth and API client setup, shared by everything above

## Core rules (always apply)

- `plugin.spec.yaml` is the source of truth. Edit it first, then run
  `insight-plugin refresh` to regenerate what derives from it.
- **Never edit generated files**: `schema.py`, `setup.py`, `bin/`, `__init__.py`,
  `.CHECKSUM`, `.dockerignore`, `Makefile`, `help.md`. Changing one is undone by the
  next refresh, and the linter's findings against them cannot be fixed by you.
- Always `self.logger`, never `print()`.
- Always raise `PluginException` or `ConnectionTestException`, never a bare
  `Exception`.
- Use `Output.FIELD_NAME` constants from the generated schema, never bare string keys.
- Descriptive variable names — no single-character identifiers, even in comprehensions.
- `connect()` stores state only and makes no API calls; `test()` validates credentials.
- `.strip()` every string credential.
- Guard before indexing: `if not results: raise PluginException(...)` before `[0]`.
- Wrap API responses in `clean()` to strip nulls and empty values.
- Pin every dependency in `requirements.txt` exactly (`requests==2.31.0`).
- Minimum 80% statement coverage on the plugin package.

## Endpoint knowledge comes from the documentation you were given

You have no web access. Vendor documentation is written verbatim into
`.builder/reference/` — read it for endpoint paths, methods, request and response
shapes, authentication, pagination and error formats. **Inferred endpoints are wrong
endpoints.** If the documentation does not cover something you need, say so rather
than guessing.

## Workflow: a new plugin

1. Write or correct `plugin.spec.yaml`
2. `insight-plugin refresh` to generate the scaffolding from it
3. Implement the connection, the API client, then each action
4. Write unit tests against a mocked client
5. Verify: `prospector`, `python -m pytest unit_test -q`, `insight-plugin validate`

## Workflow: a new action on an existing plugin

1. Read the existing spec to understand the connection, types and actions
2. Add the action to the spec; bump the version (semver) and add a `version_history`
   entry
3. `insight-plugin refresh` — this generates the action's folder and `schema.py`;
   never create those by hand
4. Implement `action.py` in the generated folder
5. Add a domain method to `util/api.py`; actions call it and never build URLs
6. Write the action's unit tests
7. Verify as above

## Versioning (semver)

| Change | Bump |
|--------|------|
| Remove or rename a field, change a type, add a required input | **Major** |
| New action/trigger/task, add an optional field | **Minor** |
| Bug fix, SDK update, dependency update | **Patch** |

Every bump needs a `version_history` entry. An unreleased plugin stays at `1.0.0`
regardless of how much changes.

## Verify before you report done

```bash
prospector icon_<plugin_name>/
python -m pytest unit_test -q
insight-plugin validate
```

Run them, read the failures, fix them, and run them again. Never report success with
a known failure open — say plainly what is still failing.

## The rest of the rulebook

| File | Provides |
|------|----------|
| `plugin-build-prep` | What to check before you start, and how to verify your work |
| `create-new-plugin` | Full workflow for a plugin from scratch, with code patterns |
| `create-plugin-action` | Full workflow for adding one action |
| `plugin-spec` | Every `plugin.spec.yaml` field, with types and UX guidance |
| `structure` | Directory layout, and which files are generated |
| `implementation` | Connection, action, trigger and API client patterns |
| `exceptions` | `PluginException` presets and correct usage |
| `testing` | Unit test patterns, mock strategy, fixtures |
| `common-mistakes` | The anti-patterns that come up most |
| `prospector` | Resolving specific linter findings |
