# Email Assist

Local-first important mail queue. You add what matters as priority chips, it syncs recent email over IMAP, scores messages with those priorities and synonym-expanded keywords, saves the important ones to SQLite, and shows them in a small dashboard. Click any captured message to open the original in Gmail; use `stored text` as a local fallback.

This is intentionally not an AI agent yet. The first useful version should reliably answer: "what mail should I not miss?"

## What it catches

Default rules look for:

- bank communication: transaction alerts, statements, OTPs, card messages
- interviews: recruiter messages, scheduled interviews, calendar invites, offer letters
- deadlines: urgent/action-required/final-reminder messages

Use the dashboard's Priorities page to enter your own priorities.

## Requirements

- Python 3
- IMAP access for your email account
- Optional: NLTK for WordNet synonym expansion

For Gmail, use an app password. Do not put your normal Google password in `.env`.

To enable NLTK synonyms:

```bash
python3 -m pip install -r requirements.txt
python3 -m nltk.downloader wordnet
```

## Quick Start

Create local config files:

```bash
python3 email_assist.py init
```

Try the dashboard with demo messages:

```bash
python3 email_assist.py demo
python3 email_assist.py serve
```

Open:

```text
http://127.0.0.1:8765
```

Add priorities from the main page. Each priority becomes a chip you can remove.

## Sync Real Email

Fill in `.env`:

```bash
EMAIL_IMAP_HOST=imap.gmail.com
EMAIL_IMAP_USER=you@example.com
EMAIL_IMAP_PASSWORD=your-app-password
EMAIL_IMAP_FOLDER=INBOX
```

Then sync recent messages:

```bash
python3 email_assist.py sync --limit 100
python3 email_assist.py serve
```

Ingest everything from June 1, 2026 onward:

```bash
python3 email_assist.py sync --since 2026-06-01
```

By default sync fetches headers only and opens the original mail in Gmail. If you also want the local `stored text` fallback populated with full message bodies, run:

```bash
python3 email_assist.py sync --since 2026-06-01 --full
```

## Daily Ingestion

The macOS LaunchAgent template is in [launchd/com.email-assist.ingest.plist](launchd/com.email-assist.ingest.plist). It runs this every day at midnight:

```bash
python3 email_assist.py sync --since 2026-06-01
```

Logs are written to:

```text
logs/ingest.out.log
logs/ingest.err.log
```

## Local AI Agent

Email Assist can use a local open-source model through Ollama tool calling. Recommended starter model:

```bash
ollama pull qwen2.5:3b
```

Then start the dashboard and open `Agent`:

```bash
python3 email_assist.py serve
```

You can also ask from the terminal:

```bash
python3 email_assist.py agent "Find new interview emails and show Gmail links"
```

The agent can call tools to:

- list, add, and remove priority chips
- search captured mail
- mark a message done
- sync mail

Optional `.env` settings:

```bash
EMAIL_ASSIST_AI_MODEL=qwen2.5:3b
EMAIL_ASSIST_OLLAMA_URL=http://127.0.0.1:11434/api/chat
```

## Rules

The dashboard writes [rules.json](rules.json) for you. Each priority chip becomes a JSON rule with a label, score, and regex patterns:

```json
{
  "label": "interview",
  "score": 8,
  "patterns": ["interview", "recruiter", "calendar invite", "scheduled"]
}
```

Higher scores appear first. Messages that match no rule are ignored. If NLTK WordNet is installed, each priority is expanded with synonyms too. You can still edit [rules.json](rules.json) directly if you want regex control. After sync, click a message subject in the queue to open Gmail on that message.

## Files

- `email_assist.py`: app, sync, dashboard
- `rules.json`: your local priority rules
- `.env`: your local email config, ignored by git
- `email_assist.sqlite3`: local message database with captured message text, ignored by git
- `test_email_assist.py`: small self-check

## Check

Run:

```bash
python3 test_email_assist.py
```

## Current Ceiling

- IMAP only; no Gmail OAuth yet
- local AI agent uses Ollama tool calling; classification still starts from rules
- NLTK synonyms are optional and depend on local WordNet data
- local dashboard only; no hosted multi-user app yet
- simple `.env` parser; use plain `KEY=value` lines

ponytail: those limits are intentional. OAuth, chat, and packaging are worth adding after the queue is useful on real inboxes.
