import email_assist
import os
import tempfile
from pathlib import Path


RAW = b"""From: recruiter@example.com
Subject: Interview scheduled
Date: Thu, 18 Jun 2026 10:00:00 +0000
Message-ID: <test>

Your interview is scheduled. Please accept the calendar invite.
"""


def test_interview_message_is_important():
    rules = [{"label": "interview", "score": 8, "patterns": ["interview", "calendar invite"]}]
    mail = email_assist.parse_message(RAW, rules)
    assert mail["labels"] == ["interview"]
    assert mail["score"] == 8


def test_load_env_sets_missing_values_only():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text("EMAIL_IMAP_HOST=imap.example\nEMAIL_IMAP_USER='me@example.com'\n")
        os.environ.pop("EMAIL_IMAP_HOST", None)
        os.environ["EMAIL_IMAP_USER"] = "shell@example.com"
        email_assist.load_env(path)
        assert os.environ["EMAIL_IMAP_HOST"] == "imap.example"
        assert os.environ["EMAIL_IMAP_USER"] == "shell@example.com"


if __name__ == "__main__":
    test_interview_message_is_important()
    test_load_env_sets_missing_values_only()
    print("ok")
