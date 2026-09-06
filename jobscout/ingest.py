"""Getting mail onto disk, so the rest of the tool can read it.

Everything downstream — :mod:`inbox`, :mod:`pursuits` — works on a list of
plain message records. This module is the only part that knows where those come
from, which is what keeps the parsing testable offline and stops the tool being
married to one mail provider.

Two sources, chosen because between them they cover the ways a person can
actually get at their own mail:

``mbox``
    Any ``.mbox`` file. Google Takeout produces one, so does Apple Mail's
    export and every Unix mail tool since 1975. Needs no credential, no network
    and no third party — the whole mailbox is already a file you own.

``imap``
    Reads the mailbox directly. For Gmail this needs an app password, which
    exists precisely so a program can read mail without being handed the
    account. Faster to set up than an export, and repeatable, which matters
    because the point is to run this weekly and diff.

Both produce the same records, and both stop at a date cutoff: a job search
moves, and mail from three years ago costs more to read past than it is worth.
"""
from __future__ import annotations

import datetime as dt
import email
import email.policy
import email.utils
import mailbox
import re
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from .inbox import Message

#: Where a reply stops and the quoted original begins. Quoted text is dropped:
#: it is the same words again, it is most of the bytes in a long thread, and
#: leaving it in means the reader sees a January sentence in a September
#: message and dates the pursuit wrongly.
_QUOTE_MARKERS = (
    re.compile(r"^\s*On .{0,120}\bwrote:\s*$", re.I | re.M),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.I | re.M),
    re.compile(r"^\s*_{10,}\s*$", re.M),
    re.compile(r"^\s*From:\s.+\nSent:\s.+$", re.I | re.M),
)

#: Signature and footer noise that adds nothing a reader needs.
_TRAILING = re.compile(
    r"\n\s*(--\s*\n.*|Sent from my \w+.*|This email was intended for.*|"
    r"Unsubscribe:.*|You are receiving.*)$", re.I | re.S)


def strip_quoted(text: str) -> str:
    """Keep only what this message actually said."""
    if not text:
        return ""
    cut = len(text)
    for marker in _QUOTE_MARKERS:
        match = marker.search(text)
        if match and match.start() < cut:
            cut = match.start()
    body = text[:cut]
    # Whole-line quotes that no marker introduced.
    body = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith(">"))
    body = _TRAILING.sub("", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def _addresses(value: str) -> List[str]:
    return [addr.lower() for _name, addr in email.utils.getaddresses([value or ""]) if addr]


def _body_of(message: EmailMessage) -> str:
    """The plain-text body, falling back to stripping tags off the HTML part."""
    try:
        part = message.get_body(preferencelist=("plain",))
        if part is not None:
            return part.get_content()
    except Exception:                      # a malformed part must not stop a run
        pass
    try:
        part = message.get_body(preferencelist=("html",))
        if part is None:
            return ""
        html = part.get_content()
    except Exception:
        return ""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>", "\n", html)
    return re.sub(r"<[^>]+>", " ", html)


def to_record(raw: EmailMessage, me: Sequence[str]) -> Optional[Message]:
    """Turn one parsed email into the record the rest of the tool reads."""
    sender = (_addresses(str(raw.get("From", ""))) or [""])[0]
    recipients = _addresses(str(raw.get("To", ""))) + _addresses(str(raw.get("Cc", "")))
    # Garbage does not raise: the email parser turns any bytes at all into a
    # message object with no headers, which then becomes a record with no
    # sender, no date and no meaning. A message with nobody on either end is
    # not correspondence.
    if not sender and not recipients:
        return None
    mine = {address.lower() for address in me if address}
    from_me = sender in mine
    # The counterpart is whoever is not you. Mail you sent to yourself — notes,
    # backups, drafts — has no counterpart and is not correspondence.
    others = [a for a in recipients if a not in mine]
    if from_me and not others:
        return None
    when = ""
    parsed = email.utils.parsedate_to_datetime(raw.get("Date", "")) if raw.get("Date") else None
    if parsed is not None:
        when = parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = strip_quoted(_body_of(raw))
    return Message(
        id=(raw.get("Message-ID") or "").strip("<> ") or "%s|%s" % (sender, when),
        thread_id=_thread_of(raw),
        date=when,
        sender=sender,
        to=(others[0] if from_me else (recipients[0] if recipients else "")),
        subject=str(raw.get("Subject", "")).strip(),
        snippet=body[:200],
        body=body[:8000],
        from_me=from_me,
    )


def _thread_of(raw: EmailMessage) -> str:
    """Group a reply with what it replied to.

    ``References`` names the whole chain, so its first entry is the thread's
    root and is stable however deep the reply goes. Without it, mail from one
    conversation lands in several pursuits.
    """
    references = (raw.get("References") or "").split()
    if references:
        return references[0].strip("<> ")
    reply_to = (raw.get("In-Reply-To") or "").strip("<> ")
    if reply_to:
        return reply_to
    return (raw.get("Message-ID") or "").strip("<> ")


def _keep(record: Optional[Message], since: Optional[dt.date]) -> bool:
    if record is None:
        return False
    if since is None or not record.date:
        return True
    when = record.when
    return when is None or when >= since


def read_mbox(path: Path, me: Sequence[str], since: Optional[dt.date] = None,
              on_progress: Optional[Callable[[int], None]] = None) -> List[Message]:
    """Read an exported mailbox file. No credentials, no network."""
    path = Path(path).expanduser()
    box = mailbox.mbox(str(path), factory=None)
    found: List[Message] = []
    for index, key in enumerate(box.keys()):
        try:
            raw = email.message_from_bytes(box.get_bytes(key),
                                           policy=email.policy.default)
        except Exception:
            continue                        # one unreadable message, not a failed run
        record = to_record(raw, me)
        if _keep(record, since):
            found.append(record)
        if on_progress and index % 500 == 0:
            on_progress(index)
    return found


#: Gmail's IMAP name for "everything", which is what you want: a reply you sent
#: and the message it answered live in different folders otherwise.
ALL_MAIL = '"[Gmail]/All Mail"'


def read_imap(host: str, user: str, password: str, *, since: Optional[dt.date] = None,
              folder: str = ALL_MAIL, limit: int = 4000,
              connection=None, on_progress: Optional[Callable[[int], None]] = None
              ) -> List[Message]:
    """Read mail straight from the server.

    ``connection`` exists so this can be tested without a network or a
    credential; leave it unset and a TLS connection is opened.
    """
    if connection is None:
        import imaplib

        connection = imaplib.IMAP4_SSL(host)
        connection.login(user, password)
    connection.select(folder, readonly=True)

    criteria = ["ALL"]
    if since:
        criteria = ["SINCE", since.strftime("%d-%b-%Y")]
    status, data = connection.search(None, *criteria)
    if status != "OK":
        raise RuntimeError("IMAP search failed: %s" % status)
    ids = (data[0] or b"").split()
    ids = ids[-limit:]                      # newest first when it has to be capped

    found: List[Message] = []
    for index, message_id in enumerate(ids):
        status, payload = connection.fetch(message_id, "(RFC822)")
        if status != "OK" or not payload:
            continue
        blob = next((part[1] for part in payload
                     if isinstance(part, tuple) and len(part) > 1), None)
        if not blob:
            continue
        try:
            raw = email.message_from_bytes(blob, policy=email.policy.default)
        except Exception:
            continue
        record = to_record(raw, [user])
        if _keep(record, since):
            found.append(record)
        if on_progress and index % 100 == 0:
            on_progress(index)
    return found
