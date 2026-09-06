"""Getting mail off disk or off a server, with nobody's real mail involved.

The IMAP reader is exercised through a stand-in connection, so these run with
no network and no credential.
"""
import datetime as dt
import email
import email.policy
from pathlib import Path

from jobscout import ingest
from jobscout.ingest import strip_quoted, to_record

ME = ["me@example.com"]


def parse(text):
    return email.message_from_string(text, policy=email.policy.default)


PLAIN = """From: Lily Kim <lily@kestrel.com>
To: me@example.com
Subject: Re: the role
Date: Wed, 19 Aug 2026 07:59:00 -0600
Message-ID: <abc@kestrel.com>
References: <root@kestrel.com> <second@kestrel.com>

Thanks Nathan- there's still no req posted- so will let you know.

On Wed, Aug 13, 2026 at 7:53 AM Nathaniel Knapp <me@example.com> wrote:
> Could you let me know what remains in the process?
"""


def test_a_reply_keeps_only_what_it_actually_said():
    """Quoted text is the same words again, and it is most of the bytes in a
    long thread. Worse, leaving it in shows the reader an August sentence
    inside a September message and dates the pursuit wrongly."""
    record = to_record(parse(PLAIN), ME)
    assert "no req posted" in record.body
    assert "what remains in the process" not in record.body


def test_the_thread_root_groups_a_deep_reply_with_its_original():
    record = to_record(parse(PLAIN), ME)
    assert record.thread_id == "root@kestrel.com"


def test_direction_and_counterpart_are_worked_out_from_the_headers():
    inbound = to_record(parse(PLAIN), ME)
    assert inbound.from_me is False
    assert inbound.sender == "lily@kestrel.com"

    outbound = to_record(parse(PLAIN.replace("From: Lily Kim <lily@kestrel.com>",
                                             "From: Me <me@example.com>")
                               .replace("To: me@example.com", "To: lily@kestrel.com")), ME)
    assert outbound.from_me is True
    assert outbound.to == "lily@kestrel.com"


def test_a_note_you_sent_to_yourself_is_not_correspondence():
    """Diary entries, drafts and backups are the bulk of some people's sent
    mail and none of it is a job lead."""
    note = parse("""From: Me <me@example.com>
To: me@example.com
Subject: Diary card
Date: Wed, 19 Aug 2026 07:59:00 -0600
Message-ID: <n@x>

-- Nathaniel
""")
    assert to_record(note, ME) is None


def test_dates_are_normalised_to_utc():
    record = to_record(parse(PLAIN), ME)
    assert record.date == "2026-08-19T13:59:00Z"
    assert record.when == dt.date(2026, 8, 19)


def test_an_html_only_message_still_yields_readable_text():
    html = parse("""From: r@agency.com
To: me@example.com
Subject: A role
Date: Wed, 19 Aug 2026 07:59:00 -0600
Message-ID: <h@x>
MIME-Version: 1.0
Content-Type: text/html; charset="utf-8"

<html><style>p{color:red}</style><body><p>Hi Nathan</p><p>A role at Kestrel.</p></body></html>
""")
    record = to_record(html, ME)
    assert "Hi Nathan" in record.body and "A role at Kestrel" in record.body
    assert "color:red" not in record.body


def test_signature_and_platform_footers_are_dropped():
    assert strip_quoted("Real content here.\n\nSent from my iPhone") == "Real content here."
    assert strip_quoted("Real content.\n\nThis email was intended for X\n"
                        "Unsubscribe: http://x") == "Real content."


# --- mbox -------------------------------------------------------------------

MBOX = """From lily@kestrel.com Wed Aug 19 07:59:00 2026
From: Lily Kim <lily@kestrel.com>
To: me@example.com
Subject: Re: the role
Date: Wed, 19 Aug 2026 07:59:00 -0600
Message-ID: <one@kestrel.com>

No req posted yet.

From old@kestrel.com Mon Jan 05 09:00:00 2015
From: Old Thing <old@kestrel.com>
To: me@example.com
Subject: Ancient
Date: Mon, 05 Jan 2015 09:00:00 -0600
Message-ID: <two@kestrel.com>

Long ago.
"""


def test_an_mbox_export_reads_without_any_credential(tmp_path):
    path = tmp_path / "export.mbox"
    path.write_text(MBOX, encoding="utf-8")
    found = ingest.read_mbox(path, ME)
    assert {m.subject for m in found} == {"Re: the role", "Ancient"}


def test_the_date_cutoff_drops_mail_the_search_has_moved_past(tmp_path):
    path = tmp_path / "export.mbox"
    path.write_text(MBOX, encoding="utf-8")
    found = ingest.read_mbox(path, ME, since=dt.date(2024, 1, 1))
    assert [m.subject for m in found] == ["Re: the role"]


# --- imap -------------------------------------------------------------------

class FakeIMAP:
    """Enough of imaplib to prove the reader without a network or a password."""

    def __init__(self, messages):
        self.messages = messages
        self.selected = None
        self.criteria = None

    def select(self, folder, readonly=False):
        self.selected = folder
        return "OK", [b""]

    def search(self, charset, *criteria):
        self.criteria = criteria
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, message_id, parts):
        index = int(message_id) - 1
        return "OK", [(b"1 (RFC822 {n}", self.messages[index].encode()), b")"]


def test_imap_reads_all_mail_so_a_reply_and_its_original_land_together():
    fake = FakeIMAP([PLAIN])
    found = ingest.read_imap("host", "me@example.com", "pw", connection=fake)
    assert fake.selected == ingest.ALL_MAIL
    assert len(found) == 1
    assert found[0].sender == "lily@kestrel.com"


def test_the_since_date_is_passed_to_the_server_not_filtered_afterwards():
    """Downloading four years of mail to throw most of it away is slow enough
    that people stop running the tool."""
    fake = FakeIMAP([PLAIN])
    ingest.read_imap("h", "me@example.com", "pw", since=dt.date(2024, 9, 1),
                     connection=fake)
    assert fake.criteria == ("SINCE", "01-Sep-2024")


def test_one_unreadable_message_does_not_end_the_run():
    fake = FakeIMAP(["this is not an email at all", PLAIN])
    found = ingest.read_imap("h", "me@example.com", "pw", connection=fake)
    assert len(found) == 1
