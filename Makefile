.PHONY: check lint format test viewer

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

viewer:
	cd viewer-app && npm ci && npm run build && npm test
