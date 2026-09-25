# Serving, secrets, schedules

Code: `backend/hatchery/serve/`, `backend/hatchery/sdk/`, `backend/hatchery/vault.py`.

## Serving

- `backend/hatchery/serve/host.py` sits in front of the one FastAPI app. A request for exactly `<agent>.<HATCHERY_SERVE_DOMAIN>` goes to the agent's routes; every other host gets the normal app. Bad or nested agent hosts get 400/421. On preview deployments the `X-Hatchery-Agent` header can name the agent instead.
- The project needs the wildcard domain `*.<HATCHERY_SERVE_DOMAIN>`. `vercel.json` already sends every path to the app.
- Routes are `agents/<id>/api/<route>/route.py` on `main`, with `GET`/`POST`/`PUT`/`PATCH`/`DELETE` functions and `[param]` or `[...rest]` path parts. Routes are found by parsing the files; user code is never imported to find them.
- Each request reads the agent's folder at current `main` (cached 15 s) and runs in the agent's one serve sandbox, redeploying it when `main` moved. So a reviewed route goes live without a Hatchery deploy; thread branches are never served. One request runs at a time per agent (busy: 429). Handlers work in `/workspace/data`.
- A handler can call `prompt()` (from `hatchery.sdk`) to start a normal turn of the same agent. The effect key makes retries safe.
- Changes to `api/`, `schedules/`, `lib/`, or the agent's root `requirements.txt` need review before they reach `main`.
- Operator endpoints live under `/api/agents/{id}/` (`serve`, `routes`, `schedules`, `secrets`); the agent's API page uses them.

## Secrets

- One `SecretVault` Rotor process per agent keeps AES-GCM encrypted values in the database, tied to the agent and the name. The key is `HATCHERY_SECRETS_KEY` (32 bytes, base64url), or a generated `<data dir>/secrets-key` outside Vercel.
- Agents ask for a secret with `secret_request(name, note)`; only the name and note are stored with the request. Values never enter transcripts or Git.
- People set (and rotate), reveal, and delete secrets. Only that agent's serve sandbox gets its values. Retiring an agent clears them, and its routes then answer 410.

## Schedules

- `agents/<id>/schedules/<name>/job.py` declares `SCHEDULE` (a `cron` with optional `tz`, or `every`) and a `run(job)` function. Files are parsed, not imported.
- Each agent has one `Scheduler` process with one Rotor `Ticker` per job. Runs happen in the agent's serve sandbox. People can pause and resume a job; pauses survive edits. Recent results are kept.
- New `main` revisions and `/api/cron` (every fifth minute) keep tickers in line with Git.
- Prompt jobs (`backend/hatchery/store/jobs.py`, a saved prompt on a cron) are separate and unchanged.
