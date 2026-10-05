# midi-recorder: run and check everything inside a virtualenv.
#   make            -> show targets
#   make all        -> clean rebuild of the venv, then format, lint, typecheck and test
#   make check      -> ruff check + ruff format --check + mypy --strict + pytest
#   make build      -> check, then build sdist + wheel into dist/ and run twine check
#   make testpypi   -> build, then upload dist/* to TestPyPI (repository section mboot_testpypi in ~/.pypirc)
#   make pypi       -> build, then upload dist/* to PyPI (repository section mboot_pypi in ~/.pypirc)
#   make run ARGS="-o ~/midi --idle 20"

PYTHON ?= python3
VENV   ?= .venv

ifeq ($(OS),Windows_NT)
    VBIN := $(VENV)/Scripts
else
    VBIN := $(VENV)/bin
endif

STAMP := $(VENV)/.installed
PY    := midi_recorder.py midi_recorder_gui.py
SRC   := $(PY) tests stubs

.DEFAULT_GOAL := help
.PHONY: help all venv lint format format-check typecheck test check build run run-gui clean distclean testpypi pypi

help:
	@echo "all           distclean + venv + lint + format + format-check + typecheck + test"
	@echo "venv          create the virtualenv and install dependencies"
	@echo "lint          ruff check"
	@echo "format        ruff format (rewrites files)"
	@echo "format-check  ruff format --check"
	@echo "typecheck     mypy --strict"
	@echo "test          pytest"
	@echo "check         lint + format-check + typecheck + test"
	@echo "build         build sdist + wheel into dist/ and run twine check"
	@echo "run           run the recorder (ARGS=\"...\" passes options)"
	@echo "run-gui       run the status window (needs tkinter; ARGS=\"...\" passes options)"
	@echo "clean         remove caches"
	@echo "distclean     clean + remove the virtualenv"
	@echo "testpypi      check + build + upload dist/* to TestPyPI"
	@echo "pypi          check + build + upload dist/* to PyPI (normally release via GitHub instead)"

all: distclean venv lint format format-check typecheck test

venv: $(STAMP)

$(STAMP): pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VBIN)/python -m pip install --upgrade pip
	$(VBIN)/python -m pip install -e ".[dev]"
	touch $(STAMP)

lint: $(STAMP)
	$(VBIN)/ruff check $(SRC)

format: $(STAMP)
	$(VBIN)/ruff format $(SRC)

format-check: $(STAMP)
	$(VBIN)/ruff format --check $(SRC)

typecheck: $(STAMP)
	$(VBIN)/mypy $(PY) tests

test: $(STAMP)
	$(VBIN)/pytest

check: lint format-check typecheck test

build: $(STAMP) check
	rm -rf dist
	$(VBIN)/python -m build
	$(VBIN)/twine check dist/*

run: $(STAMP)
	$(VBIN)/python midi_recorder.py $(ARGS)

run-gui: $(STAMP)
	$(VBIN)/python midi_recorder_gui.py $(ARGS)

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache build dist *.egg-info
	find . -name __pycache__ -not -path "./$(VENV)/*" -prune -exec rm -rf {} +

distclean: clean
	rm -rf $(VENV)

# ---------------------------------------
# Manual uploads. Credentials come from the repository sections in ~/.pypirc (username __token__, password = API token).
# The normal way to publish is a GitHub release (.github/workflows/release.yml, trusted publishing, no token).
# Every upload needs a new __version__ in midi_recorder.py: neither index accepts the same file twice.
testpypi: build
	$(VBIN)/twine upload \
		--config-file=$${HOME}/.pypirc \
		--repository=mboot_testpypi \
		dist/*

pypi: build
	$(VBIN)/twine upload \
		--repository=mboot_pypi \
		dist/*
