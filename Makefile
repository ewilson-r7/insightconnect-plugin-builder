# InsightConnect Plugin Builder - developer task runner.
#
# Usage:
#   make install   # install the package plus dev dependencies (editable)
#   make test      # single-shot test run (pytest runs once and exits)
#   make lint      # flake8 static checks
#   make format    # black auto-format
#   make check     # lint + test

PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip

.DEFAULT_GOAL := help

.PHONY: help install test lint format check clean

help:
	@echo "Targets:"
	@echo "  install  Install the package and dev dependencies (editable)"
	@echo "  test     Run the full test suite once and exit (single-shot)"
	@echo "  lint     Run flake8 static analysis"
	@echo "  format   Auto-format the codebase with black"
	@echo "  check    Run lint then test"
	@echo "  clean    Remove caches and build artifacts"

install:
	$(PIP) install -e ".[dev]"

# Single-shot test execution: pytest runs the suite once and exits (no watch mode).
test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m flake8 icplugin_builder tests

format:
	$(PYTHON) -m black icplugin_builder tests

check: lint test

clean:
	rm -rf .pytest_cache .hypothesis .mypy_cache .ruff_cache *.egg-info build dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
