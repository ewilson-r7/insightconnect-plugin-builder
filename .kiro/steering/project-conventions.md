---
inclusion: always
name: project-conventions
description: "How the InsightConnect Plugin Builder is meant to work, the quality bar a generated plugin must clear, and the conventions to follow when changing this repo."
---

# InsightConnect Plugin Builder -- Project Conventions

This tool takes a plain-language description and produces a Rapid7 InsightConnect
plugin that works on day one. It is an orchestration layer over the real
toolchain (`insight-plugin`, the InsightConnect SDK, Docker) and over the Kiro
CLI as the agent that does the development work.

## The quality bar (definition of done for a generated plugin)

A plugin is **not** done because a pipeline reported four stage results. It is
done when all of the following hold:

- `insight-plugin validate` passes.
- `prospector <package>/` is clean.
- Every generated `.py` file parses (`py_compile`) and is `black`-clean.
- `util/api.py` exists with a central `_make_request`, an `HTTP_ERROR_MAP`, and
  one domain method per action. Actions call those methods -- actions never
  import `requests` or build URLs.
- `connection.py` has a real `connect()` (state only) and a real `test()`. A
  `# TODO` or bare `pass` in `test()` means not done.
- Unit tests exist per action with mocked clients, they **pass**, and statement
  coverage of the plugin package is >=80%. A `self.fail("Unimplemented Test Case")`
  stub means not done. Tests and coverage are checked by the quality gate and are
  repairable, so a failing test is a finding rather than a late surprise at export.
- `plugin.spec.yaml` is complete: every field `insight-plugin validate` needs,
  plus `sdk.version` at the current SDK release, `version_history`, and an
  `example` on every output.
- `requirements.txt` exists with exact pins (may be empty of deps).

If a run cannot reach this bar, it must say so plainly and name what is
outstanding. Never report a plugin as ready with failures open.

## Plugin conventions live in skills and steering, not in this codebase

The authoritative rules for how an InsightConnect plugin should be written are
the Kiro skills and steering files in `~/.kiro/` (symlinked from
`~/Documents/GitHub/plugins/.kiro/`): `plugin-dev`, `create-new-plugin`,
`create-plugin-action`, `plugin-build-prep`, `common-mistakes`,
`implementation`, `plugin-spec`, `testing`, `structure`, `exceptions`.

**Do not restate those rules as prompt strings in this codebase.** A duplicated
rulebook drifts from the real one and then contradicts it. This has already
happened once: hand-written prompts in `llm_generator.py` disagreed with
`plugin-spec.md` about credential field types, and hardcoded one specific
vendor's base URL into the generic action prompt. Pass the skills to the agent
instead of paraphrasing them.

## Delegating to the Kiro CLI

Agreed direction: invoke the Kiro CLI as an **agent** with tools and a working
directory, not as a single-shot text completion whose stdout gets spliced into
files.

- Prompt goes on **stdin**, never argv -- keeps secrets out of the process list
  and avoids `E2BIG`.
- Trust the narrowest tool set that can do the job (`--trust-tools=...`).
  Blanket `--trust-all-tools` needs a stated reason.
- **Never let the child inherit this process's environment.** This tool decrypts
  tenant API keys and git credentials in memory; a plain
  `create_subprocess_exec` hands every env var to the LLM subprocess. Use a
  default-deny allowlist (Kiro needs `KIRO_ AWS_ AMAZON_ CODEWHISPERER_`).
- Surface the child's stderr on failure. Never silently return nothing.
- Do not paste untrusted content (imported production plugins, `.plg` contents,
  fetched web pages) into the prompt of a shell-capable agent.

`kiro-cli` is on PATH via a wrapper at `/opt/homebrew/bin/kiro-cli`. It must be a
wrapper, not a symlink: the launcher resolves its sibling binaries relative to
its own path and a symlink outside the `.app` bundle breaks it.

## Vendor API knowledge has to be supplied, not discovered

The delegated agent has no web access. The Kiro CLI's tool set is
`read`/`write`/`shell`/`grep`/`glob` plus MCP servers, and no fetch server is
enabled, so the agent cannot look up a vendor's API on its own. It can reach the
network via `shell`, but nothing here grants or relies on that.

So for any plugin against a real vendor API, attach the API documentation or an
OpenAPI spec. Attachments are written verbatim into the project's
`.builder/reference/` and the agent is told to use them for endpoint paths,
methods, request and response shapes, auth, pagination, and error formats. Without
them the agent will infer endpoints, and inferred endpoints are wrong.

Do not reintroduce a parser that reads the spec here and passes an extract into a
prompt. That existed, it was lossy, and it created a second representation that
drifted. Give the agent the file.

## Validation must be corrective, not decorative

Running lint/build/test/validate and reporting the results is not enough -- that
is the current behavior and it is why broken plugins ship. Failures must be fed
back for repair, with a **deterministic** convergence test (compare finding keys
round over round; converge when a round adds no new keys) and an explicit cap.
Hitting the cap is reported as hitting the cap, never as success.

## Changing this repo

`.kiro/steering/ai-coding-discipline.md` is binding -- in particular SCOPE-4
(refactors land as their own PR, before the feature work) and SCOPE-9 (no
abstraction without a second task-required caller).

SCOPE-9 has real precedent here: `InsightPluginCli.create()` is fully
implemented, documented, and property-tested, and has **zero call sites** in any
runtime path. Prefer wiring up what exists over adding more unreferenced
surface.

When writing code that reads or writes Kiro agent configs, skills, or steering,
pull in `#agentic-harness-conventions` for the canonical paths rather than
hardcoding them.

## Relationship to `.kiro/specs/`

The specification is current: Requirement 3 covers delegated implementation,
Requirement 26 corrective validation, Requirement 27 the definition of done,
Requirement 28 vendor reference material, Requirement 29 execution isolation, and
Requirement 30 spec completeness and build readiness. Each document carries a
revision note explaining what changed and why.

This file remains the short form of the same intent and is the quicker read. Where
the two differ in detail, the specification is authoritative; if you find a real
contradiction, that is a defect in one of them and worth fixing rather than
working around.

`tasks.md` has a **Remaining work** section. Check it before assuming a
requirement is implemented -- several are specified and not yet built, notably the
single definition-of-done gate (Requirement 27) and running the quality gate on
the export path.
