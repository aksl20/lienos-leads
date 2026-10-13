"""Flux RSS/Atom : Google Alerts (format RSS), Maddyness et tout autre flux d'actualités.

Chaque article retenu devient un lead « levée » ; le nom de l'entreprise est extrait
plus tard par le LLM (voir scoring.py), car les titres d'articles sont trop variés
pour une règle fixe.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import feedparser
import httpx

from ..config import RssConfig
from ..models import Lead, Segment, normalize

log = logging.getLogger(__name__)

TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return html.unescape(TAG_RE.sub(" ", text or "")).strip()


def _real_url(link: str) -> str:
    """Google Alerts enveloppe les liens dans google.com/url?url=… : on récupère la cible."""
    parsed = urlparse(link)
    if parsed.netloc.endswith("google.com") and parsed.path == "/url":
        target = parse_qs(parsed.query).get("url")
        if target:
            return target[0]
    return link


def _published(entry) -> datetime | None:
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    return datetime(*struct[:6], tzinfo=timezone.utc)


def parse_feed(name: str, content: bytes | str, include_keywords: list[str]) -> list[Lead]:
    feed = feedparser.parse(content)
    wanted = [normalize(k) for k in include_keywords]
    leads = []
    for entry in feed.entries:
        title = _clean(entry.get("title", ""))
        summary = _clean(entry.get("summary", ""))
        haystack = normalize(f"{title} {summary}")
        if wanted and not any(k in haystack for k in wanted):
            continue
        leads.append(
            Lead(
                source=name,
                segment=Segment.LEVEE,
                title=title,
                url=_real_url(entry.get("link", "")),
                description=summary,
                published_at=_published(entry),
            )
        )
    return leads


def fetch(cfg: RssConfig, client: httpx.Client) -> list[Lead]:
    if not cfg.enabled:
        return []
    leads: list[Lead] = []
    for name, url in cfg.feeds.items():
        try:
            resp = client.get(url, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:  # un flux en panne ne bloque pas les autres
            log.warning("Flux %s indisponible : %s", name, exc)
            continue
        found = parse_feed(name, resp.content, cfg.include_keywords)
        log.info("Flux %s : %d articles retenus", name, len(found))
        leads.extend(found)
    return leads
