# The demo runs live and unedited, so every step it needs must be one command.
PYTHON ?= python3
export PYTHONPATH := src

.PHONY: install test lint compare demo routes clean

install:
	pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest -q

lint:
	ruff check src tests

compare:
	$(PYTHON) -m fieldpilot.cli compare --seed 42

routes:
	$(PYTHON) -m fieldpilot.cli compare --seed 42 --routes

# The single command the demo video runs on camera.
demo: compare

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
