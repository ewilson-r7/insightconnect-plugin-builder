# InsightConnect Plugin Builder

A locally-run tool that turns a plain-language description into a Rapid7
InsightConnect plugin. Describe the plugin you want, or point it at one you
already have, and it does the development work: writes the spec, scaffolds with
`insight-plugin`, implements the connection, API client, actions and unit tests,
and runs the toolchain to check its own output.

It runs entirely on your machine. There is no hosted backend and no account
model.

## How it works

The tool is an orchestration layer, not a code generator. The two things it
wraps are the real InsightConnect toolchain (`insight-plugin`, the SDK, Docker)
and the **Kiro CLI running as an agent** in the plugin's own working directory.

Plugin conventions are not encoded in this repo. The agent's rulebook is the
InsightConnect plugin skills and steering in `~/.kiro/` -- `plugin-dev`,
`create-new-plugin`, `implementation`, `common-mistakes`, `plugin-spec`,
`testing`, and the rest. Editing one of those files changes how the tool builds
plugins, with no second copy in this codebase to keep in sync.

`plugin.spec.yaml` is the source of truth for every plugin. Derived files
(`schema.py`, `Dockerfile`, `Makefile`, `setup.py`, `help.md`, `.CHECKSUM`) are
produced by `insight-plugin refresh` and never hand-edited.

## Requirements

- Python 3.11+
- The [Kiro CLI](https://kiro.dev), installed and authenticated (`kiro-cli whoami`)
- `insight-plugin` on `PATH`
- The plugin skills and steering installed at `~/.kiro/skills` and `~/.kiro/steering`
- Docker, for the build / validate / package path only. Everything else --
  conversation, spec editing, visualization, documentation, project history --
  works without it.

## Running it

```bash
pip install -e .
icplugin-builder
```

Configuration is read from `~/.icplugin-builder/config.yaml` (override with
`ICPLUGIN_BUILDER_CONFIG`). The server binds the loopback interface by default;
open the printed URL.

On first start the tool registers its agent config at
`~/.kiro/agents/icplugin-builder.json`. A config you wrote yourself at that path
is never overwritten.

## Development

```bash
pytest                                  # full suite
flake8 icplugin_builder tests
black icplugin_builder tests
cd frontend && npm run build            # UI, served as static assets
```

`.kiro/steering/project-conventions.md` records the quality bar a generated
plugin has to clear and the conventions for changing this repo. Read it before
making changes.

## The specification

`.kiro/specs/insightconnect-plugin-builder/` holds the requirements, design, and
implementation plan, and is current as of the revision described in each document's
revision note.

Worth reading if you are changing this tool: the first version of that
specification described a plugin spec *editor*. It required a schema-valid spec
and recorded validation results, but never required that the generated plugin
run. Every requirement was implemented, the suite was green, and the output was
unusable. The revision added delegated implementation (Requirement 3), corrective
validation (Requirement 26), and an explicit definition of done (Requirement 27).

`tasks.md` carries a **Remaining work** section listing what is specified but not
yet built, so the gap between plan and code is visible rather than discovered.
