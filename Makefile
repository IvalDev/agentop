.PHONY: install lint format test run migrate migrate-revision bootstrap

install:
	pip install -e ".[dev]"

lint:
	ruff check . && ruff format --check .

format:
	ruff format . && ruff check --fix .

test:
	pytest

run:
	uvicorn app.main:app --reload

migrate:
	alembic upgrade head

migrate-revision:
	alembic revision --autogenerate -m "$(m)"

bootstrap:
	python -m app.modules.identity.seed
