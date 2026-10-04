UV ?= uv
EXTRAS = --extra dev --extra ml --extra server
RUN = $(UV) run --locked $(EXTRAS)

.PHONY: help setup check lint format test build db-init dev-api dev-web dev-web-remote web-install web-check tripcrew tripcrew-demo contract demo-offline mcp data-archive
.PHONY: dataset forge forge-natural forge-freeze eval
.PHONY: web-build diagnose verify-eval regression

help:
	@echo "setup   Install the locked development environment"
	@echo "check   Run lint, formatting checks, and tests"
	@echo "format  Format Python and sort imports"
	@echo "build   Build wheel and source distribution in dist/"
	@echo "db-init Initialize the configured SQLite database"
	@echo "dev-api Run the FastAPI server with reload on http://127.0.0.1:8000"
	@echo "dev-web Run the Next.js development server"
	@echo "dev-web-remote Run the Next.js dev server against the deployed API (REMOTE_API=...)"
	@echo "demo-offline Run API and web in Docker (compose.yaml), with data/ mounted"
	@echo "data-archive Pack data/ into blackbox-data.tar.gz for deployment (DATA_URL)"
	@echo "mcp     Run the local stdio MCP server"
	@echo "tripcrew Run 20 offline TripCrew scenarios"
	@echo "tripcrew-demo Demonstrate stale-FX repair with selective replay"
	@echo "dataset Rebuild the TripCrew dataset, train and evaluate (scripts/build_dataset.sh)"
	@echo "forge   Inject faults into passing TripCrew runs (resumes automatically)"
	@echo "forge-natural Attribute naturally failed AGENT runs with oracle fixes (test-only)"
	@echo "forge-freeze  Export AGENT labels and write DATASET_VERSION"
	@echo "eval    Train the diagnoser, run baselines/ablations/integrity checks into data/eval"
	@echo "diagnose RUN_ID=... Diagnose, verify and save one run"
	@echo "verify-eval Verify 30 diagnoses with paired replays"
	@echo "regression FORK=... Export a VERIFIED fork as an offline regression test"

setup:
	$(UV) sync --locked $(EXTRAS)

check: lint test

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

format:
	$(RUN) ruff check --select I --fix .
	$(RUN) ruff format .

test:
	$(RUN) python -m unittest discover -s tests -v

build:
	$(UV) build

db-init:
	$(RUN) python -m blackbox.store

dev-api:
	$(RUN) uvicorn server.app:app --reload --host 127.0.0.1 --port 8000

mcp:
	$(RUN) python -m blackbox.mcp_server

contract:
	$(RUN) python -m server.contract_export

demo-offline:
	docker compose up --build

web-install:
	npm --prefix web install

web-check:
	npm --prefix web run check

dev-web:
	npm --prefix web run dev

REMOTE_API ?= https://deployforgood-maharashtra-round.onrender.com

dev-web-remote:
	NEXT_PUBLIC_API_URL=$(REMOTE_API) npm --prefix web run dev

data-archive:
	$(RUN) python scripts/pack_data.py data blackbox-data.tar.gz

tripcrew:
	$(RUN) python -m agents.tripcrew run

tripcrew-demo:
	$(RUN) python -m agents.tripcrew demo --report data/tripcrew/demo.json

dataset:
	./scripts/build_dataset.sh

AGENT ?= tripcrew

forge:
	$(RUN) python -m blackbox.forge inject --agent $(AGENT) --target 360 --concurrency 4

forge-natural:
	$(RUN) python -m blackbox.forge natural --agent $(AGENT)

forge-freeze:
	$(RUN) python -m blackbox.forge freeze --agent $(AGENT)

eval:
	$(RUN) python -m blackbox.ml eval

web-build:
	npm --prefix web run build

diagnose:
	$(RUN) python -m blackbox.explain diagnose $(RUN_ID) --verify --save --data-dir data/$(AGENT)

verify-eval:
	$(RUN) python -m blackbox.explain verify-eval --n 30 --data-dir data/$(AGENT)

regression:
	$(RUN) python -m blackbox.explain export $(FORK) --data-dir data/$(AGENT)
