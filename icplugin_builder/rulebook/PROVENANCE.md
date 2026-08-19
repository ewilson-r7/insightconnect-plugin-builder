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

Vendored from `~/.kiro/{skills,steering}/`, whose files are maintained in a
separate `plugins` repository. That repository is the source of truth today, and
these copies were taken verbatim -- byte-identical, hashes checked at copy time.

They are **not** yet simplified for this tool. As vendored they still carry
material that only applies to their original home, notably:

- prod-versus-dev repository routing and absolute `~/Documents/GitHub/...` paths
  (`plugin-dev.md`, `plugin-build-prep.md`, `create-plugin-action.md`);
- references to files that were **not** vendored, so the links dangle -- chiefly
  `repos.md`, referenced by three of them;
- reading the current SDK version from a hardcoded local clone of the SDK
  repository rather than from anywhere this tool can reach;
- release-process steps for the plugins monorepo (updating a README table, a
  `docs/<plugin>.html` page) that a generated plugin has no equivalent of.

Simplifying them is a separate change, kept separate so its diff can be read
against a known-good verbatim base rather than against nothing.

## Keeping them current

`make sync-rulebook` re-copies from `~/.kiro`. Once these files have been
simplified, that target overwrites the simplification, so treat it as a way to
*see* what changed upstream (via `git diff`) rather than a routine step.
