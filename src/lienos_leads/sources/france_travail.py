"""API Offres d'emploi v2 de France Travail (francetravail.io).

Pré-requis : créer une application sur https://francetravail.io, souscrire à l'API
« Offres d'emploi v2 » et renseigner FT_CLIENT_ID / FT_CLIENT_SECRET.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from ..config import FranceTravailConfig, secret
from ..models import Lead, Segment

log = logging.getLogger(__name__)

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
SCOPE = "api_offresdemploiv2 o2dsoffre"
PAGE = 150  # taille de page maximale autorisée par l'API


def get_token(client: httpx.Client) -> str:
    resp = client.post(
        TOKEN_URL,
        params={"realm": "/partenaire"},
        data={
            "grant_type": "client_credentials",
            "client_id": secret("FT_CLIENT_ID", required=True),
            "client_secret": secret("FT_CLIENT_SECRET", required=True),
            "scope": SCOPE,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_lead(offre: dict) -> Lead:
    entreprise = offre.get("entreprise") or {}
    lieu = offre.get("lieuTravail") or {}
    origine = offre.get("origineOffre") or {}
    url = origine.get("urlOrigine") or (
        f"https://candidat.francetravail.fr/offres/recherche/detail/{offre.get('id', '')}"
    )
    return Lead(
        source="france_travail",
        segment=Segment.OFFRE_EMPLOI,
        title=offre.get("intitule", "").strip(),
        url=url,
        company=(entreprise.get("nom") or "").strip(),
        description=offre.get("description", ""),
        location=lieu.get("libelle", ""),
        published_at=_parse_date(offre.get("dateCreation")),
        extra={
            "offre_id": offre.get("id"),
            "type_contrat": offre.get("typeContrat"),
            "experience": offre.get("experienceLibelle"),
            "salaire": (offre.get("salaire") or {}).get("libelle"),
            "code_postal": lieu.get("codePostal"),
            "siret": entreprise.get("siret"),
        },
    )


def fetch(cfg: FranceTravailConfig, client: httpx.Client) -> list[Lead]:
    if not cfg.enabled:
        return []
    token = get_token(client)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    leads: dict[str, Lead] = {}
    departements = cfg.departements or [None]
    for keyword in cfg.keywords:
        for dep in departements:
            start = 0
            while start < cfg.max_results_per_keyword:
                end = min(start + PAGE, cfg.max_results_per_keyword) - 1
                params = {
                    "motsCles": keyword,
                    "publieeDepuis": cfg.published_since_days,
                    "range": f"{start}-{end}",
                }
                if dep:
                    params["departement"] = dep
                resp = client.get(SEARCH_URL, params=params, headers=headers)
                if resp.status_code == 204:  # aucun résultat
                    break
                resp.raise_for_status()
                results = resp.json().get("resultats", [])
                for offre in results:
                    lead = to_lead(offre)
                    leads.setdefault(lead.extra["offre_id"] or lead.fingerprint, lead)
                if resp.status_code != 206 or len(results) < end - start + 1:
                    break  # 200 = dernière page ; 206 = il en reste
                start = end + 1
    log.info("France Travail : %d offres", len(leads))
    return list(leads.values())
