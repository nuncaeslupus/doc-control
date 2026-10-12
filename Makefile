.PHONY: sync build lint format test check bench clean

sync:
	uv sync --locked

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

# CI (.github/workflows/ci.yml) spells out these same steps; keep both in sync.
check: lint test
	bash scripts/hygiene.sh

bench:  # exit 5 = no benchmarks collected yet, which is not a failure
	uv run pytest -q -m bench || [ $$? -eq 5 ]

clean:
	rm -rf dist build .mypy_cache .pytest_cache .ruff_cache .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
