.PHONY: install run test check eval

install:
	uv sync

run:
	uv run python -m reviewflow

test:
	uv run pytest

check:
	uv run ruff check .
	uv run mypy reviewflow
	uv run pytest -q

eval:
	uv run python -m evals.run --seeds 3 --out evals/results.md
