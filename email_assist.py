#!/usr/bin/env python3
import argparse
import email
import html
import imaplib
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

DB = Path(os.environ.get("EMAIL_ASSIST_DB", "email_assist.sqlite3"))
RULES = Path(os.environ.get("EMAIL_ASSIST_RULES", "rules.json"))
ENV = Path(os.environ.get("EMAIL_ASSIST_ENV", ".env"))
OLLAMA_URL = os.environ.get("EMAIL_ASSIST_OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
AI_MODEL = os.environ.get("EMAIL_ASSIST_AI_MODEL", "qwen2.5:3b")
STOPWORDS = {
    "a", "an", "and", "any", "for", "from", "his", "her", "important", "mail", "mails",
    "me", "my", "of", "or", "the", "to", "with",
}
EXPANSIONS = {
    "bank": ["bank", "credit card", "debit card", "statement", "transaction", "otp", "payment"],
    "interview": ["interview", "recruiter", "hiring", "calendar invite", "scheduled", "offer letter"],
    "job": ["job", "interview", "recruiter", "hiring", "offer letter", "application"],
    "visa": ["visa", "appointment", "consulate", "embassy", "passport"],
    "invoice": ["invoice", "receipt", "payment due", "paid", "billing"],
    "deadline": ["deadline", "urgent", "action required", "expires", "final reminder"],
}
MAX_SYNONYMS_PER_WORD = 12


def load_env(path=ENV):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        # ponytail: simple dotenv only; upgrade to python-dotenv if multiline/export syntax matters.
        os.environ.setdefault(key.strip(), value)


def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("""
        create table if not exists messages (
            id text primary key,
            sender text not null,
            subject text not null,
            sent_at text not null,
            snippet text not null,
            labels text not null,
            score integer not null,
            status text not null default 'new',
            url text not null default ''
        )
    """)
    columns = {row["name"] for row in db.execute("pragma table_info(messages)")}
    if "body" not in columns:
        db.execute("alter table messages add column body text not null default ''")
        db.execute("update messages set body = snippet where body = ''")
        db.commit()
    return db


def load_rules(path=RULES):
    if not path.exists():
        raise SystemExit(f"Missing {path}. Run: python3 email_assist.py init")
    with path.open() as f:
        data = json.load(f)
    return data.get("rules", [])


def priorities_from_rules(rules):
    return "\n".join(rule["label"] for rule in rules)


def load_priority_labels():
    return [rule["label"] for rule in load_rules()]


def rules_from_priorities(text):
    priorities = []
    seen = set()
    for line in text.splitlines():
        label = re.sub(r"\s+", " ", line).strip()
        key = label.lower()
        if not label or key in seen:
            continue
        seen.add(key)
        priorities.append(label)
    total = len(priorities)
    return [
        {"label": label, "score": total - index, "patterns": priority_patterns(label)}
        for index, label in enumerate(priorities)
    ]


def priority_patterns(label):
    patterns = {re.escape(label)}
    words = re.findall(r"[a-z0-9]+", label.lower())
    for word in words:
        if word in STOPWORDS:
            continue
        expanded = EXPANSIONS.get(word, [word]) + nltk_synonyms(word)
        for pattern in expanded:
            patterns.add(re.escape(pattern))
    return sorted(patterns)


def nltk_synonyms(word):
    try:
        from nltk.corpus import wordnet
        synsets = wordnet.synsets(word)
    except (ImportError, LookupError):
        return []
    synonyms = []
    seen = {word}
    for synset in synsets:
        for lemma in synset.lemma_names():
            candidate = lemma.replace("_", " ").lower()
            if candidate in seen or candidate in STOPWORDS:
                continue
            seen.add(candidate)
            synonyms.append(candidate)
            if len(synonyms) >= MAX_SYNONYMS_PER_WORD:
                return synonyms
    return synonyms


def save_rules(rules, path=RULES):
    path.write_text(json.dumps({"rules": rules}, indent=2) + "\n")


def save_priority_labels(labels):
    rules = rules_from_priorities("\n".join(labels))
    save_rules(rules)
    rescore_messages(rules)


def add_priority(label):
    labels = load_priority_labels()
    key = label.strip().lower()
    if key and key not in {item.lower() for item in labels}:
        labels.append(label.strip())
        save_priority_labels(labels)


def delete_priority(label):
    labels = [item for item in load_priority_labels() if item.lower() != label.strip().lower()]
    save_priority_labels(labels)


def gmail_url(message_id):
    message_id = (message_id or "").strip().strip("<>")
    if not message_id:
        return ""
    return "https://mail.google.com/mail/u/0/#search/" + quote(f"rfc822msgid:{message_id}", safe="")


def clean_header(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, email.errors.HeaderParseError):
        return value


def message_text(msg):
    parts = msg.walk() if msg.is_multipart() else [msg]
    chunks = []
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        chunks.append(payload.decode(charset, errors="replace"))
    return re.sub(r"\s+", " ", " ".join(chunks)).strip()


def classify(mail, rules):
    haystack = " ".join([
        mail["sender"],
        mail["subject"],
        mail["snippet"],
    ]).lower()
    labels = []
    score = 0
    for rule in rules:
        patterns = rule.get("patterns", [])
        if any(re.search(pattern, haystack, re.I) for pattern in patterns):
            labels.append(rule["label"])
            score += int(rule.get("score", 1))
    return labels, score


def parse_message(raw, rules):
    msg = email.message_from_bytes(raw)
    sent = parsedate_to_datetime(msg.get("date")) if msg.get("date") else datetime.now(timezone.utc)
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    text = message_text(msg)
    mail = {
        "id": msg.get("message-id") or f"{clean_header(msg.get('from'))}:{clean_header(msg.get('subject'))}:{sent.isoformat()}",
        "sender": clean_header(msg.get("from")),
        "subject": clean_header(msg.get("subject")),
        "sent_at": sent.astimezone(timezone.utc).isoformat(),
        "snippet": text[:500],
        "body": text,
        "url": gmail_url(msg.get("message-id")),
    }
    labels, score = classify(mail, rules)
    mail["labels"] = labels
    mail["score"] = score
    return mail


def save_messages(db, messages):
    for mail in messages:
        if mail["score"] <= 0:
            continue
        db.execute("""
            insert into messages (id, sender, subject, sent_at, snippet, body, labels, score, url)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set
                sender=excluded.sender,
                subject=excluded.subject,
                sent_at=excluded.sent_at,
                snippet=excluded.snippet,
                body=excluded.body,
                labels=excluded.labels,
                score=excluded.score,
                url=excluded.url
        """, (
            mail["id"],
            mail["sender"],
            mail["subject"],
            mail["sent_at"],
            mail["snippet"],
            mail["body"],
            json.dumps(mail["labels"]),
            mail["score"],
            mail["url"],
        ))
    db.commit()


def rescore_messages(rules):
    with connect() as db:
        for row in db.execute("select id, sender, subject, snippet from messages").fetchall():
            labels, score = classify(row, rules)
            db.execute(
                "update messages set labels = ?, score = ? where id = ?",
                [json.dumps(labels), score, row["id"]],
            )
        db.commit()


def imap_since(value):
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit("Use --since as YYYY-MM-DD, for example 2026-06-01.") from exc
    return parsed.strftime("%d-%b-%Y")


def sync_imap(limit=None, since=None, full=False):
    load_env()
    host = os.environ.get("EMAIL_IMAP_HOST")
    user = os.environ.get("EMAIL_IMAP_USER")
    password = os.environ.get("EMAIL_IMAP_PASSWORD")
    folder = os.environ.get("EMAIL_IMAP_FOLDER", "INBOX")
    if not all([host, user, password]):
        raise SystemExit("Set EMAIL_IMAP_HOST, EMAIL_IMAP_USER, and EMAIL_IMAP_PASSWORD.")

    rules = load_rules()
    client = imaplib.IMAP4_SSL(host, timeout=60)
    try:
        client.login(user, password)
        client.select(folder)
        criteria = ["SINCE", imap_since(since)] if since else ["ALL"]
        typ, data = client.search(None, *criteria)
        if typ != "OK":
            raise SystemExit("IMAP search failed.")
        ids = data[0].split()
        if limit:
            ids = ids[-limit:]
        total = len(ids)
        print(f"Found {total} messages" + (f" since {since}" if since else "") + ".", flush=True)
        messages = []
        parts = "(RFC822)" if full else "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])"
        for index, msg_id in enumerate(ids, 1):
            typ, fetched = client.fetch(msg_id, parts)
            if typ == "OK" and fetched and isinstance(fetched[0], tuple):
                messages.append(parse_message(fetched[0][1], rules))
            if index % 100 == 0:
                print(f"Fetched {index}/{total} messages.", flush=True)
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass

    with connect() as db:
        save_messages(db, messages)
    window = f" since {since}" if since else ""
    print(f"Synced {len(messages)} messages{window}; saved important matches.")


def init_files():
    if RULES.exists():
        print(f"{RULES} already exists.")
    else:
        RULES.write_text(json.dumps({
            "rules": [
                {
                    "label": "bank",
                    "score": 5,
                    "patterns": ["\\bbank\\b", "credit card", "debit card", "statement", "transaction", "otp"]
                },
                {
                    "label": "interview",
                    "score": 8,
                    "patterns": ["interview", "recruiter", "hiring", "calendar invite", "scheduled", "offer letter"]
                },
                {
                    "label": "deadline",
                    "score": 4,
                    "patterns": ["urgent", "action required", "deadline", "expires", "final reminder"]
                }
            ]
        }, indent=2) + "\n")
        print(f"Created {RULES}.")
    if not ENV.exists():
        ENV.write_text("""# Copy real values here. For Gmail, use an app password, not your normal password.
EMAIL_IMAP_HOST=imap.gmail.com
EMAIL_IMAP_USER=you@example.com
EMAIL_IMAP_PASSWORD=your-app-password
EMAIL_IMAP_FOLDER=INBOX
""")
        print(f"Created {ENV}.")


def rows(status="new", q=""):
    sql = "select * from messages where status = ? and score > 0"
    args = [status]
    if q:
        sql += " and (sender like ? or subject like ? or snippet like ? or labels like ?)"
        args += [f"%{q}%"] * 4
    sql += " order by score desc, sent_at desc limit 100"
    with connect() as db:
        return db.execute(sql, args).fetchall()


def status_counts():
    with connect() as db:
        return dict(db.execute("select status, count(*) from messages group by status").fetchall())


def get_message(message_id):
    with connect() as db:
        return db.execute("select * from messages where id = ?", [message_id]).fetchone()


def agent_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "list_priorities",
                "description": "List the user's current email priorities.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "add_priority",
                "description": "Add a user priority chip and rescore saved mail.",
                "parameters": {
                    "type": "object",
                    "properties": {"priority": {"type": "string"}},
                    "required": ["priority"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_priority",
                "description": "Remove a user priority chip and rescore saved mail.",
                "parameters": {
                    "type": "object",
                    "properties": {"priority": {"type": "string"}},
                    "required": ["priority"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_mail",
                "description": "Search captured important mail by sender, subject, snippet, or label.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "status": {"type": "string", "enum": ["new", "done"]},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mark_done",
                "description": "Mark a captured message as done by message id.",
                "parameters": {
                    "type": "object",
                    "properties": {"message_id": {"type": "string"}},
                    "required": ["message_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "sync_mail",
                "description": "Sync mail from IMAP. Use since as YYYY-MM-DD. Limit is optional.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "since": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": [],
                },
            },
        },
    ]


def execute_agent_tool(name, args):
    args = args or {}
    if name == "list_priorities":
        return {"priorities": load_priority_labels()}
    if name == "add_priority":
        add_priority(args["priority"])
        return {"ok": True, "priorities": load_priority_labels()}
    if name == "delete_priority":
        delete_priority(args["priority"])
        return {"ok": True, "priorities": load_priority_labels()}
    if name == "search_mail":
        limit = min(int(args.get("limit", 10)), 25)
        found = rows(args.get("status", "new"), args.get("query", ""))[:limit]
        return {
            "messages": [
                {
                    "id": row["id"],
                    "subject": row["subject"],
                    "sender": row["sender"],
                    "sent_at": row["sent_at"],
                    "labels": json.loads(row["labels"]),
                    "score": row["score"],
                    "gmail_url": row["url"] or gmail_url(row["id"]),
                }
                for row in found
            ]
        }
    if name == "mark_done":
        with connect() as db:
            db.execute("update messages set status = 'done' where id = ?", [args["message_id"]])
            db.commit()
        return {"ok": True}
    if name == "sync_mail":
        sync_imap(args.get("limit"), args.get("since"))
        return {"ok": True}
    return {"error": f"Unknown tool: {name}"}


def ollama_chat(messages):
    load_env()
    payload = {
        "model": os.environ.get("EMAIL_ASSIST_AI_MODEL", AI_MODEL),
        "messages": messages,
        "tools": agent_tools(),
        "stream": False,
    }
    request = urllib.request.Request(
        os.environ.get("EMAIL_ASSIST_OLLAMA_URL", OLLAMA_URL),
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.URLError as exc:
        raise SystemExit(
            "Local AI model is not reachable. Start Ollama and run: "
            "ollama pull qwen2.5:3b"
        ) from exc


def run_agent(prompt):
    messages = [
        {
            "role": "system",
            "content": (
                "You are Email Assist, a local inbox triage agent. Use tools when you need "
                "current inbox data or need to change priorities/status. Be concise. When "
                "showing mail, include subject, sender, why it matters, and Gmail URL."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    for _ in range(5):
        result = ollama_chat(messages)
        message = result.get("message", {})
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return message.get("content", "")
        for call in tool_calls:
            function = call.get("function", {})
            name = function.get("name")
            args = function.get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args)
            output = execute_agent_tool(name, args)
            messages.append({
                "role": "tool",
                "name": name,
                "content": json.dumps(output),
            })
    return "I stopped after too many tool calls. Try a narrower request."


def app_shell(title, active, content):
    queue_class = "active" if active == "queue" else ""
    agent_class = "active" if active == "agent" else ""
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{
  color-scheme: light;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --text: #17202a;
  --muted: #667085;
  --line: #d8dee6;
  --brand: #176b87;
  --brand-soft: #e3f3f7;
  --accent: #8a5a20;
  --ok: #287a4d;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
.app {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
.topbar {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 24px;
}}
.brand h1 {{ margin: 0; font-size: 24px; line-height: 1.1; }}
.brand p {{ margin: 5px 0 0; color: var(--muted); }}
nav {{ display: flex; gap: 8px; flex-wrap: wrap; }}
nav a {{
  color: var(--text);
  text-decoration: none;
  border: 1px solid var(--line);
  background: var(--panel);
  padding: 8px 12px;
  border-radius: 8px;
}}
nav a.active {{ background: var(--brand); border-color: var(--brand); color: white; }}
.panel {{
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px;
}}
.toolbar {{
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 14px;
  align-items: end;
  margin-bottom: 14px;
}}
.priority-panel {{ margin-bottom: 14px; }}
.priority-form {{ display: flex; gap: 8px; margin-top: 12px; }}
.chips {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }}
.chip {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  border: 1px solid #a8d4df;
  border-radius: 999px;
  background: var(--brand-soft);
  color: #0f536b;
  padding: 4px 8px 4px 12px;
}}
.chip form {{ margin: 0; }}
.chip button {{
  width: 22px;
  height: 22px;
  display: inline-grid;
  place-items: center;
  border-radius: 999px;
  padding: 0;
  background: white;
  color: #0f536b;
  border-color: #a8d4df;
  line-height: 1;
}}
.search {{ display: flex; gap: 8px; min-width: 0; }}
input, textarea, button {{
  font: inherit;
  border-radius: 8px;
  border: 1px solid var(--line);
}}
input, textarea {{ background: white; color: var(--text); padding: 10px 12px; }}
input {{ width: 100%; min-width: 180px; }}
textarea {{ width: 100%; min-height: 300px; resize: vertical; }}
button {{
  background: var(--brand);
  border-color: var(--brand);
  color: white;
  padding: 10px 14px;
  cursor: pointer;
  white-space: nowrap;
}}
.button-secondary {{ background: white; color: var(--text); border-color: var(--line); }}
.tabs {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.tab {{
  color: var(--text);
  text-decoration: none;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 10px;
  background: white;
}}
.tab.active {{ background: var(--brand-soft); border-color: #a8d4df; color: #0f536b; }}
.stats {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }}
.stat {{ background: #fbfcfd; border: 1px solid var(--line); border-radius: 8px; padding: 12px; }}
.stat strong {{ display: block; font-size: 24px; line-height: 1.1; }}
.stat span {{ color: var(--muted); font-size: 13px; }}
.mail-list {{ display: grid; gap: 10px; }}
article.mail {{
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  border: 1px solid var(--line);
  border-left: 4px solid var(--brand);
  border-radius: 8px;
  background: white;
  padding: 14px;
}}
.mail h2 {{ margin: 0 0 6px; font-size: 17px; line-height: 1.25; overflow-wrap: anywhere; }}
.mail h2 a {{ color: var(--text); text-decoration: none; }}
.mail h2 a:hover {{ color: var(--brand); text-decoration: underline; }}
.meta {{ color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }}
.snippet {{ margin: 10px 0; color: #394655; }}
.message-title {{ margin: 0 0 8px; font-size: 24px; line-height: 1.2; overflow-wrap: anywhere; }}
.message-body {{
  margin: 18px 0 0;
  padding-top: 18px;
  border-top: 1px solid var(--line);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: #263443;
}}
.detail-actions {{ display: flex; gap: 10px; align-items: center; justify-content: space-between; margin-top: 16px; flex-wrap: wrap; }}
.back {{ color: var(--brand); text-decoration: none; font-weight: 600; }}
.labels {{ display: flex; gap: 6px; flex-wrap: wrap; }}
.label, .score {{
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  border-radius: 999px;
  padding: 3px 8px;
  font-size: 12px;
}}
.label {{ background: var(--brand-soft); color: #0f536b; }}
.score {{ background: #fff2d6; color: var(--accent); margin-left: 6px; }}
.empty {{ color: var(--muted); padding: 28px; text-align: center; border: 1px dashed var(--line); border-radius: 8px; background: #fbfcfd; }}
.hint {{ color: var(--muted); margin-top: 0; }}
.save-row {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 12px; flex-wrap: wrap; }}
.saved {{ color: var(--ok); font-weight: 600; }}
.agent-box {{ display: grid; gap: 12px; }}
.agent-box textarea {{ min-height: 120px; }}
.agent-answer {{
  white-space: pre-wrap;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fbfcfd;
  padding: 14px;
}}
@media (max-width: 720px) {{
  .app {{ padding: 16px; }}
  .topbar, .toolbar, .priority-form {{ display: grid; grid-template-columns: 1fr; align-items: stretch; }}
  .stats {{ grid-template-columns: 1fr; }}
  article.mail {{ grid-template-columns: 1fr; }}
  .search {{ display: grid; grid-template-columns: 1fr; }}
  nav a, .tab, button {{ text-align: center; }}
}}
</style>
<main class="app">
  <header class="topbar">
    <div class="brand">
      <h1>Email Assist</h1>
      <p>{html.escape(title)}</p>
    </div>
    <nav aria-label="Primary">
      <a class="{queue_class}" href="/">Queue</a>
      <a class="{agent_class}" href="/agent">Agent</a>
    </nav>
  </header>
  {content}
</main>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            qs = parse_qs(parsed.query)
            self.page(qs.get("status", ["new"])[0], qs.get("q", [""])[0])
            return
        if parsed.path == "/priorities":
            self.priorities_page()
            return
        if parsed.path == "/message":
            qs = parse_qs(parsed.query)
            self.message_page(qs.get("id", [""])[0])
            return
        if parsed.path == "/agent":
            self.agent_page()
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/done":
            self.mark_done()
            return
        if parsed.path == "/priorities":
            self.save_priorities()
            return
        if parsed.path == "/priority/add":
            self.add_priority()
            return
        if parsed.path == "/priority/delete":
            self.delete_priority()
            return
        if parsed.path == "/agent":
            self.agent_chat()
            return
        self.send_error(404)

    def mark_done(self):
        length = int(self.headers.get("content-length", "0"))
        item = parse_qs(self.rfile.read(length).decode()).get("id", [""])[0]
        with connect() as db:
            db.execute("update messages set status = 'done' where id = ?", [item])
            db.commit()
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def save_priorities(self):
        length = int(self.headers.get("content-length", "0"))
        text = parse_qs(self.rfile.read(length).decode()).get("priorities", [""])[0]
        rules = rules_from_priorities(text)
        save_rules(rules)
        rescore_messages(rules)
        self.redirect("/")

    def add_priority(self):
        length = int(self.headers.get("content-length", "0"))
        label = parse_qs(self.rfile.read(length).decode()).get("priority", [""])[0]
        add_priority(label)
        self.redirect("/")

    def delete_priority(self):
        length = int(self.headers.get("content-length", "0"))
        label = parse_qs(self.rfile.read(length).decode()).get("priority", [""])[0]
        delete_priority(label)
        self.redirect("/")

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def page(self, status, q):
        items = rows(status, q)
        counts = status_counts()
        priorities = priority_chips(load_priority_labels())
        new_active = "active" if status == "new" else ""
        done_active = "active" if status == "done" else ""
        body = "".join(card(row) for row in items) or '<div class="empty">No matching mail.</div>'
        content = f"""
<section class="stats" aria-label="Queue summary">
  <div class="stat"><strong>{counts.get('new', 0)}</strong><span>Needs review</span></div>
  <div class="stat"><strong>{counts.get('done', 0)}</strong><span>Done</span></div>
  <div class="stat"><strong>{counts.get('new', 0) + counts.get('done', 0)}</strong><span>Total captured</span></div>
</section>
<section class="panel priority-panel">
  <h2>Priorities</h2>
  <p class="hint">Add what matters. NLTK WordNet synonyms are used when installed, then current mail is rescored.</p>
  <div class="chips">{priorities or '<span class="hint">No priorities yet.</span>'}</div>
  <form class="priority-form" method="post" action="/priority/add">
    <input name="priority" placeholder="Add priority, e.g. bank alerts, interviews, visa appointment" autocomplete="off">
    <button>Add</button>
  </form>
</section>
<section class="panel">
  <div class="toolbar">
    <div class="tabs" aria-label="Status">
      <a class="tab {new_active}" href="/">Needs review</a>
      <a class="tab {done_active}" href="/?status=done">Done</a>
    </div>
    <form class="search" method="get">
      <input type="hidden" name="status" value="{html.escape(status)}">
      <input name="q" value="{html.escape(q)}" placeholder="Search sender, subject, label">
      <button>Search</button>
    </form>
  </div>
  <div class="mail-list">{body}</div>
</section>
"""
        doc = app_shell("Important mail queue", "queue", content)
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(doc.encode())

    def message_page(self, message_id):
        row = get_message(message_id)
        if not row:
            self.send_error(404)
            return
        labels = "".join(f'<span class="label">{html.escape(label)}</span>' for label in json.loads(row["labels"]))
        content = f"""
<section class="panel">
  <a class="back" href="/">Back to queue</a>
  <h2 class="message-title">{html.escape(row['subject'])}</h2>
  <div class="meta">{html.escape(row['sender'])} · {html.escape(row['sent_at'])}<span class="score">score {row['score']}</span></div>
  <div class="labels">{labels}</div>
  <div class="message-body">{html.escape(row['body'] or row['snippet'])}</div>
  <div class="detail-actions">
    <a class="back" href="/">Back to queue</a>
    <form method="post" action="/done">
      <input type="hidden" name="id" value="{html.escape(row['id'])}">
      <button>Mark done</button>
    </form>
  </div>
</section>
"""
        doc = app_shell("Full message", "queue", content)
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(doc.encode())

    def agent_page(self, prompt="", answer=""):
        answer_html = f'<div class="agent-answer">{html.escape(answer)}</div>' if answer else ""
        content = f"""
<section class="panel agent-box">
  <h2>Agent chat</h2>
  <p class="hint">Ask it to find mail, update priorities, mark items done, or sync mail. It uses local Ollama tool calling.</p>
  <form class="agent-box" method="post" action="/agent">
    <textarea name="prompt" placeholder="Find interview emails I have not handled yet">{html.escape(prompt)}</textarea>
    <button>Ask agent</button>
  </form>
  {answer_html}
</section>
"""
        doc = app_shell("Tool-calling email agent", "agent", content)
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(doc.encode())

    def agent_chat(self):
        length = int(self.headers.get("content-length", "0"))
        prompt = parse_qs(self.rfile.read(length).decode()).get("prompt", [""])[0]
        try:
            answer = run_agent(prompt)
        except SystemExit as exc:
            answer = str(exc)
        self.agent_page(prompt, answer)

    def priorities_page(self):
        saved = "saved=1" in self.path
        priorities = html.escape(priorities_from_rules(load_rules()))
        saved_message = '<span class="saved">Saved</span>' if saved else ""
        content = f"""
<section class="panel">
  <h2>Priority order</h2>
  <p class="hint">Put one priority per line. Higher lines sort higher after the next sync.</p>
  <form method="post" action="/priorities">
    <textarea name="priorities" spellcheck="true">{priorities}</textarea>
    <div class="save-row">
      <button>Save priorities</button>
      {saved_message}
    </div>
  </form>
</section>
"""
        doc = app_shell("Choose what should not be missed", "priorities", content)
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(doc.encode())


def card(row):
    labels = "".join(f'<span class="label">{html.escape(label)}</span>' for label in json.loads(row["labels"]))
    local_href = f"/message?id={quote(row['id'], safe='')}"
    href = row["url"] or gmail_url(row["id"]) or local_href
    return f"""<article class="mail">
  <div>
    <h2><a href="{html.escape(href)}" target="_blank" rel="noreferrer">{html.escape(row['subject'])}</a></h2>
    <div class="meta">{html.escape(row['sender'])} · {html.escape(row['sent_at'])}<span class="score">score {row['score']}</span></div>
    <p class="snippet">{html.escape(row['snippet'])}</p>
    <div class="labels">{labels}<a class="label" href="{html.escape(local_href)}">stored text</a></div>
  </div>
  <form method="post" action="/done">
    <input type="hidden" name="id" value="{html.escape(row['id'])}">
    <button class="button-secondary">Done</button>
  </form>
</article>"""


def priority_chips(labels):
    chips = []
    for label in labels:
        escaped = html.escape(label)
        chips.append(f"""<span class="chip">{escaped}
  <form method="post" action="/priority/delete">
    <input type="hidden" name="priority" value="{escaped}">
    <button aria-label="Remove {escaped}" title="Remove">x</button>
  </form>
</span>""")
    return "".join(chips)


def serve(port):
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Dashboard: http://127.0.0.1:{port}")
        server.serve_forever()


def demo():
    init_files()
    rules = load_rules()
    samples = [
        b"From: hr@example.com\r\nSubject: Interview scheduled for Friday\r\nDate: Thu, 18 Jun 2026 10:00:00 +0000\r\nMessage-ID: <demo1>\r\n\r\nPlease join the interview calendar invite.",
        b"From: alerts@bank.example\r\nSubject: Credit card transaction alert\r\nDate: Thu, 18 Jun 2026 11:00:00 +0000\r\nMessage-ID: <demo2>\r\n\r\nA transaction was made on your card.",
    ]
    with connect() as db:
        save_messages(db, [parse_message(sample, rules) for sample in samples])
    print("Loaded demo messages.")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sync = sub.add_parser("sync")
    sync.add_argument("--limit", type=int)
    sync.add_argument("--since")
    sync.add_argument("--full", action="store_true", help="Fetch full message bodies instead of headers only.")
    serve_cmd = sub.add_parser("serve")
    serve_cmd.add_argument("--port", type=int, default=8765)
    agent = sub.add_parser("agent")
    agent.add_argument("prompt")
    sub.add_parser("demo")
    args = parser.parse_args()

    if args.cmd == "init":
        init_files()
    elif args.cmd == "sync":
        sync_imap(args.limit, args.since, args.full)
    elif args.cmd == "serve":
        serve(args.port)
    elif args.cmd == "agent":
        print(run_agent(args.prompt))
    elif args.cmd == "demo":
        demo()


if __name__ == "__main__":
    main()
