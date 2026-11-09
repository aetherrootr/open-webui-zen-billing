.PHONY: test check

test:
	PYTHONPATH=plugins:installer uv run --locked python -m unittest discover -s tests -v

check:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
