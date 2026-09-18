# Agent storage cutover

This release uses a new Agent domain and new thread/schedule tables. Legacy chats,
events, worker tasks, note rows, and job runs are not loaded.

Before deploying:

1. Back up the current database or `HATCHERY_DATA_DIR`.
2. Create one private GitHub repository with a `main` branch.
3. Install the Hatchery GitHub App on that repository with contents write access.
4. Configure `HATCHERY_AGENTS_REPOSITORY_URL` and, when needed,
   `HATCHERY_AGENTS_REPOSITORY_INSTALLATION_ID`.
5. From `backend/`, run:

       uv run migrate_agents.py --confirm

The importer reads only legacy `hatchery_spaces` rows or local `spaces/*.json`.
Each Space becomes an Agent, its description becomes `agents/<slug>/AGENTS.md`,
and the standard memories, skills, scripts, and schedules directories are
scaffolded. Chats and other runtime history are intentionally ignored.

Deploy the new application after the importer succeeds. Keep the backup until
each Agent is visible and its AGENTS.md can be read from the UI.
