"""Propositions reçues par InMail LinkedIn, lues dans les notifications e-mail (IMAP, lecture seule).

LinkedIn n'offre pas d'accès à sa messagerie : on lit seulement les e-mails que LinkedIn envoie
à Axel pour chaque InMail (expéditeur hit-reply@linkedin.com), qui contiennent le texte complet.
Les notifications de messages classiques n'ont pas le texte et sont ignorées, comme les réponses
dans une conversation où Axel a déjà répondu.
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from ..config import LinkedinMailConfig, secret
from ..models import Lead, Segment
from .malt_mail import _body_text, _decode

log = logging.getLogger(__name__)

THREAD_RE = re.compile(r"https://www\.linkedin\.com/(?:comm/)?messaging/thread/([^/\s?]+)")
REPLY_PREFIX_RE = re.compile(r"^Message ayant reçu une réponse\s*:\s*")
FOOTER_RE = re.compile(r"\n\s*-{20,}")  # sous ce trait : mentions LinkedIn et liens de désabonnement
# Réponse d'un recruteur à un message d'Axel : la conversation est déjà engagée
REPLY_TEMPLATES = ("reply_to_member",)


def parse_message(raw: bytes) -> Lead | None:
    msg = email.message_from_bytes(raw)
    template = (msg.get("X-LinkedIn-Template") or "").strip()
    body = _body_text(msg)
    thread = THREAD_RE.search(body)
    if not thread or any(t in template for t in REPLY_TEMPLATES):
        return None
    text = FOOTER_RE.split(body[thread.end():], maxsplit=1)[0]
    text = re.sub(r"[ \t]+", " ", text).strip(" /\n")
    if not text:  # notification sans le texte du message
        return None
    try:
        published = parsedate_to_datetime(msg.get("Date"))
    except (TypeError, ValueError):
        published = None
    return Lead(
        source="linkedin",
        segment=Segment.MISSION,
        title=REPLY_PREFIX_RE.sub("", _decode(msg.get("Subject"))).strip() or "InMail LinkedIn",
        url=thread.group(0),
        description=text[:4000],
        published_at=published,
        # Un lead par conversation, quel que soit le nombre de messages reçus ensuite
        uid=hashlib.sha1(f"linkedin|{thread.group(1)}".encode()).hexdigest()[:16],
    )


def fetch(cfg: LinkedinMailConfig, _client=None) -> list[Lead]:
    if not cfg.enabled:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=cfg.lookback_days)).strftime("%d-%b-%Y")
    leads: dict[str, Lead] = {}
    with imaplib.IMAP4_SSL(cfg.imap_host) as imap:
        imap.login(secret("IMAP_USER", required=True), secret("IMAP_PASSWORD", required=True))
        imap.select(cfg.mailbox, readonly=True)
        status, data = imap.search(None, "SINCE", since, "FROM", f'"{cfg.sender}"')
        if status != "OK":
            return []
        for num in data[0].split():
            _, msg_data = imap.fetch(num, "(BODY.PEEK[])")
            lead = parse_message(msg_data[0][1])
            if lead:
                leads.setdefault(lead.fingerprint, lead)
    log.info("LinkedIn (InMails) : %d propositions", len(leads))
    return list(leads.values())
