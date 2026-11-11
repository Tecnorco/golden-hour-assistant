# Deployment notes

Deployment is a non-goal of the capstone. This page explains what to watch out for and how to put up a
**private demo** safely if you need a shareable link.

## Why not Vercel (or other serverless hosts)

- Streamlit is a long-running server with a websocket connection; serverless functions are short-lived.
- SQLite and `outbox/` need a persistent disk; serverless file systems are read-only or wiped between calls.
- The pipeline starts the MCP server as a subprocess and takes ~30 s per email; that does not fit function
  time limits.

Hosts that run it unchanged: Streamlit Community Cloud (quickest; storage may reset on restart), Fly.io /
Render / Railway with a persistent volume, or Google Cloud Run with a mounted volume.

## Rules for any deployment

1. **No authentication exists.** Anyone with the URL can read emails and press "Approve & send". Use demo
   data only, and restrict viewers (Streamlit Community Cloud viewer allowlist, or a private network).
2. **The key goes in the host's secret settings** as `GEMINI_API_KEY`, never in the repository. The approval
   UI does not need the key at all; only the processing step (`make run` / `make eval`) does.
3. **Persist `var/`** (SQLite, index, embedding cache) and `outbox/` on a volume, or accept that a restart
   resets the demo.
4. **Process emails before or outside the UI.** The UI only reviews; run `python -m app.cli run` as a one-off
   job (or locally, then upload `var/assistant.db`).

## Pre-deployment checklist

- [ ] Fresh clone works locally: `make setup && make index && make test && make doctor`
- [ ] `make eval` passes all targets locally; keep the report as the baseline
- [ ] Secrets set in the host (not in files); `make doctor` in the deployed environment shows the key as set
- [ ] `make doctor` in the deployed environment: all green (this exercises the index, MCP server, Gemini
      access and one full email)
- [ ] `make eval` in the deployed environment, then `make compare` against the local baseline:
      same numbers means the deployment behaves the same
- [ ] Viewer access restricted; only demo data loaded

## When something breaks

1. `python -m app.cli doctor`: tells you which piece is broken and how to fix it.
2. `python -m app.cli queue --status OWNER_QUEUE`: items that failed show the reason in the note column.
3. `python -m app.cli trace <inquiry_id>`: the full step-by-step timeline with errors, tool calls and
   guardrail verdicts.
4. `python -m app.cli report`: whether failures are isolated or systematic (escalation rate, latency, cost).
