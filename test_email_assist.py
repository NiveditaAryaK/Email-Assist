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
    assert "Please accept the calendar invite." in mail["body"]
    assert mail["url"].startswith("https://mail.google.com/mail/u/0/#search/rfc822msgid%3A")


def test_load_env_sets_missing_values_only():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text("EMAIL_IMAP_HOST=imap.example\nEMAIL_IMAP_USER='me@example.com'\n")
        os.environ.pop("EMAIL_IMAP_HOST", None)
        os.environ["EMAIL_IMAP_USER"] = "shell@example.com"
        email_assist.load_env(path)
        assert os.environ["EMAIL_IMAP_HOST"] == "imap.example"
        assert os.environ["EMAIL_IMAP_USER"] == "shell@example.com"


def test_priorities_become_ordered_rules():
    rules = email_assist.rules_from_priorities("Interview scheduled\nBank alerts\nInterview scheduled\n")
    assert [rule["label"] for rule in rules] == ["Interview scheduled", "Bank alerts"]
    assert [rule["score"] for rule in rules] == [2, 1]
    assert "recruiter" in rules[0]["patterns"]
    assert "transaction" in rules[1]["patterns"]


def test_missing_nltk_is_not_fatal():
    assert isinstance(email_assist.nltk_synonyms("interview"), list)


def test_imap_since_uses_imap_date_format():
    assert email_assist.imap_since("2026-06-01") == "01-Jun-2026"


def test_agent_can_list_priorities_tool():
    output = email_assist.execute_agent_tool("list_priorities", {})
    assert "priorities" in output


if __name__ == "__main__":
    test_interview_message_is_important()
    test_load_env_sets_missing_values_only()
    test_priorities_become_ordered_rules()
    test_missing_nltk_is_not_fatal()
    test_imap_since_uses_imap_date_format()
    test_agent_can_list_priorities_tool()
    print("ok")
