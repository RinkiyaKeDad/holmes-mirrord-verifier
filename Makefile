.PHONY: install clean

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Use uv if it's on PATH (faster). Otherwise fall back to venv + pip.
UV := $(shell command -v uv 2>/dev/null)

# Local venv is only needed to run the bridge step (the Claude call that turns a
# HolmesGPT report into a patch). The verifier itself runs in-cluster — see the
# README for the cluster walkthrough.
install:
ifdef UV
	uv sync
else
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e .
endif

clean:
	rm -rf $(VENV) src/verifier/__pycache__ src/verifier.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
