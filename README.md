# TeamSync AI — The Team Intelligence Command Center

TeamSync AI is the single primary application in this repository. It uses the configured Supabase Auth and PostgreSQL backend, Telegram integration, and configured LLM adapter. Participant management, task management, messaging, and project intelligence are available in one app. The sign-in screen introduces the product without exposing workspace metrics before authentication; after sign-in, the command center is the main landing page.

## Run TeamSync AI

From the project directory, start the main app locally:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

`teamsync_app.py` remains as a backward-compatible alias for the same TeamSync application. The command center surfaces database-derived project health, completed and outstanding work, blockers, near-term deadlines, ownership gaps, workload distribution, recent activity, and prioritized next actions. **Project Timeline** groups persisted tasks by actual deadline, highlights overdue and near-term work, and links each task back to its Task Board details; it is not a calendar integration or a milestone schedule. Project name, description, goals, and target timeline can be edited on the existing singleton project record without changing tasks. Its Project Pulse panel is an explicitly rule-based summary of persisted tasks and dependencies; chat answers use the configured LLM only when requested. The interface includes an executive summary, completion progress, activity filters, and reviewed quick actions using native Streamlit components with lightweight CSS. Capacity what-if controls, agent-role visualization, and remediation previews are illustrative and never alter project data. Delivery probabilities are not shown without validated history, and no autonomous reassignments, calendar synchronization, email notifications, agent runs, or generated audio are claimed. Milestones and decision records are not implemented, so the dashboard identifies those coverage gaps instead of implying they are tracked. Recommendation approval/dismissal is a browser-session acknowledgment, not a persisted decision or task update. Motion respects the operating system's reduced-motion preference and has an in-app Reduce motion control. Missing integrations are shown honestly as unavailable; no sample conversations are passed off as live messages.

`task_dependencies` and project detail columns are initialized by the schema initializer; `python database.py` also creates/upgrades them. The workspace remains shared across registered accounts. Per-user database authorization, project milestones, persisted copilot conversation history, approval records, blocker lifecycle/category tables, and schedule prediction are not implemented. Telegram polling remains a separate `python bot.py` process.

## Prerequisites

- Python 3.11+ (3.10 may work; Python 3.11 is recommended).
- A Telegram bot token for live messaging.
- An API key and endpoint for a configured OpenAI-compatible chat-completions service for AI interpretation. The app and tests can run without either integration configured.

## Setup

From the project directory, create and activate a virtual environment:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` locally. Never commit it. Settings can also be supplied through
Streamlit secrets: create `.streamlit/secrets.toml` locally and put the same
`KEY = "value"` settings from `.env` there. This file is ignored by Git. The app
uses environment variables (including `.env`) first, then falls back to
`st.secrets`; this lets local development and Streamlit Community Cloud use the
same configuration.

For Streamlit Community Cloud, open the deployed app's **Settings → Secrets**
and paste the TOML settings there. Do not upload `.env` or `secrets.toml`, and
never put secrets in source code. The Supabase database URI contains a password;
keep it private and rotate it if it has been exposed.

Follow the Supabase setup below, then initialize its schema with:

```powershell
python database.py
```

### Supabase project, login, and database setup

1. Create a project in the Supabase dashboard and save the database password you set. The dashboard cannot show you this password later; reset it in **Project Settings → Database** if needed.
2. In **Project Settings → API**, copy the project URL to `SUPABASE_URL` and the **publishable** key (or legacy `anon` key) to `SUPABASE_ANON_KEY`. Do not use the `service_role` key.
3. In **Project Settings → Database → Connection string**, select the URI connection string and copy the **Session pooler** URI into `SUPABASE_DB_URL`. Replace the password placeholder with your database password. Keep the whole URI on one line; ensure SSL is enabled (`sslmode=require`).
4. Keep `DATABASE_BACKEND=supabase`. Create a local `.env` from `.env.example` and fill these three values. Never share or commit `.env`.
5. In **Authentication → Providers**, enable Email. Configure email confirmation as desired. With confirmation enabled, new users verify the signup email before they can log in.
6. Initialize tables and indexes: `python database.py`. The app also checks/initializes the schema when it connects.
7. Start the app using the commands below. The first page provides **Log in** and **Register**. New accounts can access and manage the same shared project data because open signup was selected.

`SUPABASE_DB_URL` contains a database password and grants database-level access; keep it only in the trusted app server's environment. The app uses Supabase Auth for the login gate and server-side PostgreSQL for data. This MVP is a shared workspace, not a per-user private-project system.

For a clearly labelled sample participant and task, run:

```powershell
python database.py --seed-demo
```

The demo participant has a placeholder chat ID and cannot receive real messages. For a live run, register a real participant and create a separate task assigned to them; demo records remain clearly labelled.

## Telegram setup

1. In Telegram, open [@BotFather](https://t.me/BotFather), run `/newbot`, and follow its prompts.
2. Copy the generated token into `TELEGRAM_BOT_TOKEN` in `.env`. Keep the token private.
3. Each participant must open the bot and send `/start` first; Telegram generally does not let a bot initiate a private conversation before that. The bot responds with that participant's own numeric chat ID.
4. Ask each participant to share their displayed chat ID, then in TeamSync open **Team & Participants** and register their name and chat ID. Registration is by the manager; `/start` does not let a sender claim another participant's identity.
5. Run `python bot.py --check-config` to check that the token is present, then start polling with `python bot.py`.
6. In the dashboard, create a task and click **Send follow-up**. Messages include the task title and deadline.

The TeamSync dashboard and Telegram bot are separate processes and must run at the same time. In two PowerShell windows:

```powershell
# Window 1
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501

# Window 2
.\.venv\Scripts\Activate.ps1
python bot.py
```

To enable periodic Telegram reminders, set `REMINDER_INTERVAL_MINUTES` to a positive value. The continuously running `bot.py` polling process will check open tasks at that interval; Streamlit reruns do not schedule or deliver reminders. Keep the bot process supervised and running for reminders to continue across app restarts. `FOLLOW_UP_COOLDOWN` (or `FOLLOW_UP_COOLDOWN_MINUTES` / `FOLLOW_UP_COOLDOWN_HOURS`) is an enforced per-task cooldown (default: 5 minutes) shared by manual sends and automatic reminders; failed sends release their reservation. Set the interval to `0` to disable automatic reminders. There is no persistent in-app reminder inbox or delivery-preference system in this MVP.

Webhook mode is not included in this local MVP. A production deployment may replace polling with Telegram webhooks behind a public HTTPS endpoint.

## LLM configuration

Set `LLM_PROVIDER=openai_compatible`, `LLM_API_URL` (base URL or full `/chat/completions` URL), `LLM_API_KEY`, and `LLM_MODEL`. Requests have a configurable timeout (`LLM_TIMEOUT_SECONDS`). The default `LLM_PROVIDER=disabled` deliberately causes replies to be retained with an explicit analysis-failed result; it does not invent a classification. The provider can be replaced by adapting `llm_provider.complete`.

The model receives only tasks assigned to the registered sender. Its JSON must contain exactly `task_id`, `status`, `progress_summary`, `blocker_reason`, `next_action`, `confidence`, and `clarification_question`. Unsupported/malformed results are rejected. Ambiguous tasks prompt for clarification; an LLM task ID must match one of that participant's open tasks. Application code independently requires clear positive completion language before allowing Complete, and blocks a completion proposal that still contains a blocker.

## Task management and handling

In TeamSync, use **Team & Participants** to register teammates and their Telegram chat IDs. Use **Task Board** to create, search, filter by status/owner/priority/deadline, edit, reassign, archive, or send reviewed Telegram follow-ups for tasks. Per-task activity history and incoming messages are available in the task details and **Team Inbox**.

Statuses are Pending, In Progress, Blocked, and Complete. The typed `Task` and `TaskStatus` model in `task_status.py` also provides `process_task_update` for reply-based status transitions. Incoming replies that say work is waiting on or depends on an external factor are recorded as Blocked with the extracted blocker reason, even if the same reply sounds like a completion. Managers may reopen a completed task to Pending or In Progress. Incoming participant changes, status transitions, and manager edits are recorded in `task_activity`. Incoming Telegram update IDs are unique, preventing repeated deliveries from reapplying the same change.

## Tests

```powershell
pytest -q
```

Tests use temporary SQLite databases and mock the LLM boundary. No Telegram or LLM credentials are needed.

## Manual end-to-end checklist

1. Configure a real bot token and LLM provider, initialize the database, register a participant after they send `/start`, and start both processes.
2. Create a task for that participant and send a follow-up from the dashboard.
3. Reply from the participant's Telegram account with a specific blocker; verify the bot acknowledges it and the dashboard shows **Blocked** plus the blocker.
4. Reply with a clear completion confirmation (for example, “The task is complete; access is fixed.”); verify status becomes **Complete** and the old blocker clears.
5. Repeat a Telegram update if possible and verify the task is not updated twice; try a reply from an unregistered account and verify no task changes.
6. Try an ambiguous reply for a participant with multiple open tasks and verify the bot asks which task.

## Demo script

Create “Build prototype” for a registered participant → click **Send follow-up** → participant replies “Blocked waiting for API access” → verify **Blocked** and blocker text in the dashboard → participant replies “The task is complete; access is fixed” → verify **Complete** and a recorded activity entry.

## Troubleshooting

- **Bot says token is missing:** check `.env` and run `python bot.py --check-config`. Restart the bot after changing environment configuration.
- **Bot cannot message participant:** have the participant open the bot and send `/start`; verify their registered chat ID.
- **Telegram API error or timeout:** confirm the token, network access, and that the bot has not been blocked. A failed send is reported in the dashboard and does not consume the cooldown.
- **No AI analysis:** check `LLM_PROVIDER`, endpoint, model, API key, timeout, and provider quota. Incoming replies remain stored with an analysis error; no task update is made.
- **Clarification response:** include a task title when the participant has more than one open task. The bot does not guess which task should change.
- **Supabase connection error:** re-copy the session pooler URI, check its password and SSL setting, and confirm the project is not paused.
- **Dashboard does not immediately reflect a reply:** click **Refresh data**; the bot process and Streamlit dashboard are independent.

## Security notes and limitations

Telegram tokens, the Supabase database password, and LLM keys are read from environment configuration only. Do not paste them into task descriptions or commit `.env` files. Sender authorization is by exact registered Telegram chat ID. The LLM does not execute SQL or tools. Supabase Auth protects the dashboard, but all registered users share the same workspace and can manage all its data. This MVP does not implement per-user/project database authorization, webhook deployment, or multi-project administration. Avoid exposing the app publicly unless its shared-workspace access model is acceptable.
