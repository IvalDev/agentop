.PHONY: install lint format test run migrate migrate-revision bootstrap demo dev chat \
	app web-build web-dev web-test

# Development settings file read by `make demo` and `make dev` when it exists: copy one
# of deployment/dev/*.env.example to .env (git-ignored). Variables already set in the
# shell win over the file. See docs/dev-setup.md.
ENV_FILE ?= .env
ENV_FILE_FLAG = $(if $(wildcard $(ENV_FILE)),--env-file $(ENV_FILE))

# React web app (web/): Node 22.22+ and npm. FastAPI serves web/dist at / once built.
WEB_DIR = web
# npm writes this file on install: (re)install whenever package-lock.json is newer.
WEB_DEPS = $(WEB_DIR)/node_modules/.package-lock.json

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

$(WEB_DEPS): $(WEB_DIR)/package-lock.json
	npm --prefix $(WEB_DIR) ci

# Build the web app, then serve it and the API on localhost:8000 (like `make dev`).
app: web-build
	uvicorn app.main:app --reload $(ENV_FILE_FLAG)

web-build: $(WEB_DEPS)
	npm --prefix $(WEB_DIR) run build

# Vite dev server with hot reload on localhost:5173; /api goes to `make dev` on :8000.
web-dev: $(WEB_DEPS)
	npm --prefix $(WEB_DIR) run dev

web-test: $(WEB_DEPS)
	npm --prefix $(WEB_DIR) run typecheck
	npm --prefix $(WEB_DIR) run lint
	npm --prefix $(WEB_DIR) test -- --run
