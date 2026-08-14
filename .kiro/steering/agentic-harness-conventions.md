---
inclusion: manual
---

<!--
Vendored from rapid7/ai-vault -- do not edit here.
  asset:   rules/agentic-harness-conventions
  version: 1.1.0
  source:  https://github.com/rapid7/ai-vault (commit 56b4497, 2026-08-14)

Kept at `inclusion: manual` (the vault's own setting): this is a reference table,
not a constant constraint. Pull it in with `#agentic-harness-conventions` when
writing code that reads or writes Kiro agent configs, skills, or steering --
which the plugin builder does when it generates the delegated agent config.

To update, re-copy from the vault and bump the version above.
-->

# Agentic Harness Conventions

Canonical reference for the directory conventions used by each agentic coding
harness (Kiro, Claude, Copilot). This document is the single source of truth
for user-level configuration paths, asset directory names, and metadata formats.
The agentic-dashboard, agentic-log-archivist, and any cross-harness tooling
should reference this document rather than hardcoding assumptions.

## User-Level Configuration Directories

Each harness stores its user-level configuration and installed assets under a
dot-directory in the user's home. The base directory can be overridden via an
environment variable.

| Harness | Default | Env Override | Description |
|---------|---------|--------------|-------------|
| Kiro | `~/.kiro` | `KIRO_HOME` | Kiro IDE agent configuration |
| Claude | `~/.claude` | `CLAUDE_CONFIG_DIR` | Claude Code CLI configuration |
| Copilot | `~/.copilot` | `COPILOT_HOME` | GitHub Copilot agent configuration |

When resolving the user directory, check the environment variable first. If set,
use its value as the base directory (replacing the default). If unset, fall back
to the default `~/.<harness>` path.

## Asset Directory Names

Asset types use **different directory names** across harnesses. Code that scans
installed assets must use the correct directory name per harness.

### Skills

All harnesses use the same directory name for skills:

| Harness | Skills Directory | Path |
|---------|-----------------|------|
| Kiro | `skills` | `~/.kiro/skills/` |
| Claude | `skills` | `~/.claude/skills/` |
| Copilot | `skills` | `~/.copilot/skills/` |

### Rules / Steering / Instructions

Each harness uses a different name and file convention for persistent agent rules:

| Harness | Directory | File Pattern | Frontmatter |
|---------|-----------|--------------|-------------|
| Kiro | `steering/` | `{name}.md` | `inclusion`, `fileMatchPattern`, `description` |
| Claude | `rules/` | `{name}.md` | `description`, `paths` (array of globs) |
| Copilot | `instructions/` | `{name}.instructions.md` | `applyTo` (comma-separated globs), `description` |

These are semantically equivalent — all provide persistent instructions that
influence agent behavior across sessions. When comparing "rules" across
harnesses, scan the correct directory and recognize the file extension per harness.

### Agents

All harnesses use the `agents/` directory but with different file extensions:

| Harness | Directory | File Pattern | Notes |
|---------|-----------|--------------|-------|
| Kiro | `agents/` | `{name}.md` | Flat file + optional companion `{name}-metadata.toml` |
| Claude | `agents/` | `{name}.md` | Flat file + optional companion `{name}-metadata.toml` |
| Copilot | `agents/` | `{name}.agent.md` | Uses `.agent.md` extension |

Agent files are markdown with YAML front-matter defining name, description,
allowed tools, model selection, and system prompt. When installed via `sx`,
a companion `{name}-metadata.toml` file is written alongside for version tracking.

### Commands (Slash Commands)

Commands are prompt files invoked by name (e.g. `/kiro-review`). `sx` distributes
them across claude-code, kiro, cline, codex, cursor, copilot, and opencode.

| Harness | Directory | File Pattern | Notes |
|---------|-----------|--------------|-------|
| Claude | `commands/` | `{name}.md` (flat) or `{name}/COMMAND.md` (dir asset) | YAML front-matter: `description`, `argument-hint`, `allowed-tools`, `disable-model-invocation` |
| Copilot | `commands/` | `{name}.md` | — |
| Others | `commands/` | `{name}.md` | Kiro/cline/codex/cursor/opencode per harness |

When distributed as an `sx` directory asset, the canonical prompt file is
`COMMAND.md` alongside `metadata.toml` (mirroring `SKILL.md`/`RULE.md`). A command
may declare a `dependencies` array pointing at the agent(s) it dispatches — e.g.
a review command depends on its `*-review-agent`. Commands that embed harness-specific
constructs (`$ARGUMENTS`, the Claude `Agent`/`subagent_type` dispatch, `CLAUDE_PLUGIN_ROOT`)
declare only the harnesses they actually run on in `clients`; they are not portable
by virtue of living in a shared `commands/` directory.

### Plugins / Powers

Plugins are bundles that package multiple components (skills, agents, hooks,
MCP servers) into a single distributable unit. Each harness uses a different
name and structure:

| Harness | Directory | Structure | Manifest |
|---------|-----------|-----------|----------|
| Kiro | `powers/` | Registry-based (`installed.json` + git clones) | `POWER.md` with frontmatter |
| Claude | `plugins/` | Directory per plugin: `plugins/{name}/` | `.claude-plugin/plugin.json` |
| Copilot | — | Not yet supported | — |

**Claude plugins** are self-contained directories with a `.claude-plugin/plugin.json`
manifest. Component subdirectories (`skills/`, `agents/`, `hooks/`) live at the
plugin root. When installed via `sx`, they use the `dirasset` handler (full
directory extraction with `metadata.toml`).

**Kiro powers** are managed via a registry system:

- `~/.kiro/powers/registry.json` — available powers catalog
- `~/.kiro/powers/installed.json` — tracks which powers are installed
- Powers are cloned from git repos at install time

Note: Copilot does not currently have an equivalent plugin/power concept.
The `plugins_dir` field is empty for Copilot in `harnesses.json`.

### Other Directories

| Directory | Kiro | Claude | Copilot |
|-----------|------|--------|---------|
| Settings/config | `settings/` | `settings.json` | `settings/` |
| Hooks | `hooks/` | `hooks/` | — |
| MCP servers | `settings/mcp.json` | `settings/mcp.json` | — |
| Specs | `specs/` | — | — |
| Powers | `powers/` | — | — |
| Plugins | `plugins/` | — | — |

## Asset Metadata Format

Skills are installed as directories containing a `SKILL.md` prompt file and
optionally scripts, references, and other supporting files. The directory name
is the canonical asset identifier.

```text
~/.kiro/skills/my-skill/
├── SKILL.md
├── scripts/
└── references/
```

If the asset was installed via `sx` (github.com/sleuth-io/sx), a `metadata.toml` file
will also be present with publishing metadata (name, version, description,
keywords). This file is specific to `sx` — the harnesses themselves do not
read or depend on it.

### metadata.toml Schema (sx-specific, not a harness convention)

```toml
[asset]
name = "my-skill"
version = "1.0.0"
type = "skill"                    # "skill", "rule", "command", or "agent"
description = "What this asset does"
keywords = ["keyword1", "keyword2"]
clients = ["claude-code"]         # optional: harnesses this asset runs on
dependencies = ["other-asset>=0.1.0"]  # optional: assets this one requires

[skill]                           # or [rule] / [command] / [agent]
prompt-file = "SKILL.md"          # SKILL.md / RULE.md / COMMAND.md / AGENT.md
```

The type-specific section name and canonical `prompt-file` track the asset type:
`[skill]`→`SKILL.md`, `[rule]`→`RULE.md`, `[command]`→`COMMAND.md`, `[agent]`→`AGENT.md`.
`clients` is the single source of truth for which harnesses an asset targets — it
travels inside the asset, not in the vault-level `sx.toml` manifest.

### Rule Front-Matter (Kiro Steering)

Kiro steering files support YAML front-matter for inclusion control:

```markdown
---
inclusion: manual | fileMatch
fileMatchPattern: "*.ts"
---
# Rule Title

Rule content...
```

Inclusion modes:

- **always** (default, no front-matter needed) — loaded into every session
- **manual** — only loaded when user explicitly references via `#` context key
- **fileMatch** — loaded when a matching file is read into context

## Configuration File

The agentic-log-archivist's `config.json` registers each harness by name,
path, and script. Directory conventions (user_dir, skills_dir, rules_dir,
env_override) live in this rule's `harnesses.json` reference file — not in
the config.

```json
// agentic-archive/config.json — archivist registry only
{
  "archivists": [
    { "name": "kiro",    "path": "../kiro-log-archivist",    "script": "scripts/archive.sh" },
    { "name": "claude",  "path": "../claude-log-archivist",  "script": "scripts/archive.sh" },
    { "name": "copilot", "path": "../copilot-log-archivist", "script": "scripts/archive.sh" }
  ]
}
```

```json
// harnesses.json — directory conventions (bundled with agentic-dashboard at references/harnesses.json)
{
  "harnesses": [
    { "name": "kiro",    "user_dir": "~/.kiro",    "env_override": "KIRO_HOME",       "skills_dir": "skills", "rules_dir": "steering",     "agents_dir": "agents" },
    { "name": "claude",  "user_dir": "~/.claude",  "env_override": "CLAUDE_CONFIG_DIR","skills_dir": "skills", "rules_dir": "rules",        "agents_dir": "agents" },
    { "name": "copilot", "user_dir": "~/.copilot", "env_override": "COPILOT_HOME",    "skills_dir": "skills", "rules_dir": "instructions", "agents_dir": "agents" }
  ]
}
```

At startup, the agentic-dashboard merges both: archivists from config.json
provide the ETL pipeline info, and harnesses.json provides the filesystem
conventions. The env_override is checked first — if set, it replaces the
default user_dir entirely.

## Cross-Harness Asset Comparison

When comparing installed assets across harnesses:

1. Scan `{user_dir}/{skills_dir}/` for each configured harness
2. Scan `{user_dir}/{rules_dir}/` for each configured harness
3. Scan `{user_dir}/{agents_dir}/` for each configured harness
4. Read `metadata.toml` from each asset subdirectory for version and description
5. Flag **missing** assets (present in one harness but not another)
6. Flag **version drift** (same asset name, different version across harnesses)

Note: Not all assets are expected to be installed everywhere. Some are
harness-specific (e.g., `kiro-inspect-logs` only makes sense in Kiro). The
inconsistency detection is informational — the user decides which gaps matter.
