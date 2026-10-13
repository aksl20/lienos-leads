"""Alertes Malt reçues par e-mail (Malt n'a pas d'API publique pour les projets).

Lecture IMAP en lecture seule de la boîte contact@lienos.fr (Zoho). Pour Zoho, activez
l'accès IMAP et créez un mot de passe d'application : IMAP_USER / IMAP_PASSWORD.

Le format exact des e-mails Malt peut changer : le parseur reste volontairement
générique (sujet + texte + liens malt.fr). Le LLM fait le tri ensuite.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from ..config import MaltMailConfig, secret
from ..models import Lead, Segment

log = logging.getLogger(__name__)

LINK_RE = re.compile(r"https?://(?:www\.)?malt\.(?:fr|com)/[^\s\"'<>)]+")
TAG_RE = re.compile(r"<[^>]+>")


def _decode(value: str | None) -> str:
    return str(make_header(decode_header(value or "")))


def _body_text(msg: Message) -> str:
    plain, html_part = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html_part:
            html_part = text
    return plain or TAG_RE.sub(" ", html_part)


def parse_message(raw: bytes) -> Lead | None:
    msg = email.message_from_bytes(raw)
    subject = _decode(msg.get("Subject"))
    body = _body_text(msg)
    links = [l for l in LINK_RE.findall(body) if "unsubscribe" not in l and "settings" not in l]
    try:
        published = parsedate_to_datetime(msg.get("Date"))
    except (TypeError, ValueError):
        published = None
    if not subject:
        return None
    return Lead(
        source="malt",
        segment=Segment.MISSION,
        title=subject,
        url=links[0] if links else "",
        description=re.sub(r"\s+", " ", body)[:4000],
        published_at=published,
        extra={"all_links": links[:10]},
    )


def fetch(cfg: MaltMailConfig, _client=None) -> list[Lead]:
    if not cfg.enabled:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=cfg.lookback_days)).strftime("%d-%b-%Y")
    leads = []
    with imaplib.IMAP4_SSL(cfg.imap_host) as imap:
        imap.login(secret("IMAP_USER", required=True), secret("IMAP_PASSWORD", required=True))
        imap.select(cfg.mailbox, readonly=True)
        status, data = imap.search(None, "SINCE", since, "FROM", f'"{cfg.sender_contains}"')
        if status != "OK":
            return []
        for num in data[0].split():
            _, msg_data = imap.fetch(num, "(BODY.PEEK[])")
            lead = parse_message(msg_data[0][1])
            if lead:
                leads.append(lead)
    log.info("Malt (e-mails) : %d alertes", len(leads))
    return leads
