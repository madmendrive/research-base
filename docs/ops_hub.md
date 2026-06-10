# Research KB + Telegram Ops Hub

This repo now has two layers:

- Existing research pipeline: downloads, triage, storage, summaries, cross-cuts, dashboard.
- Ops hub: local KB index, queue, worker, heartbeat scheduler, email/headline/folder sweeps, Telegram KB commands.

## Production Shape

Run production on the Mac mini:

- `python bot.py`
- `python main.py worker`
- `python main.py heartbeat --agenda agenda.md`

The worker is the only process that should perform heavy state-mutating ingestion. The desktop should act as a workstation and inbox source. If the desktop syncs files to the Mac mini, use one-way Syncthing and do not run a second worker on the desktop.

VS Code is optional. It is useful for editing, but the system only needs Python, Git, dependencies, `.env`, and LaunchAgents.

Template LaunchAgents live in `deploy/macos/`. Copy each `.plist.example` to
`~/Library/LaunchAgents/`, replace `YOUR_USER`, remove `.example`, then load it
with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<file>.plist`.

## First-Time Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Fill `.env` with:

- `ANTHROPIC_API_KEY` for Claude API calls.
- `OPENAI_API_KEY` for KB embeddings.
- Telegram bot token and allowlisted user IDs.
- EDINET/DART keys if using those downloaders.
- Gmail API OAuth settings for the research/Substack mailbox.

### Gmail Research Mailbox

Preferred setup is Gmail API OAuth, not IMAP passwords:

1. Create or choose the dedicated Gmail account.
2. In Google Cloud Console, create a project and enable the Gmail API.
3. Configure the OAuth consent screen and add your Gmail address as a test user if the app is in testing mode.
4. Create an OAuth client ID with application type `Desktop app`.
5. Download the client JSON and save it as `config/gmail_oauth_client_secret.json`.
6. Set these in `.env`:

```env
RESEARCH_EMAIL_PROVIDER=gmail_api
GMAIL_OAUTH_CLIENT_SECRET=config/gmail_oauth_client_secret.json
GMAIL_OAUTH_TOKEN=data/_secrets/gmail_token.json
GMAIL_QUERY=in:inbox
```

7. Run `python main.py gmail-auth` once and approve read-only Gmail access in the browser.

The token is saved locally under `data/_secrets/` and is ignored by Git. The scope is Gmail read-only.
IMAP app-password mode remains available by setting `RESEARCH_EMAIL_PROVIDER=imap`.

## Core Commands

```bash
python main.py import-claude-export ~/Downloads/claude-export.zip --dry-run
python main.py import-claude-export ~/Downloads/claude-export.zip
python main.py import-skills ~/.claude --dry-run
python main.py import-skills ~/.claude

python main.py kb-reindex --source all --no-embed --limit 10
python main.py kb-reindex --source all

python main.py folder-scan --folder ~/research-pipeline/downloads --notify
python main.py gmail-auth
python main.py email-sweep --once --notify
python main.py headline-sweep --once --notify

python main.py worker
python main.py heartbeat --agenda agenda.md

python main.py ask "What has changed in the AI optical supply chain?"
```

Use `--no-embed` for cheap smoke tests. Production indexing should use embeddings.

## Telegram Commands

- `/ask QUESTION`: analyst-style investment answer using the KB as private memory.
- `/search QUERY`: diagnostic top matching KB chunks.
- `/note TARGET TEXT`: save your own note and index it.
- `/download TICKER`: queue `download-materials`.
- `/pending`: show low-confidence files with Confirm/Reclassify/Drop buttons.
- PDF upload: saves the file under `data/_telegram_uploads/` and queues ingestion.

`/ask` is the product surface. It should read like a PM-ready analyst answer,
with business/theme explanation, bull case, bear case, book fit, what changed,
comparison versus the existing KB, implications, and what to watch next.
`/search` is intentionally raw and should mostly be used for debugging retrieval.

## Claude Skills

Bring existing Claude skills in two stages:

1. Store them as knowledge: run `python main.py import-skills <skills-dir>` or copy skill Markdown/instructions into `data/_skills/`, then run `python main.py kb-reindex --source skills`.
2. Promote the useful ones into code: convert stable prompt/workflow logic into Python modules, worker job kinds, or prompt templates used by `scripts/research.py`, `scripts/triage.py`, and `scripts/analysis_report.py`.

This avoids blindly executing every old skill while still making all of them searchable.

## Cost Boundaries

- Anthropic API credits pay for automated Claude calls from the Python app.
- Claude Max helps with Claude/Claude Code usage, but does not replace API credits for this app.
- OpenAI API billing pays for embeddings.
- ChatGPT Pro helps you work with Codex/ChatGPT, but does not pay for API usage.

## Cutover Checklist

1. Let any desktop bulk ingest finish.
2. Back up `data/`.
3. Rsync `data/` from desktop to Mac mini.
4. Stop desktop bot/sweeper/worker.
5. Start Mac mini bot, worker, and heartbeat.
6. Verify `/ask`, `/pending`, `headline-sweep --once --notify`, and one small `folder-scan`.
7. Enable Time Machine and a second nightly backup.
