.PHONY: dev test test-cov lint format migrate migrate-new seed clean install check

dev:
	uvicorn nexafreight.main:app --reload --port 8000 --app-dir src

test:
	pytest tests/ -v --tb=short

test-cov:
	pytest tests/ --cov=src/nexafreight --cov-report=html

lint:
	ruff check src/ tests/
	mypy src/nexafreight/

format:
	ruff format src/ tests/

migrate:
	alembic upgrade head

migrate-new:
	alembic revision --autogenerate -m "$(name)"

seed:
	python scripts/seed_test_user.py
	@echo "Data ingestion scripts (01-08) land later this week — see HANDOFF.md"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete
	rm -f nexafreight.db nexafreight_analytics.duckdb

install:
	pip install -e ".[dev]"

check:
	make lint
	make test
	@echo "All checks passed"
