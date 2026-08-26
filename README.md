# InsightConnect Plugin Builder

A locally-run tool that turns a plain-language description into a Rapid7
InsightConnect plugin. Describe the plugin you want, or point it at one you
already have, and it does the development work: writes the spec, scaffolds with
`insight-plugin`, implements the connection, API client, actions and unit tests,
and runs the toolchain to check its own output.

It runs entirely on your machine. There is no hosted backend and no account
model.

## Quick start

```bash
git clone https://github.com/rapid7/insightconnect-plugin-builder.git
cd insightconnect-plugin-builder
make setup            # installs dependencies and builds the web interface
icplugin-builder      # then open the printed URL, http://127.0.0.1:8787
```

`make setup` is `pip install -e ".[dev]"` plus a build of the web interface.
Installing the package alone leaves the interface unbuilt, and the server then
serves the API with nothing at `/`.

On first start the tool writes two files and tells you where:

- `~/.icplugin-builder/config.yaml` -- your configuration. Edit it freely; it is
  never overwritten once it exists.
- `~/.kiro/agents/icplugin-builder.json` -- the agent config it delegates plugin
  implementation to. A file you wrote yourself at that path is left alone.

That is enough to open the interface, describe a plugin, and edit a spec. To
*build, validate and package* a plugin you also need the toolchain below.

## What you need

To start the tool and work on a spec:

| | |
|---|---|
| Python 3.11+ | `python3 --version` |
| Node 18+ and npm | only to build the web interface, i.e. `make setup` / `make ui` |
| [Kiro CLI](https://kiro.dev), authenticated | `kiro-cli whoami` |
| *(the agent's plugin rulebook ships with the tool -- nothing to install)* | see [Agent rulebook](#agent-rulebook) |

To build, validate and package a plugin, additionally:

| | |
|---|---|
| `insight-plugin` | `pip install insight-plugin` |
| `prospector` and `black` | `pip install prospector black` |
| Docker, with the daemon running | `docker info` |
| A Python interpreter that can import **both** `insightconnect_plugin_runtime` **and** `pytest` | this is the interpreter your plugin's unit tests run under |

That last row is easy to get wrong and worth a moment. The plugin's tests run on
your machine rather than inside the plugin image, so one interpreter has to have
both the InsightConnect SDK and `pytest`. It is common to end up with the SDK in
one Python and `pytest` in another, in which case the export gate fails closed and
names the interpreter it tried -- correct behaviour, but baffling if you were not
expecting it.

**Everything in the second table has to be on the `PATH` of the shell you start
the server from.** On macOS `insight-plugin` and `prospector` often land in
`~/Library/Python/3.x/bin` and `docker` in
`/Applications/Docker.app/Contents/Resources/bin`, and neither is on a
non-login shell's `PATH`. A tool the server cannot see is reported as absent, which
looks like a defect in the plugin rather than a gap in the environment.

Without Docker and the toolchain, conversation, spec editing, visualization,
documentation and project history all still work.

## Check your setup

The interface reports what it found, and the startup output names anything
missing. To check before starting:

```bash
kiro-cli whoami                                   # authenticated?
insight-plugin --version                          # toolchain on PATH?
prospector --version && black --version
docker info > /dev/null && echo "docker ok"       # daemon running?
python3 -c "import insightconnect_plugin_runtime, pytest; print('sdk + pytest ok')"
```

The last line is the split-interpreter check. Run it with the *same* `python3`
you expect the tool to use; if it fails, install the missing one into that
interpreter rather than into another.

## Agent rulebook

The tool does not encode plugin conventions. The agent's rulebook is a set of
InsightConnect plugin skills and steering files -- `plugin-dev`,
`create-new-plugin`, `implementation`, `common-mistakes`, `plugin-spec`, `testing`,
`structure`, `exceptions`, `prospector` and the rest. They are what tell the agent
how a plugin is structured, what the spec must contain, and what the linter
enforces.

**Nothing to install.** Eleven of them ship with this package, so a new user gets a
complete rulebook with no setup. They are listed in `RULEBOOK_FILES` in
`icplugin_builder/integrations/agent_config.py` and bundled at
`icplugin_builder/rulebook/`.

**To change how the tool builds plugins, edit the rulebook.** A file at
`~/.kiro/<skills|steering>/<name>.md` takes precedence over the bundled copy, per
file, so you can override one and leave the rest alone:

```bash
mkdir -p ~/.kiro/steering
cp icplugin_builder/rulebook/steering/testing.md ~/.kiro/steering/
# edit it; the agent now follows yours
```

`$KIRO_HOME` moves that directory if you keep Kiro's config elsewhere.
`icplugin_builder/rulebook/PROVENANCE.md` records where the bundled files came from
and what still needs simplifying.

## How it works

The tool is an orchestration layer, not a code generator. The two things it
wraps are the real InsightConnect toolchain (`insight-plugin`, the SDK, Docker)
and the **Kiro CLI running as an agent** in the plugin's own working directory.

`plugin.spec.yaml` is the source of truth for every plugin. Derived files
(`schema.py`, `Dockerfile`, `Makefile`, `setup.py`, `help.md`, `.CHECKSUM`) are
produced by `insight-plugin refresh` and never hand-edited.

Generated plugins are checked before they can be exported: `insight-plugin
validate` passes, the linter is clean on hand-written code, the unit tests pass,
and coverage meets its minimum. A plugin that does not clear that bar is reported
as unfinished with the outstanding conditions named, rather than exported quietly.

**A `.plg` is a container image, not an archive of source code.** Exporting builds
the plugin's Docker image, tags it `<vendor>/<name>:<version>`, and saves it — the
code and `plugin.spec.yaml` travel inside the image's layers, and an InsightConnect
tenant loads the image on import. This is why Docker is required to *export* as well
as to build, and why the artifact is tens of megabytes rather than tens of kilobytes.
The export preview shows the image tag it will produce, because that tag is what a
tenant identifies the plugin by.

The image is built from a staged copy of exactly the files the preview lists, so
build and test byproducts left in the plugin directory — coverage data, stray
bytecode, a previous `.plg` — stay out of what you ship.

## Configuration

Read from `~/.icplugin-builder/config.yaml`, or from `$ICPLUGIN_BUILDER_CONFIG`
if set. Written on first start with the one required section filled in and the
rest commented at its default:

```yaml
llm:
  provider: kiro_cli
  kiro_cli_path: kiro-cli     # absolute path if yours is not on PATH
```

The server binds `127.0.0.1:8787` by default, so the tool is reachable only from
your own machine. `$ICPLUGIN_BUILDER_UI_DIR` overrides where the web interface is
served from.

## Troubleshooting

| What you see | What it means |
|---|---|
| `no built UI found` at startup, bare 404 at `/` | the web interface is not built -- run `make ui` |
| `ConfigError: llm: required configuration section is missing` | your config file exists but has no `llm:` section; the template is in [Configuration](#configuration) |
| `ModuleNotFoundError: hypothesis` when running `pytest` | dev dependencies are missing -- `make install` |
| A stage reports a tool as absent although you have it | the server's `PATH` does not include it; start it from a shell that does |
| The `test` stage fails naming an interpreter | that interpreter cannot import both the SDK and `pytest` -- see [What you need](#what-you-need) |
| `the Docker daemon is not reachable` when exporting | packaging builds a container image, so the daemon is needed for export too -- start Docker; nothing about the plugin needs changing |
| `cannot form a Docker image tag` | the spec's `vendor` or `name` cannot be a tag component: lowercase letters, digits, and `.`/`_`/`-` between them |
| Export blocked with conditions listed | the plugin genuinely is not finished; the named conditions are what is outstanding |

## Development

```bash
make check                              # lint + full test suite
make test                               # pytest, single-shot
make lint                               # flake8
make format                             # black
make ui                                 # rebuild the web interface
make dist                               # wheel, interface included
cd frontend && npx vitest run           # frontend tests
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
