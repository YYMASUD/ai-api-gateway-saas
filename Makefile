SHELL := /bin/bash
COMPOSE ?= docker compose

.DEFAULT_GOAL := help

.PHONY: help dev-up dev-down logs ps test test-api test-web test-worker lint lint-api lint-web lint-worker typecheck typecheck-api typecheck-web typecheck-worker migrate build compose-config

help:
	@echo "ai-api-gateway-saas local targets"
	@echo "  make dev-up          Build and start the compose stack"
	@echo "  make dev-down        Stop the compose stack (volumes kept)"
	@echo "  make logs            Follow logs from all services"
	@echo "  make ps              Show service status"
	@echo "  make migrate         Apply API database migrations"
	@echo "  make test            Run API, web, and worker tests"
	@echo "  make lint            Lint all applications"
	@echo "  make typecheck       Typecheck all applications"
	@echo "  make build           Build service images"
	@echo "  make compose-config  Validate docker-compose.yml"

dev-up:
	@test -f .env || { echo "Missing .env. Run: cp .env.example .env and fill values."; exit 1; }
	$(COMPOSE) up --build -d

dev-down:
	$(COMPOSE) down --remove-orphans

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

test: test-api test-web test-worker

test-api:
	cd apps/api && python -m pytest

test-web:
	cd apps/web && npm test

test-worker:
	cd apps/worker && python -m pytest

lint: lint-api lint-web lint-worker

lint-api:
	cd apps/api && ruff check .

lint-web:
	cd apps/web && npm run lint

lint-worker:
	cd apps/worker && ruff check .

typecheck: typecheck-api typecheck-web typecheck-worker

typecheck-api:
	cd apps/api && mypy app

typecheck-web:
	cd apps/web && npm run typecheck

typecheck-worker:
	cd apps/worker && mypy app

migrate:
	$(COMPOSE) run --rm api alembic upgrade head

build:
	$(COMPOSE) build

compose-config:
	$(COMPOSE) config --quiet
