# Golden Hour Assistant. Targets are added phase by phase.
PY := .venv/bin/python

.PHONY: setup doctor index run ui queue mcp-demo eval compare report test test-live extract eval-retrieval

setup:            ## create .venv and install dependencies (needs uv)
	uv sync
	@test -f .env || (cp .env.example .env && echo "Created .env - add your GEMINI_API_KEY")

doctor:           ## check config, data, index, MCP server, Gemini access and one end-to-end run
	$(PY) -m app.cli doctor

index:            ## chunk + embed policy docs into var/index (calls Gemini, cached)
	$(PY) -m app.cli index

run:              ## process data/emails through the full pipeline (calls Gemini)
	$(PY) -m app.cli run

ui:               ## owner approval screen (Streamlit; never holds the API key)
	.venv/bin/streamlit run ui/streamlit_app.py

queue:            ## list inquiries and their status
	$(PY) -m app.cli queue

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

eval:             ## golden-set eval report -> evals/reports/ (calls Gemini)
	$(PY) -m app.cli eval

compare:          ## diff the two most recent eval runs
	$(PY) -m app.cli eval --compare

report:           ## operations report from the app DB
	$(PY) -m app.cli report
