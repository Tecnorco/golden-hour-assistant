# Golden Hour Assistant. Targets are added phase by phase.
PY := .venv/bin/python

.PHONY: setup index run mcp-demo test test-live extract eval-retrieval

setup:            ## create .venv and install dependencies (needs uv)
	uv sync
	@test -f .env || (cp .env.example .env && echo "Created .env - add your GEMINI_API_KEY")

index:            ## chunk + embed policy docs into var/index (calls Gemini, cached)
	$(PY) -m app.cli index

run:              ## process data/emails through the full pipeline (calls Gemini)
	$(PY) -m app.cli run

mcp-demo:         ## prove the MCP server works with a generic MCP client
	$(PY) -m app.mcp_server.demo_client

test:             ## unit tests, no API key needed
	$(PY) -m pytest -q

test-live:        ## tests that call the real Gemini API
	$(PY) -m pytest -q -m live

extract:          ## run field extraction over data/emails (calls Gemini)
	$(PY) -m app.cli extract

eval-retrieval:   ## retrieval hit rate @k report -> evals/reports/
	$(PY) -m app.cli eval-retrieval
