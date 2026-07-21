"""Smoke tests verifying the project scaffold is importable and wired up.

These cover Requirement 20.1 (self-contained application layout) and 20.3
(the package is present to host the Kiro CLI LLM provider). Functional
behavior is covered by later tasks; this file only guards the skeleton.
"""

import importlib

import pytest

SUBPACKAGES = [
    "icplugin_builder",
    "icplugin_builder.core",
    "icplugin_builder.persistence",
    "icplugin_builder.integrations",
    "icplugin_builder.orchestrator",
    "icplugin_builder.api",
]


@pytest.mark.parametrize("module_name", SUBPACKAGES)
def test_subpackage_imports(module_name):
    """Every declared subpackage imports cleanly."""
    module = importlib.import_module(module_name)
    assert module is not None


def test_package_exposes_version():
    """The top-level package advertises a version string."""
    import icplugin_builder

    assert isinstance(icplugin_builder.__version__, str)
    assert icplugin_builder.__version__
