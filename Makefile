.PHONY: check lint format test

check:
	ruff check .
	ruff format --check .
	pytest -q

lint:
	ruff check .

format:
	ruff format .

test:
	pytest -q
