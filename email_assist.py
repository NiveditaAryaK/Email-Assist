#!/usr/bin/env python3
import argparse
import email
import html
import imaplib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DB = Path(os.environ.get("EMAIL_ASSIST_DB", "email_assist.sqlite3"))
RULES = Path(os.environ.get("EMAIL_ASSIST_RULES", "rules.json"))
ENV = Path(os.environ.get("EMAIL_ASSIST_ENV", ".env"))


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
    return db


def load_rules(path=RULES):
    if not path.exists():
        raise SystemExit(f"Missing {path}. Run: python3 email_assist.py init")
    with path.open() as f:
        data = json.load(f)
    return data.get("rules", [])


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
        "url": "",
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
            insert into messages (id, sender, subject, sent_at, snippet, labels, score, url)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set
                sender=excluded.sender,
                subject=excluded.subject,
                sent_at=excluded.sent_at,
                snippet=excluded.snippet,
                labels=excluded.labels,
                score=excluded.score,
                url=excluded.url
        """, (
            mail["id"],
            mail["sender"],
            mail["subject"],
            mail["sent_at"],
            mail["snippet"],
            json.dumps(mail["labels"]),
            mail["score"],
            mail["url"],
        ))
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
    with closing(imaplib.IMAP4_SSL(host)) as client:
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
    sql = "select * from messages where status = ?"
    args = [status]
    if q:
        sql += " and (sender like ? or subject like ? or snippet like ? or labels like ?)"
        args += [f"%{q}%"] * 4
    sql += " order by score desc, sent_at desc limit 100"
    with connect() as db:
        return db.execute(sql, args).fetchall()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            qs = parse_qs(parsed.query)
            self.page(qs.get("status", ["new"])[0], qs.get("q", [""])[0])
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/done":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        item = parse_qs(self.rfile.read(length).decode()).get("id", [""])[0]
        with connect() as db:
            db.execute("update messages set status = 'done' where id = ?", [item])
            db.commit()
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def page(self, status, q):
        items = rows(status, q)
        body = "".join(card(row) for row in items) or "<p>No matching mail.</p>"
        doc = f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Email Assist</title>
<style>
body {{ font: 16px/1.4 system-ui, sans-serif; max-width: 920px; margin: 32px auto; padding: 0 16px; color: #1f2933; }}
header {{ display: flex; gap: 12px; justify-content: space-between; align-items: center; flex-wrap: wrap; }}
form.search {{ display: flex; gap: 8px; }}
input, button {{ font: inherit; padding: 8px 10px; }}
article {{ border: 1px solid #ccd3db; border-radius: 8px; padding: 14px; margin: 12px 0; }}
.meta {{ color: #5b6673; font-size: 14px; }}
.labels {{ margin-top: 8px; }}
.label {{ background: #e7eef8; border-radius: 999px; padding: 3px 8px; font-size: 13px; margin-right: 6px; }}
.done {{ float: right; }}
</style>
<header>
  <h1>Important mail</h1>
  <form class="search" method="get">
    <input name="q" value="{html.escape(q)}" placeholder="Ask/search: interview, bank, deadline">
    <button>Search</button>
  </form>
</header>
{body}
"""
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(doc.encode())


def card(row):
    labels = "".join(f'<span class="label">{html.escape(label)}</span>' for label in json.loads(row["labels"]))
    return f"""<article>
  <form class="done" method="post" action="/done"><input type="hidden" name="id" value="{html.escape(row['id'])}"><button>Done</button></form>
  <h2>{html.escape(row['subject'])}</h2>
  <div class="meta">{html.escape(row['sender'])} | {html.escape(row['sent_at'])} | score {row['score']}</div>
  <p>{html.escape(row['snippet'])}</p>
  <div class="labels">{labels}</div>
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
