.PHONY: install db-up db-down migrate upgrade downgrade test run-api ingest-federal ingest-census ingest-sec ingest-news

install:
	uv sync || pip install -e ".[dev]"

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

migrate:
	alembic revision --autogenerate -m "$(msg)"

upgrade:
	alembic upgrade head

downgrade:
	alembic downgrade -1

test:
	pytest tests/ -v

run-api:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

ingest-federal:
	python -m app.cli ingest federal-register

ingest-census:
	python -m app.cli ingest census-trade

ingest-sec:
	python -m app.cli ingest sec-edgar

ingest-news:
	python -m app.cli ingest news

seed:
	python -m app.cli seed
