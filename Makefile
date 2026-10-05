# midi-recorder: run and check everything inside a virtualenv.
#   make            -> show targets
#   make all        -> clean rebuild of the venv, then format, lint, typecheck and test
#   make check      -> ruff check + ruff format --check + mypy --strict + pytest
#   make build      -> check, then build sdist + wheel into dist/ and run twine check
#   make testpypi   -> build, then upload dist/* to TestPyPI (repository section mboot_testpypi in ~/.pypirc)
#   make pypi       -> only from a clean tree whose HEAD is tagged v<version>: build, then upload dist/* to PyPI after you retype the version
#   make run ARGS="-o ~/midi --idle 20"

PYTHON ?= python3
VENV   ?= .venv

ifeq ($(OS),Windows_NT)
    VBIN := $(VENV)/Scripts
else
    VBIN := $(VENV)/bin
endif

STAMP := $(VENV)/.installed
VERSION = $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' midi_recorder.py)
PY    := midi_recorder.py midi_recorder_gui.py
SRC   := $(PY) tests stubs

.DEFAULT_GOAL := help
.PHONY: help all venv lint format format-check typecheck test check build run run-gui clean distclean testpypi pypi pypi-guard

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
	@echo "pypi          clean + tagged commit only; check + build + upload dist/* to PyPI after confirmation"

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

pypi-guard:
	@set -e; \
	version="$(VERSION)"; \
	[ -n "$$version" ] || { echo "error: cannot read __version__ from midi_recorder.py"; exit 1; }; \
	git rev-parse --git-dir >/dev/null 2>&1 || { echo "error: not a git checkout"; exit 1; }; \
	if [ -n "$$(git status --porcelain)" ]; then \
		echo "error: the working tree is not clean, commit or stash first:"; git status --short; exit 1; \
	fi; \
	tags="$$(git tag --points-at HEAD)"; \
	if ! echo "$$tags" | grep -qxF -e "v$$version" -e "$$version"; then \
		echo "error: HEAD is not tagged v$$version (tags at HEAD: $${tags:-none})"; exit 1; \
	fi; \
	echo "ok: clean tree, HEAD is tagged for $$version"

pypi: pypi-guard build
	@echo; echo "About to upload to PyPI, the real index. An upload can never be replaced:"; ls -1 dist
	@printf "Type the version (%s) to upload, anything else aborts: " "$(VERSION)"; \
	read -r answer || answer=""; \
	[ "$$answer" = "$(VERSION)" ] || { echo "aborted, nothing uploaded"; exit 1; }
	$(VBIN)/twine upload \
		--repository=mboot_pypi \
		dist/*
