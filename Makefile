SHELL := /bin/bash

POETRY ?= poetry
BACKEND_HOST ?= 0.0.0.0
BACKEND_PORT ?= 3000
PRE_COMMIT_CONFIG_PATH ?= dev_config/python/.pre-commit-config.yaml

.DEFAULT_GOAL := help

install:
	$(POETRY) install --with dev,test

install-pre-commit-hooks: install
	$(POETRY) run pre-commit install --config $(PRE_COMMIT_CONFIG_PATH)

start:
	SERVE_FRONTEND=false $(POETRY) run uvicorn openhands.app_server.app:app \
		--host $(BACKEND_HOST) --port $(BACKEND_PORT) --reload

start-backend: start

lint:
	$(POETRY) run pre-commit run --all-files --show-diff-on-failure \
		--config $(PRE_COMMIT_CONFIG_PATH)

test:
	$(POETRY) run pytest tests/unit/app_server

docker-build:
	docker build -f containers/sandbox/Dockerfile \
		-t ghcr.io/openhands/agent-server:1.37.1-python-p4-r25.2-p4mcp-2026.4.3126709 .
	docker build -f containers/app/Dockerfile -t openhands-sandbox-server:latest .

docker-run:
	docker compose up --build

help:
	@echo "make install                  Install application, development, and test dependencies"
	@echo "make install-pre-commit-hooks Install repository hooks"
	@echo "make start                    Start the standalone API server"
	@echo "make lint                     Run all pre-commit checks"
	@echo "make test                     Run the app-server unit tests"
	@echo "make docker-build             Build the standalone container"
	@echo "make docker-run               Start the Compose stack"

.PHONY: install install-pre-commit-hooks start start-backend lint test docker-build docker-run help
