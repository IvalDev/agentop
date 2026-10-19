.PHONY: install lint format test run migrate migrate-revision bootstrap demo dev chat \
	app web-build web-dev web-test widget-dev widget-build widget-package

# Development settings file read by `make demo` and `make dev` when it exists: copy one
# of deployment/dev/*.env.example to .env (git-ignored). Variables already set in the
# shell win over the file. See docs/dev-setup.md.
ENV_FILE ?= .env
ENV_FILE_FLAG = $(if $(wildcard $(ENV_FILE)),--env-file $(ENV_FILE))

# Python tools run from .venv when it exists (no shell activation needed); otherwise
# fall back to whatever is on PATH (e.g. a system-wide `pip install -e ".[dev]"`).
VENV ?= .venv
ifneq ($(wildcard $(VENV)/bin),)
PY = $(VENV)/bin/python
UVICORN = $(VENV)/bin/uvicorn
RUFF = $(VENV)/bin/ruff
PYTEST = $(VENV)/bin/pytest
ALEMBIC = $(VENV)/bin/alembic
else
PY = python
UVICORN = uvicorn
RUFF = ruff
PYTEST = pytest
ALEMBIC = alembic
endif

# React web app (web/): Node 22.22+ and npm. FastAPI serves web/dist at / once built.
WEB_DIR = web
# npm writes this file on install: (re)install whenever package-lock.json is newer.
WEB_DEPS = $(WEB_DIR)/node_modules/.package-lock.json

# Desktop widget (desktop/): Tauri 2 shell around the /widget route. Cargo lives in
# $(HOME)/.cargo/bin, which is not on the PATH of non-login shells, so the recipes
# load cargo's env file first.
WIDGET_DIR = desktop
WIDGET_DEPS = $(WIDGET_DIR)/node_modules/.package-lock.json

install:
	$(PY) -m pip install -e ".[dev]"

lint:
	$(RUFF) check . && $(RUFF) format --check .

format:
	$(RUFF) format . && $(RUFF) check --fix .

test:
	$(PYTEST)

run:
	$(UVICORN) app.main:app --reload

migrate:
	$(ALEMBIC) upgrade head

migrate-revision:
	$(ALEMBIC) revision --autogenerate -m "$(m)"

bootstrap:
	$(PY) -m app.modules.identity.seed

demo:
	$(PY) -m app.demo.seed --create-schema $(ENV_FILE_FLAG)

dev:
	$(UVICORN) app.main:app --reload $(ENV_FILE_FLAG)

chat:
	$(PY) -m app.demo.chat --user $(or $(user),owner)

$(WEB_DEPS): $(WEB_DIR)/package-lock.json
	npm --prefix $(WEB_DIR) ci

# Build the web app, then serve it and the API on localhost:8000 (like `make dev`).
app: web-build
	$(UVICORN) app.main:app --reload $(ENV_FILE_FLAG)

web-build: $(WEB_DEPS)
	npm --prefix $(WEB_DIR) run build

# Vite dev server with hot reload on localhost:5173; /api goes to `make dev` on :8000.
web-dev: $(WEB_DEPS)
	npm --prefix $(WEB_DIR) run dev

web-test: $(WEB_DEPS)
	npm --prefix $(WEB_DIR) run typecheck
	npm --prefix $(WEB_DIR) run lint
	npm --prefix $(WEB_DIR) test -- --run

$(WIDGET_DEPS): $(WIDGET_DIR)/package-lock.json
	npm --prefix $(WIDGET_DIR) ci

# Run the widget window against the configured server (W6: AA_SERVER_URL > XDG
# config file > http://127.0.0.1:8000); it loads {server}/widget.
widget-dev: $(WIDGET_DEPS)
	. "$$HOME/.cargo/env" && npm --prefix $(WIDGET_DIR) run tauri dev

# Build the widget installers (deb/AppImage as configured in src-tauri/tauri.conf.json).
widget-build: $(WIDGET_DEPS)
	. "$$HOME/.cargo/env" && npm --prefix $(WIDGET_DIR) run tauri build

# Build only the Linux release bundles (.deb + AppImage); they land in
# desktop/src-tauri/target/release/bundle/{deb,appimage}/. See docs/desktop-widget.md.
widget-package: $(WIDGET_DEPS)
	. "$$HOME/.cargo/env" && npm --prefix $(WIDGET_DIR) run tauri build -- --bundles deb,appimage
