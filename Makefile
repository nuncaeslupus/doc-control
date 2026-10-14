.PHONY: sync build lint format test check bench clean hero

sync:
	uv sync

build:
	uv build

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest -q

check: lint test
	bash scripts/hygiene.sh

bench:
	uv run pytest -q -m bench

clean:
	rm -rf dist build .mypy_cache .pytest_cache .ruff_cache .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +

hero:  ## redraw the README banner, docs/hero.svg and docs/hero.png (needs Chromium + Pillow)
	uv run --with pillow python tools/hero.py
