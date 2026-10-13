"""Missions Free-Work via un acteur Apify (outil tiers, non officiel). Désactivé par défaut.

Avant de l'activer : relire les CGU de Free-Work. Tarif de l'acteur
koatiromeo/french-it-contract-market : 0,008 $ par mission livrée, plus 0,10 $ par benchmark
de TJM (computeRateBenchmark, à laisser à false). Son entrée se règle dans [apify.input]
de config.toml : sources, maxMissions, sinceDays, onlyNewSinceLastRun ; les mots-clés
vont dans [apify] queries (un run par mot-clé, car l'acteur exige tous les mots à la fois).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx

from ..config import ApifyConfig, secret
from ..models import Lead, Segment

log = logging.getLogger(__name__)

API = "https://api.apify.com/v2"
START_URL = API + "/acts/{actor}/runs"
RUN_URL = API + "/actor-runs/{run_id}"
ITEMS_URL = API + "/datasets/{dataset_id}/items"
WAIT_STEP = 60    # durée maximale d'une attente côté Apify (waitForFinish)
MAX_WAIT = 900    # au-delà, la source est abandonnée pour ce run


def _daily_rate(item: dict) -> float | None:
    """TJM en euros : haut de la fourchette affichée, à défaut le bas."""
    if item.get("rate_period") != "day" or item.get("currency") != "EUR":
        return None
    rate = item.get("rate_max") or item.get("rate_min")
    return float(rate) if rate else None


def _location(item: dict) -> str:
    # « city » contient parfois déjà la région ou le département
    parts = [item.get("city"), item.get("region")]
    return ", ".join(dict.fromkeys(p for p in parts if p))


def to_lead(item: dict) -> Lead:
    published = item.get("published_at")
    try:
        published = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
    except ValueError:
        published = None
    return Lead(
        source=f"apify:{item.get('source') or 'free-work'}",
        segment=Segment.MISSION,
        title=(item.get("title") or "").strip(),
        url=item.get("url") or "",
        company=(item.get("company") or "").strip(),
        description=item.get("description") or "",
        location=_location(item),
        daily_rate=_daily_rate(item),
        published_at=published,
        extra={
            "remote": item.get("remote"),
            "contrats": item.get("contracts"),
            "duree": item.get("duration"),
            "experience": item.get("experience_level"),
            "competences": item.get("skills"),
            "tjm_min": item.get("rate_min"),
        },
    )


def _headers() -> dict:
    # En en-tête et non dans l'URL : httpx journalise les URL complètes
    return {"Authorization": f"Bearer {secret('APIFY_TOKEN', required=True)}"}


def start_run(cfg: ApifyConfig, client: httpx.Client, run_input: dict) -> dict:
    resp = client.post(START_URL.format(actor=cfg.actor), headers=_headers(), json=run_input)
    resp.raise_for_status()
    return resp.json()["data"]


def wait_for_items(run: dict, client: httpx.Client, deadline: float) -> list[dict]:
    """Attend la fin du run : l'appel synchrone d'Apify est limité à 300 s."""
    while run["status"] in ("READY", "RUNNING"):
        if time.monotonic() > deadline:
            raise TimeoutError(f"run Apify {run['id']} toujours en cours après {MAX_WAIT} s")
        resp = client.get(RUN_URL.format(run_id=run["id"]), headers=_headers(),
                          params={"waitForFinish": WAIT_STEP}, timeout=WAIT_STEP + 30)
        resp.raise_for_status()
        run = resp.json()["data"]
    if run["status"] != "SUCCEEDED":
        raise RuntimeError(f"run Apify {run['id']} terminé en {run['status']}")
    resp = client.get(ITEMS_URL.format(dataset_id=run["defaultDatasetId"]), headers=_headers(),
                      params={"format": "json"})
    resp.raise_for_status()
    return resp.json()


def fetch(cfg: ApifyConfig, client: httpx.Client) -> list[Lead]:
    if not cfg.enabled:
        return []
    # L'acteur exige tous les mots de « query » à la fois : un run par mot-clé, lancés ensemble
    inputs = [{**cfg.input, "query": q} for q in cfg.queries] or [cfg.input]
    runs = [(i.get("query", ""), start_run(cfg, client, i)) for i in inputs]
    deadline = time.monotonic() + MAX_WAIT
    leads: dict[str, Lead] = {}
    for query, run in runs:
        try:
            items = wait_for_items(run, client, deadline)
        except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
            log.warning("Free-Work (Apify) %r en échec : %s", query, exc)  # les autres continuent
            continue
        log.info("Free-Work (Apify) %r : %d missions", query, len(items))
        for item in items:
            lead = to_lead(item)
            leads.setdefault(lead.fingerprint, lead)
    kept = [l for l in leads.values()
            if l.daily_rate is None or l.daily_rate >= cfg.min_daily_rate]
    log.info("Free-Work (Apify) : %d missions, %d au-dessus du TJM plancher", len(leads), len(kept))
    return kept
