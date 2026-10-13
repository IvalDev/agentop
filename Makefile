.PHONY: install lint format test run migrate migrate-revision bootstrap demo dev chat

# Development settings file read by `make demo` and `make dev` when it exists: copy one
# of deployment/dev/*.env.example to .env (git-ignored). Variables already set in the
# shell win over the file. See docs/dev-setup.md.
ENV_FILE ?= .env
ENV_FILE_FLAG = $(if $(wildcard $(ENV_FILE)),--env-file $(ENV_FILE))

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

demo:
	python -m app.demo.seed --create-schema $(ENV_FILE_FLAG)

dev:
	uvicorn app.main:app --reload $(ENV_FILE_FLAG)

chat:
	python -m app.demo.chat --user $(or $(user),owner)
