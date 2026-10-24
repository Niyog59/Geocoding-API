# Common tasks. Run `make setup` once after cloning.
.PHONY: setup redis fake api frontend test test-browser lint bench lock

setup:
	python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
	cd frontend && npm ci

redis:              ## local Redis with an append-only file in ./.redis-data
	mkdir -p .redis-data && redis-server --appendonly yes --appendfsync everysec --dir .redis-data

fake:               ## fake provider on :9100 (no provider key or credits needed)
	.venv/bin/uvicorn tools.fake_upstream:app --port 9100

api:                ## API on :8000 using .env
	.venv/bin/uvicorn app.main:create_app --factory --reload --port 8000 --no-access-log

frontend:           ## playground dev server on :5173
	cd frontend && npm run dev

test:               ## unit and Redis integration tests (starts its own Redis)
	.venv/bin/pytest

test-browser:       ## Playwright tests (needs Redis on :6379)
	cd frontend && npx playwright test

lint:
	.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
	cd frontend && npm run typecheck

bench:              ## local benchmark, about 11 minutes
	.venv/bin/python -m bench.run_benchmark --duration 300 --rate 10

lock:               ## regenerate hash-locked requirements
	uv pip compile requirements.txt --generate-hashes --python-version 3.13 -o requirements.lock
