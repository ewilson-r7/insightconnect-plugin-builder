# The agent's rulebook

These eleven files are what the delegated agent loads as its rulebook: how an
InsightConnect plugin is structured, what `plugin.spec.yaml` must contain, how
actions and API clients are written, what the linter enforces, and how unit tests
are laid out. `icplugin_builder/integrations/agent_config.py` references them by
name in `RULEBOOK_FILES` and passes them to the Kiro CLI as agent resources.

They live here rather than being paraphrased into prompt strings. That distinction
is the point of `.kiro/steering/project-conventions.md`'s rule against a second
rulebook: a paraphrase drifts from the real rules and then contradicts them, which
has happened in this codebase before. These are the real files, copied verbatim.

## Resolution order

`~/.kiro/<path>` wins when it exists; this bundled copy is the fallback. So an
operator who keeps their own rulebook is unaffected by anything here, and editing
a file under `~/.kiro` still changes how the tool builds plugins. A user who has
never installed the plugin skills gets a complete rulebook instead of an agent
running with reduced guidance.

Resolution is per file, not all-or-nothing: one local override coexists with ten
bundled files.

## Provenance

Vendored from `~/.kiro/{skills,steering}/`, whose files are maintained in a separate
`plugins` repository. They were copied verbatim first -- byte-identical, hashes
checked -- and then simplified for this tool in a second commit, so that diff reads
against a known-good base.

### What the simplification removed

The vendored files were written for a plugins monorepo driven by hand from a
developer's own machine. This tool hands the agent one plugin directory and runs the
toolchain itself, so the following was material the agent could not act on:

- **Repository routing.** `plugin-dev.md` opened with a mandatory "prod or dev?"
  decision and absolute `~/Documents/GitHub/...` paths for each. There is one
  directory here and the agent is already in it.
- **Git and release flow.** Branch strategy, commit conventions, push targets, PR
  creation. The agent leaves the plugin finished in place; nothing is committed.
- **Dangling references.** Three files pointed at `repos.md`, and `plugin-dev.md`
  listed nine skills and two steering files that were not vendored and do not apply.
- **SDK version lookup.** All four skills instructed reading the latest release from
  the `## Changelog` of a local clone of the SDK repository at a hardcoded path. The
  tool resolves it from the package index and stamps it into the spec before the
  agent runs.
- **`PYENV_VERSION=3.13.x` prefixes** on every command. The tool chooses the
  interpreter.
- **Monorepo release steps** -- updating a `docs/<plugin>.html` page and a README
  plugin table -- which a generated plugin has no equivalent of.
- **Kiro IDE front-matter.** These files are passed as explicit agent resources, so
  `inclusion:` decides nothing, and `fileMatchPattern: "plugins/**/*.py"` never
  matched a tree whose root *is* the plugin.
- **One vendor's API quirks.** `implementation.md` carried a Microsoft Graph section;
  a generic rulebook is the wrong place for it.

### What it corrected

`testing.md` said to run tests from inside `unit_test/`, with
`sys.path.append(os.path.abspath("../"))` at the top of every test file. The tool runs
`python -m pytest unit_test -q` from the plugin root, where `python -m` puts the root
on `sys.path` and the append line is inert. Measured against the JumpCloud tree: 33
tests pass under the tool's own invocation. The guidance now matches it.

`prospector.md` framed the linter as a CI job gating a merge. It is the export gate,
and it judges hand-written code only -- findings in generated files are ignored, which
matters because the agent is forbidden to edit those files and so could never resolve
them.

Net effect: 56,101 bytes to 52,522, and nothing left that points outside the plugin
directory.

## Keeping them current

`make sync-rulebook` re-copies from `~/.kiro`, which **overwrites the simplification
above**. Use it to see what moved upstream -- run it, read `git diff`, then re-apply
by hand or discard -- rather than as a routine step.
