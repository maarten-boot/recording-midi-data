# midi-recorder: run and check everything inside a virtualenv.
#   make            -> show targets
#   make all        -> clean rebuild of the venv, then format, lint, typecheck and test
#   make check      -> ruff check + ruff format --check + mypy --strict + pytest
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
.PHONY: help all venv lint format format-check typecheck test check run run-gui clean distclean

help:
	@echo "all           distclean + venv + lint + format + format-check + typecheck + test"
	@echo "venv          create the virtualenv and install dependencies"
	@echo "lint          ruff check"
	@echo "format        ruff format (rewrites files)"
	@echo "format-check  ruff format --check"
	@echo "typecheck     mypy --strict"
	@echo "test          pytest"
	@echo "check         lint + format-check + typecheck + test"
	@echo "run           run the recorder (ARGS=\"...\" passes options)"
	@echo "run-gui       run the status window (needs tkinter; ARGS=\"...\" passes options)"
	@echo "clean         remove caches"
	@echo "distclean     clean + remove the virtualenv"

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

run: $(STAMP)
	$(VBIN)/python midi_recorder.py $(ARGS)

run-gui: $(STAMP)
	$(VBIN)/python midi_recorder_gui.py $(ARGS)

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache build *.egg-info
	find . -name __pycache__ -not -path "./$(VENV)/*" -prune -exec rm -rf {} +

distclean: clean
	rm -rf $(VENV)
