# InsightConnect Plugin Builder - developer task runner.
#
# Usage:
#   make setup     # everything a first-time user needs: deps + web interface
#   make install   # install the package plus dev dependencies (editable)
#   make ui        # build the web interface and stage it into the package
#   make test      # single-shot test run (pytest runs once and exits)
#   make lint      # flake8 static checks
#   make format    # black auto-format
#   make check     # lint + test
#   make dist      # build a wheel carrying the web interface

PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip
NPM    ?= npm

#: Where the built interface is staged so it ships inside the wheel. Kept in step
#: with `[tool.setuptools.package-data]` in pyproject.toml and with the resolution
#: order in `_default_ui_dir`.
UI_DIR := icplugin_builder/ui

.DEFAULT_GOAL := help

.PHONY: help setup install ui test lint format check dist clean

help:
	@echo "Targets:"
	@echo "  setup    First-time setup: dependencies and the web interface"
	@echo "  install  Install the package and dev dependencies (editable)"
	@echo "  ui       Build the web interface and stage it into the package"
	@echo "  test     Run the full test suite once and exit (single-shot)"
	@echo "  lint     Run flake8 static analysis"
	@echo "  format   Auto-format the codebase with black"
	@echo "  check    Run lint then test"
	@echo "  dist     Build a wheel with the web interface included"
	@echo "  clean    Remove caches and build artifacts"

# One target for a first-time user. Installing the package alone leaves the
# interface unbuilt, and the server then serves the API with nothing at "/" --
# which looks like a broken install rather than a missing step.
setup: install ui
	@echo
	@echo "Setup complete. Start the tool with:  icplugin-builder"

install:
	$(PIP) install -e ".[dev]"

ui:
	cd frontend && $(NPM) ci && $(NPM) run build
	rm -rf $(UI_DIR)
	cp -R frontend/dist $(UI_DIR)
	@echo "staged the web interface into $(UI_DIR)"

# Single-shot test execution: pytest runs the suite once and exits (no watch mode).
test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m flake8 icplugin_builder tests

format:
	$(PYTHON) -m black icplugin_builder tests

check: lint test

# Depends on `ui` so a wheel cannot be built without the interface in it.
dist: ui
	$(PIP) wheel . --no-deps -w dist

clean:
	rm -rf .pytest_cache .hypothesis .mypy_cache .ruff_cache *.egg-info build dist $(UI_DIR)
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
