.PHONY: check lint format test test-fast viewer

check:
	ruff check .
	ruff format --check .
	pytest -q -n auto

lint:
	ruff check .

format:
	ruff format .

test:
	pytest -q -n auto

test-fast:
	@test -n "$(PATHS)" || { echo 'Usage: make test-fast PATHS="tests/test_a.py tests/test_b.py"' >&2; exit 2; }
	pytest -q -n auto $(PATHS)

viewer:
	cd viewer-app && npm ci && npm run build && npm test
