#!/usr/bin/env python3
import argparse
import email
import html
import imaplib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

DB = Path(os.environ.get("EMAIL_ASSIST_DB", "email_assist.sqlite3"))
RULES = Path(os.environ.get("EMAIL_ASSIST_RULES", "rules.json"))
ENV = Path(os.environ.get("EMAIL_ASSIST_ENV", ".env"))
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
        for pattern in EXPANSIONS.get(word, [word]):
            patterns.add(re.escape(pattern))
    return sorted(patterns)


def save_rules(rules, path=RULES):
    path.write_text(json.dumps({"rules": rules}, indent=2) + "\n")


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


def sync_imap(limit):
    load_env()
    host = os.environ.get("EMAIL_IMAP_HOST")
    user = os.environ.get("EMAIL_IMAP_USER")
    password = os.environ.get("EMAIL_IMAP_PASSWORD")
    folder = os.environ.get("EMAIL_IMAP_FOLDER", "INBOX")
    if not all([host, user, password]):
        raise SystemExit("Set EMAIL_IMAP_HOST, EMAIL_IMAP_USER, and EMAIL_IMAP_PASSWORD.")

    rules = load_rules()
    client = imaplib.IMAP4_SSL(host)
    try:
        client.login(user, password)
        client.select(folder)
        typ, data = client.search(None, "ALL")
        if typ != "OK":
            raise SystemExit("IMAP search failed.")
        ids = data[0].split()[-limit:]
        messages = []
        for msg_id in ids:
            typ, fetched = client.fetch(msg_id, "(RFC822)")
            if typ == "OK" and fetched and isinstance(fetched[0], tuple):
                messages.append(parse_message(fetched[0][1], rules))
    finally:
        client.logout()

    with connect() as db:
        save_messages(db, messages)
    print(f"Synced {len(messages)} messages; saved important matches.")


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


def app_shell(title, active, content):
    queue_class = "active" if active == "queue" else ""
    priorities_class = "active" if active == "priorities" else ""
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
@media (max-width: 720px) {{
  .app {{ padding: 16px; }}
  .topbar, .toolbar {{ display: grid; grid-template-columns: 1fr; align-items: stretch; }}
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
      <a class="{priorities_class}" href="/priorities">Priorities</a>
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
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/done":
            self.mark_done()
            return
        if parsed.path == "/priorities":
            self.save_priorities()
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
        self.send_response(303)
        self.send_header("Location", "/priorities?saved=1")
        self.end_headers()

    def page(self, status, q):
        items = rows(status, q)
        counts = status_counts()
        new_active = "active" if status == "new" else ""
        done_active = "active" if status == "done" else ""
        body = "".join(card(row) for row in items) or '<div class="empty">No matching mail.</div>'
        content = f"""
<section class="stats" aria-label="Queue summary">
  <div class="stat"><strong>{counts.get('new', 0)}</strong><span>Needs review</span></div>
  <div class="stat"><strong>{counts.get('done', 0)}</strong><span>Done</span></div>
  <div class="stat"><strong>{counts.get('new', 0) + counts.get('done', 0)}</strong><span>Total captured</span></div>
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
    sync.add_argument("--limit", type=int, default=100)
    serve_cmd = sub.add_parser("serve")
    serve_cmd.add_argument("--port", type=int, default=8765)
    sub.add_parser("demo")
    args = parser.parse_args()

    if args.cmd == "init":
        init_files()
    elif args.cmd == "sync":
        sync_imap(args.limit)
    elif args.cmd == "serve":
        serve(args.port)
    elif args.cmd == "demo":
        demo()


if __name__ == "__main__":
    main()
