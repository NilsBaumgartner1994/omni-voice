# Kurzbefehle rund um den OmniVoice-Container.
COMPOSE ?= docker compose
GPU_FILES = -f docker-compose.yml -f docker-compose.gpu.yml

.DEFAULT_GOAL := help

.PHONY: help build up up-gpu down logs restart shell prefetch gradio smoke test dev lint clean-models

help: ## Diese Hilfe anzeigen
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

build: ## Image bauen (CPU)
	$(COMPOSE) build

up: ## Container starten und auf http://localhost:7860 bereitstellen
	$(COMPOSE) up -d --build
	@echo "-> http://localhost:$${OMNIVOICE_PORT:-7860}  (erster Start laedt die Modelle, siehe 'make logs')"

up-gpu: ## Container mit NVIDIA-GPU starten
	$(COMPOSE) $(GPU_FILES) up -d --build

down: ## Container stoppen (Modell-Cache bleibt erhalten)
	$(COMPOSE) down

logs: ## Logs verfolgen
	$(COMPOSE) logs -f

restart: ## Container neu starten
	$(COMPOSE) restart

shell: ## Bash im Container
	$(COMPOSE) run --rm omnivoice shell

prefetch: ## Modellgewichte vorab herunterladen (kein HF-Token noetig)
	$(COMPOSE) run --rm omnivoice prefetch

gradio: ## Originale Gradio-Oberflaeche starten (vorher 'make down')
	$(COMPOSE) run --rm --service-ports omnivoice gradio

smoke: ## Container ohne Modell testen (Dummy-Engine, ~1 Minute)
	OMNIVOICE_ENGINE=dummy $(COMPOSE) up -d --build
	@bash scripts/smoke_test.sh
	$(COMPOSE) down

test: ## Python-Tests lokal ausfuehren (ohne Docker, ohne Modell)
	python -m pytest tests -q

dev: ## Server lokal ohne Docker starten (Dummy-Engine)
	OMNIVOICE_ENGINE=dummy python -m omnivoice_server --port 7860

clean-models: ## Modell-Cache-Volume loeschen (erzwingt Neu-Download)
	$(COMPOSE) down -v
