# Deployment notes

Deployment is a non-goal of the capstone, but the approval screen can be shown as a **hosted demo** on
Streamlit Community Cloud. The hosted copy shows the 20 fictional sample emails, already processed, so it
needs **no Gemini API key**: it can only review, approve, edit and reject, never call the model.

## Showcase on Streamlit Community Cloud (step by step)

### What is already in the repository

| File | Why it matters |
|---|---|
| `ui/streamlit_app.py` | The app's entry point |
| `uv.lock` | Community Cloud reads this first and installs the exact dependency versions with uv |
| `.streamlit/config.toml` | Light theme, bundled font, usage statistics off, Deploy button hidden |
| `ui/static/fonts/` | The Geist font, served by the app itself |
| `demo/assistant.db` | Snapshot of the 20 sample emails, processed locally (fictional data only) |

### 1. Before you deploy

1. Merge the latest pull request into `main` on GitHub.
2. Have the demo username and password ready (given to you separately; they are not written anywhere in the
   repository). You can also choose your own in step 3.

### 2. Create the app

1. Go to **https://share.streamlit.io** and sign in with your GitHub account. If the repository is private,
   allow Streamlit to access private repositories when asked.
2. Click **Create app**, then choose to deploy an app from a GitHub repository.
3. Fill in:
   - **Repository:** `Tecnorco/golden-hour-assistant`
   - **Branch:** `main`
   - **Main file path:** `ui/streamlit_app.py`
   - **App URL:** pick something short, e.g. `golden-hour-demo`

### 3. Advanced settings (before clicking Deploy)

1. **Python version:** 3.12 or 3.13.
2. **Secrets:** paste this:

   ```toml
   GHA_DEMO_SNAPSHOT = "demo/assistant.db"

   # Optional: replace the default demo login with your own
   # GHA_UI_USERNAME = "admin"
   # GHA_UI_PASSWORD = "choose-a-new-password"
   ```

   Do **not** add `GEMINI_API_KEY`: the hosted demo does not need it.

### 4. Deploy and check

1. Click **Deploy**. The first build installs the dependencies and takes a few minutes.
2. Open the app URL. You should see the **Sign in** page; sign in with the demo credentials.
3. The inbox shows the 20 sample inquiries: 17 pending approval, 2 blocked prompt injections and 1 blocked spam.

### 5. Before showing it to your professor

- **Access:** if the repository is public, the app URL is public too. The login keeps the inbox private;
  in addition you can restrict who can open the app in the app's **Settings → Sharing** and invite your
  professor by email (options depend on your Streamlit account).
- **Reset:** after a run-through, click **Reset demo data** in the sidebar to put all 20 inquiries back to their
  original state and clear the outbox.
- **Wake-up:** free apps go to sleep after a period without visitors. Open the URL a few minutes before the
  demo; if it says the app is asleep, click the button to wake it up.

### 6. Updating the demo later

- Every push to `main` redeploys the app automatically.
- To refresh the demo data after changing prompts or the price list, run this on your computer (it calls Gemini,
  about 10 minutes) and commit the new `demo/assistant.db`:
  - Mac/Linux: `make demo-snapshot`
  - Windows: `uv run python -m app.cli demo-snapshot`

### If something goes wrong

Open **Manage app** (bottom right of the app) to see the logs.

| Symptom | Likely cause and fix |
|---|---|
| `ModuleNotFoundError: No module named 'app'` | The build did not use `uv.lock`; check it is committed at the repository root, then reboot the app |
| Inbox is empty after signing in | The `GHA_DEMO_SNAPSHOT` secret is missing or misspelled, or `demo/assistant.db` is not on `main` |
| Wrong password | If you set `GHA_UI_PASSWORD` in Secrets, the default password no longer works (by design) |
| Plain fonts or a "Deploy" button | `.streamlit/config.toml` or `ui/static/` is missing from the branch |

## Why not Vercel (or other serverless hosts)

- Streamlit is a long-running server with a websocket connection; serverless functions are short-lived.
- SQLite and `outbox/` need a persistent disk; serverless file systems are read-only or wiped between calls.
- The pipeline starts the MCP server as a subprocess and takes ~30 s per email; that does not fit function
  time limits.

Hosts that run it unchanged: Streamlit Community Cloud (quickest; storage may reset on restart), Fly.io /
Render / Railway with a persistent volume, or Google Cloud Run with a mounted volume.

## Rules for any other deployment

1. **The login is a single shared demo account**, not real user management. Use demo data only, set your own
   `GHA_UI_PASSWORD`, and restrict viewers where the host allows it.
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
