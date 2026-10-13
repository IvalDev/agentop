.PHONY: install lint format test run

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
