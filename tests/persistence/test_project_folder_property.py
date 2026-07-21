"""Property-based test for Project_Folder save/list fidelity (task 11.2).

The unit tests in ``test_project_folder.py`` pin specific save/list examples;
this module covers design Property 38 with Hypothesis. Across arbitrary plugin
drafts saved to their ``Project_Folder`` -- each with its own spec, generated
code tree, documentation, other generated files, and build artifacts -- the
stored content must match exactly what was saved (round-trip fidelity), and
``list_projects`` must return each plugin with its name, current version, and
last-modification timestamp (Req 21.1, 21.2, 21.4).
"""

from datetime import datetime, timezone
from typing import Dict, List, NamedTuple

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.spec_model import PluginSpec
from icplugin_builder.core.yaml_codec import load_plugin_spec
from icplugin_builder.persistence.project_folder import (
    DEFAULT_PACKAGE_PREFIX,
    ProjectFolder,
    ProjectListing,
    list_projects,
)
from tests.strategies import plugin_specs, snake_case_names


class PluginPayload(NamedTuple):
    """Everything saved for one plugin, plus the expected listing timestamp."""

    plugin_name: str
    spec: PluginSpec
    help_md: str
    generated_files: Dict[str, str]
    package_files: Dict[str, str]
    artifacts: Dict[str, bytes]
    modified_utc: datetime


def _round_trip_text(max_size: int = 40) -> st.SearchStrategy[str]:
    """Generate printable-ASCII text that survives text-mode file round trips.

    Restricted to codepoints 32-126 (no newlines or control characters) so that
    ``Path.write_text``/``read_text`` reproduce the value exactly regardless of
    platform newline translation.
    """
    return st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=0, max_size=max_size)


def _utc_datetimes() -> st.SearchStrategy[datetime]:
    """Generate timezone-aware UTC datetimes across a wide, comparable range."""
    return st.datetimes(
        min_value=datetime(2000, 1, 1),
        max_value=datetime(2035, 12, 31),
        timezones=st.just(timezone.utc),
    )


@st.composite
def plugin_payloads(draw: st.DrawFn) -> List[PluginPayload]:
    """Generate a batch of distinct plugin drafts to save into one projects root.

    Plugin names (the on-disk folder names) are unique so each is an independent
    listing row. Each draft carries its own generated file names are prefixed and
    suffixed so they never collide with the reserved ``plugin.spec.yaml``,
    ``help.md``, package-tree, or ``.builder`` paths. Artifacts use binary content
    to exercise byte-exact storage; docs/code use printable text for exact
    text-mode round trips.
    """
    names = draw(st.lists(snake_case_names(), min_size=1, max_size=4, unique=True))

    payloads: List[PluginPayload] = []
    for name in names:
        generated_names = draw(st.lists(snake_case_names(), max_size=3, unique=True))
        package_names = draw(st.lists(snake_case_names(), max_size=3, unique=True))
        artifact_names = draw(st.lists(snake_case_names(), max_size=3, unique=True))

        payloads.append(
            PluginPayload(
                plugin_name=name,
                spec=draw(plugin_specs()),
                help_md=draw(_round_trip_text(max_size=60)),
                generated_files={f"gen_{gen}.txt": draw(_round_trip_text()) for gen in generated_names},
                package_files={f"{pkg}.py": draw(_round_trip_text()) for pkg in package_names},
                artifacts={f"{art}.plg": draw(st.binary(max_size=32)) for art in artifact_names},
                modified_utc=draw(_utc_datetimes()),
            )
        )
    return payloads


# Feature: insightconnect-plugin-builder, Property 38: Project-folder save/list fidelity
@settings(max_examples=100)
@given(payloads=plugin_payloads())
def test_save_and_list_fidelity(payloads, tmp_path_factory):
    """Stored spec/code/docs/artifacts match the draft; listing is faithful.

    For each generated plugin draft, creates its ``Project_Folder``, saves the
    current draft together with its package tree, documentation, other generated
    files, and build artifacts, then asserts every stored piece matches what was
    saved. Finally asserts ``list_projects`` returns exactly one row per plugin
    carrying its name, current version, and last-modification timestamp.

    **Validates: Requirements 21.1, 21.2, 21.4**
    """
    projects_root = tmp_path_factory.mktemp("projects")

    expected_listings: Dict[str, ProjectListing] = {}
    for payload in payloads:
        # Materialize the package source tree on disk for this plugin.
        package_source = tmp_path_factory.mktemp("pkg")
        for relative_path, content in payload.package_files.items():
            (package_source / relative_path).write_text(content, encoding="utf-8")

        folder = ProjectFolder.create(projects_root, payload.plugin_name, payload.spec)
        metadata = folder.save(
            payload.spec,
            package_source=package_source if payload.package_files else None,
            help_md=payload.help_md,
            generated_files=payload.generated_files or None,
            artifacts=payload.artifacts or None,
            modified_utc=payload.modified_utc,
        )

        # Req 21.2: the stored spec is a byte-for-model round trip of the draft.
        stored_spec = load_plugin_spec(folder.spec_path.read_text(encoding="utf-8"))
        assert stored_spec == payload.spec

        # Req 21.2: the stored code tree matches the saved package source.
        for relative_path, content in payload.package_files.items():
            assert (folder.package_dir() / relative_path).read_text(encoding="utf-8") == content

        # Req 21.2: the stored documentation matches what was saved.
        assert (folder.path / "help.md").read_text(encoding="utf-8") == payload.help_md

        # Req 21.2: other generated files match what was saved.
        for relative_path, content in payload.generated_files.items():
            assert (folder.path / relative_path).read_text(encoding="utf-8") == content

        # Req 21.2: build artifacts are stored byte-for-byte.
        for filename, content in payload.artifacts.items():
            assert (folder.path / ".builder" / "artifacts" / filename).read_bytes() == content

        # The saved metadata reflects the draft version and modification stamp.
        expected_version = str(payload.spec.version)
        expected_modified = payload.modified_utc.isoformat()
        assert metadata.current_version == expected_version
        assert metadata.last_modified_utc == expected_modified
        assert folder.package_dir() == folder.path / f"{DEFAULT_PACKAGE_PREFIX}_{payload.plugin_name}"

        expected_listings[payload.plugin_name] = ProjectListing(
            plugin_name=payload.plugin_name,
            current_version=expected_version,
            last_modified_utc=expected_modified,
        )

    # Req 21.4: listing returns each plugin with name, current version, last-modified.
    listings = list_projects(projects_root)
    assert {listing.plugin_name for listing in listings} == set(expected_listings)
    for listing in listings:
        assert listing == expected_listings[listing.plugin_name]
