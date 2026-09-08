# Kurzbefehle rund um den OmniVoice-Container.
COMPOSE ?= docker compose
GPU_FILES = -f docker-compose.yml -f docker-compose.gpu.yml

.DEFAULT_GOAL := help

MODELS_DIR ?= $(if $(OMNIVOICE_MODELS_PATH),$(OMNIVOICE_MODELS_PATH),./models)
DATA_DIR ?= $(if $(OMNIVOICE_DATA_PATH),$(OMNIVOICE_DATA_PATH),./data)

.PHONY: help build up up-gpu down logs restart shell prefetch gradio smoke test dev lint clean-models dirs

help: ## Diese Hilfe anzeigen
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

build: ## Image bauen (CPU)
	$(COMPOSE) build

dirs: ## Host-Ordner fuer Gewichte und Stimmen anlegen
	@mkdir -p "$(MODELS_DIR)" "$(DATA_DIR)"

up: dirs ## Container starten und auf http://localhost:7860 bereitstellen
	$(COMPOSE) up -d --build
	@echo "-> http://localhost:$${OMNIVOICE_PORT:-7860}  (erster Start laedt die Modelle, siehe 'make logs')"

up-gpu: dirs ## Container mit NVIDIA-GPU starten
	$(COMPOSE) $(GPU_FILES) up -d --build

down: ## Container stoppen (Modell-Cache bleibt erhalten)
	$(COMPOSE) down

logs: ## Logs verfolgen
	$(COMPOSE) logs -f

restart: ## Container neu starten
	$(COMPOSE) restart

shell: ## Bash im Container
	$(COMPOSE) run --rm omnivoice shell

prefetch: dirs ## Modellgewichte vorab herunterladen (kein HF-Token noetig)
	$(COMPOSE) run --rm omnivoice prefetch

gradio: ## Originale Gradio-Oberflaeche starten (vorher 'make down')
	$(COMPOSE) run --rm --service-ports omnivoice gradio

smoke: dirs ## Container ohne Modell testen (Dummy-Engine, ~1 Minute)
	OMNIVOICE_ENGINE=dummy $(COMPOSE) up -d --build
	@bash source/scripts/smoke_test.sh
	$(COMPOSE) down

test: ## Python-Tests lokal ausfuehren (ohne Docker, ohne Modell)
	python -m pytest -q

dev: ## Server lokal ohne Docker starten (Dummy-Engine)
	OMNIVOICE_ENGINE=dummy PYTHONPATH=source python -m omnivoice_server --port 7860

clean-models: ## Heruntergeladene Modellgewichte loeschen (erzwingt Neu-Download)
	$(COMPOSE) down
	@# Inhalt loeschen, den Ordner (samt .gitkeep) aber stehen lassen.
	find "$(MODELS_DIR)" -mindepth 1 -maxdepth 1 -not -name .gitkeep -exec rm -rf {} +
	@echo "Stimm-Bibliothek in $(DATA_DIR) ist unberuehrt geblieben."
