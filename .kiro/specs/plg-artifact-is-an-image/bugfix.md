# Bugfix: the exported `.plg` is not an importable artifact

## Summary

Every `.plg` this tool has produced is a gzipped tarball of the plugin's **source
tree**. An importable `.plg` is a gzipped tarball of the plugin's **built container
image**. InsightConnect loads the image; the plugin's code and `plugin.spec.yaml`
live inside its layers.

So the export path has never produced an artifact a tenant can accept. Reported
2026-08-26 after an iterate-and-export run on the JumpCloud plugin: the tool's
export was rejected on import, and `insight-plugin refresh` followed by
`insight-plugin export` in the same directory produced one that imported cleanly.

## 1. Findings

### 1.1 The two artifacts are different kinds of thing

Measured on the same plugin at the same version, minutes apart.

| | Tool's export | `insight-plugin export` |
|---|---|---|
| Path | `.builder/artifacts/jumpcloud-1.0.1.plg` | `rapid7_custom_jumpcloud_1.0.1.plg` |
| Size | 12,637 bytes | 81,305,076 bytes |
| Top-level members | `.CHECKSUM`, `Dockerfile`, `Makefile`, `bin/`, `icon_jumpcloud/`, `help.md`, `icon.png` … 37 files | `oci-layout`, `index.json`, `manifest.json`, `blobs/` — 26 entries |
| Kind | source tree | OCI image archive, 13 layers |
| Imports into InsightConnect | **no** | **yes** |

The importable archive's `manifest.json` carries:

```json
"RepoTags": ["rapid7_custom/jumpcloud:1.0.1"]
```

and `index.json` annotates `io.containerd.image.name:
docker.io/rapid7_custom/jumpcloud:1.0.1`. The identity is
`<vendor>/<name>:<version>`, with the `_custom`-suffixed vendor (Req 11).

### 1.2 The filename convention also differs

`insight-plugin export` writes `<vendor>_<name>_<version>.plg` —
`rapid7_custom_jumpcloud_1.0.1.plg`. The tool writes `<name>-<version>.plg`. Whether
a tenant depends on the filename is unverified, but there is no reason to differ from
the toolchain.

### 1.3 The tool builds an image, but tags it for its own use only

The `build` stage produces `icplugin-validate/<name>:latest`. That tag exists so the
validate stage has something to run; it is not the plugin's published identity. A
`docker save` of it would carry the wrong `RepoTags`, so **the fix is not simply
"save the image we already built"** — the image has to be tagged
`<vendor>/<name>:<version>` first.

### 1.4 The premise is in the specification, not only the code

Requirement 9.2 reads:

> THE Build_Engine SHALL produce a PLG_Artifact that is a gzipped tarball containing
> the built plugin.

A `docker save | gzip` output *is* a gzipped tarball, so the clause is not false —
but "containing the built plugin" is ambiguous, and `build_engine.py` resolved it as
"containing the plugin's files". Its module docstring states the reading plainly:
"a plugin be packaged into a single ``.plg`` file that is a **gzipped tarball**". Every
downstream decision followed consistently, which is why nothing flagged it.

Requirement 16.2 inherits the same mistake: the export preview lists "the exact files
that would be included in the `.plg`". For an image archive that list is not
meaningful in the same way.

### 1.5 `insight-plugin export` mis-detects failure when `docker build` writes to stdout

Found while verifying the fix, and it constrains the design.

`insight_plugin/features/common/command_line_util.py`:

```python
if child_process.returncode == 0:
    return str(child_process.stdout)     # returns STDOUT on success
```

`insight_plugin/features/common/builder.py`:

```python
err = CommandLineUtil.run_command(cmd, args)
if err:                                   # any non-empty return is treated as failure
    raise InsightException(message="Docker build command failed", ...)
```

So a **successful** `docker build` whose stdout is non-empty is reported as a build
failure. Reproduced: the build logged `Successfully built 6058ad25094c` and
`Successfully tagged rapid7_custom/jumpcloud:1.0.1`, and `insight-plugin` then raised
`Docker build command failed`.

Whether stdout is empty depends on the builder:

- **BuildKit** writes progress to stderr, leaving stdout empty → the check passes.
- **the legacy builder** writes progress to stdout → the check fails.

In this environment BuildKit is unavailable (`BuildKit is enabled but the buildx
component is missing or broken`), so the legacy builder is used and `insight-plugin
export` cannot succeed. The operator's own terminal has working buildx, which is why
the same command worked for them minutes earlier.

**This makes `insight-plugin export` environment-dependent in a way that has nothing
to do with the plugin.** A design that calls it must confront that.

### 1.6 An existing `.plg` in the directory is *not* what breaks the export

Reported as a constraint, and worth recording because it is a plausible-looking
cause that the evidence rules out: the export fails identically with **no** `.plg`
present. The cause is 1.5. Any requirement to clear a stale artifact first should
rest on artifact hygiene — a stale `.plg` from a previous version sitting in the
plugin directory is untidy and gets copied into the build context — not on a build
failure it does not cause.

### 1.7 `docker save` produces an equivalent artifact

`docker save rapid7_custom/jumpcloud:1.0.1` piped through gzip: 81,302,505 bytes
against the SDK's 81,305,076, the same four top-level members and the same
`RepoTags`. Both routes reach the same artifact; they differ only in robustness.

### 1.8 A correction to an earlier check of mine

On 2026-08-17 I ran a leak check over `jumpcloud-1.0.0.plg` and recorded it as
**PASS**: 39 entries, no `.builder/`, no vendor swagger, no provenance. That check was
accurate and worthless. It examined the contents of the artifact scrupulously and
never asked whether a source tarball was the right kind of artifact at all. The
absence of a test that *imports* what we produce is the actual gap, and no amount of
inspecting the wrong archive would have closed it.

## 2. Decisions

### 2.1 The `.plg` is an image archive

**Decision.** A `PLG_Artifact` is a gzipped `docker save` of the plugin image tagged
`<vendor>/<name>:<version>`, where `<vendor>` carries the `_custom` suffix. The
artifact is named `<vendor>_<name>_<version>.plg`.

Requirement 9.2 is amended to say so explicitly rather than leaving "containing the
built plugin" to interpretation.

### 2.2 How the image is produced and saved — **open, needs a decision**

Two routes reach the identical artifact (1.7). They are not equally safe.

**Route A — call `insight-plugin export`.** Matches this project's stated philosophy
of wrapping the real toolchain rather than reimplementing it, and
`InsightPluginCli` already has `create()` and `refresh()` with an obvious gap where
`export()` belongs. **But** it inherits 1.5: it fails wherever `docker build` writes
to stdout, which is any host without working buildx. The tool would have to force
BuildKit in the child environment and would still be broken on hosts that cannot
provide it — while reporting "Docker build command failed" about a plugin that built
perfectly.

**Route B — `docker build` + `docker tag` + `docker save` in the tool.** Depends only
on Docker, which the build stage already drives, so it works under either builder. It
duplicates a few lines of what the SDK does, against the "wrap the toolchain"
convention.

The operator chose Route A before 1.5 was known. Recording it as open because the
finding changes the tradeoff materially: Route A's failure mode is silent, misleading,
and environmental.

### 2.3 The export preview describes an image, not a file list

Requirement 16.2's file list is not meaningful for an image archive. The preview
should name what the operator is about to get: the image tag, the version, the
artifact filename, and the size. What goes *into* the image is governed by the
plugin's `.dockerignore`, not by our packaging filters.

Amendment required to Requirement 16.2, and Property 69 — which asserts the packaged
member set *equals* the plugin's file set — no longer states anything true.

### 2.4 A stale `.plg` in the plugin directory is removed before export

Not because it breaks the build (1.6 rules that out) but because it is a build-context
artifact: `docker build` sends the whole directory as context, so an 81 MB archive
from the previous version gets uploaded to the daemon for no reason, and two `.plg`
files in one directory is ambiguous for anyone looking.

### 2.5 The export path is verified against a real import shape

The defect survived because no test asserted anything about the artifact's *kind*. A
test must assert the produced archive is an image archive — `oci-layout` present,
`manifest.json` carrying the expected `RepoTags` — not merely that it is a tarball
with the right members.

## 3. What this invalidates

Recorded plainly, because it is a meaningful amount of recent work.

- **Property 69** (packaged set equals plugin files) — tests the wrong claim entirely.
- **Wave 14's byproduct exclusions** (`.coverage`, `.pyc`, `build/`, `.egg-info`) —
  irrelevant to an image archive, whose contents come from `.dockerignore`. The
  predicates in `core/plugin_files.py` keep their other callers.
- **`BuildEngine.package`, `list_plugin_files`, `preview_export_files`** — the first
  changes meaning; the latter two lose their purpose on the export path.
- **Requirement 9.2 and 16.2** — amended.
- The `.plg` leak check of 2026-08-17 — superseded; the equivalent question for an
  image is what the `.dockerignore` admits into the layers.

## 4. Environment

- `insight-plugin` 1.9.20.
- Docker 29.7.2. **buildx unavailable in the tool's shell**, so BuildKit cannot be
  used there; the operator's interactive terminal has it.
- `~/.docker/config.json` is unreadable from the tool's shell
  (`operation not permitted`), which is what disables buildx.
- The image `rapid7_custom/jumpcloud:1.0.1` exists locally, built by the operator's
  successful 08:02 export.
- Reference artifacts kept for comparison: `/tmp/icpb_backup_working.plg` (the SDK's
  importable archive) and `/tmp/icpb_direct_save.tar.gz` (the `docker save`
  equivalent).
