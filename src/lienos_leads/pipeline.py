"""Orchestration : collecte → dédoublonnage → enrichissement → scoring → stockage → digest."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import httpx

from .config import Config
from .digest import send_email, write_files
from .enrich import Enricher
from .models import Lead, Segment
from .scoring import Scorer
from .sources import france_travail, freework_apify, malt_mail, rss
from .store import Store

log = logging.getLogger(__name__)

LEVEE_DEDUP_DAYS = 30  # une même levée est reprise par plusieurs médias, parfois sur plusieurs jours
USER_AGENT = "lienos-leads/0.1 (+https://lienos.fr; contact@lienos.fr)"


def collect(cfg: Config, client: httpx.Client) -> list[Lead]:
    connectors = [
        ("France Travail", lambda: france_travail.fetch(cfg.france_travail, client)),
        ("RSS", lambda: rss.fetch(cfg.rss, client)),
        ("Malt (e-mails)", lambda: malt_mail.fetch(cfg.malt_mail)),
        ("Free-Work (Apify)", lambda: freework_apify.fetch(cfg.apify, client)),
    ]
    leads: list[Lead] = []
    for name, run in connectors:
        try:
            leads.extend(run())
        except Exception as exc:  # une source en panne ne doit pas bloquer le digest
            log.error("Source %s en échec : %s", name, exc)
    return leads


def run(cfg: Config, *, client: httpx.Client | None = None, llm_client=None,
        send: bool = True, today: date | None = None) -> dict:
    today = today or date.today()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    own_client = client is None
    client = client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
    store = Store(cfg.db_path)
    try:
        collected = collect(cfg, client)

        # Dédoublonnage : dans le lot, puis contre l'historique
        unique: dict[str, Lead] = {}
        for lead in collected:
            unique.setdefault(lead.fingerprint, lead)
        new = [l for l in unique.values() if not store.is_known(l)]
        new = new[: cfg.scoring.max_leads_per_run]

        enricher = Enricher(client, store, cfg.recontact_cooldown_days, cfg.excluded_companies)
        candidates = []
        for lead in new:
            enricher.apply(lead)
            if lead.excluded_reason:
                lead.score = 0
                store.save(lead)  # mémorisé pour ne pas le réévaluer demain
            else:
                candidates.append(lead)

        scorer = Scorer(cfg.scoring, store.feedback_examples(), client=llm_client)
        scorer.score(candidates)

        # Les levées n'ont un nom d'entreprise qu'après le LLM : on applique alors les exclusions.
        # Meilleur score d'abord, pour garder le meilleur article quand plusieurs couvrent la même levée.
        for lead in sorted(candidates, key=lambda l: -(l.score or 0)):
            if lead.company and not lead.company_info:
                enricher.apply(lead)
            if (not lead.excluded_reason and lead.segment == Segment.LEVEE
                    and store.levee_already_seen(lead.company_key, lead.fingerprint, LEVEE_DEDUP_DAYS)):
                lead.excluded_reason = "levée déjà signalée par un autre article"
            if lead.excluded_reason:
                lead.score = 0
            store.save(lead)

        selected = [l for l in candidates
                    if not l.excluded_reason
                    and (l.score or 0) >= cfg.scoring.threshold_for(l.segment)]
        stats = {
            "collected": len(collected),
            "new": len(new),
            "excluded": sum(1 for l in new if l.excluded_reason),
            "scored": len(candidates),
            "selected": len(selected),
            "llm": scorer.uses_llm,
            "llm_fallbacks": scorer.fallbacks,
            "started": started,
        }
        html_path, text_path = write_files(selected, stats, cfg.digest, today)
        stats["digest_html"] = str(html_path)
        stats["digest_text"] = str(text_path)
        if send:
            send_email(selected, stats, cfg.digest, today)
        log.info("Terminé : %s", stats)
        return stats
    finally:
        store.close()
        if own_client:
            client.close()
